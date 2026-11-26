"""The recovery ladder's power-cycle rung on MINERVA's PDUs (docs/recovery.md,
"Power-cycling the mount"; JDE 2026-11-10: panel and inst on T1 and T2).

T2, 2026-11-09: the mount controller dropped off the network; reconnecting,
restarting PWI2 and homing could not bring it back, and a power cycle of
the ``panel`` outlet did. Here that night on simulators: a PWI2 simulator
whose controller drops, robofast's PWI server over it, and the PDU server
with ``t2_pdu.json``'s outlets on the APC simulator, whose ``panel`` reboot
power-cycles the simulated controller. Also the ``inst`` cycle of a wedged
camera (the camera alone), the guards, and the MINERVA example policies.

Fast tier: everything in this process, on loopback."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from robofast.devices.apc_pdu import REBOOT, APCPDUSim, Credentials
from robofast.devices.pdu_server import PDUConfig, PDUServer
from robofast.devices.pwi_server import PWIServer
from robofast.devices.pwi_sim import PWISim
from robofast.recovery import PDUSwitch, RecoveryLadder
from robofast.testing.fakes import FakeDeviceError
from tests.core.test_pwi_recovery import PORT1, no_sleep, pwi2_config, restart_hook, telescope

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "sites/minerva/remote"
PANEL, INST = 2, 3  # the APC outlets (t1_pdu.json, t2_pdu.json)


def _json(name: str) -> dict:
    return json.loads((REMOTE / name).read_text())


@pytest.fixture
async def rig():
    """A PWI2 simulator (ready, homed, parked at the server's park position)
    and the PWI server over it; the APC simulator with T2's outlets and the
    PDU server with t2_pdu.json's outlet map. A reboot of ``panel``
    power-cycles the simulated mount controller."""
    made: list = []
    sim = PWISim(
        "pwi2", slew_rate=20.0, home_time=0.3, ready=True, homed=True, model_file=PORT1
    ).start()
    made.append(sim)
    server = PWIServer(pwi2_config(sim.port)).start()
    made.append(server)
    switched: list[tuple[int, int]] = []

    def on_set(outlet: int, value: int) -> None:
        switched.append((outlet, value))
        if outlet == PANEL and value == REBOOT:
            with sim.model.lock:
                sim.model.power_cycle()

    creds = Credentials("r", "w")
    names = [o["name"] for o in _json("t2_pdu.json")["outlets"]] + ["empty8"]
    pdu_sim = await APCPDUSim(creds, names, reboot_delay=0.05, on_set=on_set).start()
    d = _json("t2_pdu.json")
    d.update(
        listen="127.0.0.1:0",
        pdu={"host": "127.0.0.1", "port": pdu_sim.port, "timeout": 0.5},
        allow_hardware=False,
    )
    d.pop("credentials")
    pdu = PDUServer(PDUConfig.from_dict(d), credentials=creds).start()
    made.append(pdu)
    owner, grant = telescope(server)
    await grant.call("Park")
    for _ in range(200):
        if await grant.get("AtPark"):
            break
        await asyncio.sleep(0.05)
    assert await grant.get("AtPark")
    yield sim, server, pdu, switched, owner, grant
    pdu_sim.close()
    for x in reversed(made):
        x.stop()


def _policy(sim: PWISim, pdu: PDUServer, **kw) -> dict:
    """T2's telescope ladder (sites/minerva/remote/t2_recovery.example.json)
    on the simulators: restart and pwi_start restart the PWI2 simulator (a
    fresh PWI: not connected, not homed; a dropped controller stays
    dropped), pwi_stop does nothing."""
    return {
        "steps": ["reconnect", "restart", "home", "powercycle"],
        "restart": restart_hook(sim),
        "powercycle": {
            "pdu": pdu.address,
            "switch": 1,  # panel
            "boot_time": 0.1,
            "requires_pwi_stop": True,
            "pwi_stop": {"command": [sys.executable, "-c", "print('PWI stopped')"]},
            "pwi_start": restart_hook(sim),
        },
        "verify_timeout": 1.0,
        "step_timeout": 20.0,
        "home_timeout": 30.0,
        "mount_connect_timeout": 2.0,
        **kw,
    }


async def camera_idle(device: str, member: str):
    return {"CameraState": 0, "CCDTemperature": -20.0}[member]


def _ladder(policies: dict, events: list | None = None, alarms: list | None = None):
    return RecoveryLadder.from_config(
        "t2",
        policies,
        pdu=lambda address: PDUSwitch(address, timeout=5),
        sleep=no_sleep,
        event=lambda what, **kw: events.append((what, kw)) if events is not None else None,
        alarm=alarms.append if alarms is not None else None,
    )


async def test_a_dropped_controller_comes_back_by_the_panel_cycle_and_a_home(rig):
    """T2, 2026-11-09: reconnect, restart and home fail; the panel cycle
    (PWI stopped first, started after) brings the controller back, PWI
    reconnects to the mount, the mount is homed: recovered."""
    sim, _server, pdu, switched, owner, grant = rig
    m = sim.model
    with m.lock:
        m.drop_controller()
    events: list = []
    lad = _ladder({"telescope": _policy(sim, pdu)}, events)
    res = await lad.recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert res.recovered, res.tried
    assert [r for r, _ in res.tried] == ["reconnect", "restart", "home", "powercycle"]
    assert [o.split(":")[0] for _, o in res.tried] == ["failed", "failed", "failed", "ok"]
    # The home rung waited for PWI to connect to the dropped controller, in vain.
    assert "did not connect to the mount" in res.tried[2][1]
    # Only the panel was switched, once.
    assert switched == [(PANEL, REBOOT)]
    assert m.connected and all(m.enabled) and m.homed and not m.controller_down
    whats = [w for w, _ in events]
    i = whats.index("recovery_powercycle")
    assert whats.index("recovery_pwi_stop") < i < whats.index("recovery_pwi_start")
    after = whats[i:]
    assert after.index("recovery_mount_connect") < after.index("recovery_motors_enable")
    assert after.index("recovery_motors_enable") < after.index("recovery_home")
    # The home came after the cycle, on the real (simulated) controller.
    cmds = [c.split("&")[0] for c in m.commands]
    last_connect = len(cmds) - 1 - cmds[::-1].index("mount:connect")
    assert cmds.index("mount:findhome", last_connect) > last_connect
    status = json.loads(await grant.call("Action", "Status", ""))
    assert status["homed"] is True and status["pwi"]["connected"] is True


async def test_the_guards_hold_an_unparked_mount_is_not_cycled(rig):
    sim, _server, pdu, switched, owner, grant = rig
    await grant.call("Unpark")
    with sim.model.lock:
        sim.model.drop_controller()
    res = await _ladder({"telescope": _policy(sim, pdu, steps=["powercycle"])}).recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert not res.recovered and "not parked" in res.tried[0][1]
    assert switched == [] and sim.model.controller_down


async def test_a_panel_cycle_without_home_permission_does_not_count(rig):
    """A mount whose controller lost power must be homed before it observes:
    a PWI server that may not home fails the rung, even with no home step."""
    sim, server, pdu, switched, owner, grant = rig
    server.mount.config.allow_find_home = False
    with sim.model.lock:
        sim.model.drop_controller()
    res = await _ladder({"telescope": _policy(sim, pdu, steps=["powercycle"])}).recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert not res.recovered and "cannot be homed" in res.tried[0][1], res.tried
    assert switched == [(PANEL, REBOOT)]


async def test_a_wedged_camera_comes_back_by_its_inst_cycle_alone(rig):
    """T2's Apogee ("Camera Init error"): reconnect fails, the inst cycle
    brings it back; the mount and PWI are not touched."""
    sim, _server, pdu, switched, _owner, grant = rig
    reconnects = []

    async def probe(member: str):
        if (INST, REBOOT) not in switched:
            raise FakeDeviceError("Camera Init error")
        return {"CameraState": 0, "CCDTemperature": 15.0}[member]

    async def others(device: str, member: str):
        return await grant.get(member)  # the telescope: AtPark

    policy = {
        "steps": ["reconnect", "powercycle"],
        "powercycle": {"pdu": pdu.address, "switch": 2, "boot_time": 0.1},
        "camera_warm_above": -5,
        "verify_timeout": 0.5,
    }
    n = len(sim.model.commands)
    res = await _ladder({"camera": policy}).recover(
        "camera", reconnect=lambda: reconnects.append(1), probe=probe, others=others
    )
    assert res.recovered and [r for r, _ in res.tried] == ["reconnect", "powercycle"]
    assert switched == [(INST, REBOOT)]
    assert sim.model.commands[n:] == [] and sim.model.connected and sim.model.homed


# -- the site's configs ----------------------------------------------------------------------


@pytest.mark.parametrize("t", ["t1", "t2"])
def test_minerva_recovery_examples(t):
    """telescope: reconnect, restart, home, powercycle(panel, PWI stopped
    for it); camera: reconnect, powercycle(inst); both on that telescope's
    PDU server; only panel and inst are cyclable there."""
    from robofast.testing.mount_guard import Endpoint
    from robofast.testing.rehearsal import RealTelescope, RehearsalSettings

    pdu = _json(f"{t}_pdu.json")
    policy = _json(f"{t}_recovery.example.json")
    lad = RecoveryLadder.from_config(t, policy)
    tel, cam = lad.policies["telescope"], lad.policies["camera"]
    assert tel.steps == ["reconnect", "restart", "home", "powercycle"]
    assert cam.steps == ["reconnect", "powercycle"]
    for pol, name in ((tel, "panel"), (cam, "inst")):
        assert pol.powercycle["pdu"] == pdu["listen"]
        outlet = pdu["outlets"][pol.powercycle["switch"]]
        assert outlet["name"] == name and outlet["cycle"] is True
        assert pol.requires_parked and pol.max_powercycles == 1
    assert tel.powercycle["requires_pwi_stop"]
    assert not cam.powercycle.get("requires_pwi_stop")
    assert cam.camera_warm_above is not None
    # The rehearsal takes it as a --recovery policy.
    ep = Endpoint("127.0.0.1", 1, 0)
    RehearsalSettings(real={t: RealTelescope(ep, ep, ep, None)}, recovery={t: policy})


def test_t1_pdu_config_is_refused_without_hardware():
    from robofast import hwgate
    from robofast.devices.apc_pdu import Credentials
    from robofast.testing.tunnel import FakeTunnel

    d = _json("t1_pdu.json")
    assert d["listen"] == "127.0.0.1:11131"
    assert d["pdu"]["host"] == "127.0.0.1" and d["pdu"]["port"] == 7161 and d["pdu"]["relay"]
    assert d["credentials"] == "~/.config/robofast/pdu-t1.credentials.json"
    assert d["min_cycle_interval"] == _json("t2_pdu.json")["min_cycle_interval"]
    assert [o["name"] for o in d["outlets"]] == [
        "T1",
        "panel",
        "inst",
        "A1 South Heater",
        "fiber box",
        "switch",
        "empty7",
        "monitor",
    ]
    assert {o["name"] for o in d["outlets"] if o.get("writable")} == {"panel", "inst"}
    assert {o["name"] for o in d["outlets"] if o.get("cycle")} == {"panel", "inst"}
    with FakeTunnel() as tunnel:
        d["pdu"] = {**d["pdu"], "port": tunnel.port}
        d["listen"] = "127.0.0.1:0"
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            PDUServer(PDUConfig.from_dict(d), credentials=Credentials("r", "w"))
        assert not tunnel.touched


async def test_regression_20261126_the_ladder_waits_for_pwi_to_connect_after_the_panel_cycle(rig):
    """MINERVA T3/T4 2026-11-26 (and T2 2026-11-25): after the panel power
    cycle PWI answered MountConnect at once but reported the mount connected
    only later (the controller was still booting); the ladder sent
    MotorsEnable 5 s after MountConnect, got "PWI is not connected" and gave
    up, though the mount was connected minutes later. Now it waits for PWI
    to report the mount connected (and the axes enabled), then homes."""
    sim, _server, pdu, switched, owner, grant = rig
    m = sim.model
    with m.lock:
        m.drop_controller()
        m.connect_delay = 1.5
    events: list = []
    lad = _ladder({"telescope": _policy(sim, pdu, mount_connect_timeout=20.0)}, events)
    res = await lad.recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert res.recovered, res.tried
    assert [r for r, _ in res.tried][-1] == "powercycle"
    assert res.tried[-1][1] == "ok"
    assert switched == [(PANEL, REBOOT)]
    assert m.connected and all(m.enabled) and m.homed and not m.controller_down
    whats = [w for w, _ in events]
    i = whats.index("recovery_powercycle")
    after = whats[i:]
    assert after.index("recovery_mount_connect") < after.index("recovery_motors_enable")
    assert after.index("recovery_motors_enable") < after.index("recovery_home")
    # MotorsEnable was sent only once PWI reported the mount connected.
    assert "enable" not in m.ignored


async def test_the_wait_for_pwi_to_connect_is_bounded(rig):
    """A mount PWI never connects to (the controller stays down): each rung's
    wait ends after mount_connect_timeout with that reason, not with
    MotorsEnable's "PWI is not connected", and the ladder climbs on."""
    sim, _server, pdu, switched, owner, grant = rig
    m = sim.model
    with m.lock:
        m.drop_controller()
    lad = _ladder(
        {"telescope": _policy(sim, pdu, steps=["reconnect", "restart"], mount_connect_timeout=1.0)}
    )
    res = await lad.recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert not res.recovered
    assert all("did not connect to the mount within 1 s" in how for _, how in res.tried), res.tried
    assert switched == []


def _az_fault(m) -> None:
    """MINERVA T3, 2026-11-26: during a home PWI2 disabled the azimuth axis
    ("Automatic commutation: Too many trials during phase 3"); the mount was
    left connected, unhomed, unparked, altitude still enabled."""
    with m.lock:
        m.enabled[0] = False
        m.stop()
        m.motor_errors["azm"] = (1, "Automatic commutation: Too many trials during phase 3")
        m.axes_homed = [False, False]


async def test_regression_20261126_a_mount_stopped_on_a_drive_fault_gets_its_panel_cycle(rig):
    """The panel cycle (which clears a drive fault) was refused because the
    faulted mount was not parked. A mount PWI reports connected, with an
    axis disabled, and that holds still once stopped is cycled, then homed."""
    sim, _server, pdu, switched, owner, grant = rig
    await grant.call("Unpark")
    m = sim.model
    _az_fault(m)
    events: list = []
    lad = _ladder({"telescope": _policy(sim, pdu, steps=["powercycle"])}, events)
    res = await lad.recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=lambda mem, *a: grant.call(mem, *a),
    )
    assert res.recovered, res.tried
    assert switched == [(PANEL, REBOOT)]
    assert m.connected and all(m.enabled) and m.homed and not m.motor_errors
    (why,) = [kw["reason"] for w, kw in events if w == "recovery_cycle_unparked"]
    assert "az axis disabled" in why and "phase 3" in why


async def test_a_faulted_mount_that_still_moves_is_not_cycled(rig):
    """The exception needs the mount still: one that keeps tracking after
    the ladder's AbortSlew (ignored here) with an axis disabled keeps the
    parked guard."""
    sim, _server, pdu, switched, owner, grant = rig
    await grant.call("Unpark")
    m = sim.model
    _az_fault(m)
    with m.lock:
        m.tracking = True
    sent: list = []

    async def call(mem, *a):
        sent.append(mem)
        if mem == "AbortSlew":
            return None
        return await grant.call(mem, *a)

    res = await _ladder({"telescope": _policy(sim, pdu, steps=["powercycle"])}).recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda mem: grant.get(mem),
        others=camera_idle,
        call=call,
    )
    assert "AbortSlew" in sent
    assert not res.recovered and "not parked" in res.tried[0][1]
    assert switched == []
