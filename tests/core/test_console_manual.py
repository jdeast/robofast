"""Manual control (docs/console.md, "Manual control"): an operator takes one
telescope out of robotic control and slews, offsets, focuses, exposes and
guides it through the sequencer's fenced grants, under the lease; the
safety supervisor still overrides everything."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from robofast_core import sky
from robofast_core.api_auth import TokenStore
from robofast_core.console.hold import ManualSession, OperatorHold, read_holds, send_command
from robofast_core.console.server import (
    Console,
    ConsoleHttpServer,
    build_enclosures,
    load_console_config,
)
from robofast_core.plan import CallablePlan, Observation, StaticPlan
from robofast_core.sequencer_service import write_snapshot
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.checks import wait_until

from . import test_console as _console
from .test_console import Recorder, write_site

make_console = _console.make_console


# -- the hold wrapper --------------------------------------------------------------------


async def test_manual_hold_gives_the_telescope_a_manual_session(tmp_path):
    inner = StaticPlan([{"name": "a"}, {"name": "b"}], ["t1", "t2"])
    hold = OperatorHold(inner, ["t1", "t2"], tmp_path)
    pre = Recorder()
    hold.set_preempt_callback(pre)
    # Commands before manual control are refused.
    send_command(tmp_path, "command", ["t1"], operator="al", reason="r", manual={"op": "slew"})
    send_command(tmp_path, "release", ["t1"], operator="al", reason="r")
    assert [r["ok"] for r in hold.take_in()] == [False, False]
    send_command(tmp_path, "manual", ["t1"], operator="al", reason="look at a comet")
    hold.take_in()
    assert pre.calls[-1] == ("t1", "now-after-readout", "operator manual: look at a comet")
    obs = await hold.next_observation("t1")
    assert obs.kind == "manual" and obs.light and (await hold.peek("t1")).kind == "manual"
    assert (await hold.next_observation("t2")).name == "a"  # t2 stays robotic
    hold.report("t1", obs, "interrupted", 0)  # nothing of the robotic plan's
    assert inner.history == []
    session = hold.manual_session("t1")
    assert isinstance(session, ManualSession) and session.operator == "al"
    # Not running yet (no lease): a command is refused, not queued.
    cmd = {"op": "slew", "ra_hours": 1.0, "dec_deg": 2.0}
    send_command(tmp_path, "command", ["t1"], operator="al", reason="r", manual=cmd)
    (res,) = hold.take_in()
    assert res["ok"] is False and "not running" in res["error"]
    session.started(3)
    send_command(tmp_path, "command", ["t1"], operator="bob", reason="r", manual=cmd)
    send_command(tmp_path, "command", ["t1"], operator="al", reason="r", manual=cmd)
    bob, al = hold.take_in()
    assert bob["ok"] is False and "al's control" in bob["error"] and al["ok"] is True
    doc = read_holds(tmp_path)
    assert doc["holds"]["t1"]["action"] == "manual"
    assert doc["manual"]["t1"]["active"] is True and doc["manual"]["t1"]["queued"] == 1
    got = await session.next()
    assert got["op"] == "slew" and got["ra_hours"] == 1.0
    session.result(got, True, {"ra_hours": 1.0})
    assert read_holds(tmp_path)["manual"]["t1"]["results"][-1]["ok"] is True
    # The manual hold survives a restart of the sequencer.
    again = OperatorHold(StaticPlan([{"name": "a"}], ["t1", "t2"]), ["t1", "t2"], tmp_path)
    assert (await again.next_observation("t1")).kind == "manual"
    # Release ends the session and wakes the telescope for its plan.
    send_command(tmp_path, "release", ["t1"], operator="al", reason="done")
    hold.take_in()
    assert await session.next() is None and hold.manual_session("t1") is None
    assert pre.calls[-1][:2] == ("t1", "next")
    assert (await hold.next_observation("t1")).kind == "expose"


# -- the console against a real supervisor and sequencer on fakes ------------------------------


async def test_manual_control_end_to_end_and_weather_preempts_it(make_rig, tmp_path):
    k = iter(range(10**6))
    inner = CallablePlan(lambda: Observation(f"target{next(k)}", exptime=0.3, count=1000))
    hold = OperatorHold(inner, ["t1", "t2"], tmp_path / "control", poll=0.05)
    rig = make_rig(telescopes=2, plan=hold, seq={"idle_release_after": None})
    await rig.start()
    sup = await SupervisorServer(rig.supervisor).start()
    serve = asyncio.create_task(hold.serve())

    async def snapshots():
        while True:
            write_snapshot(rig.sequencer, tmp_path / "seq-status.json")
            await asyncio.sleep(0.1)

    snaps = asyncio.create_task(snapshots())
    path = write_site(
        tmp_path, supervisor_url=sup.url, hold_ack_timeout=5.0, cache=0, action_burst=60
    )
    cfg = json.loads((tmp_path / "aqawan1.json").read_text())
    del cfg["telescopes"]["t1"]["guider"], cfg["devices"]["safetymonitor"]
    (tmp_path / "aqawan1.json").write_text(json.dumps(cfg))
    config, spec, base = load_console_config(path)
    del spec["aqawan1"]["spectrograph"]
    store = TokenStore(config.tokens)
    tokens = {n: store.add(n, ["read", "operator"])[1] for n in ("alice", "bob")}
    app = Console(build_enclosures(spec, base), store, config=config)
    server = await ConsoleHttpServer(app).start()
    clients = {n: httpx.AsyncClient(base_url=server.url, timeout=30) for n in tokens}
    headers = {}

    async def manual(who: str = "alice", **body) -> httpx.Response:
        return await clients[who].post(
            "/console/api/manual",
            json={"telescope": "t1", **body},
            headers=headers[who],
        )

    def session() -> dict:
        return (read_holds(tmp_path / "control") or {}).get("manual", {}).get("t1") or {}

    async def result(n: int) -> dict:
        await wait_until(lambda: len(session().get("results", [])) >= n, 10, what="a result")
        return session()["results"][n - 1]

    # A target well up now (the plan's site), and one below the horizon.
    site = app.enclosures["aqawan1"].requests.view().site
    lst = sky.sky_now(site, datetime.now(UTC)).lst_hours
    up = {"ra_hours": round(lst % 24, 4), "dec_deg": round(site.latitude - 25, 2)}
    down = {"ra_hours": round((lst + 12) % 24, 4), "dec_deg": -60.0}
    try:
        for n, c in clients.items():
            csrf = (await c.post("/console/login", json={"token": tokens[n]})).json()["csrf"]
            headers[n] = {"X-CSRF-Token": csrf}
        await wait_until(lambda: rig.dome_open() and rig.exposing(), 10, what="observing")

        # Take t1: a reason and confirm needed; the scheduler stops using it.
        assert (await manual(op="take", reason="comet")).status_code == 400  # no confirm
        r = await manual(op="take", reason="comet", confirm=True)
        assert r.status_code == 200 and r.json()["result"]["applied"] is True
        await wait_until(lambda: session().get("active"), 10, what="manual session running")
        t_take = rig.timeline.first_event("manual_start").t
        st = (await clients["alice"].get("/console/api/status")).json()["enclosures"]["aqawan1"]
        assert st["telescopes"]["t1"]["manual"]["operator"] == "alice"
        assert rig.exposing(1)  # t2 observes on

        # Slew, refused outside the limits; offset; expose; focus (no focuser here).
        r = await manual(op="slew", **down)
        assert r.status_code == 409 and "altitude" in r.json()["error"]
        r = await manual(op="slew", **up)
        assert r.status_code == 200, r.text
        assert (await result(1))["ok"] is True
        assert (rig.tels[0].ra, rig.tels[0].dec) == pytest.approx((up["ra_hours"], up["dec_deg"]))
        assert (await manual(who="bob", op="slew", **up)).status_code == 409  # alice's
        r = await manual(op="offset", east_arcsec=0, north_arcsec=36)
        assert r.status_code == 200, r.text
        assert (await result(2))["ok"] is True
        assert rig.tels[0].dec == pytest.approx(up["dec_deg"] + 0.01, abs=1e-6)
        r = await manual(op="expose", exptime=0.2, count=1, object="comet")
        assert r.status_code == 200, r.text
        assert (await result(3))["detail"]["frames"] == 1
        r = await manual(op="focus", delta=100)
        assert r.status_code == 200
        res = await result(4)
        assert res["ok"] is False and "no focuser" in res["detail"]
        r = await manual(op="guide_start")
        assert r.status_code == 200
        res = await result(5)
        assert res["ok"] is False and "no guider" in res["detail"]
        # Abort: a long exposure is stopped; the session goes on.
        r = await manual(op="expose", exptime=30, count=1)
        assert r.status_code == 200
        await wait_until(lambda: rig.exposing(0), 5, what="exposing")
        assert (await manual(op="expose", exptime=1)).status_code == 409  # busy
        assert (await manual(op="abort")).status_code == 200
        res = await result(6)
        assert res["ok"] is False and "interrupted" in res["detail"]
        await wait_until(lambda: rig.camera_idle(0) and session().get("active"), 10, what="again")
        aborts = [c for c in rig.timeline.commands(device="t1.camera") if c.what == "AbortExposure"]
        assert aborts
        for bad in ({"op": "slew"}, {"op": "expose", "exptime": -1}, {"op": "jump"}):
            assert (await manual(**bad)).status_code == 400, bad
        # The robotic plan never got t1 while it was manual.
        starts = [
            e
            for e in rig.timeline.events("target_start")
            if e.t > t_take and e.detail["telescope"] == "t1"
        ]
        assert {e.detail["target"] for e in starts} <= {"manual-t1"}

        # Bad weather: the supervisor revokes the lease -- the manual session's
        # grants with it -- parks and closes; nothing needing the sky is accepted.
        rig.weather.safe = False
        await wait_until(lambda: rig.dome_closed() and rig.parked(), 15, what="closed")
        await wait_until(lambda: not session().get("active"), 5, what="session stopped")
        assert any(
            e.detail.get("why") == "the lease was revoked" or e.detail.get("why") == "interrupted"
            for e in rig.timeline.events("manual_end")
        )
        r = await manual(op="slew", **up)
        assert r.status_code == 409 and "lease" in r.json()["error"]
        r = await manual(op="expose", exptime=0.2)
        assert r.status_code == 409
        assert read_holds(tmp_path / "control")["holds"]["t1"]["action"] == "manual"

        # Safe again: the supervisor reopens by its own policy; the session
        # runs again; release returns t1 to the plan.
        rig.weather.safe = True
        await wait_until(lambda: rig.dome_open() and session().get("active"), 15, what="back")
        r = await manual(op="release", reason="done")
        assert r.status_code == 200, r.text
        await wait_until(lambda: rig.exposing(0), 10, what="t1 robotic again")
        assert "t1" not in (read_holds(tmp_path / "control") or {}).get("holds", {})
        audit = [
            json.loads(x)
            for x in (tmp_path / "log" / "console-audit.jsonl").read_text().splitlines()
        ]
        ops = [a.get("op") for a in audit if a.get("action") == "manual"]
        assert ops[:3] == ["take", "take", "slew"] and "release" in ops
        assert all(c.source == "safety" for c in rig.timeline.commands(device="dome"))
    finally:
        snaps.cancel()
        serve.cancel()
        for c in clients.values():
            await c.aclose()
        await server.stop()
        await app.aclose()
        await sup.stop()
        await rig.stop()


async def test_manual_needs_operator_control_and_the_operator_scope(make_console):
    rig = await make_console()
    await rig.login("watcher")
    r = await rig.act("manual", telescope="t1", op="take", reason="x", confirm=True)
    assert r.status_code == 403
    await rig.login("alice")
    r = await rig.act("manual", telescope="t9", op="take", reason="x", confirm=True)
    assert r.status_code == 400
    r = await rig.act("manual", telescope="t1", op="slew", ra_hours=1, dec_deg=1)
    assert r.status_code == 409 and "not under manual control" in r.json()["error"]


def test_the_page_has_manual_control():
    from robofast_core.console.page import render_page

    page = render_page("n")
    assert 'id="manual"' in page and 'id="mc-take"' in page and "/console/api/manual" in page
