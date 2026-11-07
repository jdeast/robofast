"""Enclosure operating modes (robofast/modes.py, docs/modes.md): the
definitions and their checks, the state file, and the mode honored by the
supervisor (leases, duty, the watched enclosure), the sequencer (manual and
shadow), and switched over the supervisor's API."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from robofast import Observation, SafetyConfig
from robofast.config import load_config
from robofast.modes import (
    BUILTIN_MODES,
    ModeGate,
    ModeStore,
    lease_survives,
    modes_from_config,
)
from robofast.plan import Idle, StaticPlan
from robofast.registry import Telemetry, ValueRegistry
from robofast.testing.checks import wait_until
from robofast.testing.processes import ROOT

pytestmark = pytest.mark.virtual_time

# -- the definitions -----------------------------------------------------------------


def test_the_built_in_modes_and_a_sites_changes():
    ms = modes_from_config({"mode": "robotic"})
    assert list(ms.modes) == ["robotic", "attended", "engineering", "shadow"]
    assert ms.default == "robotic" and ms.configured
    assert ms.get("shadow").enclosure == "watch" and not ms.get("shadow").open
    assert ms.get("engineering").observe == "manual" and not ms.get("engineering").requests
    ms = modes_from_config(
        {
            "mode": "night",
            "modes": {
                "attended": {"duty_ttl": 600},  # a built-in, changed
                "engineering": None,  # removed
                "night": {"like": "attended", "description": "ours"},
                "_note": "comments are skipped",
            },
        }
    )
    assert "engineering" not in ms.modes and ms.default == "night"
    assert ms.get("attended").duty_ttl == 600.0
    assert ms.get("night").attended_only and ms.get("night").description == "ours"
    # Without a "mode": the behavior from before modes.
    assert modes_from_config({}).default == "robotic"
    assert not modes_from_config({}).configured
    assert modes_from_config({}, attended_floor=True).default == "attended"


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"modes": {"x": {"open": True}}}, r"modes\.x: a new mode gives observe"),
        ({"modes": {"robotic": {"opne": True}}}, r"modes\.robotic: unknown keys \['opne'\]"),
        ({"modes": {"robotic": {"observe": "maybe"}}}, r"modes\.robotic\.observe: one of"),
        ({"modes": {"robotic": {"open": "yes"}}}, r"modes\.robotic\.open: true or false"),
        ({"modes": {"robotic": {"duty_ttl": 0}}}, r"modes\.robotic\.duty_ttl"),
        ({"modes": {"shadow": {"open": True}}}, r"modes\.shadow: observe 'shadow'"),
        ({"modes": {"x": {"like": "nope"}}}, r"modes\.x\.like: one of"),
        ({"modes": {"x y": {}}}, r"is not a mode name"),
        ({"modes": []}, r"modes: \{name"),
        ({"mode": "nope"}, r"mode: 'nope' is not a mode here"),
        ({"modes": {"robotic": {"fault_auto_clear": True}}}, r"needs safety\.fault_action"),
    ],
)
def test_bad_definitions_are_refused_with_their_path(config, match):
    with pytest.raises(ValueError, match=match):
        modes_from_config(config, fault_action=False, where="enc.json")


def test_an_attended_only_enclosure_is_a_floor_no_mode_lowers():
    ms = modes_from_config({"mode": "attended"}, attended_floor=True)
    assert "robotic" not in ms.modes  # not offered there
    assert set(ms.modes) == {"attended", "engineering", "shadow"}  # shadow never opens
    for bad in ({"robotic": {}}, {"attended": {"duty_expiry": "alarm"}}):
        with pytest.raises(ValueError, match="must not run unattended"):
            modes_from_config({"modes": bad}, attended_floor=True, where="flwo60.json")
    # The same changes are fine where there is a heartbeat.
    modes_from_config({"modes": {"attended": {"duty_expiry": "alarm"}}})


def test_the_shipped_site_configs():
    for path, offered in [
        ("sites/minerva/aqawan1.json", {"robotic", "attended", "engineering", "shadow"}),
        ("sites/flwo60/flwo60.json", {"attended", "engineering", "shadow"}),
        # No watchdog, but the site opts in to running unattended (JDE 2026-11-02).
        ("sites/mearth_south/mearth_south.json", {"robotic", "attended", "engineering", "shadow"}),
    ]:
        config = load_config(ROOT / path)
        scfg = SafetyConfig(**config["safety"])
        ms = modes_from_config(
            config, attended_floor=bool(scfg.attended_only), fault_action=bool(scfg.fault_action)
        )
        assert set(ms.modes) == offered, path
        # MINERVA (a hardware heartbeat) starts robotic; the others attended.
        want = "robotic" if "minerva" in path else "attended"
        assert ms.default == want and ms.configured, path
        assert scfg.unwatched is ("mearth" in path), path


def test_which_leases_survive_a_switch():
    b = BUILTIN_MODES
    assert lease_survives(b["robotic"], b["attended"])  # same work; duty checked on top
    assert lease_survives(b["attended"], b["robotic"])
    assert not lease_survives(b["robotic"], b["engineering"])  # other work
    assert not lease_survives(b["engineering"], b["robotic"])
    assert not lease_survives(b["robotic"], b["shadow"])  # may not open


# -- the state file ------------------------------------------------------------------


def test_the_mode_file(tmp_path):
    ms = modes_from_config({"mode": "attended"})
    store = ModeStore(tmp_path / "modes" / "e.json")
    mode, record, why = store.load(ms)
    assert mode == "attended" and record == {} and why.startswith("no mode file")
    assert store.save({"mode": "robotic", "by": "alice", "reason": "night", "since": "t"})
    mode, record, why = store.load(ms)
    assert mode == "robotic" and record["by"] == "alice" and why is None
    for text in ("{not json", '{"mode": 3}', '{"mode": "gone"}'):
        store.path.write_text(text)
        mode, _, why = store.load(ms)
        assert mode == "attended" and why and not why.startswith("no mode file"), text
    assert ModeStore(None).load(ms) == ("attended", {}, None)
    blocked = tmp_path / "file"
    blocked.write_text("")
    bad = ModeStore(blocked / "e.json")  # its directory is a file
    assert not bad.save({"mode": "robotic"}) and "could not write" in bad.error


# -- the supervisor ------------------------------------------------------------------


async def _open(rig):
    lease = await asyncio.wait_for(rig.supervisor.request_open("seq"), 3)
    renewer = asyncio.create_task(_renew(lease))
    await asyncio.wait_for(lease.wait_open(), 3)
    return lease, renewer


async def _renew(lease) -> None:
    while lease.live:
        lease.renew()
        await asyncio.sleep(0.1)


async def test_a_safer_mode_closes_at_once_and_is_logged(make_rig):
    rig = make_rig(modes={"mode": "robotic"})
    await rig.start(sequencer=False)
    sup = rig.supervisor
    lease, renewer = await _open(rig)
    try:
        with pytest.raises(ValueError, match="reason"):
            sup.set_mode("engineering", "alice", " ")
        with pytest.raises(ValueError, match="unknown mode"):
            sup.set_mode("party", "alice", "why not")
        st = sup.set_mode("engineering", "alice", "fixing the roof motor")
        assert st["changed"] and st["mode"] == "engineering" and st["by"] == "alice"
        await asyncio.wait_for(lease.revoked.wait(), 1)
        assert "mode engineering" in lease.revoke_reason
    finally:
        renewer.cancel()
    lease.release()
    await wait_until(rig.dome_closed, 3, what="closed after the switch")
    (event,) = rig.timeline.events("mode")
    assert event.detail["was"] == "robotic" and event.detail["reason"] == "fixing the roof motor"
    status = sup.status()["mode"]
    assert status["notice"] == "mode robotic -> engineering by alice: fixing the roof motor"
    assert status["policy"]["observe"] == "manual" and "robotic" in status["modes"]
    # Engineering needs an operator on duty to open (for manual control).
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sup.request_open("seq"), 0.5)
    sup.duty_on("alice", "engineering")
    lease = await asyncio.wait_for(sup.request_open("seq"), 3)
    lease.release()


async def test_a_switch_keeps_a_lease_only_when_it_may_go_on(make_rig):
    rig = make_rig(modes={"mode": "attended"})
    await rig.start(sequencer=False)
    sup = rig.supervisor
    sup.duty_on("alice", "observing")
    lease, renewer = await _open(rig)
    try:
        sup.set_mode("robotic", "alice", "going to bed")  # less safe: nothing to do
        await asyncio.sleep(0.3)
        assert lease.live and rig.dome_open()
        sup.duty_off("alice", "bed")  # robotic: no duty needed
        await asyncio.sleep(0.3)
        assert lease.live and rig.dome_open()
        # Back to attended with nobody on duty: closes at once.
        sup.set_mode("attended", "bob", "someone should watch")
        await asyncio.wait_for(lease.revoked.wait(), 1)
        assert "needs an operator on duty" in lease.revoke_reason
    finally:
        renewer.cancel()
    lease.release()


async def test_a_less_safe_mode_never_opens_by_itself(make_rig):
    rig = make_rig(modes={"mode": "shadow"}, plan=StaticPlan([{"name": "a", "count": 2}], ["t1"]))
    await rig.start()
    await asyncio.sleep(1.0)  # past the reopen hold, weather safe
    assert not rig.timeline.events("lease_granted") and rig.dome_closed()
    rig.supervisor.set_mode("robotic", "alice", "go")
    await wait_until(lambda: rig.timeline.events("open_commanded"), 5, what="opened")
    mode_t = rig.timeline.events("mode")[0].t
    granted = rig.timeline.events("lease_granted")[0].t
    assert mode_t < granted < rig.timeline.events("open_commanded")[0].t


async def test_shadow_commands_nothing(make_rig):
    rig = make_rig(
        modes={"mode": "shadow"},
        plan=StaticPlan([{"name": "a", "count": 2}, {"name": "b", "light": False}], ["t1"]),
    )
    rig.tel._parked = False  # a restart would park it: shadow must not
    await rig.start()
    sup = rig.supervisor
    await wait_until(lambda: rig.timeline.events("shadow_would"), 3, what="shadow log")
    # The supervisor's "would open", once the weather has been safe for the hold.
    await wait_until(
        lambda: any(e.detail.get("open") for e in rig.timeline.events("shadow_would")),
        5,
        what="would open",
    )
    assert sup.status()["mode"]["would_open"]
    # The sequencer's "would observe a" (peeked: the plan is unchanged).
    await wait_until(
        lambda: (
            "observe a (expose, light)"
            in [e.detail.get("would") for e in rig.timeline.events("shadow_would")]
        ),
        5,
        what="the sequencer's shadow log",
    )
    assert rig.timeline.events("startup_held") and not rig.timeline.events("startup_cleanup")
    assert not rig.tel._parked and not rig.timeline.events("lease_granted")
    # Someone else opens the enclosure: an alarm, never a close.
    rig.dome._move(0)
    await wait_until(lambda: sup.critical(), 3, what="watch alarm")
    assert "commands nothing" in sup.critical()[0]
    await asyncio.sleep(0.5)
    assert rig.dome_open() and not rig.timeline.events("close_commanded")
    calls = [e.what for e in rig.timeline.entries() if e.kind == "command"]
    assert not {"Park", "AbortExposure", "CloseShutter", "OpenShutter"} & set(calls)


# -- shadow intervenes on an active safety risk (JDE 2026-11-02) -----------------------


async def test_shadow_closes_on_rain_while_someone_else_has_it_open(make_rig):
    rig = make_rig(modes={"mode": "shadow"})
    await rig.start(sequencer=False)
    sup = rig.supervisor
    await asyncio.sleep(0.3)  # weather read: safe
    rig.dome._move(0)  # the legacy system opens
    await wait_until(lambda: sup.critical(), 3, what="watch alarm")
    await asyncio.sleep(0.5)
    assert rig.dome_open() and not rig.timeline.events("close_commanded")  # safe: no
    rig.weather.set_safe(False)  # rain
    await wait_until(rig.dome_closed, 5, what="closed on rain")
    (ev,) = rig.timeline.events("shadow_intervened")
    assert "weather unsafe" in ev.detail["risk"] and ev.detail["shutter"] == "OPEN"
    assert any("intervened" in a for a in sup.alarms)
    await wait_until(
        lambda: any(e.detail.get("after") for e in rig.timeline.events("watching")),
        3,
        what="watching again",
    )
    assert sup.status()["mode"]["intervening"] is None
    # Safe again: shadow never opens.
    rig.weather.set_safe(True)
    await asyncio.sleep(1.0)
    assert rig.dome_closed() and not rig.timeline.events("open_commanded")
    assert not rig.timeline.events("lease_granted")


async def test_shadow_closes_on_an_enclosure_fault(make_rig):
    rig = make_rig(modes={"mode": "shadow"}, safety=dict(fault_action="Faults"))
    state = {"fault": False}

    def action(name, parameters=""):
        assert name == "Faults"
        return json.dumps({"fault": state["fault"], "error": False, "faults": "overcurrent"})

    rig.dome.Action = action
    await rig.start(sequencer=False)
    rig.dome._move(0)
    await asyncio.sleep(0.5)
    assert rig.dome_open()
    state["fault"] = True
    await wait_until(rig.dome_closed, 5, what="closed on the fault")
    (ev,) = rig.timeline.events("shadow_intervened")
    assert "enclosure fault" in ev.detail["risk"]


async def test_shadow_does_not_act_on_what_it_does_not_know(make_rig):
    # No successful weather read (the feed is down): not an active risk, so
    # a restarted supervisor in shadow leaves the other system's dome alone.
    rig = make_rig(modes={"mode": "shadow"})
    rig.sm.fail("weather bridge down")
    rig.dome._move(0)
    await rig.start(sequencer=False)
    await asyncio.sleep(1.0)
    assert not rig.supervisor.safe and rig.dome_open()
    assert not rig.timeline.events("shadow_intervened")


async def test_intervention_is_a_per_mode_knob(make_rig):
    rig = make_rig(modes={"mode": "shadow", "modes": {"shadow": {"intervene": False}}})
    await rig.start(sequencer=False)
    rig.dome._move(0)
    await asyncio.sleep(0.3)
    rig.weather.set_safe(False)
    await asyncio.sleep(1.0)
    assert rig.dome_open() and not rig.timeline.events("close_commanded")
    assert "commands nothing" in rig.supervisor.critical()[0]
    with pytest.raises(ValueError, match="intervene: true or false"):
        modes_from_config({"modes": {"shadow": {"intervene": "yes"}}})


async def test_switching_to_shadow_while_open_closes_first_then_watches(make_rig):
    rig = make_rig(modes={"mode": "robotic"})
    await rig.start()
    await wait_until(lambda: rig.cam._state == 2, 5, what="exposing")
    rig.supervisor.set_mode("shadow", "alice", "legacy takes over")
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 5, what="parked and closed")
    await wait_until(lambda: rig.timeline.events("watching"), 3, what="watching")
    assert not rig.supervisor.status()["mode"]["finishing_close"]
    n = len(rig.timeline.events("lease_granted"))
    await asyncio.sleep(1.0)
    assert len(rig.timeline.events("lease_granted")) == n and rig.dome_closed()


async def test_duty_expiry_may_only_alarm_where_there_is_a_heartbeat(make_rig):
    rig = make_rig(
        safety=dict(duty_ttl=0.4, duty_grace=0.4),
        modes={"mode": "attended", "modes": {"attended": {"duty_expiry": "alarm"}}},
    )
    await rig.start(sequencer=False)
    sup = rig.supervisor
    sup.duty_on("alice", "observing")

    async def console() -> None:  # the page renews the duty while it is open
        while True:
            sup.duty_renew("alice")
            await asyncio.sleep(0.1)

    page = asyncio.create_task(console())
    try:
        lease, renewer = await _open(rig)
    finally:
        page.cancel()
    try:
        await wait_until(lambda: sup.critical(), 2, what="duty expiry alarm")
        assert "does not close for it" in sup.critical()[0]
        await asyncio.sleep(1.0)  # well past ttl + grace
        assert lease.live and rig.dome_open()
    finally:
        renewer.cancel()
    lease.release()


async def test_the_mode_survives_a_restart(make_rig, tmp_path):
    store = ModeStore(tmp_path / "aqawan1.json")
    rig = make_rig(modes={"mode": "attended"}, mode_store=store)
    rig.supervisor.set_mode("robotic", "alice", "unattended tonight")
    again = make_rig(modes={"mode": "attended"}, mode_store=ModeStore(store.path))
    assert again.supervisor.mode == "robotic"
    assert again.supervisor.status()["mode"]["by"] == "alice"
    store.path.write_text("garbage")
    third = make_rig(modes={"mode": "attended"}, mode_store=ModeStore(store.path))
    assert third.supervisor.mode == "attended"
    assert any("unreadable" in a for a in third.supervisor.alarms)


@pytest.mark.parametrize("lease_path", ["remote"])
async def test_the_mode_over_the_api(make_rig):
    rig = make_rig(modes={"mode": "robotic"})
    await rig.start(sequencer=False)
    url = rig.server.url
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{url}/mode", json={"operator": "alice", "mode": "nope", "reason": "x"}
        )
        assert r.status_code == 400 and "unknown mode" in r.text
        r = await http.post(f"{url}/mode", json={"operator": "", "mode": "shadow", "reason": "x"})
        assert r.status_code == 400
        r = await http.post(
            f"{url}/mode", json={"operator": "alice", "mode": "shadow", "reason": "test"}
        )
        assert r.status_code == 200 and r.json()["mode"] == "shadow"
        st = (await http.get(f"{url}/status")).json()
    assert st["mode"]["mode"] == "shadow" and st["mode"]["policy"]["enclosure"] == "watch"
    assert (await rig.remote.mode_status())["mode"] == "shadow"


# -- the sequencer's gate ------------------------------------------------------------


class _Source:
    def __init__(self, mode: str | None) -> None:
        self.mode = mode
        self.fail = False

    async def mode_status(self):
        if self.fail:
            raise OSError("unreachable")
        if self.mode is None:
            return None
        return {"mode": self.mode, "policy": BUILTIN_MODES[self.mode].as_json()}


class _Hold:
    """An OperatorHold stand-in: t1 is under manual control."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.modes: list = []

    def manual_session(self, telescope):
        return object() if telescope == "t1" else None

    async def next_observation(self, telescope):
        self.asked.append(("next", telescope))
        return Observation(f"obs-{telescope}")

    async def peek(self, telescope):
        self.asked.append(("peek", telescope))
        return Observation(f"obs-{telescope}")

    def set_mode(self, mode, policy):
        self.modes.append(mode)


async def test_the_gate():
    events: list = []
    src, plan = _Source("robotic"), _Hold()
    gate = ModeGate(plan, lambda: src, lambda what, **kw: events.append((what, kw)), refresh=0)
    assert (await gate.next_observation("t2")).name == "obs-t2"
    src.mode = "engineering"  # manual: only t1 (under manual control) is asked
    assert (await gate.next_observation("t1")).name == "obs-t1"
    plan.asked.clear()
    answer = await gate.next_observation("t2")
    assert isinstance(answer, Idle) and "manual control only" in answer.reason
    assert ("next", "t2") not in plan.asked  # only peeked, never taken
    src.mode = "shadow"
    plan.asked.clear()
    assert isinstance(await gate.next_observation("t1"), Idle)
    assert plan.asked == [("peek", "t1")]
    assert (
        "shadow_would",
        {"telescope": "t1", "mode": "shadow", "would": "observe obs-t1 (expose, light)"},
    ) in events
    assert plan.modes == ["robotic", "engineering", "shadow"]
    src.mode = None  # a supervisor from before modes
    assert (await gate.next_observation("t2")).name == "obs-t2"
    fresh = ModeGate(plan, lambda: src, lambda *a, **k: None)
    src.fail = True  # never read: nothing autonomous
    assert isinstance(await fresh.next_observation("t2"), Idle)
    assert await ModeGate(plan, lambda: object(), print).update() == "plan"  # no modes at all


async def test_the_mode_goes_into_the_fits_headers():
    class Sup:
        def status(self):
            return {"shutter": "CLOSED", "safe": True, "mode": {"mode": "attended"}}

    reg = ValueRegistry()
    await Telemetry(reg, supervisor=Sup()).poll_enclosure()
    v = reg.get("enclosure.mode")
    assert v.value == "attended" and v.keyword == "OBSMODE"


def test_the_dispatch_scheduler_records_the_mode(tmp_path):
    from robofast.dispatch import dispatch_from_config
    from robofast.plan_record import PlanRecord, read_record

    rec = PlanRecord(tmp_path, name="e")
    entry = json.loads((ROOT / "sites/minerva/dispatch.example.json").read_text())
    drop = ("type", "_note", "record", "state", "too_dropbox", "requests")
    entry = {k: v for k, v in entry.items() if k not in drop}
    sched = dispatch_from_config(
        entry,
        ["t1", "t2"],
        base=tmp_path,
    )
    sched.record = rec
    sched.set_mode("attended", {"observe": "plan"})
    sched.set_mode("attended", {"observe": "plan"})  # unchanged: not recorded again
    sched.set_held({"t1"})
    entries = [e for f in tmp_path.glob("*.jsonl") for e in read_record(f)]
    kinds = [e["kind"] for e in entries if e["kind"] != "snapshot"]
    assert kinds == ["mode", "held"]
    assert all(e["mode"] == "attended" for e in entries if e["kind"] != "snapshot")


@pytest.mark.parametrize("lease_path", ["remote"])
async def test_the_command_line(make_rig, tmp_path, capsys):
    from robofast.modes import main

    rig = make_rig(modes={"mode": "attended"})
    await rig.start(sequencer=False)
    cfg = tmp_path / "e.json"
    cfg.write_text(json.dumps({"name": "e", "supervisor_url": rig.server.url}))
    await asyncio.to_thread(main, ["--config", str(cfg), "show"])
    assert json.loads(capsys.readouterr().out)["mode"] == "attended"
    args = ["--config", str(cfg), "set", "robotic", "--operator", "jde", "--reason", "tonight"]
    await asyncio.to_thread(main, args)
    assert json.loads(capsys.readouterr().out)["mode"] == "robotic"
    assert rig.supervisor.status()["mode"]["by"] == "jde"
    with pytest.raises(SystemExit, match="refused"):
        await asyncio.to_thread(main, [*args[:3], "nope", *args[4:]])


def test_the_supervisor_service_keeps_the_mode_in_the_data_directory(monkeypatch, tmp_path):
    from robofast import supervisor_service
    from robofast.modes import mode_path
    from robofast.timeline import Timeline

    monkeypatch.setenv("ROBOFAST_DATA_DIR", str(tmp_path))
    config = load_config(ROOT / "sites/flwo60/flwo60.json")
    assert mode_path(config) == tmp_path / "modes" / "flwo60.json"
    assert mode_path({**config, "mode_file": "x/m.json"}) == tmp_path / "x" / "m.json"
    sup = supervisor_service.build(config, Timeline())
    assert sup.mode == "attended" and "robotic" not in sup.status()["mode"]["modes"]
    sup.set_mode("engineering", "jde", "dome work")
    assert json.loads((tmp_path / "modes" / "flwo60.json").read_text())["mode"] == "engineering"
    with pytest.raises(ValueError, match="must not run unattended"):
        supervisor_service.build({**config, "modes": {"robotic": {}}}, Timeline())
