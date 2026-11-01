"""The TRES front end's calibration stage: the PI GCS client, its simulator,
the Alpaca FilterWheel/Focuser server, the legacy Redis shim (against an
in-process fake Redis only), and the sequencer selecting a position.

Fast tier: everything runs in this process against the simulated
controller (``port.type`` "sim"); no serial port is opened and no real
Redis is contacted.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import (
    AlpacaDevice,
    DriverException,
    InvalidOperationException,
    InvalidValueException,
    NotConnectedException,
)
from robofast_core.config import device_address, telescopes
from robofast_core.devices.calstage_redis import LegacyKeys, LegacyRedisConfig
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.devices.pi_gcs import MOVING, GCSAxis, GCSError, GCSTimeout
from robofast_core.devices.tres_frontend_server import TRESFrontEndConfig, TRESFrontEndServer
from robofast_core.devices.tres_frontend_sim import C663Sim
from robofast_core.dispatch import TelescopeLimits
from robofast_core.plan import StaticPlan
from robofast_core.request_model import InstrumentConfig
from robofast_core.resp import RedisError
from robofast_core.sequencer_service import build_units
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT
from robofast_core.testing.redis_feed import RedisFeed, RedisFeedState
from robofast_core.timeline import Timeline

SITE = ROOT / "sites" / "flwo60"
POSITIONS = {"out": 0.0, "sky": 10.0, "science": 20.0, "in": None}
FAST = dict(poll_interval=0.02, status_max_age=0.02, request_timeout=3.0, start_grace=0.2)


def moving_commands(sim: C663Sim) -> list[str]:
    return [c for c in sim.commands if c.split(" ", 1)[0] in MOVING]


# -- the GCS client against the simulator ---------------------------------------------


async def test_gcs_queries_and_moves():
    sim = C663Sim(velocity=100.0)
    axis = GCSAxis(sim, timeout=0.5)
    assert "C-663" in await axis.identify()
    assert await axis.limits() == (0.0, 26.0)
    assert await axis.referenced() and await axis.servo() and not await axis.moving()
    await axis.move(12.5)
    assert sim.commands[-2:] == ["MOV 1 12.5", "ERR?"]
    await wait_until(lambda: not sim.moving, 2.0)
    assert await axis.position() == pytest.approx(12.5)
    assert await axis.on_target()


async def test_gcs_errors_carry_the_controller_code():
    sim = C663Sim(referenced=False)
    axis = GCSAxis(sim, timeout=0.5)
    with pytest.raises(GCSError, match="not referenced") as e:
        await axis.move(5.0)
    assert e.value.code == 5
    await axis.reference_negative()
    await wait_until(lambda: sim.referenced, 5.0)
    with pytest.raises(GCSError) as e:
        await axis.move(30.0)
    assert e.value.code == 7  # out of limits
    assert await axis.error() == 0  # read and cleared by the command check


async def test_gcs_halt_clears_the_stop_error():
    sim = C663Sim(velocity=1.0)
    axis = GCSAxis(sim, timeout=0.5)
    await axis.move(20.0)
    assert await axis.moving()
    await axis.halt()
    assert not await axis.moving() and sim.err == 0
    assert 0.0 < await axis.position() < 20.0


async def test_gcs_refuses_commands_outside_its_allowlist():
    sim = C663Sim()
    axis = GCSAxis(sim, timeout=0.5)
    for line in ("RON 1 0", "WPA 100", "DFH 1", "POS 1 3", "SPA 1 0x1 1"):
        with pytest.raises(GCSError, match="allowlist"):
            await axis.query(line)
    assert sim.commands == []


async def test_gcs_timeout_is_bounded():
    sim = C663Sim()
    sim.silent = True
    axis = GCSAxis(sim, timeout=0.2)
    t = time.monotonic()
    with pytest.raises(GCSTimeout):
        await axis.position()
    assert time.monotonic() - t < 1.0


async def test_gcs_multiline_answers():
    class Two(C663Sim):
        def handle(self, line):
            self.commands.append(line)
            self._answer("1=3.000000", "2=4.000000")

    axis = GCSAxis(Two(), axis="2", timeout=0.5)
    assert await axis.position() == 4.0


# -- the server ---------------------------------------------------------------------------


@pytest.fixture
def serve():
    servers: list[TRESFrontEndServer] = []

    def build(sim: dict | None = None, **kw) -> TRESFrontEndServer:
        d = {
            "listen": "127.0.0.1:0",
            "port": {"type": "sim", "sim": {"velocity": 50.0, **(sim or {})}},
            "positions": POSITIONS,
            **FAST,
            **kw,
        }
        server = TRESFrontEndServer(TRESFrontEndConfig.from_dict(d)).start()
        servers.append(server)
        return server

    yield build
    for s in servers:
        s.stop()


def wheel(server: TRESFrontEndServer) -> AlpacaDevice:
    return AlpacaDevice(server.address, "filterwheel")


def focuser(server: TRESFrontEndServer) -> AlpacaDevice:
    return AlpacaDevice(server.address, "focuser")


async def connected(dev: AlpacaDevice) -> AlpacaDevice:
    await dev.put("Connected", timeout=3, Connected=True)
    return dev


async def settle(dev: AlpacaDevice, member: str = "Position", busy=-1, timeout=5.0):
    async with asyncio.timeout(timeout):
        while (value := await dev.get(member, timeout=3)) == busy:
            await asyncio.sleep(0.02)
    return value


async def test_starting_connecting_reading_and_stopping_never_move(serve):
    s = serve()
    sim = s.sim
    fw, fo = await connected(wheel(s)), await connected(focuser(s))
    assert await fw.get("Names", timeout=3) == list(POSITIONS)
    assert await fw.get("FocusOffsets", timeout=3) == [0, 0, 0, 0]
    assert await fw.get("Position", timeout=3) == 0
    await fw.put("Action", timeout=3, Action="Status", Parameters="")
    assert await fo.get("Position", timeout=3) == 0
    assert await fo.get("MaxStep", timeout=3) == 26000
    await fw.get("DeviceState", timeout=3)
    s.stop()
    assert moving_commands(sim) == [] and not any(c.startswith("SVO ") for c in sim.commands)


async def test_named_positions_through_the_filterwheel(serve):
    s = serve()
    fw = await connected(wheel(s))
    await fw.put("Position", timeout=3, Position=2)
    assert await fw.get("Position", timeout=3) == -1  # moving
    assert await settle(fw) == 2
    assert s.sim.position == pytest.approx(20.0)
    await fw.put("Position", timeout=3, Position=1)
    assert await settle(fw) == 1
    status = json.loads(await fw.put("Action", timeout=3, Action="Status", Parameters=""))
    assert status["name"] == "sky" and status["position_mm"] == 10.0 and not status["moving"]
    assert moving_commands(s.sim) == ["MOV 1 20", "MOV 1 10"]


async def test_unconfigured_and_invalid_positions_are_refused(serve):
    s = serve()
    fw = await connected(wheel(s))
    with pytest.raises(InvalidOperationException, match="not configured"):
        await fw.put("Position", timeout=3, Position=3)  # "in": null
    for bad in (-1, 4):
        with pytest.raises(InvalidValueException):
            await fw.put("Position", timeout=3, Position=bad)
    assert moving_commands(s.sim) == []


async def test_not_connected(serve):
    s = serve()
    fw = wheel(s)
    for member in ("Names", "Position"):
        with pytest.raises(NotConnectedException):
            await fw.get(member, timeout=3)


async def test_unreferenced_stage_refuses_moves_until_referenced(serve):
    s = serve(sim={"referenced": False, "servo": False, "position": 7.0})
    fw = await connected(wheel(s))
    with pytest.raises(InvalidOperationException, match="not referenced"):
        await fw.get("Position", timeout=3)
    with pytest.raises(InvalidOperationException, match="not referenced"):
        await fw.put("Position", timeout=3, Position=1)
    assert moving_commands(s.sim) == []
    reply = await fw.put("Action", timeout=3, Action="Reference", Parameters="")
    assert "negative limit" in reply
    assert s.sim.commands[-4:] == ["SVO? 1", "SVO 1 1", "ERR?", "FNL 1"] or "FNL 1" in s.sim.commands
    assert await settle(fw) == 0  # the negative limit is "out" here
    await fw.put("Position", timeout=3, Position=1)
    assert await settle(fw) == 1


async def test_focuser_moves_in_microns_and_the_wheel_reports_off_position(serve):
    s = serve()
    fw, fo = await connected(wheel(s)), await connected(focuser(s))
    await fo.put("Move", timeout=3, Position=12500)
    assert await settle(fo, "IsMoving", busy=True) is False
    assert await fo.get("Position", timeout=3) == 12500
    with pytest.raises(InvalidOperationException, match=r"12\.500 mm, not at a named"):
        await fw.get("Position", timeout=3)
    with pytest.raises(InvalidValueException):
        await fo.put("Move", timeout=3, Position=26001)
    await fw.put("Position", timeout=3, Position=2)  # a named move from anywhere
    assert await settle(fw) == 2


async def test_focuser_halt_stops_the_stage(serve):
    s = serve(sim={"velocity": 2.0})
    fo = await connected(focuser(s))
    await fo.put("Move", timeout=3, Position=20000)
    await asyncio.sleep(0.2)
    await fo.put("Halt", timeout=3)
    assert await fo.get("IsMoving", timeout=3) is False
    assert 0 < await fo.get("Position", timeout=3) < 20000


async def test_a_move_that_stops_short_is_reported(serve):
    s = serve()
    s.sim.stop_short = 0.5
    fw = await connected(wheel(s))
    await fw.put("Position", timeout=3, Position=1)
    with pytest.raises(InvalidOperationException, match=r"9\.500 mm.*stopped at"):
        await settle(fw)
    status = json.loads(await fw.put("Action", timeout=3, Action="Status", Parameters=""))
    assert "stopped at 9.500 mm, target 10.000 mm" in status["fault"]
    s.sim.stop_short = 0.0
    await fw.put("Position", timeout=3, Position=1)  # does not block the next move
    assert await settle(fw) == 1
    status = json.loads(await fw.put("Action", timeout=3, Action="Status", Parameters=""))
    assert status["fault"] is None


async def test_a_jammed_stage_is_halted_after_move_timeout(serve):
    s = serve(move_timeout=0.4)
    s.sim.stuck = True
    fw = await connected(wheel(s))
    await fw.put("Position", timeout=3, Position=2)
    await wait_until(lambda: "HLT 1" in s.sim.commands, 3.0)
    status = json.loads(await fw.put("Action", timeout=3, Action="Status", Parameters=""))
    assert "did not finish within 0.4 s" in status["fault"] and not status["moving"]


async def test_a_dead_controller_is_a_bounded_error(serve):
    s = serve()
    fw = await connected(wheel(s))
    await asyncio.sleep(0.1)  # past status_max_age: the next read goes to the controller
    s.sim.silent = True
    t = time.monotonic()
    with pytest.raises(DriverException):  # 0x502 maps to a driver error
        await fw.get("Position", timeout=5)
    assert time.monotonic() - t < 4.0


def test_hardware_needs_the_config_and_the_flag():
    cfg = TRESFrontEndConfig.from_dict(json.loads((SITE / "tres_frontend.json").read_text()))
    assert cfg.port.type == "serial" and not cfg.allow_hardware
    for hardware in (False, True):
        with pytest.raises(HardwareNotAllowed, match="allow_hardware"):
            TRESFrontEndServer(cfg, hardware=hardware)
    cfg.allow_hardware = True
    with pytest.raises(HardwareNotAllowed):
        TRESFrontEndServer(cfg, hardware=False)


def test_config_validation():
    with pytest.raises(ValueError, match="unknown config keys"):
        TRESFrontEndConfig.from_dict({"bogus": 1})
    with pytest.raises(ValueError, match="closer than"):
        TRESFrontEndConfig.from_dict({"positions": {"a": 1.0, "b": 1.01}})
    with pytest.raises(ValueError, match="not a number"):
        TRESFrontEndConfig.from_dict({"positions": {"a": "x"}})
    with pytest.raises(ValueError, match="legacy_redis"):
        TRESFrontEndConfig.from_dict({"legacy_redis": {"bogus": 1}})


def test_shipped_site_configs():
    cfg = TRESFrontEndConfig.from_dict(json.loads((SITE / "tres_frontend.json").read_text()))
    assert cfg.legacy_redis is None and cfg.listen.startswith("127.0.0.1:")
    assert list(cfg.positions)[:3] == ["out", "sky", "science"]
    site = json.loads((SITE / "flwo60.json").read_text())
    (entry,) = telescopes(site).values()
    assert device_address(entry["calstage"]) == (cfg.listen, 0)
    units = build_units(site, Timeline(), 5.0)
    assert units[0].calstage is not None and units[0].filterwheel is None


def test_the_command_line_refuses_hardware_and_runs_the_simulator():
    cfg = str(SITE / "tres_frontend.json")
    mod = "robofast_core.devices.tres_frontend_server"
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
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert p2.stdout is not None
        hello = json.loads(p2.stdout.readline())
        assert hello["port"] == "simulator" and hello["url"].startswith("http://127.0.0.1:")
    finally:
        p2.terminate()
        p2.wait(10)


# -- the legacy Redis shim (fake, in-process Redis only) -------------------------------------


@pytest.fixture
def fake_redis():
    feed = RedisFeed(RedisFeedState({0: {"calstage": "science"}}))
    feed.feed.writable = True
    with feed:
        yield feed


def shim(feed: RedisFeed, **kw) -> dict:
    host, port = feed.server_address[:2]
    return {"host": host, "port": port, "poll_interval": 0.05, "timeout": 0.5, **kw}


def keys(feed: RedisFeed) -> dict[str, str]:
    return feed.feed.snapshot(0)


async def test_shim_mirrors_state_and_ignores_commands_by_default(serve, fake_redis):
    s = serve(legacy_redis=shim(fake_redis))
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "out", 3.0)
    # The command key follows the position reached.
    await wait_until(lambda: keys(fake_redis)["calstage"] == "out", 3.0)
    fake_redis.feed.dbs[0]["calstage"] = "sky"  # a legacy request
    await asyncio.sleep(0.3)
    assert moving_commands(s.sim) == []
    fw = await connected(wheel(s))
    await fw.put("Position", timeout=3, Position=2)
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "science", 5.0)
    await wait_until(lambda: keys(fake_redis)["calstage"] == "science", 3.0)
    sent = {tuple(c[:2]) for c in fake_redis.feed.commands}
    assert {c[0] for c in sent} <= {"SET"}  # mirror only: no reads
    assert {c[1] for c in sent} <= {"calstage", "calstage_status"}


async def test_shim_accepts_legacy_commands_only_on_change(serve, fake_redis):
    s = serve(legacy_redis=shim(fake_redis, accept_commands=True))
    # "science" was there before the shim started: a baseline, never acted on.
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "out", 3.0)
    await wait_until(lambda: keys(fake_redis)["calstage"] == "out", 3.0)
    await asyncio.sleep(0.2)
    assert moving_commands(s.sim) == []
    fake_redis.feed.dbs[0]["calstage"] = "sky"
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "sky", 5.0)
    fake_redis.feed.dbs[0]["calstage"] = "12.5"  # calstage position 12.5
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "12.500", 5.0)
    fake_redis.feed.dbs[0]["calstage"] = "Requested position out of range"
    fake_redis.feed.dbs[0]["calstage"] = "in"  # not configured: refused, logged
    await asyncio.sleep(0.3)
    assert moving_commands(s.sim) == ["MOV 1 10", "MOV 1 12.5"]
    assert s.mirror is not None and s.mirror.accepted == ["sky", "12.5"]
    assert {c[0] for c in fake_redis.feed.commands} <= {"GET", "SET"}


async def test_shim_survives_a_dead_redis(serve, fake_redis):
    fake_redis.feed.drop = True
    s = serve(legacy_redis=shim(fake_redis))
    fw = await connected(wheel(s))
    await fw.put("Position", timeout=3, Position=1)
    assert await settle(fw) == 1
    fake_redis.feed.drop = False
    await wait_until(lambda: keys(fake_redis).get("calstage_status") == "sky", 3.0)


def test_shim_never_reaches_a_non_loopback_redis_without_permission():
    d = {
        "listen": "127.0.0.1:0",
        "port": {"type": "sim"},
        "legacy_redis": {"host": "192.0.2.10"},  # TEST-NET: never contacted
    }
    with pytest.raises(HardwareNotAllowed, match="legacy_redis"):
        TRESFrontEndServer(TRESFrontEndConfig.from_dict(d), hardware=True)


async def test_shim_client_touches_only_its_two_keys():
    k = LegacyKeys("127.0.0.1", 9, 0, {"calstage", "calstage_status"}, timeout=0.1)
    for parts in ((b"GET", "time"), (b"SET", "state", "x"), (b"DEL", "calstage"), (b"FLUSHALL",)):
        with pytest.raises(RedisError, match="refusing"):
            await k._call(*parts)
    with pytest.raises(ValueError, match="differ"):
        LegacyRedisConfig.from_dict({"command_key": "a", "status_key": "a"})


# -- requests and the sequencer -------------------------------------------------------------


def test_request_calstage_round_trip():
    d = {"exposure_time": 30, "optical_elements": {"calstage": "sky"}}
    ic = InstrumentConfig.from_dict(d, "ic")
    assert ic.calstage == "sky" and ic.filter is None
    assert ic.to_dict()["optical_elements"] == {"calstage": "sky"}
    assert TelescopeLimits.from_dict({"calstage": ["out", "sky"]}, "t").calstage == ["out", "sky"]


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


async def test_sequencer_selects_the_calstage_before_exposing(make_rig):
    plan = StaticPlan([{"name": "sky", "calstage": "sky", "exptime": 0.1, "count": 2}], ["t1"])
    rig = make_rig(plan=plan, calstages=True)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    assert plan.history == [("t1", "sky", "complete", 2)]
    assert rig.calstages[0].Position == 1 and rig.calstages[0].moves == 1
    assert _events(rig, "calstage_selected")[0].t < _events(rig, "exposure_started")[0].t


@pytest.mark.parametrize(
    ("kw", "error"),
    [({}, "has no calibration stage"), ({"calstages": True}, "no calstage position 'in'")],
)
async def test_an_unknown_calstage_fails_the_observation(make_rig, kw, error):
    plan = StaticPlan([{"name": "x", "calstage": "in", "exptime": 0.1, "count": 1}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1), **kw)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    errors = _events(rig, "telescope_error")
    assert errors and error in errors[0].detail["error"]
    assert rig.cams[0].exposures_started == 0
    assert plan.history[0][2] == "failed"


async def test_sequencer_drives_the_real_server(make_rig, serve):
    """The sequencer's calstage owner against the Alpaca server and its
    simulated controller, starting off any named position."""
    s = serve()
    s.sim.handle("MOV 1 5")
    await wait_until(lambda: not s.sim.moving, 2.0)
    plan = StaticPlan(
        [{"name": "cal", "calstage": "science", "light": False, "exptime": 0.1, "count": 1}],
        ["t1"],
    )
    rig = make_rig(plan=plan)
    unit = rig.sequencer.units["t1"]
    unit.calstage = alpaca_owner("t1.calstage", "filterwheel", s.address, rig.timeline)
    rig.owners["t1.calstage"] = unit.calstage
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    assert plan.history == [("t1", "cal", "complete", 1)]
    assert s.sim.position == pytest.approx(20.0)
    assert moving_commands(s.sim)[-1] == "MOV 1 20"
