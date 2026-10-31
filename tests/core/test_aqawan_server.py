"""The Aqawan Alpaca Dome server, end to end against the PAC simulator.

Fast tier: the simulator and the server run in this process (on their own
threads) and are driven through robofast's own Alpaca client and
:class:`~robofast_core.owner.DeviceOwner`, then by the safety supervisor,
in-process and as its own (killed) process. Nothing here leaves loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from robofast_core import (
    DeviceOwner,
    NotAuthorized,
    Observation,
    SafetyConfig,
    SafetySupervisor,
    Sequencer,
    SequencerConfig,
    Timeline,
)
from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import (
    AlpacaDevice,
    DriverException,
    InvalidOperationException,
    NotConnectedException,
    NotImplementedException,
)
from robofast_core.devices.aqawan_pac import HardwareNotAllowed, parse_status
from robofast_core.devices.aqawan_server import (
    CLOSED,
    CLOSING,
    ERROR,
    OPEN,
    OPENING,
    AqawanConfig,
    AqawanServer,
    aggregate,
)
from robofast_core.devices.aqawan_sim import AqawanSim
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import (
    FakeCamera,
    FakeObservingConditions,
    FakeSafetyMonitor,
    FakeTelescope,
    FakeWeather,
)
from robofast_core.testing.processes import ROOT, Service, SupervisorProcess

from .conftest import FAST_REMOTE, FAST_SAFETY, FAST_SEQ

NOT_LOOPBACK = "192.0.2.1"  # RFC 5737 documentation address; never contacted
TRAVEL = 0.3
PAC_FAST = dict(connect_timeout=1.0, command_timeout=2.0, resend_after=0.15, idle_end=0.1)


def server_config(sim: AqawanSim, **kw) -> AqawanConfig:
    base = {
        "name": "test",
        "listen": "127.0.0.1:0",
        "pac": {"host": "127.0.0.1", "port": sim.port, **PAC_FAST},
        "status_max_age": 0.1,
        "poll_interval": 0.05,
        "start_grace": 1.0,
        "shutter_open_timeout": 5.0,
        "request_timeout": 3.0,
    }
    return AqawanConfig.from_dict({**base, **kw})


@dataclass
class Rig:
    sim: AqawanSim
    server: AqawanServer
    owners: list[DeviceOwner] = field(default_factory=list)

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

    def opened_at(self, command: str) -> float:
        return next(t for t, c in self.sim.log if c == command)


@pytest.fixture
async def make_rig():
    rigs: list[Rig] = []

    def build(sim_kw: dict | None = None, **config) -> Rig:
        sim_kw = {"open_time": TRAVEL, "close_time": TRAVEL, "settle": 0.05, **(sim_kw or {})}
        sim = AqawanSim(**sim_kw).start()
        try:
            server = AqawanServer(server_config(sim, **config)).start()
        except BaseException:
            sim.stop()
            raise
        rig = Rig(sim, server)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        for o in rig.owners:
            o.close()
        await asyncio.sleep(0)
        rig.server.stop()
        rig.sim.stop()


async def until_status(dome, state: int, timeout: float) -> float:
    t0 = time.monotonic()
    while (s := await dome.get("ShutterStatus")) != state:
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"ShutterStatus {s}, wanted {state}")
        await asyncio.sleep(0.05)
    return time.monotonic() - t0


# -- the Dome members ---------------------------------------------------------------


async def test_open_and_close_in_the_legacy_order(make_rig):
    rig = make_rig({"heartbeat_timeout": 30.0}, open_order=[2, 1])
    dome = rig.dome()
    assert await dome.get("ShutterStatus") == CLOSED
    assert await dome.get("CanSetShutter") is True and await dome.get("CanSetAzimuth") is False
    # The PAC refuses to open until it has had a heartbeat; the caller sees why.
    with pytest.raises(DriverException, match="Heartbeat timer expired"):
        await dome.call("OpenShutter")
    assert await dome.call("Action", "Heartbeat", "") == "Success=TRUE"
    await dome.call("OpenShutter")
    assert await dome.get("ShutterStatus") == OPENING
    assert await dome.get("Slewing") is True
    await until_status(dome, OPEN, 5)
    assert rig.sim.open() and await dome.get("Slewing") is False
    cmds = rig.sim.commands()
    assert "LIGHTS_OFF" in cmds
    # Shutter 2 first, and shutter 1 only once shutter 2 had finished opening.
    assert cmds.index("OPEN_SHUTTER_2") < cmds.index("OPEN_SHUTTER_1")
    assert rig.opened_at("OPEN_SHUTTER_1") - rig.opened_at("OPEN_SHUTTER_2") >= TRAVEL
    assert "OPEN_SHUTTERS" not in cmds

    await dome.call("CloseShutter")
    assert await dome.get("ShutterStatus") == CLOSING
    await until_status(dome, CLOSED, 5)
    assert rig.sim.closed() and "CLOSE_SEQUENTIAL" in rig.sim.commands()


async def test_failed_first_shutter_stops_the_sequence(make_rig):
    rig = make_rig({"heartbeat_timeout": None, "open_time": 1.0}, open_order=[1, 2])
    dome = rig.dome()
    await dome.call("OpenShutter")
    await asyncio.sleep(0.3)
    rig.sim.set_fault("Estop active", clearable=False)  # shutter 1 stops part-way
    await until_status(dome, ERROR, 3)
    await asyncio.sleep(0.5)
    assert "OPEN_SHUTTER_2" not in rig.sim.commands()
    assert rig.sim.shutter_states() == ("STOPPED", "CLOSED")
    # The PAC refuses to move under an E-stop; the close reports its reason.
    with pytest.raises(DriverException, match="Estop active"):
        await dome.call("CloseShutter")
    rig.sim.set_fault(None)
    await dome.call("CloseShutter")
    await until_status(dome, CLOSED, 5)


@pytest.mark.parametrize(
    "inject, why",
    [
        (lambda s: s.set_fault("Estop active"), "Fault=True"),
        (lambda s: s.set_error("motor overcurrent"), "Error=True"),
        (lambda s: s.set_door("EntryDoor2", "OPEN"), "EntryDoor2 is OPEN"),
        (lambda s: s.set_op_mode("MANUAL"), "MANUAL"),
    ],
    ids=["fault", "error", "door", "manual"],
)
async def test_open_refused_by_interlocks(make_rig, inject, why):
    rig = make_rig({"heartbeat_timeout": None}, interlock_doors=["EntryDoor2"])
    inject(rig.sim)
    dome = rig.dome()
    with pytest.raises(InvalidOperationException, match=why):
        await dome.call("OpenShutter")
    assert not [c for c in rig.sim.commands() if c.startswith("OPEN")]


async def test_doors_are_not_interlocked_by_default(make_rig):
    """Door state does not matter for observing (JDE, 2026-10-31)."""
    assert AqawanConfig().interlock_doors == ()
    rig = make_rig({"heartbeat_timeout": None})
    rig.sim.set_door("EntryDoor1", "OPEN")
    rig.sim.set_door("PanelDoor", "OPEN")
    dome = rig.dome()
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)


async def test_close_cancels_an_open_sequence(make_rig):
    rig = make_rig({"heartbeat_timeout": None, "open_time": 0.6})
    dome = rig.dome()
    await dome.call("OpenShutter")
    await asyncio.sleep(0.2)
    await dome.call("CloseShutter")
    await until_status(dome, CLOSED, 5)
    await asyncio.sleep(1.0)  # the sequence must not carry on to shutter 2
    assert rig.sim.closed()
    cmds = rig.sim.commands()
    assert "OPEN_SHUTTER_2" not in cmds and cmds.count("CLOSE_SEQUENTIAL") == 1


async def test_abortslew_sends_stop(make_rig, caplog):
    caplog.set_level("INFO", logger="robofast_core.devices.aqawan_server")
    rig = make_rig({"heartbeat_timeout": None, "open_time": 1.0})
    dome = rig.dome()
    await dome.call("OpenShutter")
    await asyncio.sleep(0.2)
    await dome.call("AbortSlew")
    cmds = rig.sim.commands()
    assert "STOP" in cmds
    # STATUS is re-read after STOP, and the (unknown) raw string is logged.
    assert cmds[cmds.index("STOP") + 1] == "STATUS"
    assert rig.sim.shutter_states()[0] == "STOPPED"
    assert "after STOP: Shutter1='STOPPED'" in caplog.text
    assert "unrecognised shutter state Shutter1='STOPPED'" in caplog.text
    await until_status(dome, ERROR, 2)  # neither open nor closed
    # Logged once per distinct value, not on every poll.
    await asyncio.sleep(0.3)
    await dome.get("ShutterStatus")
    assert caplog.text.count("unrecognised shutter state") == 1


async def test_hung_pac_is_bounded(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    dome = rig.dome()
    assert await dome.get("ShutterStatus") == CLOSED
    await asyncio.sleep(0.2)
    rig.sim.set_mode("hang")
    for member in ("ShutterStatus", "CloseShutter"):
        t0 = time.monotonic()
        with pytest.raises(DriverException):
            if member == "ShutterStatus":
                await dome.get(member)
            else:
                await dome.call(member)
        assert time.monotonic() - t0 < 3.5, member
    rig.sim.set_mode("ok")
    assert await dome.get("ShutterStatus") == CLOSED


async def test_garbled_status_is_an_error_not_a_state(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    rig.sim.set_mode("garble")
    with pytest.raises(DriverException, match="missing"):
        await rig.dome().get("ShutterStatus")


async def test_actions_and_unimplemented_members(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    dome = rig.dome()
    actions = await dome.get("SupportedActions")
    assert "Heartbeat" in actions
    status = json.loads(await dome.call("Action", "Status", ""))
    assert status["shutter1"] == "CLOSED" and status["fault"] is False
    rig.sim.set_fault("Estop active")
    assert "Estop active" in await dome.call("Action", "GetFaults", "")
    for member in ("Azimuth", "Altitude"):
        with pytest.raises(NotImplementedException):
            await dome.get(member)


async def faults_text(dome, timeout: float = 2.0) -> dict:
    """The Faults action, once the PAC's fault text has been fetched."""
    t0 = time.monotonic()
    while True:
        f = json.loads(await dome.call("Action", "Faults", ""))
        if f["faults"] or time.monotonic() - t0 > timeout:
            return f
        await asyncio.sleep(0.05)


async def test_fault_is_a_separate_axis_from_position(make_rig):
    """JDE 2026-10-31: "closed and fault" and "open and fault" are different
    states; only the second is shutterError (so the supervisor closes)."""
    rig = make_rig({"heartbeat_timeout": None})
    dome = rig.dome()
    actions = await dome.get("SupportedActions")
    assert "Faults" in actions and "ClearFaults" in actions
    f = json.loads(await dome.call("Action", "Faults", ""))
    assert f == {
        "fault": False,
        "error": False,
        "faults": None,
        "errors": None,
        "shutters": ["CLOSED", "CLOSED"],
    }
    # Closed and fault: the position stays closed; the channel says fault.
    rig.sim.set_fault("Estop active")
    await asyncio.sleep(0.15)
    assert await dome.get("ShutterStatus") == CLOSED
    f = await faults_text(dome)
    assert f["fault"] is True and "Estop active" in f["faults"]
    assert f["shutters"] == ["CLOSED", "CLOSED"]
    with pytest.raises(InvalidOperationException, match="Fault=True"):
        await dome.call("OpenShutter")
    # Open and fault: shutterError; the close still goes through.
    rig.sim.set_fault(None)
    await asyncio.sleep(0.15)
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)
    rig.sim.set_fault("Motor overload", blocks_motion=False)
    await until_status(dome, ERROR, 2)
    f = await faults_text(dome)
    assert f["fault"] is True and f["shutters"] == ["OPEN", "OPEN"]
    assert "Motor overload" in f["faults"]
    await dome.call("CloseShutter")
    await until_status(dome, CLOSED, 5)  # closed (and still faulted)
    assert json.loads(await dome.call("Action", "Faults", ""))["fault"] is True


async def test_clear_faults_is_privileged_and_needs_the_enclosure_closed(make_rig, caplog):
    caplog.set_level("INFO", logger="robofast_core.devices.aqawan_server")
    rig = make_rig({"heartbeat_timeout": None})
    owner = rig.owner()
    safety = owner.grant("safety", privileged=True)
    sequencer = owner.grant("sequencer")
    with pytest.raises(NotAuthorized):
        await sequencer.call("Action", "ClearFaults", "sneaky")
    await safety.call("OpenShutter")
    await until_status(safety, OPEN, 5)
    rig.sim.set_fault("Motor overload", blocks_motion=False)
    with pytest.raises(InvalidOperationException, match="not closed"):
        await safety.call("Action", "ClearFaults", "jde: testing")
    assert "CLEAR_FAULTS" not in rig.sim.commands()
    await safety.call("CloseShutter")
    await until_status(safety, CLOSED, 5)
    reply = await safety.call("Action", "ClearFaults", "jde: testing")
    assert "Success=TRUE" in reply and rig.sim.fault is None
    cmds = rig.sim.commands()
    assert cmds.index("CLEAR_FAULTS") < cmds.index("CLEAR_ERRORS")
    assert "ClearFaults requested (ClientID" in caplog.text and "jde: testing" in caplog.text
    # A latched fault (an E-stop) is refused by the PAC, and the caller sees why.
    rig.sim.set_fault("Estop active", clearable=False)
    with pytest.raises(DriverException, match="Estop active"):
        await safety.call("Action", "ClearFaults", "jde: again")
    assert rig.sim.fault == "Estop active"


async def test_contrary_motion_after_a_close_shows_at_once(make_rig):
    """Once the PAC has acknowledged a close, ShutterStatus is the PAC's
    report again, so a late duplicate OPEN (a re-send executed after the
    close) shows as opening instead of hiding behind "closing"."""
    rig = make_rig({"heartbeat_timeout": None, "open_time": 1.0, "close_time": 1.0})
    dome = rig.dome()
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)
    await dome.call("CloseShutter")
    await wait_until(lambda: "CLOSING" in rig.sim.shutter_states(), 1, what="closing")
    await asyncio.sleep(0.15)
    assert await dome.get("ShutterStatus") == CLOSING  # acknowledged
    rig.sim.execute("OPEN_SHUTTER_1")  # the late duplicate
    await asyncio.sleep(0.15)
    assert await dome.get("ShutterStatus") == OPENING  # within start_grace


def test_shipped_configs():
    """The heartbeat timeout is 60 s (JDE 2026-10-31); the site config pings
    four times per timeout or more; doors are not interlocked."""
    assert AqawanSim().heartbeat_timeout == 60.0
    site = json.loads((ROOT / "sites/minerva/aqawan1.json").read_text())
    scfg = SafetyConfig(**site["safety"])
    assert scfg.heartbeat_timeout == 60 and scfg.heartbeat_interval <= 15
    assert scfg.fault_action == "Faults"
    deploy = json.loads((ROOT / "deploy/aqawan.example.json").read_text())
    assert AqawanConfig.from_dict(deploy).interlock_doors == ()


async def test_connection_is_per_client(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    await asyncio.to_thread(_per_client_checks, f"http://{rig.server.address}/api/v1/dome/0/")


def _per_client_checks(base: str) -> None:
    def call(method: str, member: str, client: int, **params: str) -> dict:
        args = {"ClientID": str(client), "ClientTransactionID": "7", **params}
        kw = {"params": args} if method == "GET" else {"data": args}
        r = httpx.request(method, base + member, trust_env=False, timeout=5, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    assert call("GET", "shutterstatus", 1)["ErrorNumber"] == 0x407
    assert call("PUT", "connected", 1, Connected="True")["ErrorNumber"] == 0
    assert call("PUT", "connected", 2, Connected="True")["ErrorNumber"] == 0
    assert call("PUT", "connected", 2, Connected="False")["ErrorNumber"] == 0
    r = call("GET", "connected", 1)
    assert r["Value"] is True and r["ClientTransactionID"] == 7
    assert call("GET", "connected", 2)["Value"] is False
    assert call("GET", "shutterstatus", 1)["Value"] == CLOSED
    # Alpaca details: lower-case URLs, case-sensitive PUT names, bad values.
    bad = httpx.get(base.replace("dome", "Dome") + "name", trust_env=False, timeout=5)
    assert bad.status_code == 400
    r = httpx.put(base + "connected", data={"connected": "True"}, trust_env=False, timeout=5)
    assert r.status_code == 400
    r = httpx.put(base + "connected", data={"Connected": "yes"}, trust_env=False, timeout=5)
    assert r.status_code == 400


async def test_not_connected_is_reported(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    d = AlpacaDevice(rig.server.address, "dome")
    try:
        with pytest.raises(NotConnectedException):
            await d.get("ShutterStatus", timeout=3)
    finally:
        await d.aclose()


async def test_management_api_and_discovery(make_rig):
    rig = make_rig({"heartbeat_timeout": None}, discovery_port=0)
    await asyncio.to_thread(_management_checks, rig.server)


def _management_checks(server: AqawanServer) -> None:
    base = f"http://{server.address}/management"
    r = httpx.get(base + "/v1/configureddevices", trust_env=False, timeout=5).json()
    assert r["Value"][0]["DeviceType"] == "Dome" and r["Value"][0]["DeviceNumber"] == 0
    assert httpx.get(base + "/apiversions", trust_env=False, timeout=5).json()["Value"] == [1]
    assert server._discovery is not None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(2)
        s.sendto(b"alpacadiscovery1", ("127.0.0.1", server._discovery.port))
        reply = json.loads(s.recv(1024))
    assert reply == {"AlpacaPort": server.server_address[1]}


def _status(**changes: str):
    pairs = dict(p.split("=", 1) for p in AqawanSim().status_text(0.0).split(", "))
    pairs.update(changes)
    return parse_status(", ".join(f"{k}={v}" for k, v in pairs.items()))


@pytest.mark.parametrize(
    "changes, sequence, failed, expected",
    [
        ({}, False, None, CLOSED),
        ({"Fault": "TRUE"}, False, None, CLOSED),  # closed wins
        ({"Shutter1": "OPEN", "Shutter2": "OPEN"}, False, None, OPEN),
        ({"Shutter1": "OPEN", "Shutter2": "OPEN", "Fault": "TRUE"}, False, None, ERROR),
        ({"Shutter1": "OPEN", "Shutter2": "OPEN", "Error": "TRUE"}, False, None, ERROR),
        ({"Shutter1": "OPENING"}, False, None, OPENING),
        ({"Shutter1": "OPEN"}, True, None, OPENING),  # between the two shutters
        ({"Shutter1": "OPEN"}, False, None, ERROR),  # half open, nothing running
        ({"Shutter1": "OPEN"}, False, "refused", ERROR),
        ({"Shutter1": "OPEN", "Shutter2": "CLOSING"}, False, None, CLOSING),
        ({"Shutter1": "CLOSING", "Fault": "TRUE"}, False, None, CLOSING),
        ({"Shutter1": "STOPPED"}, False, None, ERROR),
        ({"Shutter2": "PARTIAL"}, True, None, ERROR),
    ],
)
def test_shutter_status_aggregation(changes, sequence, failed, expected):
    assert aggregate(_status(**changes), sequence=sequence, failed=failed) == expected


# -- safety ----------------------------------------------------------------------------


def test_server_refuses_a_non_loopback_pac(tmp_path):
    sim = AqawanSim()  # not started: only for its port number default
    for allow, hardware in ((False, False), (True, False), (False, True)):
        cfg = server_config(sim, allow_hardware=allow)
        cfg.pac.host = NOT_LOOPBACK
        with pytest.raises(HardwareNotAllowed):
            AqawanServer(cfg, hardware=hardware)
    # The CLI exits non-zero before listening.
    path = tmp_path / "aqawan.json"
    path.write_text(json.dumps({"pac": {"host": NOT_LOOPBACK, "port": 22004}}))
    proc = subprocess.run(
        [sys.executable, "-m", "robofast_core.devices.aqawan_server", "--config", str(path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2 and "not loopback" in proc.stderr
    with pytest.raises(ValueError, match="unknown config keys"):
        AqawanConfig.from_dict({"allow_hardwar": True})


async def test_heartbeat_stop_closes(make_rig):
    hb = 1.0
    rig = make_rig({"heartbeat_timeout": hb})
    dome = rig.dome()
    await dome.call("Action", "Heartbeat", "")
    await dome.call("OpenShutter")
    pinger_stop = asyncio.Event()

    async def pinger() -> None:
        while not pinger_stop.is_set():
            await dome.call("Action", "Heartbeat", "")
            await asyncio.sleep(0.2)

    task = asyncio.create_task(pinger())
    await until_status(dome, OPEN, 5)
    pinger_stop.set()
    await task
    t0 = time.monotonic()
    await wait_until(rig.sim.closed, hb + 2 * TRAVEL + 1.0, what="PAC closes itself")
    assert rig.sim.watchdog_trips == 1
    assert time.monotonic() - t0 >= 2 * TRAVEL  # it closed by itself, after the timeout
    assert await dome.get("ShutterStatus") == CLOSED
    with pytest.raises(DriverException, match="Heartbeat timer expired"):
        await dome.call("OpenShutter")


SUP_SAFETY = {
    **FAST_SAFETY,
    "dome_timeout": 4.0,
    "shutter_timeout": 10.0,
    "heartbeat_interval": 0.2,
    "heartbeat_timeout": 1.5,  # the shortest PAC timeout used below
    "heartbeat_max_loop_age": 1.0,
}


async def test_supervisor_closes_on_unsafe_weather(make_rig):
    rig = make_rig({"heartbeat_timeout": 2.0}, open_order=[2, 1])
    tl = Timeline()
    weather = FakeWeather(safe=True)
    sm = DeviceOwner("safetymonitor", lambda: FakeSafetyMonitor(weather), tl, default_timeout=0.5)
    dome = alpaca_owner(
        "dome",
        "dome",
        rig.server.address,
        tl,
        default_timeout=4.0,
        privileged_members=ENCLOSURE_MEMBERS,
    )
    rig.owners.append(dome)
    sup = SafetySupervisor(
        "safety",
        dome=dome,
        safety_monitor=sm,
        heartbeat=dome,
        timeline=tl,
        config=SafetyConfig(**SUP_SAFETY),
    )
    run = asyncio.create_task(sup.run())
    try:
        lease = await asyncio.wait_for(sup.request_open("tester"), 10)

        async def renew() -> None:
            while True:
                lease.renew()
                await asyncio.sleep(0.2)

        renewer = asyncio.create_task(renew())
        await asyncio.wait_for(lease.wait_open(), 15)
        assert rig.sim.open()
        weather.set_safe(False)
        t0 = time.monotonic()
        await wait_until(rig.sim.closed, 5, what="closed on bad weather")
        dt = time.monotonic() - t0
        renewer.cancel()
        assert rig.sim.watchdog_trips == 0  # the supervisor closed it, not the PAC
        assert "CLOSE_SEQUENTIAL" in rig.sim.commands()
        assert lease.revoked.is_set()
        print(f"unsafe weather: Aqawan sim closed {dt:.2f} s later")
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@dataclass
class SupRig:
    sup: SafetySupervisor
    run: asyncio.Task
    timeline: Timeline

    async def stop(self) -> None:
        self.run.cancel()
        await asyncio.gather(self.run, return_exceptions=True)


def start_supervisor(rig: Rig, **safety) -> SupRig:
    tl = Timeline()
    weather = FakeWeather(safe=True)
    sm = DeviceOwner("safetymonitor", lambda: FakeSafetyMonitor(weather), tl, default_timeout=0.5)
    dome = alpaca_owner(
        "dome",
        "dome",
        rig.server.address,
        tl,
        default_timeout=4.0,
        privileged_members=ENCLOSURE_MEMBERS,
    )
    rig.owners.append(dome)
    sup = SafetySupervisor(
        "safety",
        dome=dome,
        safety_monitor=sm,
        heartbeat=dome,
        timeline=tl,
        config=SafetyConfig(**{**SUP_SAFETY, "fault_action": "Faults", **safety}),
    )
    return SupRig(sup, asyncio.create_task(sup.run()), tl)


def close_commands(sim: AqawanSim) -> int:
    return sum(c.startswith("CLOSE") for c in sim.commands())


async def test_supervisor_open_and_fault_alarms_and_closes(make_rig):
    rig = make_rig({"heartbeat_timeout": 2.0})
    s = start_supervisor(rig)
    try:
        lease = await asyncio.wait_for(s.sup.request_open("tester"), 10)

        async def renew() -> None:
            while True:
                lease.renew()
                await asyncio.sleep(0.2)

        renewer = asyncio.create_task(renew())
        await asyncio.wait_for(lease.wait_open(), 15)
        assert s.sup.fault is None and rig.sim.open()
        # A fault that still lets the PAC close: the weather is fine and the
        # lease is renewed, yet the supervisor closes.
        rig.sim.set_fault("Motor overload", blocks_motion=False)
        dt = await wait_until(rig.sim.closed, 5, what="closed on a fault")
        renewer.cancel()
        assert lease.revoked.is_set() and "Fault" in (lease.revoke_reason or "")
        assert any(a.startswith("enclosure reports Fault") for a in s.sup.alarms)
        assert any("FAULT WITH ENCLOSURE NOT CLOSED" in a for a in s.sup.alarms)
        await wait_until(lambda: "Motor overload" in (s.sup.fault or ""), 2, what="fault text")
        assert rig.sim.watchdog_trips == 0
        print(f"open and fault: Aqawan sim closed {dt:.2f} s later")
        # Closed and still faulted: nothing reopens, and the close is not re-sent.
        closes = close_commands(rig.sim)
        await asyncio.sleep(1.0)
        assert rig.sim.closed() and close_commands(rig.sim) == closes
        assert not s.sup.open_allowed()
        # An operator clears it (the enclosure is closed); opening is allowed again.
        assert "Success=TRUE" in await s.sup.clear_faults("jde", "tested the motor")
        await wait_until(lambda: s.sup.fault is None, 3, what="fault cleared")
        assert s.timeline.events("faults_cleared") and s.timeline.events("enclosure_fault_cleared")
        lease.release()
        await wait_until(s.sup.open_allowed, 3, what="open allowed")
    finally:
        await s.stop()


async def test_supervisor_fault_while_closed_only_alarms(make_rig):
    rig = make_rig({"heartbeat_timeout": 2.0})
    rig.sim.set_fault("Estop active", clearable=False)
    s = start_supervisor(rig)
    server = SupervisorServer(s.sup)
    try:
        await wait_until(lambda: (s.sup.fault or "").startswith("enclosure reports"), 3)
        await asyncio.sleep(1.0)  # many enclosure passes
        fault_alarms = [a for a in s.sup.alarms if "Fault" in a]
        assert len(fault_alarms) == 1, s.sup.alarms  # once, not every poll
        assert not any("NOT CLOSED" in a for a in s.sup.alarms)
        assert close_commands(rig.sim) == 0 and rig.sim.closed()
        assert s.sup.status()["fault"] == s.sup.fault and not s.sup.open_allowed()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(s.sup.request_open("tester"), 0.5)
        # The operator API: a name is required; a refusal is a 409.
        code, body = await server._respond(
            "POST", "/faults/clear", b'{"operator": " ", "reason": "x"}'
        )
        assert code == 400
        req = json.dumps({"operator": "jde", "reason": "e-stop reset"}).encode()
        code, body = await server._respond("POST", "/faults/clear", req)
        assert code == 409 and "Estop active" in body["error"]
        rig.sim.fault_clearable = True
        code, body = await server._respond("POST", "/faults/clear", req)
        assert code == 200 and "Success=TRUE" in body["reply"]
        await wait_until(lambda: s.sup.fault is None, 3, what="fault cleared")
    finally:
        await s.stop()


async def test_supervisor_unreadable_faults_count_as_a_fault(make_rig):
    rig = make_rig({"heartbeat_timeout": 2.0})
    s = start_supervisor(rig, fault_action="NoSuchAction")
    try:
        await wait_until(lambda: (s.sup.fault or "").startswith("cannot read"), 3)
        await asyncio.sleep(0.5)
        assert sum("cannot read enclosure faults" in a for a in s.sup.alarms) == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(s.sup.request_open("tester"), 0.5)
        assert not any(c.startswith("OPEN") for c in rig.sim.commands())
    finally:
        await s.stop()


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGSTOP], ids=["killed", "hung"])
async def test_supervisor_dies_aqawan_closes_itself(make_rig, tmp_path, sig):
    hb = 1.5
    rig = make_rig({"heartbeat_timeout": hb})
    weather = FakeWeather(safe=True)
    tel, cam = FakeTelescope(), FakeCamera()
    fakes = FakeAlpacaServer(
        {
            "safetymonitor": FakeSafetyMonitor(weather),
            "observingconditions": FakeObservingConditions(weather),
        }
    ).start()
    sup: Service | None = None
    remote: RemoteSupervisor | None = None
    seq_task: asyncio.Task | None = None
    try:
        sup = SupervisorProcess(
            {
                "listen": "127.0.0.1:0",
                "devices": {
                    "dome": rig.server.address,
                    "safetymonitor": fakes.address,
                    "observingconditions": fakes.address,
                },
                "safety": SUP_SAFETY,
            },
            Path(tmp_path),
        )
        tl = Timeline()
        remote = RemoteSupervisor(sup.url, **FAST_REMOTE)
        seq = Sequencer(
            "seq",
            supervisor=remote,
            telescope=DeviceOwner("telescope", lambda: tel, tl, default_timeout=0.5),
            camera=DeviceOwner("camera", lambda: cam, tl, default_timeout=0.5),
            plan=lambda: Observation("target", exptime=30.0, count=1000),
            timeline=tl,
            config=SequencerConfig(**FAST_SEQ),
        )
        seq_task = asyncio.create_task(seq.run())
        await wait_until(lambda: rig.sim.open() and cam.CameraState == 2, 20, what="observing")
        t0 = time.monotonic()
        os.kill(sup.pid, sig)
        dt = await wait_until(rig.sim.closed, hb + 2 * TRAVEL + 2.0, what="PAC closes itself")
        assert rig.sim.watchdog_trips == 1
        print(f"supervisor {signal.Signals(sig).name}: Aqawan sim closed itself {dt:.2f} s later")
        assert time.monotonic() - t0 < hb + 2 * TRAVEL + 2.0
    finally:
        if seq_task is not None:
            seq_task.cancel()
            await asyncio.gather(seq_task, return_exceptions=True)
        if remote is not None:
            await remote.aclose()
        if sup is not None:
            sup.stop()
        fakes.stop()
