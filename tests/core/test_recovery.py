"""The recovery ladder (robofast_core/recovery.py, docs/recovery.md): rungs,
escalation, the power-cycle guards, bounds, and the sequencer using it --
with fakes and, for the power cycle, the PDU server on its simulator."""

from __future__ import annotations

import asyncio
import sys

import pytest

from robofast_core.devices.apc_pdu import APCPDUSim, Credentials
from robofast_core.devices.pdu_server import PDUConfig, PDUServer
from robofast_core.recovery import DevicePolicy, PDUSwitch, RecoveryLadder
from robofast_core.sequencer_service import snapshot
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import FakeCamera, FakeDeviceError

OK_HOOK = {"command": [sys.executable, "-c", "print('restarted')"]}


class Device:
    """A device that fails until ``fixed_by`` happens."""

    def __init__(self, fixed_by: str | None = None, **state) -> None:
        self.fixed_by = fixed_by
        self.broken = True
        self.reconnects = 0
        self.state = {"AtPark": True, "CameraState": 0, "CCDTemperature": 10.0, **state}

    def event(self, what: str) -> None:
        if what == self.fixed_by:
            self.broken = False

    def reconnect(self) -> None:
        self.reconnects += 1
        self.event("reconnect")

    async def probe(self, member: str):
        if self.broken:
            raise FakeDeviceError("no answer")
        return self.state.get(member, 0)


class FakePDU:
    def __init__(self, device: Device | None = None) -> None:
        self.cycled: list[int] = []
        self.device = device

    def __call__(self, address: str) -> FakePDU:
        return self

    async def cycle(self, switch: int) -> None:
        self.cycled.append(switch)
        if self.device is not None:
            self.device.event("powercycle")


async def no_sleep(s: float) -> None:
    await asyncio.sleep(0)


def ladder(policy: dict, *, pdu=None, hook=None, clock=None, alarms=None):
    kw = {"pdu": pdu or FakePDU(), "sleep": no_sleep}
    if hook is not None:
        kw["hook"] = hook
    if clock is not None:
        kw["clock"] = clock
    return RecoveryLadder(
        "t1",
        {"camera": DevicePolicy.from_dict(policy)},
        alarm=(alarms.append if alarms is not None else None),
        **kw,
    )


FULL = {
    "steps": ["reconnect", "restart", "powercycle"],
    "restart": OK_HOOK,
    "powercycle": {"pdu": "127.0.0.1:1", "switch": 0, "boot_time": 1},
    "verify_timeout": 0.05,
    "step_timeout": 10,
}


async def test_reconnect_is_enough():
    dev = Device(fixed_by="reconnect")
    alarms: list[str] = []
    r = await ladder(FULL, alarms=alarms).recover(
        "camera", reconnect=dev.reconnect, probe=dev.probe
    )
    assert r.recovered and r.tried == [("reconnect", "ok")] and not r.exhausted
    assert alarms == ["t1 camera: recovered by reconnect"]


async def test_restart_hook_runs_a_command():
    dev = Device()
    seen = []

    async def hook(h, timeout):
        seen.append(h)
        dev.broken = False
        return "ok"

    r = await ladder(FULL, hook=hook).recover("camera", reconnect=dev.reconnect, probe=dev.probe)
    assert [s for s, _ in r.tried] == ["reconnect", "restart"] and r.recovered
    assert seen == [OK_HOOK]
    # The default hook really runs the command (and a failing one is an error).
    from robofast_core.recovery import run_hook

    assert await run_hook(OK_HOOK, 10) == "restarted"
    with pytest.raises(RuntimeError, match="exited 3"):
        await run_hook({"command": [sys.executable, "-c", "raise SystemExit(3)"]}, 10)
    with pytest.raises(RuntimeError, match="timed out"):
        await run_hook({"command": [sys.executable, "-c", "import time; time.sleep(5)"]}, 0.2)


async def test_power_cycle_then_exhausted_then_reset():
    dev = Device(fixed_by="powercycle")
    pdu = FakePDU(dev)
    t = [0.0]
    lad = ladder(FULL, pdu=pdu, clock=lambda: t[0])

    async def failing_hook(h, timeout):
        raise RuntimeError("the vendor software would not start")

    lad.hook = failing_hook

    async def read(device, member):
        return {"AtPark": True, "CameraState": 0}[member]

    r = await lad.recover("camera", reconnect=dev.reconnect, probe=dev.probe, others=read)
    assert [o.split(":")[0] for _, o in r.tried] == ["failed", "failed", "ok"] and r.recovered
    assert pdu.cycled == [0]
    # It fails again soon after: the ladder starts above the rung that worked,
    # finds none left, and gives up for the night.
    dev.broken = True
    t[0] = 100.0
    r = await lad.recover("camera", reconnect=dev.reconnect, probe=dev.probe)
    assert r.exhausted and not r.recovered and r.tried == []
    # After a quiet escalate_window it starts again from the bottom (but the
    # night's one power cycle is used up).
    t[0] = 100.0 + 3601.0
    r = await lad.recover("camera", reconnect=dev.reconnect, probe=dev.probe, others=read)
    assert [s for s, _ in r.tried] == ["reconnect", "restart", "powercycle"]
    assert "already power-cycled 1 time(s) tonight" in r.tried[-1][1] and r.exhausted


@pytest.mark.parametrize(
    "others,why",
    [
        ({"AtPark": False}, "not parked"),
        ({"CameraState": 2}, "camera is not idle"),
        ({"CCDTemperature": -30.0}, "below -5"),
    ],
)
async def test_power_cycle_guards(others, why):
    dev = Device(fixed_by="powercycle")
    dev.kind = "focuser"
    pdu = FakePDU(dev)
    state = {"AtPark": True, "CameraState": 0, "CCDTemperature": 10.0, **others}

    async def read(device, member):
        return state[member]

    lad = RecoveryLadder(
        "t1",
        {"focuser": DevicePolicy.from_dict({**FULL, "steps": ["powercycle"],
                                             "camera_warm_above": -5.0})},
        pdu=pdu,
        sleep=no_sleep,
    )  # fmt: skip
    r = await lad.recover("focuser", reconnect=dev.reconnect, probe=dev.probe, others=read)
    assert r.exhausted and why in r.tried[0][1] and pdu.cycled == []


async def test_a_dead_camera_can_still_be_cycled_if_the_rest_is_safe():
    dev = Device(fixed_by="powercycle")

    async def read(device, member):
        return {"AtPark": True}[member]

    lad = ladder({**FULL, "steps": ["powercycle"], "camera_warm_above": -5.0}, pdu=FakePDU(dev))
    r = await lad.recover("camera", reconnect=dev.reconnect, probe=dev.probe, others=read)
    assert r.recovered


async def test_every_step_is_bounded():
    dev = Device()

    async def slow_hook(h, timeout):
        await asyncio.sleep(10)
        return ""

    lad = ladder({**FULL, "steps": ["restart"], "step_timeout": 0.1}, hook=slow_hook)
    r = await asyncio.wait_for(
        lad.recover("camera", reconnect=dev.reconnect, probe=dev.probe), timeout=3
    )
    assert r.exhausted and "TimeoutError" in r.tried[0][1]


def test_policy_validation():
    with pytest.raises(ValueError, match="steps"):
        DevicePolicy(steps=["pray"])
    with pytest.raises(ValueError, match="restart hook"):
        DevicePolicy(steps=["restart"])
    with pytest.raises(ValueError, match=r"powercycle\.pdu"):
        DevicePolicy(steps=["powercycle"])
    with pytest.raises(ValueError, match="unknown devices"):
        RecoveryLadder("t1", {"dome": DevicePolicy()})
    with pytest.raises(ValueError, match="unknown keys"):
        RecoveryLadder.from_config("t1", {"camera": {"stepz": []}})


# -- the PDU for real (simulated) ------------------------------------------------------------


async def test_power_cycle_through_the_pdu_server():
    creds = Credentials("r", "w")
    sim = await APCPDUSim(creds, ["cam", "x"], reboot_delay=0.05).start()
    cfg = PDUConfig.from_dict(
        {
            "listen": "127.0.0.1:0",
            "pdu": {"host": "127.0.0.1", "port": sim.port, "timeout": 0.5},
            "outlets": [{"outlet": 1, "name": "t1 camera", "writable": True, "cycle": True}],
        }
    )
    server = PDUServer(cfg, credentials=creds).start()
    try:
        dev = Device()
        lad = ladder(
            {**FULL, "steps": ["powercycle"], "powercycle": {"pdu": server.address, "switch": 0}},
            pdu=lambda address: PDUSwitch(address, timeout=5),
        )

        async def probe(member):
            if not any(s == (1, 3) for s in sim.sets):
                raise FakeDeviceError("no power")
            return 0

        async def read(device, member):
            return {"AtPark": True, "CameraState": 0}[member]

        r = await lad.recover("camera", reconnect=dev.reconnect, probe=probe, others=read)
        assert r.recovered and sim.sets == [(1, 3)]
    finally:
        server.stop()
        sim.close()


# -- in the sequencer ------------------------------------------------------------------------


class FlakyCamera(FakeCamera):
    """Fails every call while ``broken``; a restart hook mends it."""

    broken = False

    async def _gate(self) -> None:
        await super()._gate()
        if self.broken:
            raise FakeDeviceError("camera driver crashed")


async def test_sequencer_climbs_the_ladder_and_observes_on(make_rig):
    rig = make_rig(exptime=0.05, count=3, telescopes=2, seq={"error_backoff": 0.1})
    cam = FlakyCamera()
    unit = rig.sequencer.units["t1"]
    from robofast_core.owner import DeviceOwner

    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)

    async def hook(h, timeout):
        cam.broken = False
        return "service restarted"

    unit.recovery = RecoveryLadder(
        "t1",
        {"camera": DevicePolicy.from_dict({**FULL, "steps": ["reconnect", "restart"]})},
        hook=hook,
        sleep=no_sleep,
    )
    await rig.start()
    await wait_until(lambda: cam.exposures_started >= 1, timeout=10)
    cam.broken = True
    await wait_until(lambda: rig.timeline.events("recovery_done"), timeout=20)
    [done] = rig.timeline.events("recovery_done")
    assert done.detail["recovered"] and done.detail["device"] == "camera"
    assert [t.split(":")[0] for t in done.detail["tried"]] == ["reconnect", "restart"]
    n = cam.exposures_started
    await wait_until(lambda: cam.exposures_started > n + 2, timeout=20)
    assert not unit.gave_up
    assert "t1 camera: recovered by restart" in unit.alarms
    assert (
        "t1 camera: recovered by restart" in snapshot(rig.sequencer)["telescopes"]["t1"]["alarms"]
    )


async def test_sequencer_gives_up_when_the_ladder_is_exhausted(make_rig):
    rig = make_rig(exptime=0.05, count=3, telescopes=2, seq={"error_backoff": 0.1})
    cam = FlakyCamera()
    unit = rig.sequencer.units["t1"]
    from robofast_core.owner import DeviceOwner

    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    unit.recovery = RecoveryLadder(
        "t1", {"camera": DevicePolicy(verify_timeout=0.05)}, sleep=no_sleep
    )
    await rig.start()
    await wait_until(lambda: cam.exposures_started >= 1, timeout=10)
    cam.broken = True
    await wait_until(lambda: unit.gave_up, timeout=20)
    [give] = rig.timeline.events("giving_up")
    assert "recovery ladder exhausted" in give.detail["reason"]
    assert give.detail["errors"] == 1  # the ladder, not the error count, gave up
    assert any("giving up tonight" in a for a in unit.alarms)
    # The other telescope observes on.
    t2 = rig.cams[1].exposures_started
    await wait_until(lambda: rig.cams[1].exposures_started > t2 + 1, timeout=20)
