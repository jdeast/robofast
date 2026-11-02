"""The TRES calibration lamps: the Pulizzi protocol client and its simulator,
the Alpaca Switch server (gate, read-only outlets, max-on, clean stop), the
request model and dispatch for ARC/LAMP_FLAT, and the sequencer's lamp
frames (lamp always switched off).

Fast tier: everything runs in this process against the simulated unit
(``port.type`` "sim"); no serial port is opened.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time

import httpx
import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.config import device_address, telescopes
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.devices.pulizzi import (
    PulizziError,
    PulizziIPC,
    PulizziTimeout,
    allowed,
    parse_status,
)
from robofast_core.devices.pulizzi_server import PulizziConfig, PulizziServer
from robofast_core.devices.pulizzi_sim import PulizziSim
from robofast_core.dispatch import TelescopeLimits
from robofast_core.lamps import LampSetup
from robofast_core.plan import Observation, StaticPlan
from robofast_core.request_model import NO_LAMPS, RequestGroup
from robofast_core.sequencer_service import build_units, load_plan
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT
from robofast_core.timeline import Timeline
from tests.core.test_dispatch import group
from tests.core.test_request_api import make_api  # noqa: F401 (fixture)

SITE = ROOT / "sites" / "flwo60"
LAMPS = [
    {"outlet": 2, "name": "ThAr1", "lamp": True, "max_on": 600},
    {"outlet": 4, "name": "Incand", "lamp": True, "max_on": 600},
    {"outlet": 3, "name": "other"},
]


def lamp_config(**kw) -> PulizziConfig:
    sim = kw.pop("sim", {"on": [3, 5]})
    d = {
        "listen": "127.0.0.1:0",
        "port": {"type": "sim", "timeout": 0.3, "sim": sim},
        "outlets": kw.pop("outlets", LAMPS),
        "poll_interval": 0.05,
        "status_max_age": 0.02,
        "request_timeout": 4.0,
        **kw,
    }
    return PulizziConfig.from_dict(d)


@pytest.fixture
def serve():
    servers: list[PulizziServer] = []

    def start(**kw) -> PulizziServer:
        s = PulizziServer(lamp_config(**kw)).start()
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.stop()


class Client:
    def __init__(self, server: PulizziServer, client_id: int = 7) -> None:
        self.base = f"http://{server.address}/api/v1/switch/0/"
        self.id = client_id
        self.http = httpx.Client(timeout=10)

    def get(self, member: str, **params):
        r = self.http.get(self.base + member, params={"ClientID": self.id, **params}).json()
        return (
            r["Value"] if r["ErrorNumber"] == 0 else ("error", r["ErrorNumber"], r["ErrorMessage"])
        )

    def put(self, member: str, **params):
        data = {"ClientID": str(self.id), **{k: str(v) for k, v in params.items()}}
        r = self.http.put(self.base + member, data=data).json()
        return (
            r.get("Value")
            if r["ErrorNumber"] == 0
            else ("error", r["ErrorNumber"], r["ErrorMessage"])
        )

    def connect(self) -> Client:
        assert self.put("connected", Connected=True) is None
        return self

    def status(self) -> dict:
        return json.loads(self.put("action", Action="Status", Parameters=""))


def switching(sim: PulizziSim) -> list[str]:
    return [c for c in sim.commands if c[:1] in "NF" and c != sim.name]


# -- the protocol client against the simulator ----------------------------------------


async def test_client_logs_in_reads_and_switches():
    sim = PulizziSim(on=[3], nulls=True)
    unit = PulizziIPC(sim, timeout=0.3, settle=0.05)
    st = await unit.status()
    assert st.outlets == {k: k == 3 for k in range(1, 9)}
    st = await unit.switch(2, True)
    assert st.outlets[2] and sim.lit() == [2, 3]
    await unit.switch(2, False)
    assert sim.commands == ["@@@@", "DX0", "N02", "DX0", "F02", "DX0"]


def test_only_the_allowed_commands_are_ever_sent():
    for ok in ("@@@@", "DX0", "N01", "F08"):
        assert allowed(ok, "@@@@")
    # all-outlets, sequences, reboot, watchdog, power-on preset, name,
    # password, log-out, other units, outlets out of range, lower case
    for bad in ("A00", "A10", "S10", "S00", "SR0", "WE0", "WT0", "IS0", "AD0", "PW0", "PD0"):
        assert not allowed(bad, "@@@@")
    for bad in ("LO", "DX1", "N09", "N00", "N2", "n02", "N02 ", "?", "CN01", "DN0"):
        assert not allowed(bad, "@@@@")
    unit = PulizziIPC(PulizziSim(), timeout=0.2)
    with pytest.raises(PulizziError, match="refusing"):
        asyncio.run(unit._send("A10"))
    with pytest.raises(PulizziError, match="no outlet"):
        asyncio.run(unit.switch(9, True))
    with pytest.raises(ValueError):
        PulizziIPC(PulizziSim(), name="ab")


async def test_a_lost_session_is_logged_into_again():
    sim = PulizziSim()
    unit = PulizziIPC(sim, timeout=0.2, settle=0.05)
    await unit.status()
    sim.drop_session()  # e.g. the unit power-cycled: it ignores DX0 now
    st = await unit.status()
    assert st.outlets[1] is False and sim.commands.count("@@@@") == 2


async def test_invalid_ends_the_session_and_the_client_recovers():
    sim = PulizziSim()
    unit = PulizziIPC(sim, timeout=0.2, settle=0.05)
    await unit.status()
    await sim.write(b"XYZ\r")  # noise on the line: INVALID, session over
    st = await unit.status()
    assert st.outlets and sim.logged_in


async def test_a_dead_unit_is_a_bounded_error():
    sim = PulizziSim()
    sim.silent = True
    unit = PulizziIPC(sim, timeout=0.1)
    t0 = time.monotonic()
    with pytest.raises(PulizziTimeout):
        await unit.status()
    assert time.monotonic() - t0 < 1.0


async def test_a_front_panel_override_is_reported():
    sim = PulizziSim()
    sim.remote_disabled = True  # the unit says DONE but switches nothing
    unit = PulizziIPC(sim, timeout=0.2, settle=0.05)
    with pytest.raises(PulizziError, match="front-panel"):
        await unit.switch(2, True)


async def test_auto_event_listings_are_drained():
    sim = PulizziSim(auto_event=True)
    unit = PulizziIPC(sim, timeout=0.2, settle=0.05)
    await unit.switch(4, True)
    assert (await unit.status()).outlets[4] and sim.lit() == [4]


def test_status_parsing_tolerates_other_lines():
    st = parse_status(["", "OUTLET   1   ON ( UNIT#0 J1 )Web Serv", "REMOTE ENABLED"])
    assert st.outlets == {1: True} and st.other == ["REMOTE ENABLED"]


# -- the server ---------------------------------------------------------------------


def test_starting_connecting_reading_and_stopping_with_no_lamp_on_never_switch(serve):
    s = serve()
    c = Client(s).connect()
    for i in range(3):
        c.get("getswitch", Id=i)
        c.get("getswitchvalue", Id=i)
        c.get("canwrite", Id=i)
    c.status()
    time.sleep(0.2)  # the monitor polls too
    sim = s.sim
    assert sim is not None
    s.stop()
    assert switching(sim) == [] and set(sim.commands) == {"@@@@", "DX0"}
    assert sim.lit() == [3, 5]  # the non-lamp loads, untouched


def test_a_lamp_found_on_is_left_on_at_startup_and_timed_from_then(serve):
    s = serve(sim={"on": [2]}, outlets=[{**LAMPS[0], "max_on": 0.6}])
    sim = s.sim
    time.sleep(0.3)
    assert sim.lit() == [2] and switching(sim) == []  # startup switched nothing
    wait_until_sync(lambda: sim.lit() == [], 3.0)
    assert switching(sim) == ["F02"]
    c = Client(s).connect()
    (trip,) = c.status()["tripped"]
    assert trip["name"] == "ThAr1" and trip["on_for"] >= 0.6


def wait_until_sync(cond, timeout: float) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def test_only_lamps_are_writable(serve):
    s = serve()
    c = Client(s).connect()
    assert c.get("maxswitch") == 3
    assert [c.get("canwrite", Id=i) for i in range(3)] == [True, True, False]
    assert [c.get("getswitchname", Id=i) for i in range(3)] == ["ThAr1", "Incand", "other"]
    assert c.get("getswitch", Id=2) is True  # outlet 3 is on
    err = c.put("setswitch", Id=2, State=False)
    assert err[0] == "error" and err[1] == 0x400 and "not writable" in err[2]
    assert c.put("setswitchvalue", Id=2, Value=0)[1] == 0x400
    assert c.put("setswitch", Id=0, State=True) is None
    assert c.get("getswitch", Id=0) is True and s.sim.lit() == [2, 3, 5]
    assert c.put("setswitchvalue", Id=0, Value=0.0) is None
    assert s.sim.lit() == [3, 5]
    assert c.get("getswitch", Id=5)[1] == 0x401  # no such switch
    assert switching(s.sim) == ["N02", "F02"]


def test_not_connected(serve):
    c = Client(serve())
    assert c.get("getswitch", Id=0)[1] == 0x407
    assert c.put("setswitch", Id=0, State=True)[1] == 0x407


def test_max_on_switches_a_lamp_off_and_a_repeated_on_does_not_reset_it(serve):
    s = serve(outlets=[{**LAMPS[0], "max_on": 0.8}, LAMPS[1]])
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    t0 = time.monotonic()
    time.sleep(0.4)
    c.put("setswitch", Id=0, State=True)  # already on: the clock keeps running
    wait_until_sync(lambda: 2 not in s.sim.lit(), 3.0)
    assert 0.75 < time.monotonic() - t0 < 1.6
    st = c.status()
    assert st["tripped"][0]["name"] == "ThAr1"
    assert st["outlets"][0]["on"] is False and st["outlets"][0]["on_for"] is None
    # Off, then on again: a fresh max_on.
    c.put("setswitch", Id=0, State=True)
    st = c.status()
    assert st["outlets"][0]["on"] and 0.5 < st["outlets"][0]["remaining"] <= 0.8


def test_lamps_off_action_and_clean_stop(serve):
    s = serve()
    sim = s.sim
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    c.put("setswitch", Id=1, State=True)
    off = json.loads(c.put("action", Action="LampsOff", Parameters=""))["off"]
    assert sorted(off) == ["Incand", "ThAr1"] and sim.lit() == [3, 5]
    c.put("setswitch", Id=1, State=True)
    s.stop()  # lamps_off_on_stop (default)
    assert sim.lit() == [3, 5] and switching(sim)[-1] == "F04"


def test_a_stop_can_leave_lamps_alone_when_configured(serve):
    s = serve(lamps_off_on_stop=False)
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    s.stop()
    assert s.sim.lit() == [2, 3, 5]


def test_a_dead_unit_is_a_bounded_alpaca_error(serve):
    s = serve()
    c = Client(s).connect()
    s.sim.silent = True
    t0 = time.monotonic()
    err = c.put("setswitch", Id=0, State=True)
    assert err[0] == "error" and err[1] in (0x500, 0x502)
    assert time.monotonic() - t0 < 4.5
    s.sim.silent = False


def test_hardware_needs_the_config_and_the_flag():
    cfg = lamp_config()
    cfg.port.type = "serial"
    with pytest.raises(HardwareNotAllowed, match="allow_hardware"):
        PulizziServer(cfg)
    with pytest.raises(HardwareNotAllowed):
        PulizziServer(cfg, hardware=True)  # the flag alone is not enough
    cfg.allow_hardware = True
    with pytest.raises(HardwareNotAllowed):
        PulizziServer(cfg)  # nor the config alone


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"outlets": []}, "at least one"),
        ({"outlets": [{"outlet": 9, "name": "x"}]}, "not in 1..8"),
        ({"outlets": [{"outlet": 2, "name": "a"}, {"outlet": 2, "name": "b"}]}, "twice"),
        ({"outlets": [{"outlet": 2, "name": "a"}, {"outlet": 3, "name": "a"}]}, "twice"),
        ({"outlets": [{"outlet": 2, "name": "a", "lamp": True, "max_on": 0}]}, "max_on"),
        ({"outlets": [{"outlet": 2, "name": "a", "lamp": True, "max_on": 1e6}]}, "max_on"),
        ({"outlets": [{"outlet": 2, "name": "a", "cycle": True}]}, "unknown keys"),
        ({"bogus": 1}, "unknown config keys"),
        ({"port": {"type": "tcp"}}, "port.type"),
        ({"unit_name": "abc"}, "unit name"),
    ],
)
def test_config_validation(change, error):
    base = {"outlets": LAMPS, "port": {"type": "sim"}}
    with pytest.raises(ValueError, match=error):
        cfg = PulizziConfig.from_dict({**base, **change})
        PulizziServer(cfg).stop()


def test_shipped_site_configs(tmp_path):
    cfg = PulizziConfig.from_dict(json.loads((SITE / "pulizzi.json").read_text()))
    assert not cfg.allow_hardware and cfg.listen.startswith("127.0.0.1:")
    assert cfg.port.type == "serial" and cfg.lamps_off_on_stop
    lamps = {o.name: o.outlet for o in cfg.outlets if o.lamp}
    # FLWO's legacy outlet map (docs/pulizzi.md).
    assert lamps == {"ThAr1": 2, "ThAr2": 7, "Incand": 4, "Incand2": 1}
    assert all(o.lamp for o in cfg.outlets)  # nothing else is even listed
    assert all(o.rated_hours for o in cfg.outlets) and cfg.hours_file is None
    site = json.loads((SITE / "flwo60.json").read_text())
    (entry,) = telescopes(site).values()
    setup = LampSetup.from_dict(entry["lamps"])
    assert device_address(setup.switch) == (cfg.listen, 0)
    assert set(setup.lamps) == set(lamps)
    (unit,) = build_units(site, Timeline(), 5.0)
    assert unit.lamps is not None and unit.lamp_setup is not None
    for name in ("dispatch.example.json", "requests.example.json"):
        shutil.copy(SITE / name, tmp_path / name)
    plan = load_plan(tmp_path / "dispatch.example.json", ["tillinghast"])
    assert set(plan.limits["tillinghast"].lamps) == set(lamps)
    lamp_of = {
        c.type: c.instrument_configs[0].lamp
        for r in plan.requests.values()
        for c in r.configurations
    }
    assert lamp_of == {"ARC": "ThAr1", "LAMP_FLAT": "Incand"}


def test_the_command_line_refuses_hardware_and_runs_the_simulator():
    cfg = str(SITE / "pulizzi.json")
    mod = "robofast_core.devices.pulizzi_server"
    p = subprocess.run(
        [sys.executable, "-m", mod, "--config", cfg],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert p.returncode == 2 and "allow_hardware" in p.stderr
    p2 = subprocess.Popen(
        [sys.executable, "-m", mod, "--config", cfg, "--simulate", "--listen", "127.0.0.1:0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert p2.stdout is not None
        hello = json.loads(p2.stdout.readline())
        assert hello["port"] == "simulator" and hello["url"].startswith("http://127.0.0.1:")
    finally:
        p2.terminate()
        _, err = p2.communicate(timeout=20)
    assert p2.returncode == 0 and "lamps off" in err


# -- requests and dispatch -----------------------------------------------------------


def arc_group(gid: str = "arc", lamp: str | None = "ThAr1", ctype: str = "ARC", **kw) -> dict:
    ic: dict = {"exposure_time": 30.0, "exposure_count": 2}
    if lamp is not None:
        ic["optical_elements"] = {"lamp": lamp}
    g = group(gid, ics=[ic], **kw)
    g["requests"][0]["configurations"][0]["type"] = ctype
    return g


def test_lamp_requests_parse_and_round_trip():
    for ctype in ("ARC", "LAMP_FLAT"):
        g = RequestGroup.from_dict(arc_group(ctype=ctype))
        (c,) = g.configurations()
        (ic,) = c.instrument_configs
        assert c.type == ctype and ic.lamp == "ThAr1" and not ic.light and not c.light
        again = RequestGroup.from_dict(g.to_dict())
        assert again.configurations()[0].instrument_configs[0].lamp == "ThAr1"
        assert "light" not in json.dumps(g.to_dict())


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"lamp": None}, r"needs instrument_configs\[0\].optical_elements.lamp.*iodine only"),
        ({"ctype": "EXPOSE"}, "only for ARC and LAMP_FLAT"),
    ],
)
def test_lamp_requests_are_checked(change, error):
    with pytest.raises(ValueError, match=error):
        RequestGroup.from_dict(arc_group(**change))


def test_a_guided_or_light_lamp_frame_is_refused():
    g = arc_group()
    g["requests"][0]["configurations"][0]["guiding_config"] = {"mode": "ON"}
    with pytest.raises(ValueError, match="not guided"):
        RequestGroup.from_dict(g)
    g = arc_group()
    g["requests"][0]["configurations"][0]["instrument_configs"][0]["extra_params"] = {"light": True}
    with pytest.raises(ValueError, match="no sky light"):
        RequestGroup.from_dict(g)


def test_lamps_in_extra_params_are_still_refused_with_the_reason():
    g = group("x", ics=[{"exposure_time": 1.0, "extra_params": {"thar": True}}])
    with pytest.raises(ValueError, match=r"optical_elements\.lamp.*iodine only"):
        RequestGroup.from_dict(g)


async def test_dispatch_sends_a_lamp_frame_only_where_the_lamp_is():
    from datetime import timedelta

    from tests.core.test_dispatch import NIGHT, Clock, make

    limits = {"t1": TelescopeLimits(), "t2": TelescopeLimits(lamps=["ThAr1", "Incand"])}
    d = make([arc_group()], clock=Clock(NIGHT), limits=limits)
    cands = {c.request.id: c for c in d.evaluate("t1")}
    assert any("has no lamp 'ThAr1'" in r for r in cands["arc-r"].reasons)
    # No sky light: the Sun limit does not apply (daytime is fine).
    noon = NIGHT + timedelta(hours=12)
    assert {c.request.id: c for c in d.evaluate("t2", now=noon)}["arc-r"].ok
    obs = await d.next_observation("t2")
    assert isinstance(obs, Observation), obs
    assert obs.kind == "arc" and obs.lamp == "ThAr1" and not obs.light
    assert obs.count == 2 and obs.ra_hours is None and not obs.guide
    assert TelescopeLimits.from_dict({"lamps": ["ThAr1"]}, "t").lamps == ["ThAr1"]


async def test_the_request_api_refuses_lamps_where_there_are_none(make_api):  # noqa: F811
    api = await make_api()
    r = await api.post("tom", "/requestgroups/", arc_group())
    assert r.status_code == 400, r.text
    assert NO_LAMPS in r.text  # MINERVA's reason, as before
    enc = api.app.enclosures["aqawan1"]
    enc.planner.limits["t2"].lamps = ["Incand"]
    r = await api.post("tom", "/requestgroups/", arc_group("arc2"))
    assert r.status_code == 400 and "no lamp 'ThAr1'" in r.text and NO_LAMPS not in r.text
    r = await api.post("tom", "/requestgroups/", arc_group("arc3", lamp="Incand"))
    assert r.status_code == 202, r.text


# -- the sequencer ---------------------------------------------------------------------


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


def lamp_rig(make_rig, s: PulizziServer, plan, *, warmup: float = 0.2, calstage=None, **kw):
    rig = make_rig(plan=plan, **kw)
    unit = rig.sequencer.units["t1"]
    unit.lamps = alpaca_owner("t1.lamps", "switch", s.address, rig.timeline, default_timeout=5)
    rig.owners["t1.lamps"] = unit.lamps
    unit.lamp_setup = LampSetup.from_dict(
        {
            "switch": s.address,
            "frame_overhead": 1.0,
            "lamps": {
                "ThAr1": {"warmup": warmup, "calstage": calstage},
                "Incand": {"warmup": 0},
            },
        }
    )
    return rig


def arc(count: int = 2, exptime: float = 0.1, **kw) -> dict:
    d = {"name": "thar", "kind": "arc", "lamp": "ThAr1", "light": False}
    return {**d, "exptime": exptime, "count": count, **kw}


async def test_lamp_frames_stage_lamp_warmup_frames_off(serve, make_rig):
    s = serve()
    plan = StaticPlan([arc()], ["t1"])
    rig = lamp_rig(make_rig, s, plan, calstage="science", calstages=True)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    assert plan.history == [("t1", "thar", "complete", 2)]
    t = {w: _events(rig, w)[0].t for w in ("calstage_selected", "lamp_on", "exposure_started")}
    assert t["calstage_selected"] < t["lamp_on"] < t["exposure_started"]
    assert t["exposure_started"] - t["lamp_on"] >= 0.2  # the warm-up
    assert _events(rig, "lamp_off")[0].t > _events(rig, "exposure_complete")[-1].t
    assert s.sim.lit() == [3, 5] and switching(s.sim)[:2] == ["N02", "F02"]
    assert rig.cams[0].exposures_started == 2
    assert rig.dome.opens == 0  # no sky light: no lease, the dome stays shut


async def test_the_lamp_goes_off_when_the_camera_fails(serve, make_rig):
    s = serve()
    plan = StaticPlan([arc(count=3, exptime=0.3)], ["t1"])
    rig = lamp_rig(make_rig, s, plan, seq=dict(max_consecutive_errors=1))
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 5.0)
    rig.cams[0].fail("camera gone")
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    assert plan.history[0][2] == "failed"
    assert s.sim.lit() == [3, 5]
    assert _events(rig, "lamp_off")


async def test_the_lamp_goes_off_when_the_sequencer_is_cancelled(serve, make_rig):
    s = serve()
    plan = StaticPlan([arc(count=5, exptime=2.0)], ["t1"])
    rig = lamp_rig(make_rig, s, plan)
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 5.0)
    assert 2 in s.sim.lit()
    rig.tasks[-1].cancel()
    await asyncio.wait([rig.tasks[-1]], timeout=15)
    assert s.sim.lit() == [3, 5]
    assert plan.history[0][2] == "interrupted"


async def test_a_lamp_switched_off_under_the_frames_fails_the_observation(serve, make_rig):
    """Switched off by hand, or by the switch's max-on limit, mid-frames."""
    s = serve()
    plan = StaticPlan([arc(count=3, exptime=0.5)], ["t1"])
    rig = lamp_rig(make_rig, s, plan, warmup=0.0, seq=dict(max_consecutive_errors=1))
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 5.0)
    other = Client(s, client_id=99).connect()
    await asyncio.to_thread(other.put, "setswitch", Id=0, State=False)
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    errors = _events(rig, "telescope_error")
    assert errors and "went off" in errors[0].detail["error"]
    assert plan.history[0][2] == "failed" and s.sim.lit() == [3, 5]


async def test_frames_that_cannot_fit_in_max_on_are_refused_before_any_lamp(serve, make_rig):
    s = serve(outlets=[{**LAMPS[0], "max_on": 5.0}, LAMPS[1]])
    plan = StaticPlan([arc(count=10, exptime=1.0)], ["t1"])
    rig = lamp_rig(make_rig, s, plan)
    await rig.start()
    await wait_until(lambda: plan.history, 10.0)
    assert plan.history[0][2] == "failed"  # not the telescope's fault: no backoff
    failed = _events(rig, "observation_failed")
    assert failed and "max_on" in failed[0].detail["error"]
    assert switching(s.sim) == [] and rig.cams[0].exposures_started == 0


async def test_another_lamp_is_switched_off_first(serve, make_rig):
    s = serve()
    Client(s).connect().put("setswitch", Id=1, State=True)  # Incand left on
    plan = StaticPlan([arc(count=1)], ["t1"])
    rig = lamp_rig(make_rig, s, plan, warmup=0.0)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    assert plan.history == [("t1", "thar", "complete", 1)]
    assert switching(s.sim) == ["N04", "F04", "N02", "F02"]


@pytest.mark.parametrize(("lamps", "error"), [(False, "has no lamps"), (True, "no lamp 'ThAr9'")])
async def test_a_telescope_without_the_lamp_fails_the_observation(serve, make_rig, lamps, error):
    plan = StaticPlan([arc(lamp="ThAr9")], ["t1"])
    if lamps:
        rig = lamp_rig(make_rig, serve(), plan, seq=dict(max_consecutive_errors=1))
    else:
        rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1))
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    errors = _events(rig, "telescope_error")
    assert errors and error in errors[0].detail["error"]
    assert rig.cams[0].exposures_started == 0


async def test_every_cleanup_switches_the_lamps_off(serve, make_rig):
    """A lamp switched on by hand is off after the sequencer's next cleanup
    (here: the end of a dark-frame generation)."""
    s = serve()
    Client(s).connect().put("setswitch", Id=0, State=True)
    plan = StaticPlan([{"name": "dark", "light": False, "exptime": 0.1, "count": 1}], ["t1"])
    rig = lamp_rig(make_rig, s, plan)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    assert s.sim.lit() == [3, 5]
    assert any(e.detail.get("lamps") == ["ThAr1"] for e in _events(rig, "lamps_off"))


def test_observation_lamp_checks():
    with pytest.raises(ValueError, match="names its lamp"):
        Observation("x", kind="arc", light=False)
    with pytest.raises(ValueError, match="no sky light"):
        Observation("x", kind="lamp_flat", lamp="Incand")
    with pytest.raises(ValueError, match="'lamp' is for"):
        Observation("x", lamp="Incand")


def test_lamp_setup_validation():
    with pytest.raises(ValueError, match="switch"):
        LampSetup.from_dict({"lamps": {"a": {}}})
    with pytest.raises(ValueError, match="at least one"):
        LampSetup.from_dict({"switch": "h:1/0", "lamps": {}})
    with pytest.raises(ValueError, match="warmup"):
        LampSetup.from_dict({"switch": "h:1/0", "lamps": {"a": {"warmup": -1}}})
    with pytest.raises(ValueError, match="unknown keys"):
        LampSetup.from_dict({"switch": "h:1/0", "lamps": {"a": {"max_on": 5}}})
