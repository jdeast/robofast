"""Camera cooling (robofast/cooling.py, docs/cooling.md): the one path that
sets the cooler, the camera owner's on-connect hook, the sequencer keeping a
camera cooled through startup, recovery and a cooler found off, and the
tolerance warning -- on fake cameras (FakeCamera models the cooler and, with
``cooler_off_on_connect``, MINERVA T1's Andor driver)."""

from __future__ import annotations

import asyncio

import pytest

from robofast.cooling import CameraCooling, CoolingConfig, set_cooling
from robofast.owner import DeviceOwner, Unsupported
from robofast.recovery import DevicePolicy, RecoveryLadder
from robofast.testing.checks import wait_until
from robofast.testing.fakes import FakeCamera, FakeDeviceError
from robofast.timeline import Timeline

SETPOINT = -30.0


def cooling(**kw) -> CameraCooling:
    return CameraCooling("t1", CoolingConfig(**{"setpoint": SETPOINT, "poll": 0.02, **kw}))


# -- config ----------------------------------------------------------------------------------


def test_config():
    c = CoolingConfig.from_dict({"setpoint": -50, "tolerance": 0.75, "settle_timeout": 600})
    assert (c.setpoint, c.tolerance, c.settle_timeout) == (-50, 0.75, 600)
    assert CoolingConfig.from_dict(5).setpoint == 5.0  # a bare setpoint
    assert CoolingConfig.from_dict({"setpoint": -50}).tolerance == 1.0
    for bad, why in (
        ({}, "no setpoint"),
        ({"setpoint": -50, "tol": 1}, "unknown keys"),
        ({"setpoint": "cold"}, "a number"),
        ({"setpoint": -500}, "outside"),
        ({"setpoint": -50, "tolerance": 0}, "tolerance"),
        ({"setpoint": -50, "settle_timeout": -1}, "settle_timeout"),
        ("cold", "expected"),
    ):
        with pytest.raises(ValueError, match=why):
            CoolingConfig.from_dict(bad)


# -- the one path ----------------------------------------------------------------------------


async def test_set_cooling_sets_the_setpoint_then_the_cooler():
    cam = FakeCamera()
    cam.CoolerOn = False
    await set_cooling(cam, -50.0)
    assert cam.SetCCDTemperature == -50.0 and cam.CoolerOn is True


async def test_a_camera_that_cannot_cool_is_unsupported_and_logged_once():
    cam = FakeCamera()
    cam.can_cool = False
    cam.CoolerOn = False
    with pytest.raises(Unsupported, match="CanSetCCDTemperature false"):
        await set_cooling(cam, -50.0)
    events = []
    c = cooling()
    c.event = lambda what, **kw: events.append(what)
    assert await c.apply(cam, "test") is False
    assert await c.apply(cam, "test") is False
    assert events == ["cooling_unsupported"] and c.unsupported
    assert cam.CoolerOn is False and cam.SetCCDTemperature == -20.0  # left alone
    assert await c.check(cam, wait=True) is None


async def test_not_implemented_is_unsupported():
    class NoCooler(FakeCamera):
        async def set(self, member, value, *, timeout=None):
            if member == "SetCCDTemperature":
                exc = FakeDeviceError("not implemented")
                exc.number = 0x400
                raise exc
            return await super().set(member, value, timeout=timeout)

    with pytest.raises(Unsupported, match="SetCCDTemperature not implemented"):
        await set_cooling(NoCooler(), -50.0)


# -- the owner's on-connect hook -------------------------------------------------------------


async def test_the_hook_runs_on_every_fresh_connection():
    cam = FakeCamera()
    cam.cooler_off_on_connect = True
    tl = Timeline()
    c = cooling()
    owner = DeviceOwner("t1.camera", lambda: cam, tl, default_timeout=0.3, on_connect=c.on_connect)
    g = owner.grant("test")
    cam.Connected = False
    assert await g.get("CoolerOn") is True  # connected (cooler off: Andor), then set on
    assert cam.SetCCDTemperature == SETPOINT and cam.cooler_switched_on == 1
    await g.get("CoolerOn")
    assert cam.cooler_switched_on == 1  # same connection: not set again
    # The recovery ladder's reconnect after the vendor software restarted.
    cam.Connected = False
    owner.reconnect("test")
    assert await g.get("CoolerOn") is True and cam.cooler_switched_on == 2
    # A timeout drops the driver; the next command connects (and cools) again.
    cam.hang()
    with pytest.raises(Exception, match="did not complete"):
        await g.get("CameraState")
    cam.unhang()
    cam.Connected = False
    assert await g.get("CoolerOn") is True and cam.cooler_switched_on == 3


async def test_a_failing_hook_never_fails_the_command():
    cam = FakeCamera()
    tl = Timeline()

    async def broken(driver, timeout):
        raise RuntimeError("boom")

    async def slow(driver, timeout):
        await asyncio.sleep(10)

    owner = DeviceOwner("t1.camera", lambda: cam, tl, default_timeout=0.3, on_connect=broken)
    assert await owner.grant("x").get("CameraState") == 0
    [e] = tl.events("on_connect_failed")
    assert "boom" in e.detail["error"]
    # A hook that hangs is bounded on its own: the command still has its time.
    owner = DeviceOwner("t1.camera", lambda: cam, tl, default_timeout=0.3, on_connect=slow)
    assert await owner.grant("x").get("CameraState") == 0
    assert "TimeoutError" in tl.events("on_connect_failed")[-1].detail["error"]


# -- in the sequencer ------------------------------------------------------------------------


def cooled(rig, cam: FakeCamera | None = None, **kw) -> CameraCooling:
    unit = rig.sequencer.units["t1"]
    if cam is not None:
        unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    unit.cooling = cooling(**kw)
    return unit.cooling


async def test_the_sequencer_cools_the_camera_at_startup(make_rig):
    rig = make_rig(exptime=0.05, count=2)
    rig.cam.CoolerOn = False
    cooled(rig)
    await rig.start()
    await wait_until(lambda: rig.cam.exposures_started >= 1, timeout=10)
    [e] = [e for e in rig.timeline.events("cooling_set") if e.detail["telescope"] == "t1"]
    assert e.detail["why"] == "connected" and e.detail["setpoint"] == SETPOINT
    assert rig.cam.CoolerOn and rig.cam.SetCCDTemperature == SETPOINT


async def test_an_unsupported_camera_is_left_alone_and_the_night_goes_on(make_rig):
    rig = make_rig(exptime=0.05, count=2)
    rig.cam.can_cool = False
    cooled(rig)
    await rig.start()
    await wait_until(lambda: rig.cam.exposures_started >= 3, timeout=10)
    assert len(rig.timeline.events("cooling_unsupported")) == 1
    assert rig.cam.SetCCDTemperature == -20.0
    assert not rig.sequencer.units["t1"].gave_up


async def test_out_of_tolerance_warns_once_and_exposes_anyway(make_rig, tmp_path):
    from robofast.plan import StaticPlan
    from tests.core.test_imaging import attach, headers, run_night

    plan = StaticPlan(
        [
            {"name": "f", "object": "f", "ra_hours": 0.7, "dec_deg": 41.3, "exptime": 0.05,
             "count": 3},
        ],
        ["t1"],
    )  # fmt: skip
    rig = make_rig(plan=plan)
    rig.cam.coldest = -25.0  # a warm night: the cooler cannot reach -30
    cooled(rig, settle_timeout=0.3)
    imaging = attach(rig, tmp_path)
    await run_night(rig, imaging)
    [w] = rig.timeline.events("cooling_not_settled")
    assert w.detail["ccd_temp"] == -25.0 and w.detail["level"] == "warning"
    assert rig.cam.exposures_started == 3
    assert [f["ccd_settled"] for f in rig.sequencer.frames] == [False] * 3
    hs = [h for _, h in headers(tmp_path, "t1")]
    assert len(hs) == 3
    for h in hs:
        assert h["COOLOK"] is False and h["CCD-TEMP"] == -25.0
        assert h["SET-TEMP"] == SETPOINT and h["COOLERON"] is True


async def test_light_frames_wait_for_the_ccd_to_settle(make_rig):
    rig = make_rig(exptime=0.05, count=1)
    rig.cam.cooling_rate = 20.0  # C/s: -20 -> -30 in 0.5 s
    rig.cam.CoolerOn = False
    rig.cam.CCDTemperature = -20.0
    cooled(rig, settle_timeout=30)
    await rig.start()
    await wait_until(lambda: rig.sequencer.frames, timeout=10)
    assert rig.cam.CCDTemperature == pytest.approx(SETPOINT, abs=1.0)
    assert rig.sequencer.frames[0]["ccd_settled"] is True
    assert not rig.timeline.events("cooling_not_settled")


async def test_a_cooler_found_off_is_turned_back_on_rate_limited(make_rig):
    rig = make_rig(exptime=0.05, count=2)
    cooled(rig, restart_interval=3600)
    await rig.start()
    await wait_until(lambda: rig.cam.exposures_started >= 1, timeout=10)
    on = rig.cam.cooler_switched_on
    rig.cam.CoolerOn = False  # someone (or something) switched it off
    await wait_until(lambda: rig.cam.cooler_switched_on == on + 1, timeout=10)
    unit = rig.sequencer.units["t1"]
    assert "t1 camera: cooler found off; turning it back on" in unit.alarms
    # Off again at once: not switched on again within restart_interval.
    rig.cam.CoolerOn = False
    await wait_until(lambda: rig.timeline.events("cooler_off"), timeout=10)
    n = rig.cam.exposures_started
    await wait_until(lambda: rig.cam.exposures_started > n + 2, timeout=10)
    assert rig.cam.cooler_switched_on == on + 1 and not rig.cam.CoolerOn
    assert not unit.gave_up


class AndorCamera(FakeCamera):
    """MINERVA T1's camera: connecting it turns its cooler off; fails every
    call while ``broken`` (its ASCOM driver crashed)."""

    broken = False

    def __init__(self) -> None:
        super().__init__()
        self.cooler_off_on_connect = True

    async def _gate(self) -> None:
        await super()._gate()
        if self.broken:
            raise FakeDeviceError("camera driver crashed")


@pytest.mark.parametrize("with_cooling", [True, False])
async def test_recovery_turns_the_cooler_back_on(make_rig, with_cooling):
    """The driver restarts (the recovery ladder's restart rung): its new
    connection switches the Andor's cooler off; robofast's reconnect sets it
    on again. Without a cooling entry the camera is left warming."""
    rig = make_rig(exptime=0.05, count=3, seq={"error_backoff": 0.1})
    cam = AndorCamera()
    unit = rig.sequencer.units["t1"]
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    if with_cooling:
        unit.cooling = cooling()

    async def hook(h, timeout):
        cam.broken = False
        cam.Connected = False  # a restarted driver: not connected
        return "camera service restarted"

    unit.recovery = RecoveryLadder(
        "t1",
        {
            "camera": DevicePolicy.from_dict(
                {"steps": ["reconnect", "restart"], "restart": {"command": ["true"]},
                 "verify_timeout": 0.2}
            )
        },
        hook=hook,
        sleep=lambda s: asyncio.sleep(0),
    )  # fmt: skip
    await rig.start()
    await wait_until(lambda: cam.exposures_started >= 1, timeout=10)
    assert cam.CoolerOn
    on = cam.cooler_switched_on
    cam.broken = True
    await wait_until(lambda: rig.timeline.events("recovery_done"), timeout=20)
    [done] = rig.timeline.events("recovery_done")
    assert done.detail["recovered"] and "restart: ok" in done.detail["tried"]
    n = cam.exposures_started
    await wait_until(lambda: cam.exposures_started > n, timeout=20)
    if with_cooling:
        assert cam.CoolerOn and cam.cooler_switched_on == on + 1
        assert cam.SetCCDTemperature == SETPOINT
        sets = [e.detail["why"] for e in rig.timeline.events("cooling_set")]
        assert sets.count("connected") >= 2  # startup and the recovery's reconnect
        assert not any("cooler found off" in a for a in unit.alarms)  # the hook got there first
    else:
        assert not cam.CoolerOn  # the Andor quirk, and nothing undid it
