"""The recovery ladder (robofast/recovery.py, docs/recovery.md): rungs,
escalation, the power-cycle guards, bounds, and the sequencer using it --
with fakes and, for the power cycle, the PDU server on its simulator."""

from __future__ import annotations

import asyncio
import sys

import pytest

from robofast.devices.apc_pdu import APCPDUSim, Credentials
from robofast.devices.pdu_server import PDUConfig, PDUServer
from robofast.recovery import DevicePolicy, PDUSwitch, RecoveryLadder
from robofast.sequencer_service import snapshot
from robofast.testing.checks import wait_until
from robofast.testing.fakes import FakeCamera, FakeDeviceError

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
        self.actions: list[str] = []
        self.device = device

    def __call__(self, address: str) -> FakePDU:
        return self

    async def cycle(self, switch: int, action: str = "PowerCycle") -> None:
        self.cycled.append(switch)
        self.actions.append(action)
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
    from robofast.recovery import run_hook

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
    assert pdu.cycled == [0] and pdu.actions == ["PowerCycle"]  # robofast's PDU server's
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
    from robofast.owner import DeviceOwner

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
    from robofast.owner import DeviceOwner

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
    assert any("recovery ladder exhausted" in a for a in unit.alarms)
    [inc] = rig.timeline.events("incident")
    assert "no agent configured" in inc.detail["summary"]
    # The other telescope observes on.
    t2 = rig.cams[1].exposures_started
    await wait_until(lambda: rig.cams[1].exposures_started > t2 + 1, timeout=20)


async def test_the_power_cycle_action_is_configurable():
    dev = Device(fixed_by="powercycle")
    pdu = FakePDU(dev)
    policy = {**FULL, "steps": ["powercycle"]}
    policy["powercycle"] = {**FULL["powercycle"], "action": "CycleOutlet"}

    async def read(device, member):
        return {"AtPark": True, "CameraState": 0}[member]

    r = await ladder(policy, pdu=pdu).recover(
        "camera", reconnect=dev.reconnect, probe=dev.probe, others=read
    )
    assert r.recovered and pdu.actions == ["CycleOutlet"]
    with pytest.raises(ValueError, match=r"powercycle\.action"):
        DevicePolicy.from_dict({**policy, "powercycle": {**policy["powercycle"], "action": ""}})


# -- a camera sharing power with the mount (requires_pwi_stop) -------------------------------

PWI_CYCLE = {
    **FULL,
    "steps": ["powercycle"],
    "powercycle": {
        **FULL["powercycle"],
        "requires_pwi_stop": True,
        "pwi_stop": {"command": ["stop-pwi"]},
        "pwi_start": {"command": ["start-pwi"]},
    },
}


class FakeMount:
    """The telescope as a PWI server sees it after PWI is started again."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.status = {"pwi": {"connected": False, "az_enabled": False, "alt_enabled": False}}

    def reconnect(self) -> None:
        self.order.append("mount reconnect")

    async def probe(self, member: str):
        return {"SupportedActions": ["Status"], "CanFindHome": False, "AtPark": True}[member]

    async def call(self, member: str, *args):
        if member == "Action" and args[0] == "Status":
            import json

            return json.dumps(self.status)
        self.order.append(f"{member} {args[0] if args else ''}".strip())
        if args and args[0] == "MountConnect":
            self.status["pwi"]["connected"] = True


async def test_a_camera_sharing_power_with_the_mount_stops_and_starts_pwi():
    """Legacy imager.recover(): telescope shut down and PWI killed before a
    camera's power cycle, PWI started and the mount initialized after."""
    from robofast.recovery import MountAccess

    order: list[str] = []
    dev = Device(fixed_by="powercycle")

    class PDU(FakePDU):
        async def cycle(self, switch, action="PowerCycle"):
            order.append("cycle")
            await super().cycle(switch, action)

    async def hook(h, timeout):
        order.append(h["command"][0])
        return "ok"

    mount = FakeMount(order)

    async def read(device, member):
        return {"AtPark": True, "CameraState": 0}[member]

    r = await ladder(PWI_CYCLE, pdu=PDU(dev), hook=hook).recover(
        "camera",
        reconnect=dev.reconnect,
        probe=dev.probe,
        others=read,
        mount=MountAccess(mount.reconnect, mount.probe, mount.call),
    )
    assert r.recovered
    assert order == [
        "stop-pwi",
        "cycle",
        "start-pwi",
        "mount reconnect",
        "Action MountConnect",
        "Action MotorsEnable",
    ]
    # Not parked: refused before PWI is touched.
    order.clear()

    async def unparked(device, member):
        return {"AtPark": False, "CameraState": 0}[member]

    r = await ladder(PWI_CYCLE, pdu=PDU(Device()), hook=hook).recover(
        "camera", reconnect=dev.reconnect, probe=dev.probe, others=unparked
    )
    assert not r.recovered and "not parked" in r.tried[0][1] and order == []


async def test_pwi_is_started_again_when_the_cycle_fails():
    order: list[str] = []

    class BrokenPDU(FakePDU):
        async def cycle(self, switch, action="PowerCycle"):
            raise RuntimeError("PDU does not answer")

    async def hook(h, timeout):
        order.append(h["command"][0])
        return "ok"

    async def read(device, member):
        return {"AtPark": True, "CameraState": 0}[member]

    alarms: list[str] = []
    dev = Device()
    r = await ladder(PWI_CYCLE, pdu=BrokenPDU(), hook=hook, alarms=alarms).recover(
        "camera", reconnect=dev.reconnect, probe=dev.probe, others=read
    )
    assert not r.recovered and "PDU does not answer" in r.tried[0][1]
    assert order == ["stop-pwi", "start-pwi"]
    assert any("mount was not reinitialized" in a for a in alarms)


def test_requires_pwi_stop_validation():
    pc = PWI_CYCLE["powercycle"]
    with pytest.raises(ValueError, match="pwi_start hook"):
        DevicePolicy.from_dict({**PWI_CYCLE, "powercycle": {**pc, "pwi_start": None}})
    with pytest.raises(ValueError, match="requires_parked"):
        DevicePolicy.from_dict({**PWI_CYCLE, "requires_parked": False})
    # The telescope's own (panel) cycle stops and starts PWI too (legacy).
    RecoveryLadder.from_config(
        "t1", {"telescope": PWI_CYCLE, "camera": PWI_CYCLE, "guider": PWI_CYCLE}
    )


# -- the guider ------------------------------------------------------------------------------

GUIDER_POLICY = {"steps": ["reconnect", "restart"], "restart": OK_HOOK, "verify_timeout": 0.5}


def test_guider_errors_are_the_guiders():
    lad = RecoveryLadder.from_config("t2", {"guider": {"steps": ["reconnect"]}})
    names = {"telescope": "t2.telescope", "camera": "t2.camera"}
    for error in (
        "GuiderError('guider t2: not guiding after 10.0 s (acquiring: )')",
        "guider t2: failed: star lost 4 times",
    ):
        assert lad.device_for(error, names) == "guider"
    assert lad.device_for("DeviceError('t2.camera.StartExposure: x')", names) is None
    assert RecoveryLadder("t2", {}).device_for("GuiderError('guider t2: x')", names) is None


async def test_guider_ladder_on_the_guider_service(tmp_path):
    """The guider's rungs against the real service on its simulator:
    ``reconnect`` closes and opens the guide camera (POST /reconnect);
    a camera that will not open fails it, and the restart hook (the
    camera's driver restarted) mends it."""
    from robofast.guider.base import GuiderError
    from robofast.guider.service import GuiderRemote, GuiderService
    from tests.core.test_guider_loop import _service_config

    svc = await GuiderService(_service_config(tmp_path, calibrations={})).start()
    remote = GuiderRemote(svc.url, scope="t2", acquire_timeout=10)
    cam = svc.tap._camera
    opens = {"n": 0, "dead": False}
    real_open = cam.open

    async def open_():
        opens["n"] += 1
        if opens["dead"]:
            raise GuiderError("camera not found on USB")
        await real_open()

    cam.open = open_
    try:
        svc.guider._set_state("failed", "acquisition failed: no star")
        lad = RecoveryLadder.from_config(
            "t2",
            {"guider": GUIDER_POLICY},
            sleep=no_sleep,
        )

        async def probe(member):
            return await getattr(remote, member)()

        r = await lad.recover("guider", reconnect=remote.reconnect, probe=probe)
        assert r.recovered and r.tried == [("reconnect", "ok")] and opens["n"] == 1
        st = await remote.status()
        assert st["state"] == "idle" and st["reason"] == "camera reconnected"

        # The camera does not reopen: reconnect fails; the restart hook mends it.
        opens["dead"] = True

        async def hook(h, timeout):
            opens["dead"] = False
            return "camera driver restarted"

        lad = RecoveryLadder.from_config(
            "t2",
            {"guider": GUIDER_POLICY},
            sleep=no_sleep,
            hook=hook,
        )
        r = await lad.recover("guider", reconnect=remote.reconnect, probe=probe)
        assert [s for s, _ in r.tried] == ["reconnect", "restart"] and r.recovered
        assert "camera not found on USB" in r.tried[0][1]
        assert (await remote.status())["state"] == "idle"
    finally:
        await remote.stop("teardown")
        await svc.stop()


class RecoverableGuider:
    """A guider that cannot acquire until its camera is reconnected
    (``fails`` acquisitions in all; None: never acquires)."""

    def __init__(self, fails: int | None) -> None:
        self.fails = fails
        self.starts = 0
        self.reconnects = 0
        self.broken = False

    async def start(self, **params):
        self.starts += 1
        if self.broken or self.fails is None:
            from robofast.guider.base import GuiderError

            self.broken = True
            raise GuiderError("guider t1: not guiding after 10.0 s (acquiring: )")
        return {"state": "guiding"}

    async def check(self):
        return None

    async def stop(self, reason):
        return True

    async def reconnect(self):
        self.reconnects += 1
        if self.fails is not None:
            self.fails -= 1
            self.broken = self.fails > 0
        return {"state": "idle"}

    async def status(self):
        return {"state": "idle"}


async def test_sequencer_climbs_the_guiders_ladder_before_giving_up(make_rig):
    """night1108: T2's guider timed out three acquisitions in a row and the
    telescope gave up without trying anything. Now each guider failure
    climbs the guider's ladder first, like any device."""
    from robofast.plan import StaticPlan

    plan = StaticPlan([{"name": "x", "exptime": 0.05, "count": 2, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq={"error_backoff": 0.1, "max_consecutive_errors": 3})
    unit = rig.sequencer.units["t1"]
    g = unit.guider = RecoverableGuider(fails=1)
    g.broken = True
    unit.recovery = RecoveryLadder.from_config(
        "t1", {"guider": {"steps": ["reconnect"], "verify_timeout": 0.05}}, sleep=no_sleep
    )
    await rig.start()
    await wait_until(lambda: any(h[2] == "complete" for h in plan.history), timeout=20)
    [done] = rig.timeline.events("recovery_done")
    assert done.detail["device"] == "guider" and done.detail["recovered"]
    assert g.reconnects == 1 and plan.history[-1][2] == "complete" and not unit.gave_up
    assert "t1 guider: recovered by reconnect" in unit.alarms


async def test_a_guider_that_never_recovers_gives_the_telescope_up(make_rig):
    from robofast.plan import StaticPlan

    plan = StaticPlan([{"name": "x", "exptime": 0.05, "count": 2, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq={"error_backoff": 0.1, "max_consecutive_errors": 3})
    unit = rig.sequencer.units["t1"]
    g = unit.guider = RecoverableGuider(fails=None)

    async def failing_hook(h, timeout):
        raise RuntimeError("ASCOM Remote would not restart")

    unit.recovery = RecoveryLadder.from_config(
        "t1",
        {"guider": {"steps": ["reconnect", "restart"], "restart": OK_HOOK, "verify_timeout": 0.05}},
        sleep=no_sleep,
        hook=failing_hook,
    )
    await rig.start()
    await wait_until(lambda: unit.gave_up, timeout=30)
    [give] = rig.timeline.events("giving_up")
    assert give.detail["reason"].startswith("recovery ladder exhausted (guider)")
    tried = [e.detail["tried"] for e in rig.timeline.events("recovery_done")]
    assert [t.split(":")[0] for r in tried for t in r] == ["reconnect", "restart"]
    assert g.reconnects >= 1
