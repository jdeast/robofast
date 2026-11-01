"""The MEarth South roof: its parallel-port protocol, its simulator, and the
Alpaca Dome server end to end, alone and under the safety supervisor.

Fast tier: everything runs in this process against the simulator (the
server's ``port.type`` "sim"); no real port is ever opened.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

import pytest

from robofast_core import DeviceOwner, SafetyConfig, SafetySupervisor, SequencerConfig, Timeline
from robofast_core import config as enclosure_config
from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import (
    ActionNotImplementedException,
    InvalidOperationException,
)
from robofast_core.devices.aqawan_server import CLOSED, CLOSING, ERROR, OPEN, OPENING
from robofast_core.devices.mearth_roof_port import (
    CMD_CLOSE,
    CMD_OPEN,
    CMD_STOP,
    CONTROL_STROBE,
    PPCLAIM,
    PPFCONTROL,
    PPRSTATUS,
    PPWDATA,
    HardwareNotAllowed,
    PPDevPort,
    RoofInterface,
    StrobeTiming,
    decode_limits,
)
from robofast_core.devices.mearth_roof_server import MEarthRoofConfig, MEarthRoofServer
from robofast_core.devices.mearth_roof_sim import MEarthSouthRoofSim
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.sequencer_service import build
from robofast_core.supervisor_service import build as build_supervisor
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import FakeSafetyMonitor, FakeWeather
from robofast_core.testing.processes import ROOT
from robofast_core.weather_bridge import build_sources, parse_config
from robofast_core.weather_bridge import load_config as load_weather

from .conftest import FAST_SAFETY

SITE = ROOT / "sites" / "mearth_south"
TRAVEL = 0.6
FAST = dict(
    poll_interval=0.02,
    status_max_age=0.02,
    departure_timeout=0.3,
    open_timeout=2.0,
    close_timeout=2.0,
    request_timeout=3.0,
)


# -- the protocol ---------------------------------------------------------------------


def test_ppdev_ioctl_numbers_match_linux():
    # linux/ppdev.h, as the legacy code used them through ppdev.ph
    assert PPRSTATUS == 0x80017081
    assert PPWDATA == 0x40017086
    assert PPFCONTROL == 0x4002708E
    assert PPCLAIM == 0x708B


@pytest.mark.parametrize(
    ("status", "open_", "closed"),
    [(0x80 | 0x20, True, False), (0x00, False, True), (0x20, False, False), (0x80, True, True)],
)
def test_limits_decode_like_legacy(status, open_, closed):
    lim = decode_limits(status | 0x18)
    assert (lim.open, lim.closed) == (open_, closed)
    assert lim.both == (open_ and closed) and lim.neither == (not open_ and not closed)


class RecordingPort:
    def __init__(self) -> None:
        self.ops: list[tuple[str, int, int]] = []

    def write_data(self, value: int) -> None:
        self.ops.append(("data", value, 0))

    def frob_control(self, mask: int, value: int) -> None:
        self.ops.append(("control", mask, value))

    def read_status(self) -> int:
        return 0x38

    def close(self) -> None:
        pass


async def test_send_strobes_one_byte_and_only_the_three_commands():
    port = RecordingPort()
    iface = RoofInterface(port, timing=StrobeTiming(0, 0, 0))
    try:
        await iface.send(CMD_CLOSE)
        assert port.ops == [
            ("data", ord("c"), 0),
            ("control", CONTROL_STROBE, CONTROL_STROBE),  # line low
            ("control", CONTROL_STROBE, 0),  # line high: latched
        ]
        assert iface.last_command == CMD_CLOSE
        with pytest.raises(ValueError):
            await iface.send(ord("x"))
        assert len(port.ops) == 3
    finally:
        iface.close()


def test_ppdev_port_needs_permission():
    with pytest.raises(HardwareNotAllowed):
        PPDevPort("/dev/parport0")


# -- the simulator --------------------------------------------------------------------


def _latch(sim: MEarthSouthRoofSim, byte: int) -> None:
    sim.write_data(byte)
    sim.frob_control(CONTROL_STROBE, CONTROL_STROBE)
    sim.frob_control(CONTROL_STROBE, 0)


def test_sim_latches_on_the_strobe_and_stops_at_the_limit():
    sim = MEarthSouthRoofSim(travel_time=0.2)
    sim.write_data(CMD_OPEN)  # data alone does nothing
    time.sleep(0.05)
    assert sim.closed() and sim.latched == CMD_STOP
    _latch(sim, CMD_OPEN)
    assert sim.latched == CMD_OPEN
    time.sleep(0.3)
    lim = decode_limits(sim.read_status())
    assert sim.open() and lim.open and not lim.closed and not sim.moving()
    _latch(sim, ord("x"))  # unknown byte: stop (assumed)
    assert sim.latched == CMD_STOP and sim.commands() == [CMD_OPEN, ord("x")]


def test_sim_faults():
    sim = MEarthSouthRoofSim(travel_time=0.2)
    sim.set_manual(True)
    _latch(sim, CMD_OPEN)
    time.sleep(0.25)
    assert sim.closed()  # local control: the box latches, the drive ignores it
    sim.manual_drive(0.5)
    assert decode_limits(sim.read_status()).neither
    sim.set_manual(False)
    sim.set_sense_power(False)
    _latch(sim, CMD_CLOSE)
    time.sleep(0.25)
    assert sim.closed() and decode_limits(sim.read_status()).neither  # moved, unseen
    sim.set_sense_power(True)
    sim.stick_limit("open")
    assert decode_limits(sim.read_status()).both


# -- the server -------------------------------------------------------------------------


@dataclass
class Rig:
    server: MEarthRoofServer
    owners: list[DeviceOwner] = field(default_factory=list)

    @property
    def sim(self) -> MEarthSouthRoofSim:
        assert self.server.sim is not None
        return self.server.sim

    def owner(self, name: str = "dome") -> DeviceOwner:
        o = alpaca_owner(
            name,
            "dome",
            self.server.address,
            Timeline(),
            default_timeout=4.0,
            privileged_members=ENCLOSURE_MEMBERS,
        )
        self.owners.append(o)
        return o

    def dome(self):
        return self.owner().grant("test", privileged=True)


@pytest.fixture
async def make_rig():
    rigs: list[Rig] = []

    def make(sim: dict | None = None, **config) -> Rig:
        cfg = {
            "name": "test",
            "listen": "127.0.0.1:0",
            "port": {"type": "sim", "sim": {"travel_time": TRAVEL, **(sim or {})}},
            **FAST,
            **config,
        }
        rig = Rig(MEarthRoofServer(MEarthRoofConfig.from_dict(cfg)).start())
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        for o in rig.owners:
            o.close()
        await asyncio.sleep(0)
        rig.server.stop()


async def until_status(dome, state: int, timeout: float) -> float:
    t0 = time.monotonic()
    while (s := await dome.get("ShutterStatus")) != state:
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"ShutterStatus {s}, wanted {state}")
        await asyncio.sleep(0.02)
    return time.monotonic() - t0


async def faults(dome) -> dict:
    return json.loads(await dome.call("Action", "Faults", ""))


async def test_open_and_close_stop_at_the_limits(make_rig):
    rig = make_rig()
    dome = rig.dome()
    assert await dome.get("ShutterStatus") == CLOSED
    assert rig.sim.commands() == []  # starting never writes to the port
    await dome.call("OpenShutter")
    assert await dome.get("ShutterStatus") == OPENING and await dome.get("Slewing")
    await until_status(dome, OPEN, 3)
    assert rig.sim.open()
    await wait_until(lambda: rig.sim.latched == CMD_STOP, 1, what="stop after open")
    assert rig.sim.commands() == [CMD_OPEN, CMD_STOP]
    await dome.call("OpenShutter")  # already open: nothing sent
    assert rig.sim.commands() == [CMD_OPEN, CMD_STOP]
    await dome.call("CloseShutter")
    assert await dome.get("ShutterStatus") == CLOSING
    await until_status(dome, CLOSED, 3)
    await wait_until(lambda: rig.sim.latched == CMD_STOP, 1, what="stop after close")
    f = await faults(dome)
    assert f["fault"] is False and f["shutters"] == ["CLOSED"] and f["estop"] is False
    status = json.loads(await dome.call("Action", "Status", ""))
    assert status["limits"]["closed"] and status["last_command"] == "stop"
    with pytest.raises(ActionNotImplementedException):
        await dome.call("Action", "Heartbeat", "")  # no watchdog on this roof


async def test_local_control_is_caught_by_the_departure_timeout(make_rig):
    rig = make_rig()
    rig.sim.set_manual(True)
    dome = rig.dome()
    await dome.call("OpenShutter")
    await wait_until(lambda: rig.server.roof.stuck is not None, 2, what="stuck")
    assert "did not leave its starting limit" in rig.server.roof.stuck
    # The stop is queued with the fault and latched on the port thread.
    await wait_until(lambda: rig.sim.latched == CMD_STOP, 1, what="stop latched")
    assert rig.sim.closed()
    # Closed and faulted: the position reads closed; the fault is separate.
    assert await dome.get("ShutterStatus") == CLOSED
    f = await faults(dome)
    assert f["fault"] and "starting limit" in f["faults"] and f["shutters"] == ["CLOSED"]
    with pytest.raises(InvalidOperationException, match="fault latched"):
        await dome.call("OpenShutter")
    rig.sim.set_manual(False)
    reply = await dome.call("Action", "ClearFaults", "jde: switched back to computer")
    assert reply.startswith("cleared")
    assert (await faults(dome))["fault"] is False
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 3)


@pytest.mark.parametrize("fault", ["jam", "vfd"])
async def test_a_stall_mid_travel_times_out_as_stuck(make_rig, fault):
    rig = make_rig(open_timeout=1.0, close_timeout=1.0)
    dome = rig.dome()
    await dome.call("OpenShutter")
    await wait_until(lambda: 0.2 < rig.sim.position < 0.9, 2, what="part-way")
    rig.sim.set_jammed(True) if fault == "jam" else rig.sim.trip_vfd()
    await wait_until(lambda: rig.server.roof.stuck is not None, 3, what="stuck")
    assert "limit not reached" in rig.server.roof.stuck
    assert await dome.get("ShutterStatus") == ERROR
    f = await faults(dome)
    assert f["fault"] and f["shutters"] == ["UNKNOWN"]
    # A close is always attempted; with the drive tripped it cannot finish.
    await dome.call("CloseShutter")
    assert rig.sim.latched == CMD_CLOSE
    with pytest.raises(InvalidOperationException, match="not closed"):
        await dome.call("Action", "ClearFaults", "too early")
    rig.sim.set_jammed(False) if fault == "jam" else rig.sim.reset_vfd()  # on site
    await until_status(dome, CLOSED, 3)
    assert (await faults(dome))["fault"]  # still latched until an operator clears
    await dome.call("Action", "ClearFaults", "jde: drive reset on site")
    assert (await faults(dome))["fault"] is False


async def test_unknown_or_impossible_positions_are_faults(make_rig):
    rig = make_rig()
    dome = rig.dome()
    rig.sim.manual_drive(0.5)  # moved by hand
    assert await dome.get("ShutterStatus") == ERROR
    f = await faults(dome)
    assert f["fault"] and "position unknown" in f["faults"] and f["shutters"] == ["UNKNOWN"]
    with pytest.raises(InvalidOperationException, match="position unknown"):
        await dome.call("OpenShutter")
    await dome.call("CloseShutter")
    await until_status(dome, CLOSED, 3)
    assert (await faults(dome))["fault"] is False  # not latched: it went away
    rig.sim.stick_limit("open")
    assert await dome.get("ShutterStatus") == ERROR
    assert "both limit" in (await faults(dome))["faults"]
    with pytest.raises(InvalidOperationException, match="both limit"):
        await dome.call("OpenShutter")
    rig.sim.stick_limit(None)
    rig.sim.set_sense_power(False)  # the limit contacts' 15 V supply
    assert await dome.get("ShutterStatus") == ERROR
    with pytest.raises(InvalidOperationException, match="position unknown"):
        await dome.call("OpenShutter")


async def test_abort_latches_stop(make_rig):
    rig = make_rig()
    dome = rig.dome()
    await dome.call("OpenShutter")
    await wait_until(lambda: rig.sim.position > 0.2, 2, what="moving")
    await dome.call("AbortSlew")
    assert rig.sim.latched == CMD_STOP and not rig.sim.moving()
    assert await dome.get("ShutterStatus") == ERROR  # part-way: unknown


async def test_server_stop_sends_nothing_and_the_latch_holds(make_rig):
    rig = make_rig(sim={"travel_time": 2.0}, open_timeout=10.0, close_timeout=10.0)
    dome = rig.dome()
    await dome.call("OpenShutter")
    await wait_until(rig.sim.open, 4, what="open")
    await wait_until(lambda: rig.sim.latched == CMD_STOP, 1, what="stopped")
    await dome.call("CloseShutter")
    sim, n = rig.sim, len(rig.sim.commands())
    for o in rig.owners:
        o.close()
    rig.owners.clear()
    rig.server.stop()
    # The software is gone: the latched close still finishes (the box holds
    # it), but nothing would ever close an open roof (no watchdog).
    assert not sim.closed()  # still on its way when the software went
    await wait_until(sim.closed, 4, what="closed by the latched command")
    assert len(sim.commands()) == n
    rig.server = MEarthRoofServer(  # for the fixture's teardown
        MEarthRoofConfig.from_dict({"listen": "127.0.0.1:0", "port": {"type": "sim"}, **FAST})
    ).start()


# -- configuration and the hardware gate ---------------------------------------------------


def test_config_validation_and_the_hardware_gate(tmp_path):
    with pytest.raises(ValueError, match="unknown config keys"):
        MEarthRoofConfig.from_dict({"allow_hardwar": True})
    with pytest.raises(ValueError, match=r"port\.type"):
        MEarthRoofConfig.from_dict({"port": {"type": "serial"}})
    with pytest.raises(ValueError, match="departure_timeout"):
        MEarthRoofConfig.from_dict({"departure_timeout": 500})
    real = {"listen": "127.0.0.1:0", "port": {"type": "ppdev", "device": "/dev/null"}}
    with pytest.raises(HardwareNotAllowed):
        MEarthRoofServer(MEarthRoofConfig.from_dict(real))
    with pytest.raises(HardwareNotAllowed):  # the config alone is not enough
        MEarthRoofServer(MEarthRoofConfig.from_dict({**real, "allow_hardware": True}))
    with pytest.raises(HardwareNotAllowed):  # nor the flag alone
        MEarthRoofServer(MEarthRoofConfig.from_dict(real), hardware=True)
    path = tmp_path / "roof.json"
    path.write_text(json.dumps(real))
    proc = subprocess.run(
        [sys.executable, "-m", "robofast_core.devices.mearth_roof_server", "--config", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2 and "real hardware" in proc.stderr


def test_shipped_roof_config_is_locked():
    cfg = MEarthRoofConfig.from_dict(json.loads((SITE / "roof.json").read_text()))
    assert not cfg.allow_hardware and cfg.port.type == "ppdev"
    assert cfg.open_timeout == cfg.close_timeout == 180  # legacy South
    with pytest.raises(HardwareNotAllowed):
        MEarthRoofServer(cfg, hardware=True)


COVERED = {"README.md", "mearth_south.json", "roof.json", "weather.yaml"}


def test_every_site_file_is_covered_and_holds_no_addresses():
    assert {p.name for p in SITE.iterdir()} == COVERED
    for path in SITE.iterdir():
        text = path.read_text()
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            assert ip == "127.0.0.1", f"{path.name}: {ip}"
        assert not re.search(r"(?i)\b(password|passwd|community)\s*[:=]", text), path.name


async def test_shipped_enclosure_config_builds_both_services():
    config = enclosure_config.load_config(SITE / "mearth_south.json")
    names = sorted(enclosure_config.telescopes(config))
    assert names == [f"tel{n}" for n in range(11, 19)]
    safety = SafetyConfig(**config["safety"])
    # No watchdog on this roof: attended only, and a roll-off roof parks first.
    assert safety.attended_only and safety.heartbeat_interval is None
    assert safety.park_before_close and safety.fault_action == "Faults"
    assert not safety.fault_auto_clear
    SequencerConfig(**config["sequencer"])
    roof = MEarthRoofConfig.from_dict(json.loads((SITE / "roof.json").read_text()))
    assert config["devices"]["dome"] == roof.listen
    build_supervisor(config, Timeline())
    seq, remote = build(config, Timeline(), base=SITE)
    try:
        assert sorted(seq.units) == names
    finally:
        await remote.aclose()


def test_shipped_weather_config_fails_closed():
    raw = load_weather(SITE / "weather.yaml")
    cfg = parse_config(raw)
    assert build_sources(cfg)
    station = raw["sources"]["station"]
    # A required placeholder on loopback: nothing serves it, so unsafe.
    assert station["required"] is True and "127.0.0.1" in station["url"]
    site = json.loads((SITE / "mearth_south.json").read_text())["site"]
    for key in ("latitude", "longitude", "elevation"):
        assert raw["site"][key] == site[key]


# -- under the safety supervisor ---------------------------------------------------------


def start_supervisor(rig: Rig, weather: FakeWeather) -> SafetySupervisor:
    tl = Timeline()
    sm = DeviceOwner("safetymonitor", lambda: FakeSafetyMonitor(weather), tl, default_timeout=0.5)
    dome = rig.owner()
    return SafetySupervisor(
        "safety",
        dome=dome,
        safety_monitor=sm,
        timeline=tl,
        config=SafetyConfig(
            **{
                **FAST_SAFETY,  # attended_only off: these tests are not about duty
                "dome_timeout": 4.0,
                "shutter_timeout": 3.0,
                "fault_action": "Faults",
            }
        ),
    )


async def test_supervisor_opens_and_closes_on_unsafe_weather(make_rig):
    rig = make_rig()
    weather = FakeWeather(safe=True)
    sup = start_supervisor(rig, weather)
    run = asyncio.create_task(sup.run())
    try:
        lease = await asyncio.wait_for(sup.request_open("tester"), 10)

        async def renew() -> None:
            while True:
                lease.renew()
                await asyncio.sleep(0.2)

        renewer = asyncio.create_task(renew())
        await asyncio.wait_for(lease.wait_open(), 10)
        assert rig.sim.open()
        weather.set_safe(False)
        dt = await wait_until(rig.sim.closed, 5, what="closed on bad weather")
        renewer.cancel()
        assert lease.revoked.is_set() and CMD_CLOSE in rig.sim.commands()
        print(f"unsafe weather: MEarth roof sim closed {dt:.2f} s later")
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_supervisor_closes_a_roof_that_is_moved_by_hand(make_rig):
    rig = make_rig()
    weather = FakeWeather(safe=True)
    sup = start_supervisor(rig, weather)
    run = asyncio.create_task(sup.run())
    try:
        await wait_until(lambda: sup.fault is None, 2, what="no fault")
        rig.sim.manual_drive(0.6)  # someone moved it during the day
        # Unknown position: shutterError (and a fault, while it lasts), so
        # the supervisor closes; whichever it reads first.
        await wait_until(rig.sim.closed, 5, what="closed again")
        assert sup.timeline.events("close_commanded")
        await wait_until(lambda: sup.fault is None, 3, what="no fault once closed")
        assert CMD_CLOSE in rig.sim.commands()
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
