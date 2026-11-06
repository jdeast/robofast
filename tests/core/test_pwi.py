"""The PWI server, client, simulators and jog tool (docs/pwi.md).

Fast tier: the simulators and the server run in this process (on their own
threads). Nothing here leaves loopback; the "tunnel" tests show that a
loopback endpoint that is not a registered simulator is refused without the
hardware gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import tempfile
import time
from pathlib import Path

import pytest

from robofast import hwgate
from robofast.alpaca_client import (
    ActionNotImplementedException,
    AlpacaDevice,
    InvalidOperationException,
    InvalidValueException,
    NotConnectedException,
    NotImplementedException,
    ParkedException,
)
from robofast.devices.pwi_client import (
    PWI2Client,
    PWI4Client,
    PWICommandError,
    PWIError,
    PWIProtocolError,
    PWITimeout,
    make_client,
    parse_pwi2_status,
    parse_pwi4_status,
)
from robofast.devices.pwi_homed import HomeRecord, encoders_reset
from robofast.devices.pwi_jog import Jog, JogRefused, plan_altaz, plan_radec
from robofast.devices.pwi_jog import main as jog_main
from robofast.devices.pwi_server import PWIConfig, PWIServer, target_altaz
from robofast.devices.pwi_sim import SIM_LATITUDE, SIM_LONGITUDE, PWISim, pwi2_status
from robofast.testing.checks import wait_until
from robofast.testing.tunnel import FakeTunnel

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ("pwi4", "pwi2")
T = 5.0


def config(port: int, backend: str, **kw) -> PWIConfig:
    base = {
        "name": "test",
        "backend": backend,
        "latitude": SIM_LATITUDE,
        "longitude": SIM_LONGITUDE,
        "elevation": 2000.0,
        "min_alt": 20.0,
        "max_alt": 85.0,
        "park_alt": 30.0,
        "park_az": 90.0,
        "focus_min": 100,
        "focus_max": 33000,
        "ports": {"imager": 1, "fau": 2},
        "listen": "127.0.0.1:0",
        "pwi": {"host": "127.0.0.1", "port": port, "timeout": 2.0},
        "status_max_age": 0.05,
        "slew_report_min": 0.2,
        "watch_interval": 0.1,
        "m3_timeout": 5.0,
        "request_timeout": 5.0,
        "allow_find_home": True,
        # Never the real data directory's record.
        "home_record": str(Path(tempfile.mkdtemp(prefix="pwi-home-")) / "home.json"),
    }
    return PWIConfig.from_dict({**base, **kw})


@pytest.fixture
def make():
    made: list = []

    def build(
        backend: str,
        *,
        ready: bool = True,
        hardware: bool = False,
        allow_unhomed: bool = False,
        sim_kw=None,
        **kw,
    ):
        skw = {
            "slew_rate": 20.0,
            "home_time": 0.3,
            "m3_time": 0.3,
            "ready": ready,
            "homed": True,
            "report_homed": True,
            **(sim_kw or {}),
        }
        sim = PWISim(backend, **skw)
        sim.start()
        made.append(sim)
        server = PWIServer(
            config(sim.port, backend, **kw), hardware=hardware, allow_unhomed=allow_unhomed
        ).start()
        made.append(server)
        return sim, server

    yield build
    for x in reversed(made):
        x.stop()


async def device(server: PWIServer, kind: str = "telescope") -> AlpacaDevice:
    d = AlpacaDevice(server.address, kind)
    await d.connect(timeout=T)
    return d


async def until(fn, timeout: float = 5.0, what: str = "") -> None:
    t0 = time.monotonic()
    while not await fn():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out: {what}")
        await asyncio.sleep(0.05)


# -- the gate -------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_tunnel_on_loopback_is_refused_without_the_gate(backend):
    tunnel = FakeTunnel()
    try:
        for allow, hw in ((False, False), (True, False), (False, True)):
            cfg = config(tunnel.port, backend, allow_hardware=allow)
            with pytest.raises(hwgate.HardwareNotAllowed, match="not a simulator"):
                PWIServer(cfg, hardware=hw)
        time.sleep(0.2)
        assert not tunnel.touched  # not even a connection
        # The gate open (config AND --hardware): allowed, still nothing sent
        # until a client asks.
        server = PWIServer(config(tunnel.port, backend, allow_hardware=True), hardware=True)
        server.stop()
        assert not tunnel.touched
    finally:
        tunnel.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tunnel_taking_the_port_later_is_never_sent_anything(make, backend):
    """The simulator goes away and something else (a tunnel) listens on its
    port: the server refuses before sending a byte."""
    sim, server = make(backend)
    tel = await device(server)
    await tel.get("Altitude", timeout=T)
    port = sim.port
    sim.stop()
    tunnel = socket.socket()
    tunnel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tunnel.bind(("127.0.0.1", port))
    tunnel.listen()
    tunnel.settimeout(0.5)
    try:
        await asyncio.sleep(0.2)  # past the status cache
        with pytest.raises(Exception, match="not a simulator"):
            await tel.get("Altitude", timeout=T)
        with pytest.raises(TimeoutError):
            tunnel.accept()  # never connected to
    finally:
        tunnel.close()
        await tel.aclose()


def test_not_loopback_is_refused_without_the_gate():
    cfg = config(8220, "pwi4", pwi={"host": "192.0.2.1", "port": 8220})
    with pytest.raises(hwgate.HardwareNotAllowed, match="not loopback"):
        PWIServer(cfg)


@pytest.mark.parametrize("name", ["t1_pwi2", "t2_pwi2", "t1_pwi4", "t2_pwi4"])
def test_remote_configs_need_hardware_flag(name):
    path = ROOT / "sites" / "minerva" / "remote" / f"{name}.json"
    d = json.loads(path.read_text())
    cfg = PWIConfig.from_dict(d)
    assert cfg.allow_hardware and cfg.pwi.host == "127.0.0.1"
    assert cfg.min_alt == 21.0 and cfg.max_alt == 83.0 and cfg.max_jog_deg <= 10.0
    with FakeTunnel() as tunnel:  # the tunnel, on a free port
        d["pwi"]["port"] = tunnel.port
        with pytest.raises(hwgate.HardwareNotAllowed):
            PWIServer(PWIConfig.from_dict(d))  # no --hardware
        assert not tunnel.touched


def test_jog_refuses_tunnel_without_hardware(tmp_path, capsys):
    tunnel = FakeTunnel()
    try:
        cfg = json.loads((ROOT / "sites/minerva/remote/t1_pwi2.json").read_text())
        cfg["pwi"]["port"] = tunnel.port
        p = tmp_path / "c.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(SystemExit) as e:
            jog_main(["--config", str(p), "--log", str(tmp_path / "j.log"), "stop"])
        assert e.value.code == 2
        assert not tunnel.touched
        assert "not a simulator" in (tmp_path / "j.log").read_text()
    finally:
        tunnel.close()


# -- the client ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_client_status_and_allowlist(backend):
    sim = PWISim(backend).start()
    c = make_client(backend, "127.0.0.1", sim.port, timeout=1.0)
    try:
        st = await c.status()
        assert not st.connected and not st.motors_enabled and st.m3_port == 1
        assert st.lst_hours is not None and abs(st.alt_deg - 30.0) < 1e-6
        assert sim.model.commands == []  # a status read commands nothing
        with pytest.raises(PWIError, match="not an allowed"):
            if backend == "pwi4":
                await c._cmd("/mount/sync")  # type: ignore[attr-defined]
            else:
                await c._cmd("mount", "sync")  # type: ignore[attr-defined]
        sim.set_mode("garble")
        with pytest.raises(PWIProtocolError):
            await c.status()
        sim.set_mode("http500")
        with pytest.raises(PWICommandError):
            await c.status()
        sim.set_mode("hang")
        t0 = time.monotonic()
        with pytest.raises(PWITimeout):
            await c.status()
        assert time.monotonic() - t0 < 3.0
    finally:
        await c.aclose()
        sim.stop()


def test_pwi2_moving_quirk_and_parsing():
    from robofast.devices.pwi_sim import CDK700Model

    m = CDK700Model(ready=True)
    st = parse_pwi2_status(pwi2_status(m))
    assert st.raw["mount.moving"] == "True"  # not tracking: PWI2 says moving
    assert st.slewing is None  # ... which means nothing
    m.set_tracking(True)
    st = parse_pwi2_status(pwi2_status(m))
    assert st.tracking and st.slewing is False
    with pytest.raises(PWIProtocolError):
        parse_pwi2_status("<html/>")
    with pytest.raises(PWIProtocolError):
        parse_pwi4_status("mount.is_connected=true\n")


# -- the server -------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_connecting_and_reading_never_commands(make, backend):
    sim, server = make(backend, ready=False)
    tel = await device(server)
    foc = await device(server, "focuser")
    rot = await device(server, "rotator")
    for member in ("Altitude", "Azimuth", "RightAscension", "Slewing", "Tracking", "AtPark"):
        await tel.get(member, timeout=T)
    await foc.get("Position", timeout=T)
    await rot.get("Position", timeout=T)
    json.loads(await tel.call("Action", "Status", "", timeout=T))
    await asyncio.sleep(0.3)  # the limit watch reads too
    assert sim.model.commands == []
    assert not sim.model.connected and sim.model.enabled == [False, False]
    # Motion is refused until connected and enabled -- explicitly.
    with pytest.raises(InvalidOperationException, match="MountConnect"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Action", "MountConnect", "", timeout=T)
    with pytest.raises(InvalidOperationException, match="MotorsEnable"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Action", "MotorsEnable", "", timeout=T)
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), what="slew end")
    assert abs(await tel.get("Altitude", timeout=T) - 40.0) < 0.01
    for c in (tel, foc, rot):
        await c.aclose()


async def _not(aw):
    return not await aw


@pytest.mark.parametrize("backend", BACKENDS)
async def test_altitude_limits_are_enforced(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    for alt in (19.9, 85.1):
        with pytest.raises(InvalidValueException, match="outside"):
            await tel.put("SlewToAltAzAsync", Altitude=alt, Azimuth=100.0, timeout=T)
    await tel.set("Tracking", True, timeout=T)
    lst = await tel.get("SiderealTime", timeout=T)
    # Below the horizon limit: hour angle 6 h at Dec -40.
    with pytest.raises(InvalidValueException, match="outside"):
        await tel.call("SlewToCoordinatesAsync", (lst - 6.0) % 24, -40.0, timeout=T)
    # Near the zenith: above max_alt.
    with pytest.raises(InvalidValueException, match="outside"):
        await tel.call("SlewToCoordinatesAsync", lst, SIM_LATITUDE, timeout=T)
    assert not any("goto" in c or "move" in c for c in sim.model.commands)
    ra, dec = (lst - 1.0) % 24, 20.0
    alt, _ = target_altaz(ra, dec, lst, SIM_LATITUDE)
    assert 20 < alt < 85
    await tel.call("SlewToCoordinatesAsync", ra, dec, timeout=T)
    assert await tel.get("Slewing", timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=30, what="slew end")
    assert abs(await tel.get("Declination", timeout=T) - dec) < 0.5
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_abort_park_home(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 2.0})
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=60.0, Azimuth=120.0, timeout=T)
    await asyncio.sleep(0.3)
    await tel.call("AbortSlew", timeout=T)
    alt = sim.model.state()["alt"]
    await asyncio.sleep(0.3)
    assert abs(sim.model.state()["alt"] - alt) < 1e-6  # stopped
    assert alt < 59.0
    sim.model.slew_rate = 20.0
    await tel.call("Park", timeout=T)
    await until(lambda: tel.get("AtPark", timeout=T), what="parked")
    assert not await tel.get("Tracking", timeout=T)
    with pytest.raises(ParkedException):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Unpark", timeout=T)
    await tel.put("FindHome", timeout=T)
    await until(lambda: tel.get("AtHome", timeout=T), what="home")
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_stop_action_needs_no_connection(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 1.0})
    sim.model.goto_altaz(60.0, 120.0)
    tel = AlpacaDevice(server.address, "telescope")
    with pytest.raises(NotConnectedException):
        await tel.get("Altitude", timeout=T)
    await tel.call("Action", "Stop", "", timeout=T)
    assert not sim.model.state()["slewing"]
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("rate", "start", "low", "high"), [(5.0, 24.0, 19.0, 21.5), (15.0, 50.0, 26.0, 28.6)]
)
async def test_limit_watch_stops_a_mount_going_low(make, backend, rate, start, low, high):
    """A stop coasts (stop_distance 8 deg from 15 deg/s): the watch stops a
    mount heading for the limit early enough that it would come to rest
    inside it. (The simulator stops dead, so it ends where the stop was sent.)"""
    sim, server = make(backend, sim_kw={"slew_rate": rate, "alt": start})
    # Something else (not robofast) slews it toward the horizon.
    with sim.model.lock:
        sim.model.goto_altaz(5.0, 90.0)
    await until(lambda: _async(server.mount.watch_stops >= 1), timeout=8, what="watch stop")
    # The count goes up just before the stop is sent.
    await until(lambda: _async(not sim.model.state()["slewing"]), timeout=3, what="stopped")
    st = sim.model.state()
    assert not st["slewing"] and low < st["alt"] < high


@pytest.mark.parametrize("backend", BACKENDS)
async def test_limit_watch_leaves_a_fast_slew_to_a_target_inside_the_limits(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 15.0, "alt": 70.0})
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=21.0, Azimuth=90.0, timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=10, what="slew end")
    assert abs(sim.model.state()["alt"] - 21.0) < 0.01
    assert server.mount.watch_stops == 0
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_limit_watch_skips_a_bad_read(make, backend):
    """MINERVA T1, 2026-11-05: PWI2 reported 61.4 deg altitude mid-slew from
    near 40. One read implying a speed no mount reaches is not motion: no stop."""
    _sim, server = make(backend, sim_kw={"alt": 30.0})
    mount = server.mount
    real = mount.pwi.status
    bad = {"n": 0}

    async def status():
        st = await real()
        if mount.watch_glitches == 0 and bad["n"] < 1 and st.alt_deg is not None:
            bad["n"] += 1
            return dataclasses.replace(st, alt_deg=st.alt_deg - 6.0)  # "60 deg/s" down
        return st

    await asyncio.sleep(0.3)  # the watch has a baseline
    mount.pwi.status = status
    await until(lambda: _async(mount.watch_glitches >= 1), timeout=3, what="glitch seen")
    await asyncio.sleep(0.5)
    assert mount.watch_stops == 0


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_slew_the_limit_watch_stops_fails(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 5.0, "alt": 60.0})
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=90.0, timeout=T)
    await asyncio.sleep(0.3)
    with sim.model.lock:
        sim.model.goto_altaz(5.0, 90.0)  # something else drives it past the target
    with pytest.raises(Exception, match="LIMIT"):
        await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=10, what="slew end")
    assert server.mount.watch_stops == 1
    await tel.aclose()


async def _async(v):
    return v


@pytest.mark.parametrize("backend", BACKENDS)
async def test_select_port_focuser_rotator(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    foc = await device(server, "focuser")
    rot = await device(server, "rotator")
    with pytest.raises(InvalidValueException):
        await tel.call("Action", "SelectPort", "3", timeout=T)
    with pytest.raises(ActionNotImplementedException):
        await tel.call("Action", "Nope", "", timeout=T)
    await foc.call("Move", 20000, timeout=T)
    await until(lambda: _not(foc.get("IsMoving", timeout=T)), what="focus")
    assert await foc.get("Position", timeout=T) == 20000
    with pytest.raises(InvalidValueException):
        await foc.call("Move", 50, timeout=T)  # below focus_min
    await rot.put("MoveMechanical", Position=45.0, timeout=T)
    await until(lambda: _not(rot.get("IsMoving", timeout=T)), what="rotator")
    assert abs(await rot.get("MechanicalPosition", timeout=T) - 45.0) < 0.01
    out = json.loads(await tel.call("Action", "SelectPort", "fau", timeout=T))
    assert out == {"port": 2, "moved": True}
    assert sim.model.state()["m3_port"] == 2
    await foc.call("Move", 21000, timeout=T)
    await until(lambda: _not(foc.get("IsMoving", timeout=T)), what="focus 2")
    assert sim.model.focusers[2].pos == 21000 and sim.model.focusers[1].pos == 20000
    if backend == "pwi4":
        # PWI4 reports only Rotator index 0 (port 1).
        with pytest.raises(Exception, match="only port 1"):
            await rot.get("MechanicalPosition", timeout=T)
    else:
        assert abs(await rot.get("MechanicalPosition", timeout=T)) < 0.01
    out = json.loads(await tel.call("Action", "SelectPort", "2", timeout=T))
    assert out == {"port": 2, "moved": False}
    for c in (tel, foc, rot):
        await c.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_offset_and_pulse_guide(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match="tracking"):
        await tel.call("Action", "Offset", '{"east": 10}', timeout=T)
    await tel.set("Tracking", True, timeout=T)
    ra0, dec0 = sim.model.radec
    await tel.call("Action", "Offset", '{"east": 0, "north": 36}', timeout=T)
    assert abs(sim.model.radec[1] - dec0 - 0.01) < 1e-9
    with pytest.raises(InvalidValueException):
        await tel.call("Action", "Offset", '{"north": 6000}', timeout=T)
    await tel.call("PulseGuide", 1, 2000, timeout=T)  # south, 2 s at 7.5"/s
    assert await tel.get("IsPulseGuiding", timeout=T)
    assert abs(sim.model.radec[1] - dec0 - 0.01 + 15 / 3600) < 1e-9
    await tel.call("PulseGuide", 2, 1000, timeout=T)  # east: RA coordinate + 7.5"
    assert abs((sim.model.radec[0] - ra0) * 15 * 3600 - 7.5) < 1e-6
    await tel.aclose()


async def test_hung_pwi_is_bounded(make):
    sim, server = make("pwi4")
    tel = await device(server)
    sim.set_mode("hang")
    await asyncio.sleep(0.3)  # past the status cache
    t0 = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 -- the timeout, as an Alpaca error
        await tel.get("Altitude", timeout=10)
    assert time.monotonic() - t0 < 6.0
    sim.set_mode("ok")
    await tel.aclose()


# -- the jog tool ----------------------------------------------------------------------------


class _Out(list):
    def __call__(self, text: str) -> None:
        self.append(text)


async def _no(prompt: str) -> bool:
    return False


async def _yes(prompt: str) -> bool:
    return True


def jog_for(sim: PWISim, backend: str, confirm=_yes, **kw) -> tuple[Jog, _Out, PWIConfig]:
    cfg = config(sim.port, backend, **kw)
    client = make_client(backend, "127.0.0.1", sim.port, timeout=1.0)
    out = _Out()
    return Jog(cfg, client, out=out, confirm=confirm, poll=0.05), out, cfg


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_altaz_arrives(backend):
    sim = PWISim(
        backend, ready=True, slew_rate=2.0, alt=40.0, az=100.0, homed=True, report_homed=True
    ).start()
    jog, out, _ = jog_for(sim, backend)
    try:
        assert await jog.altaz(0.5, -0.3)
        st = sim.model.state()
        assert abs(st["alt"] - 40.5) < 1e-3 and abs(st["az"] - 99.7) < 1e-3
        assert any(line.startswith("PLAN") for line in out) and out[-1] == "arrived"
    finally:
        await jog.pwi.aclose()
        sim.stop()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_not_confirmed_sends_nothing(backend):
    sim = PWISim(backend, ready=True, homed=True, report_homed=True).start()
    jog, _out, _ = jog_for(sim, backend, confirm=_no)
    try:
        assert not await jog.altaz(0.5, 0.0)
        assert sim.model.commands == []
    finally:
        await jog.pwi.aclose()
        sim.stop()


def test_jog_plans_refuse():
    from robofast.devices.pwi_client import parse_pwi4_status
    from robofast.devices.pwi_sim import CDK700Model, pwi4_status

    m = CDK700Model(ready=True, alt=20.5, az=359.8)
    cfg = config(1, "pwi4")
    st = parse_pwi4_status(pwi4_status(m))
    with pytest.raises(JogRefused, match="max_jog_deg"):
        plan_altaz(cfg, st, 1.5, 0)
    with pytest.raises(JogRefused, match="outside"):
        plan_altaz(cfg, st, -0.6, 0)  # 19.9 < min_alt 20
    with pytest.raises(JogRefused, match="crosses 0/360"):
        plan_altaz(cfg, st, 0.2, 0.5)
    assert plan_altaz(cfg, st, 0.5, -0.5) == (21.0, pytest.approx(359.3))
    with pytest.raises(JogRefused, match="tracking"):
        plan_radec(cfg, st, 10, 0)
    m.set_tracking(True)
    st = parse_pwi4_status(pwi4_status(m))
    with pytest.raises(JogRefused, match="tracking is on"):
        plan_altaz(cfg, st, 0.1, 0)
    assert plan_radec(cfg, st, 0, 36) == pytest.approx(0.01)
    m2 = CDK700Model(ready=False)
    with pytest.raises(JogRefused, match="not connected"):
        plan_altaz(cfg, parse_pwi4_status(pwi4_status(m2)), 0.1, 0)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_stops_a_runaway(backend):
    """PWI goes further than planned: the jog sends stop."""
    sim = PWISim(
        backend, ready=True, slew_rate=3.0, alt=40.0, az=100.0, homed=True, report_homed=True
    ).start()
    jog, out, _ = jog_for(sim, backend)
    real = jog.pwi.goto_altaz

    async def wrong(alt, az):  # as if PWI took the long way round
        return await real(alt + 5.0, az)

    jog.pwi.goto_altaz = wrong  # type: ignore[method-assign]
    try:
        assert not await jog.altaz(0.5, 0.0)
        assert any(line.startswith("STOP") for line in out)
        st = sim.model.state()
        assert not st["slewing"] and st["alt"] < 41.5
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_cancel_sends_stop():
    sim = PWISim(
        "pwi4", ready=True, slew_rate=0.5, alt=40.0, az=100.0, homed=True, report_homed=True
    ).start()
    jog, out, _ = jog_for(sim, "pwi4")
    try:
        task = asyncio.ensure_future(jog.altaz(1.0, 0.0))
        await wait_until(lambda: sim.model.state()["slewing"], timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "STOP (interrupted)" in out
        assert not sim.model.state()["slewing"]
    finally:
        await jog.pwi.aclose()
        sim.stop()


def test_jog_cli_status_and_stop(tmp_path, capsys):
    sim = PWISim("pwi2", ready=True).start()
    try:
        p = tmp_path / "c.json"
        cfg = json.loads((ROOT / "sites/minerva/remote/t1_pwi2.json").read_text())
        cfg["pwi"]["port"] = sim.port
        cfg["allow_hardware"] = False  # a simulator needs no gate
        p.write_text(json.dumps(cfg))
        log = tmp_path / "jog.log"
        for args in (["status"], ["stop"]):
            with pytest.raises(SystemExit) as e:
                jog_main(["--config", str(p), "--log", str(log), *args])
            assert e.value.code == 0
        assert sim.model.commands == ["mount:stop"]
        text = log.read_text()
        assert "STOP (requested)" in text and "alt " in text
    finally:
        sim.stop()


def test_pwi4_client_and_pwi2_client_are_backends():
    assert PWI4Client.backend == "pwi4" and PWI2Client.backend == "pwi2"


# -- homing -----------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_moves_refused_unhomed_unless_override(make, backend):
    sim, server = make(backend, sim_kw={"homed": False})
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match="homed"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    with pytest.raises(InvalidOperationException, match="homed"):
        await tel.set("Tracking", True, timeout=T)
    with pytest.raises(InvalidOperationException, match="homed"):
        await tel.call("Park", timeout=T)
    assert not any("goto" in c or "move" in c or "tracking" in c for c in sim.model.commands)
    await tel.aclose()
    # The config flag alone is not enough...
    _, server2 = make(backend, sim_kw={"homed": False}, allow_unhomed_moves=True)
    tel2 = await device(server2)
    with pytest.raises(InvalidOperationException, match="homed"):
        await tel2.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel2.aclose()
    # ... config AND --allow-unhomed.
    sim3, server3 = make(
        backend, sim_kw={"homed": False}, allow_unhomed_moves=True, allow_unhomed=True
    )
    tel3 = await device(server3)
    await tel3.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await until(lambda: _not(tel3.get("Slewing", timeout=T)), what="slew end")
    assert abs(sim3.model.state()["alt"] - 40.0) < 0.01
    await tel3.aclose()


async def test_pwi4_without_homed_flag_counts_as_unhomed_until_homed_here(make):
    _, server = make("pwi4", sim_kw={"report_homed": False})
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match="homed state unknown"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.put("FindHome", timeout=T)
    await until(lambda: tel.get("AtHome", timeout=T), what="home")
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_find_home_needs_opt_in_and_never_happens_implicitly(make, backend):
    sim, server = make(backend, ready=False, sim_kw={"homed": False}, allow_find_home=False)
    tel = await device(server)
    assert not await tel.get("CanFindHome", timeout=T)
    with pytest.raises(NotImplementedException):
        await tel.put("FindHome", timeout=T)
    with pytest.raises(NotImplementedException):
        await tel.call("Action", "FindHome", "both", timeout=T)
    await tel.call("Action", "MountConnect", "", timeout=T)
    await tel.call("Action", "MotorsEnable", "", timeout=T)
    with pytest.raises(InvalidOperationException):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    with pytest.raises(InvalidOperationException):
        await tel.set("Tracking", True, timeout=T)
    await tel.call("Action", "Stop", "", timeout=T)
    await asyncio.sleep(0.3)
    assert not any("home" in c for c in sim.model.commands)
    assert sim.model.state()["axes_homed"] == [False, False]
    await tel.aclose()


async def test_find_home_one_axis(make):
    sim, server = make("pwi4", sim_kw={"homed": False})
    tel = await device(server)
    await tel.call("Action", "FindHome", "1", timeout=T)
    await until(lambda: _async(sim.model.state()["axes_homed"] == [False, True]), what="alt")
    assert "/mount/findhome?axis=1" in sim.model.commands
    await until(lambda: _not(tel.get("Slewing", timeout=T)), what="alt done")
    with pytest.raises(InvalidOperationException, match="homed"):  # azimuth still unhomed
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Action", "FindHome", "0", timeout=T)
    await until(lambda: _async(sim.model.state()["homed"]), what="az")
    await until(lambda: _not(tel.get("Slewing", timeout=T)), what="home done")
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.aclose()


async def test_find_home_one_axis_needs_pwi4_417(make):
    _, server = make("pwi4", sim_kw={"version": "4.1.6.0"})  # T1 today
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match=r"4\.1\.7"):
        await tel.call("Action", "FindHome", "0", timeout=T)
    await tel.aclose()
    _, server2 = make("pwi2")
    tel2 = await device(server2)
    with pytest.raises(InvalidOperationException, match=r"4\.1\.7"):
        await tel2.call("Action", "FindHome", "1", timeout=T)
    await tel2.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_refuses_unhomed_unless_override(backend):
    sim = PWISim(backend, ready=True, homed=False, alt=40.0, az=100.0).start()
    jog, out, _ = jog_for(sim, backend)
    try:
        with pytest.raises(JogRefused, match="allow-unhomed"):
            await jog.altaz(0.2, 0.0)
        assert sim.model.commands == []
        jog.unhomed_ok = True
        assert await jog.altaz(0.2, 0.0)
        assert any("UNHOMED MOVE" in line for line in out)
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_home_one_axis_then_the_other():
    sim = PWISim("pwi4", ready=True, report_homed=True, slew_rate=20.0, home_time=0.2).start()
    jog, out, _ = jog_for(sim, "pwi4")
    try:
        assert await jog.home(1)
        assert sim.model.state()["axes_homed"] == [False, True]
        assert await jog.home(0)
        assert sim.model.state()["homed"]
        assert any("FIND HOME, ALTITUDE" in line for line in out)
        assert any(line.startswith("PWI (pwi4) version 4.1.8.0") for line in out)
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_home_stops_if_the_other_axis_moves():
    """A PWI4 that ignored the axis parameter homes both: the jog stops it."""
    sim = PWISim("pwi4", ready=True, slew_rate=2.0, alt=40.0, az=100.0).start()
    jog, out, _ = jog_for(sim, "pwi4")
    real = jog.pwi.find_home

    async def both(axis=None):
        return await real(None)

    jog.pwi.find_home = both  # type: ignore[method-assign]
    try:
        assert not await jog.home(0)
        assert any("the other axis moved" in line for line in out)
        assert not sim.model.state()["slewing"]
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_home_refusals():
    sim = PWISim("pwi2", ready=True).start()
    jog, _, _ = jog_for(sim, "pwi2")
    try:
        with pytest.raises(JogRefused, match=r"4\.1\.7"):
            await jog.home(0)
        assert not await Jog(jog.cfg, jog.pwi, out=_Out(), confirm=_no).home(None)
        assert sim.model.commands == []
    finally:
        await jog.pwi.aclose()
        sim.stop()


# -- real-hardware behaviour of PWI4 4.1.6 (docs/pwi.md) --------------------------------------


async def test_slew_beyond_pwi4s_own_limit_fails_and_stops(make):
    """PWI4 takes a goto below its own altitude limit, stops at the limit and
    reports slewing forever: the server stops it and Slewing says why."""
    sim, server = make(
        "pwi4", sim_kw={"pwi_min_alt": 25.0, "alt": 40.0}, stall_time=0.5, slew_report_min=0.1
    )
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=22.0, Azimuth=90.0, timeout=T)
    with pytest.raises(Exception, match="beyond PWI's own axis limits"):
        await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=10, what="slew")
    st = sim.model.state()
    assert not st["slewing"] and abs(st["alt"] - 25.0) < 0.01
    assert "/mount/stop" in sim.model.commands
    # Sticky until the next command.
    with pytest.raises(Exception, match="slew failed"):
        await tel.get("Slewing", timeout=T)
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=90.0, timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=10, what="slew 2")
    assert abs(sim.model.state()["alt"] - 40.0) < 0.01
    await tel.aclose()


async def test_jog_stops_a_slew_stuck_at_pwi4s_limit():
    sim = PWISim(
        "pwi4", ready=True, homed=True, report_homed=True, alt=30.0, az=100.0, pwi_min_alt=29.5
    ).start()
    jog, out, _ = jog_for(sim, "pwi4", stall_time=0.5)
    try:
        assert not await jog.altaz(-1.0, 0.0)
        assert any("beyond PWI's own axis limits" in line for line in out)
        assert not sim.model.state()["slewing"]
    finally:
        await jog.pwi.aclose()
        sim.stop()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_select_port_resends_an_ignored_m3_goto(make, backend):
    sim, server = make(backend, m3_resend_after=0.5)
    tel = await device(server)
    sim.model.m3_drops = 1
    out = json.loads(await tel.call("Action", "SelectPort", "2", timeout=10))
    assert out == {"port": 2, "moved": True}
    assert server.mount.m3_resends == 1
    assert "m3_goto" in sim.model.state()["ignored"]
    # Every goto ignored: bounded retries, then an error and m3 stop.
    sim.model.m3_drops = 10
    with pytest.raises(Exception, match="M3 not at port 1"):
        await tel.call("Action", "SelectPort", "1", timeout=20)
    gotos = [c for c in sim.model.commands if "m3" in c and ("goto" in c or "select" in c)]
    assert len(gotos) == 2 + 3  # two for port 2, then 1 + m3_retries (2) for port 1
    assert any("m3" in c and "stop" in c for c in sim.model.commands)
    await tel.aclose()


def test_pwi4_m3_port_minus_one_is_moving():
    st = parse_pwi4_status(
        "pwi4.version=4.1.6.0\nmount.is_connected=true\nmount.altitude_degs=40\n"
        "mount.azimuth_degs=100\nmount.ra_apparent_hours=1\nmount.dec_apparent_degs=2\n"
        "m3.exists=1\nm3.port=-1\n"
    )
    assert st.m3_port == 0 and st.m3_moving


HOME = {"home_alt": 60.0, "home_az": 180.0}  # the simulator's home


async def test_pwi4_home_is_recorded_and_survives_a_server_restart(make):
    sim, server = make("pwi4", sim_kw={"report_homed": False, "homed": False}, **HOME)
    path = server.config.home_record
    tel = await device(server)
    await tel.put("FindHome", timeout=T)
    await until(lambda: tel.get("AtHome", timeout=T), what="home")
    assert HomeRecord(path).axes(await server.mount.pwi.status()) == {0, 1}
    await tel.aclose()
    server.stop()
    # A new server (same record) may move at once.
    server2 = PWIServer(config(sim.port, "pwi4", home_record=path, **HOME)).start()
    try:
        tel2 = await device(server2)
        await tel2.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
        await until(lambda: _not(tel2.get("Slewing", timeout=T)), what="slew")
        # A controller power cycle: encoders read 0, PWI disconnected.
        sim.model.power_cycle()
        await until(lambda: _async(not Path(path).exists()), what="record dropped")
        sim.model.connect()
        sim.model.enable()
        with sim.model.lock:
            sim.model.alt, sim.model.az = 40.0, 100.0  # even if moved away since
        with pytest.raises(InvalidOperationException, match="homed state unknown"):
            await tel2.put("SlewToAltAzAsync", Altitude=45.0, Azimuth=100.0, timeout=T)
        await tel2.aclose()
    finally:
        server2.stop()


async def test_pwi4_home_ending_elsewhere_is_not_counted(make):
    """PWI4 can abort its fine search and stop away from home."""
    _, server = make(
        "pwi4", sim_kw={"report_homed": False, "homed": False}, home_alt=50.0, home_az=180.0
    )
    tel = await device(server)
    await tel.put("FindHome", timeout=T)
    await until(lambda: tel.get("AtHome", timeout=T), what="home")
    with pytest.raises(InvalidOperationException, match="homed state unknown"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    assert HomeRecord(server.config.home_record).load() is None
    await tel.aclose()


async def test_jog_home_records_and_forget_home():
    sim = PWISim("pwi4", ready=True, slew_rate=20.0, home_time=0.2, version="4.1.6.0").start()
    jog, out, cfg = jog_for(sim, "pwi4", **HOME)
    try:
        with pytest.raises(JogRefused, match="NOT REPORTED"):
            await jog.altaz(0.5, 0.0)
        assert await jog.home(None)
        assert any("home recorded" in line for line in out)
        assert await jog.altaz(-0.5, 0.0)  # no override needed now
        # A second jog process sees the same record.
        jog2, _, _ = jog_for(sim, "pwi4", home_record=cfg.home_record, **HOME)
        assert await jog2.altaz(0.5, 0.0)
        await jog2.pwi.aclose()
        jog.forget_home()
        with pytest.raises(JogRefused, match="NOT REPORTED"):
            await jog.altaz(0.5, 0.0)
        # A record from another PWI version does not count.
        await jog.home(None)
        sim.model.version = "4.1.7.0"
        with pytest.raises(JogRefused, match="NOT REPORTED"):
            await jog.altaz(0.5, 0.0)
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_home_without_configured_home_is_not_recorded():
    sim = PWISim("pwi4", ready=True, slew_rate=20.0, home_time=0.2).start()
    jog, out, cfg = jog_for(sim, "pwi4")
    try:
        assert not await jog.home(None)
        assert any("home NOT recorded" in line and "home_alt" in line for line in out)
        assert HomeRecord(cfg.home_record).load() is None
    finally:
        await jog.pwi.aclose()
        sim.stop()


def test_encoders_reset_needs_both_axes_near_zero():
    st = parse_pwi4_status(
        "pwi4.version=4.1.6.0\nmount.is_connected=true\nmount.altitude_degs=0.01\n"
        "mount.azimuth_degs=359.9\nmount.ra_apparent_hours=1\nmount.dec_apparent_degs=2\n"
    )
    assert encoders_reset(st)
    st.alt_deg = 25.0  # parked at az 0 is not a reset
    assert not encoders_reset(st)


# -- the azimuth wrap, stalls, park timeout, disabled axes ----------------------------------

WRAP = {"az_wrap_min": -90.0, "az_wrap_max": 450.0}


async def test_park_at_az_0_goes_the_short_legal_way(make):
    """From mechanical 281, PWI4's default wrap (0..360) would take az 0 the
    long way to mechanical 0; robofast sets the wrap so it goes to 360."""
    sim, server = make("pwi4", sim_kw={"az": 281.0, "alt": 46.0}, park_az=0.0, **WRAP)
    tel = await device(server)
    await tel.call("Park", timeout=T)
    await until(lambda: tel.get("AtPark", timeout=T), timeout=15, what="parked")
    st = sim.model.state()
    assert abs(st["mech"] - 360.0) < 0.01 and abs(st["wrap_min"] - 180.0) < 1e-9
    assert "/mount/set_axis0_wrap_range_min?degs=180.0" in sim.model.commands
    # Back to 100: nearest legal is mechanical 100 (no wrap change needed? 100 < 180).
    await tel.call("Unpark", timeout=T)
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=15, what="slew")
    assert abs(sim.model.state()["mech"] - 100.0) < 0.01
    await tel.aclose()


async def test_goto_refused_when_pwi4_reports_no_wrap_range(make):
    sim, server = make("pwi4", **WRAP)
    sim.model.report_wrap = False
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match="wrap range"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    with pytest.raises(InvalidOperationException, match="wrap range"):
        await tel.call("Park", timeout=T)
    assert not any("goto" in c for c in sim.model.commands)
    await tel.aclose()


async def test_park_the_long_way_into_an_obstruction_is_stopped(make):
    """No wrap configured: PWI4 goes the long way and the axis stops short
    (here at mechanical 17.3); robofast stops it and the park fails."""
    sim, server = make(
        "pwi4",
        sim_kw={"az": 281.0, "alt": 46.0, "az_mech_min": 17.3, "slew_rate": 30.0},
        park_az=0.0,
        stall_time=0.5,
    )
    tel = await device(server)
    await tel.call("Park", timeout=T)
    with pytest.raises(Exception, match="no progress"):
        await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=20, what="park")
    # The stop reaches the simulator just after robofast reports the stall.
    for _ in range(50):
        if not sim.model.state()["slewing"]:
            break
        await asyncio.sleep(0.1)
    st = sim.model.state()
    assert not st["slewing"] and abs(st["mech"] - 17.3) < 0.01
    assert not await tel.get("AtPark", timeout=T)
    await tel.aclose()


async def test_park_timeout(make):
    sim, server = make("pwi4", sim_kw={"slew_rate": 1.0, "az": 100.0}, park_timeout=1.0)
    tel = await device(server)
    await tel.call("Park", timeout=T)
    await asyncio.sleep(1.6)
    with pytest.raises(Exception, match="park_timeout"):
        await tel.get("Slewing", timeout=T)
    assert not sim.model.state()["slewing"]
    await tel.aclose()


async def test_axis_disabled_during_a_slew_fails_it(make):
    sim, server = make("pwi4", sim_kw={"slew_rate": 2.0})
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=60.0, Azimuth=120.0, timeout=T)
    await asyncio.sleep(0.3)
    with sim.model.lock:
        sim.model.enabled[0] = False  # PWI disables azimuth (a drive fault)
    with pytest.raises(Exception, match="disabled the azimuth axis"):
        await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=5, what="slew")
    with pytest.raises(InvalidOperationException, match="azimuth axis motor is disabled"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    assert await tel.get("Connected", timeout=T)
    await tel.aclose()


async def test_watch_stops_azimuth_leaving_the_wrap_range(make):
    sim, server = make("pwi4", sim_kw={"slew_rate": 5.0, "az": 0.0}, **WRAP)
    with sim.model.lock:
        sim.model.mech = -80.0
        sim.model.goto_altaz(30.0, 0.0)
        sim.model.mech_target = -200.0  # something else drives it out
    await until(lambda: _async(server.mount.watch_stops >= 1), timeout=8, what="watch stop")
    await until(lambda: _async(not sim.model.state()["slewing"]), timeout=3, what="stopped")
    assert sim.model.state()["mech"] > -91.0


def test_wrap_config_checks():
    with pytest.raises(ValueError, match="go together"):
        config(1, "pwi4", az_wrap_min=-90.0)
    with pytest.raises(ValueError, match="pwi4 backend"):
        config(1, "pwi2", **WRAP)
    with pytest.raises(ValueError, match="at least 360"):
        config(1, "pwi4", az_wrap_min=0.0, az_wrap_max=300.0)


async def test_jog_refuses_a_long_way_round():
    sim = PWISim("pwi4", ready=True, homed=True, report_homed=True, alt=30.0, az=0.0).start()
    with sim.model.lock:
        sim.model.mech = 360.0  # parked through 360; PWI4's wrap still 0..360
    jog, _, _ = jog_for(sim, "pwi4", **WRAP)
    try:
        with pytest.raises(JogRefused, match="the long way"):
            await jog.altaz(0.5, 0.0)
        assert sim.model.commands == []
        with sim.model.lock:
            sim.model.wrap_min = 180.0
        assert await jog.altaz(0.5, 0.0)
        assert abs(sim.model.state()["mech"] - 360.0) < 0.01
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_targets_do_not_carry_over_to_a_new_session(make):
    _, server = make("pwi4")
    tel = await device(server)
    await tel.set("Tracking", True, timeout=T)
    lst = await tel.get("SiderealTime", timeout=T)
    await tel.call("SlewToCoordinatesAsync", (lst - 1.0) % 24, 20.0, timeout=T)
    assert await tel.get("TargetDeclination", timeout=T) == 20.0
    await tel.set("Connected", False, timeout=T)
    await tel.aclose()
    tel2 = await device(server)  # a new session (e.g. ConformU)
    with pytest.raises(InvalidOperationException, match="not been set"):
        await tel2.get("TargetRightAscension", timeout=T)
    await tel2.call("AbortSlew", timeout=T)
    await tel2.aclose()


async def test_driver_info_carries_the_pwi_version(make):
    """It goes in FITS headers as MNTSWVER (docs/data.md)."""
    _, server = make("pwi4", sim_kw={"version": "4.1.6.0"})
    tel = await device(server)
    info = await tel.get("DriverInfo", timeout=T)
    assert info.startswith("PlaneWave PWI4 4.1.6.0;") and "pwi_server" in info
    await tel.aclose()
