"""The guide loop in the simulator: closed-loop residuals against gain and
frame rate, latency, acquisition onto the fiber, loss and re-acquisition,
offloading, telemetry, and the HTTP service with its sequencer client."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time

import numpy as np
import pytest

from robofast_core.guider.base import ROI, HardwareNotAllowed, RealClock, VirtualClock
from robofast_core.guider.calibration import Calibration
from robofast_core.guider.loop import Backlight, GuideConfig, Guider
from robofast_core.guider.offload import OffloadConfig, Offloader
from robofast_core.guider.redis_pub import TrackingPublisher
from robofast_core.guider.service import GuiderRemote, GuiderService, GuiderServiceConfig
from robofast_core.guider.sim import Fiber, Jitter, SimCamera, SimMount, SimTipTilt, SimWorld
from robofast_core.guider.tiptilt import MountOffset
from robofast_core.registry import ValueRegistry

TARGET = (320.0, 240.0)
#: Where the closed-loop table goes when set (CI step summary, the report).
RESULTS = os.environ.get("ROBOFAST_GUIDER_RESULTS")


def _seeing(**kw) -> Jitter:
    """1 px rms seeing with a 1 Hz knee, an 8 Hz wind-shake line of 0.5 px
    and a tracking drift: 0.5"/px plate scale makes it ~0.6" of image
    motion -- a bad night for a 0.7 m telescope's tip/tilt."""
    args = dict(rms=1.0, knee=1.0, shake=[(8.0, 0.5)], drift=(0.05, -0.03), seed=3)
    args.update(kw)
    return Jitter(**args)


def _save(path: str | None, obj) -> None:
    if path:
        with open(path, "w") as f:
            json.dump(obj, f, indent=1)


def _rig(
    clock,
    *,
    star=(322.0, 238.5),
    jitter=None,
    tt_limits=((-100.0, 100.0), (-100.0, 100.0)),
    mount=False,
    config=None,
    world_kw=None,
    tt_kw=None,
):
    tt = SimTipTilt(clock=clock, limits=tt_limits, **(tt_kw or {}))
    world = SimWorld(
        clock=clock,
        star=star,
        jitter=jitter,
        tiptilt=tt,
        tt_matrix=np.array([[0.08, 0.03], [-0.03, 0.08]]),
        **(world_kw or {}),
    )
    cal = Calibration("simtt", "fau", world.tt_matrix.tolist())
    m = mcal = None
    if mount:
        sm = SimMount(clock=clock, tau=0.3, delay=0.05)
        world.mount = sm
        m = MountOffset(sm, max_offset=100.0)
        m.clock = clock
        mcal = Calibration("mount", "fau", world.mount_matrix.tolist())
    cfg = GuideConfig(
        **{
            "target": TARGET,
            "acq_roi": (160, 120, 320, 240),
            "acq_exposure": 0.2,
            "coarse_settle": 1.5,
            "scope": "t2",
            **(config or {}),
        }
    )
    g = Guider(SimCamera(world), cfg, tiptilt=tt, tt_cal=cal, mount=m, mount_cal=mcal, clock=clock)
    return g, world


async def _guide_for(g: Guider, clock, seconds: float) -> float:
    g.start()
    await g.wait_state(("guiding", "failed"), 30)
    assert g.state == "guiding", g.reason
    t0 = clock.now()
    while clock.now() < t0 + seconds:
        await clock.sleep(0.05)
    return t0


def _open_rms(world: SimWorld, t0: float, t1: float) -> float:
    ts = np.arange(t0, t1, 1e-3)
    d = [
        world.jitter.at(t - world.t_origin) + np.asarray(world.star) - np.asarray(TARGET)
        for t in ts
    ]
    return float(np.sqrt(np.mean(np.sum(np.square(d), axis=1))))


# -- closed loop ---------------------------------------------------------------------


async def test_closed_loop_residual_against_gain_and_frame_rate():
    """Residual rms (truth, sampled every ms) for a sweep of gain and frame
    rate on the same seeing, in simulated time."""
    rows = []
    for rate in (25, 100, 400):
        for kp in (0.2, 0.5, 0.8):
            clock = VirtualClock()
            g, world = _rig(
                clock, jitter=_seeing(), config={"exposure": 1 / rate, "gains": {"kp": kp}}
            )
            t0 = await _guide_for(g, clock, 3.0)
            await g.stop()
            closed = world.residual_rms(TARGET, t0 + 1.0, t0 + 3.0)
            rows.append(
                {
                    "rate_hz": rate,
                    "kp": kp,
                    "residual_px": round(closed, 3),
                    "open_px": round(_open_rms(world, t0 + 1.0, t0 + 3.0), 3),
                    "measured_rate_hz": round(g.summary()["loop_rate"], 1),
                }
            )
    print("\nclosed-loop residual (px rms; 1 px rms seeing + 0.5 px 8 Hz shake + drift):")
    for r in rows:
        rate, kp, res, op = r["rate_hz"], r["kp"], r["residual_px"], r["open_px"]
        print(f"  {rate:4d} Hz  kp {kp:.1f}: {res:.3f}  (open {op:.3f})")
    _save(RESULTS, rows)
    by = {(r["rate_hz"], r["kp"]): r["residual_px"] for r in rows}
    for r in rows:
        assert r["residual_px"] < 0.6 * r["open_px"]
        assert r["measured_rate_hz"] == pytest.approx(r["rate_hz"], rel=0.05)
    # Faster is better at a given gain, and at 400 Hz the loop holds the
    # star to a small fraction of a pixel.
    for kp in (0.2, 0.5, 0.8):
        assert by[(400, kp)] < by[(100, kp)] < by[(25, kp)]
    assert by[(400, 0.5)] < 0.2


async def test_latency_on_the_real_clock():
    """Exposure end to command sent, with real compute: the simulated
    camera's rendering (standing in for readout), the centroid, the
    controller and the command."""
    clock = RealClock()
    g, _ = _rig(
        clock,
        jitter=_seeing(rms=0.3),
        config={"exposure": 0.005, "roi_size": 32, "acq_exposure": 0.01},
    )
    g.start()
    await g.wait_state(("guiding", "failed"), 10)
    await asyncio.sleep(1.0)
    await g.stop()
    s = g.summary()
    print(
        f"\nlatency (exposure end -> command sent): median {1e3 * s['latency_median']:.2f} ms,"
        f" p95 {1e3 * s['latency_p95']:.2f} ms, max {1e3 * s['latency_max']:.2f} ms"
        f" (the guider's own processing: median {1e3 * s['processing_median']:.2f} ms,"
        f" p95 {1e3 * s['processing_p95']:.2f} ms);"
        f" loop {s['loop_rate']:.0f} Hz at 5 ms exposures"
    )
    keys = (
        "latency_median",
        "latency_p95",
        "latency_max",
        "processing_median",
        "processing_p95",
        "loop_rate",
    )
    _save(RESULTS and RESULTS + ".latency.json", {k: s[k] for k in keys})
    assert 0 < s["processing_median"] <= s["latency_median"] < 0.02
    assert s["loop_rate"] > 20


# -- acquisition -----------------------------------------------------------------------


async def test_acquisition_offsets_the_mount_then_guides():
    clock = VirtualClock()
    g, world = _rig(clock, star=(380.0, 200.0), mount=True, config={"capture_radius": 5.0})
    t0 = await _guide_for(g, clock, 1.0)
    assert len(g.mount.sent) >= 1
    assert world.residual_rms(TARGET, t0 + 0.5, t0 + 1.0) < 0.2
    assert g.reacquisitions == 0 and not g.alarms


async def test_acquisition_without_a_mount_fails_far_from_the_target():
    clock = VirtualClock()
    g, _ = _rig(clock, star=(380.0, 200.0), config={"max_reacquire": 1})
    g.start()
    await g.wait_state(("failed",), 30)
    assert "no calibrated mount" in g.reason or "acquisition failed" in g.reason
    assert [a.kind for a in g.alarms] == ["acquisition_failed"]


async def test_wrong_mount_calibration_is_not_converging():
    clock = VirtualClock()
    g, _ = _rig(clock, star=(380.0, 200.0), mount=True, config={"max_reacquire": 0})
    g.mount_cal = Calibration("mount", "fau", (-g.mount_cal.m).tolist())  # sign error
    g.start()
    await g.wait_state(("failed",), 60)
    assert "not_converging" in [a.kind for a in g.alarms]


async def test_star_lost_and_reacquired():
    clock = VirtualClock()
    g, world = _rig(clock, config={"exposure": 0.01, "lost_frames": 5})
    await _guide_for(g, clock, 0.5)
    world.transparency = 0.0  # a cloud
    await g.wait_state(("lost", "acquiring"), 5)
    world.transparency = 1.0
    await g.wait_state(("guiding",), 10)
    assert g.reacquisitions == 1
    assert [a.kind for a in g.alarms] == ["star_lost"]
    await g.stop()


async def test_star_gone_for_good_fails():
    clock = VirtualClock()
    g, world = _rig(clock, config={"exposure": 0.01, "lost_frames": 5, "max_reacquire": 2})
    await _guide_for(g, clock, 0.2)
    world.transparency = 0.0
    await g.wait_state(("failed",), 60)
    assert "star_lost" in [a.kind for a in g.alarms]


async def test_locate_the_backlit_fiber_and_guide_on_the_hole():
    clock = VirtualClock()
    fiber = Fiber(326.4, 243.7, radius=3.0)
    g, world = _rig(
        clock,
        star=(327.0, 245.0),
        jitter=_seeing(rms=0.3, shake=(), drift=(0.0, 0.0)),
        world_kw={"fiber": fiber},
        config={"target": None, "centroider": {"kind": "annulus"}, "exposure": 0.01, "fwhm": 4.0},
    )

    async def backlight(on: bool) -> None:
        world.backlit = on

    g.backlight = Backlight(backlight)
    x, y = await g.locate_fiber()
    assert (x, y) == pytest.approx((326.4, 243.7), abs=0.05)
    assert not world.backlit and g._centroider.name == "annulus"
    t0 = await _guide_for(g, clock, 3.0)
    assert world.residual_rms((x, y), t0 + 1.0, t0 + 3.0) < 0.15
    await g.stop()


# -- offloading, modes, setpoint ---------------------------------------------------------


async def test_offloading_keeps_the_tiptilt_in_range():
    clock = VirtualClock()
    g, world = _rig(
        clock,
        jitter=_seeing(rms=0.2, shake=(), drift=(1.5, -1.0)),  # a bad drift: px/s
        tt_limits=((-60.0, 60.0), (-60.0, 60.0)),
        mount=True,
        config={"exposure": 0.01, "gains": {"kp": 0.5, "ki": 0.02}},
    )
    g.offloader = Offloader(
        g.tiptilt,
        g.tt_cal,
        g.mount,
        g.mount_cal,
        OffloadConfig(threshold=0.4, period=5.0, settle=1.5),
        clock=clock,
    )
    t0 = await _guide_for(g, clock, 15.0)
    await g.stop()
    # Without offloading the drift (1.8 px/s = ~19 units/s) would hit the
    # limit in ~3 s.
    assert g.offloader.count >= 3
    assert world.residual_rms(TARGET, t0 + 1.0, t0 + 15.0) < 0.4
    assert "tiptilt_limit" not in [a.kind for a in g.alarms]


async def test_tiptilt_limit_alarm_without_offloading():
    clock = VirtualClock()
    g, _ = _rig(
        clock,
        jitter=_seeing(rms=0.1, shake=(), drift=(3.0, 0.0)),
        tt_limits=((-30.0, 30.0), (-30.0, 30.0)),
        config={"exposure": 0.01},
    )
    await _guide_for(g, clock, 4.0)
    await g.stop()
    assert "tiptilt_limit" in [a.kind for a in g.alarms]


async def test_open_mode_measures_but_moves_nothing():
    clock = VirtualClock()
    g, _ = _rig(clock, jitter=_seeing(), config={"mode": "open", "exposure": 0.01})
    await _guide_for(g, clock, 0.5)
    await g.stop()
    assert g.tiptilt.moves == 0
    assert g.summary()["residual_rms"] > 1.0


async def test_moving_the_target_does_not_reset_the_controller():
    clock = VirtualClock()
    g, world = _rig(
        clock, jitter=_seeing(rms=0.1, shake=()), config={"gains": {"kp": 0.4, "ki": 0.05}}
    )
    await _guide_for(g, clock, 2.0)
    integ = g.controller.integrator.copy()
    g.set_target(322.0, 241.0)
    np.testing.assert_array_equal(g.controller.integrator, integ)
    t1 = clock.now()
    while clock.now() < t1 + 1.0:
        await clock.sleep(0.05)
    assert world.residual_rms((322.0, 241.0), t1 + 0.5, t1 + 1.0) < 0.2
    await g.stop()


async def test_a_tiptilt_failure_fails_the_guider():
    clock = VirtualClock()
    g, _ = _rig(clock)
    await _guide_for(g, clock, 0.2)
    g.tiptilt.fail = "serial port gone"
    await g.wait_state(("failed",), 5)
    assert "serial port gone" in g.reason
    assert [a.kind for a in g.alarms] == ["device_error"]


async def test_start_refused_without_target_or_calibration():
    clock = VirtualClock()
    g, _ = _rig(clock, config={"target": None})
    with pytest.raises(Exception, match="no target"):
        g.start()
    g.set_target(*TARGET)
    g.tt_cal = None
    with pytest.raises(Exception, match="not calibrated"):
        g.start()


# -- telemetry -------------------------------------------------------------------------


async def test_values_and_tracking_data_are_published():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        def publish(self, channel, message):
            self.calls.append(("publish", channel, message))

    clock = VirtualClock()
    reg = ValueRegistry()
    redis = FakeRedis()
    g, _ = _rig(
        clock, jitter=_seeing(rms=0.3), config={"plate_scale": 0.5, "publish_interval": 0.5}
    )
    g.registry = reg
    g.publisher = TrackingPublisher(redis, plate_scale=0.5)
    await _guide_for(g, clock, 2.0)
    await asyncio.sleep(0.05)  # the executor's sends
    snap = reg.snapshot(["t2"])
    assert snap.current("t2.guide_state") == "guiding"
    assert reg.get("t2.guide_rms").keyword == "GUIDRMS"
    assert reg.get("t2.fiber_x").value == pytest.approx(TARGET[0])
    assert reg.get("t2.guide_rate").value == pytest.approx(50.0, rel=0.05)
    await g.stop()
    assert reg.get("t2.guide_state").value == "stopped"
    assert redis.calls and {c[0] for c in redis.calls} == {"publish"}
    msg = json.loads(redis.calls[-1][2])
    assert set(msg) == {
        "timestamp", "x_mispointing", "y_mispointing", "north_mispointing", "east_mispointing",
        "dx", "dy", "north", "east", "counts", "fwhm", "platescale",
        "roi_x1", "roi_x2", "roi_y1", "roi_y2",
    }  # fmt: skip
    assert msg["platescale"] == 0.5 and msg["roi_x2"] - msg["roi_x1"] == 48


# -- the service -----------------------------------------------------------------------


def _service_config(tmp_path, **kw) -> GuiderServiceConfig:
    d = {
        "name": "t2-guider",
        "scope": "t2",
        "listen": "127.0.0.1:0",
        "port": "fau",
        "calibration_dir": str(tmp_path / "cal"),
        "camera": {"kind": "sim"},
        "tiptilt": {"kind": "sim", "limits": [[-100, 100], [-100, 100]]},
        "mount": {"kind": "sim", "tau": 0.05, "delay": 0.01},
        "backlight": {"sim": True},
        "offload": {"threshold": 0.5, "period": 30},
        "guide": {
            "exposure": 0.01,
            "acq_exposure": 0.05,
            "acq_roi": [200, 160, 240, 160],
            "coarse_settle": 0.3,
            "plate_scale": 0.5,
            "centroider": {"kind": "annulus"},
        },
        "sim": {
            "star": [340.0, 255.0],
            "fiber": {"x": 321.5, "y": 239.5, "radius": 3.0},
            "jitter": {"rms": 0.1, "knee": 1.0, "seed": 5},
            "tt_matrix": [[0.08, 0.03], [-0.03, 0.08]],
        },
    }
    d.update(kw)
    return GuiderServiceConfig.from_dict(d)


async def test_service_end_to_end(tmp_path):
    cfg = _service_config(tmp_path, calibrations={})
    svc = await GuiderService(cfg).start()
    reg = ValueRegistry()
    remote = GuiderRemote(svc.url, scope="t2", registry=reg, acquire_timeout=20, poll_interval=0.2)
    try:
        import httpx

        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=30) as http:
            st = (await http.get("/status")).json()
            assert st["state"] == "idle" and st["calibrations"]["tiptilt"] is None
            # Not calibrated, no target yet.
            assert (await http.post("/start", json={})).status_code == 409
            r = await http.post("/locate", json={})
            assert r.status_code == 200
            # Located from a noisy backlit frame whose noise draw depends on
            # timing: within a few tenths (CI once saw 0.10 px off), as in
            # test_console_images.
            assert r.json()["target"] == pytest.approx([321.5, 239.5], abs=0.4)
            r = await http.post(
                "/calibrate",
                json={"device": "tiptilt", "step": 80, "exposure": 0.02, "frames": 5},
            )
            assert r.status_code == 200, r.text
            np.testing.assert_allclose(r.json()["matrix"], [[0.08, 0.03], [-0.03, 0.08]], atol=0.01)
            assert (tmp_path / "cal" / "simtt__fau.json").exists()
            r = await http.post(
                "/calibrate",
                json={"device": "mount", "step": 5, "exposure": 0.02, "frames": 3, "settle": 0.3},
            )
            assert r.status_code == 200, r.text
            assert (await http.post("/gains", json={"kp": 0.6, "ki": 0.01})).json()["gains"][
                "kp"
            ] == 0.6
            assert (await http.post("/gains", json={"kp": 9})).status_code == 400

            await remote.start(dec=20.0)
            assert (await http.post("/calibrate", json={"step": 10})).status_code == 409
            assert (await http.post("/start", json={})).status_code == 409
            await asyncio.sleep(1.0)
            st = (await http.get("/status")).json()
            assert st["state"] == "guiding", st["reason"]
            tel = st["telemetry"]
            assert tel["residual_rms"] < 1.0 and tel["loop_rate"] > 20
            assert tel["latency_median"] < 0.02
            assert st["offloads"] == 0
            await remote.check()
            assert reg.get("t2.guide_state").value == "guiding"
            assert reg.get("t2.guide_rms").keyword == "GUIDRMS"
            r = await http.post("/target", json={"x": 322.0, "y": 240.0})
            assert r.json()["target"] == [322.0, 240.0]
            assert (await http.post("/roi", json={"size": 40})).status_code == 200
            assert (await http.post("/exposure", json={"exposure": 0.02})).status_code == 200
            assert (await http.post("/mode", json={"mode": "bogus"})).status_code == 400
            recs = (await http.get("/telemetry?n=5")).json()["records"]
            assert len(recs) == 5 and {"latency", "ex", "tt_a"} <= set(recs[0])
            values = (await http.get("/values")).json()
            assert values["t2.guide_state"]["value"] == "guiding"
            assert await remote.stop("done")
            assert (await http.get("/status")).json()["state"] == "stopped"
            assert reg.get("t2.guide_state").value == "stopped"
            with pytest.raises(Exception, match="stopped"):
                remote.started = True
                await remote.check()
    finally:
        await remote.stop("teardown")
        await svc.stop()


async def test_remote_stop_never_raises_and_start_fails_cleanly(tmp_path):
    remote = GuiderRemote("http://127.0.0.1:9", scope="t1", timeout=0.5)
    assert await remote.stop("nobody there") is False
    cfg = _service_config(tmp_path, calibrations={})
    svc = await GuiderService(cfg).start()
    try:
        remote = GuiderRemote(svc.url, scope="t2", acquire_timeout=5)
        with pytest.raises(Exception, match="no target"):
            await remote.start()
        with pytest.raises(Exception, match="not calibrated"):
            await remote.start(target=[321.5, 239.5])
    finally:
        await svc.stop()


async def test_remote_stop_ends_a_poll_whose_cancel_was_lost():
    """A cancel landing inside an httpx request can be lost (the request
    returns normally). The status poll must still end, and stop() must
    return (bounded) and pass on its own caller's cancellation: in the e2e
    night a telescope hung for good in stop() after a spectrum, and once the
    sequencer quarantined itself because the cancel of the generation was
    swallowed there."""
    remote = GuiderRemote(
        "http://127.0.0.1:9", scope="t1", registry=ValueRegistry(), timeout=0.5, poll_interval=0.01
    )
    polls = lost = 0

    async def status():
        nonlocal polls, lost
        polls += 1
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if lost:
                raise
            lost += 1  # the first cancel is lost, as inside httpx
        return {"state": "guiding", "telemetry": {}}

    remote.status = status  # type: ignore[method-assign]
    remote.started = True
    remote._poll = asyncio.create_task(remote._poll_loop())
    await asyncio.sleep(0.1)
    poll = remote._poll
    stopping = asyncio.create_task(remote.stop("done"))
    done, _ = await asyncio.wait({stopping}, timeout=2)
    assert done, "stop() hangs on a status poll whose cancel was lost"
    done, _ = await asyncio.wait({poll}, timeout=1)
    assert done, "the status poll outlives stop()"
    n = polls
    await asyncio.sleep(0.1)
    assert polls == n, "the status poll goes on after stop()"

    # stop()'s own caller is cancelled while it waits for that poll: the
    # cancellation reaches the caller (stop() must not swallow it).
    async def slow_status():
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # lost, and the request goes on a while
        return {"state": "guiding", "telemetry": {}}

    remote.status = slow_status  # type: ignore[method-assign]
    remote._poll = asyncio.create_task(remote._poll_loop())
    await asyncio.sleep(0.03)  # inside slow_status
    caller = asyncio.create_task(remote.stop("done"))
    await asyncio.sleep(0.05)  # stop() waits for the poll
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 2)


def test_service_hardware_gate(tmp_path):
    for hw in (
        {"tiptilt": {"kind": "sxao", "port": "/dev/ttyUSB0"}},
        {"tiptilt": {"kind": "pi", "usb_serial": "0123"}},
        {"camera": {"kind": "zwo"}},
        {"camera": {"kind": "alpaca", "address": "192.0.2.1:11111"}},
        {"mount": {"kind": "alpaca", "address": "192.0.2.1:11111"}},
    ):
        cfg = _service_config(tmp_path, calibrations={}, allow_hardware=True, **hw)
        with pytest.raises(HardwareNotAllowed):
            GuiderService(cfg, hardware=False)
        cfg = _service_config(tmp_path, calibrations={}, **hw)
        with pytest.raises(HardwareNotAllowed):
            GuiderService(cfg, hardware=True)


async def test_service_with_the_protocol_simulators(tmp_path):
    """The SX AO and PI drivers, each on its protocol simulator, behind the
    service, guiding the simulated star."""
    for tt in (
        {"kind": "sxao-sim", "limits": [[-400, 400], [-400, 400]], "travel": 1000},
        {"kind": "pi-sim"},
    ):
        matrix = (
            [[0.05, 0.0], [0.0, 0.05]] if tt["kind"] == "sxao-sim" else [[40.0, 0.0], [0.0, -40.0]]
        )
        cfg = _service_config(
            tmp_path,
            tiptilt=tt,
            mount=None,
            offload=None,
            calibrations={},
            sim={"star": [322.0, 241.0], "tt_matrix": matrix, "jitter": {"rms": 0.1, "seed": 2}},
            guide={
                "exposure": 0.01,
                "target": [320.0, 240.0],
                "acq_roi": [200, 160, 240, 160],
                "acq_exposure": 0.05,
            },
        )
        svc = await GuiderService(cfg).start()
        try:
            step = 100 if tt["kind"] == "sxao-sim" else 0.1
            cal = await svc.calibrate(
                {"device": "tiptilt", "step": step, "exposure": 0.03, "frames": 4, "settle": 0.05}
            )
            np.testing.assert_allclose(cal.m, matrix, atol=0.05 * np.abs(matrix).max())
            svc.guider.start()
            await svc.guider.wait_state(("guiding", "failed"), 10)
            await asyncio.sleep(0.5)
            s = svc.guider.summary()
            assert svc.guider.state == "guiding" and s["residual_rms"] < 1.0, (tt, s)
        finally:
            await svc.stop()


async def test_cli_rejects_hardware_config_without_flag(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(
        json.dumps({"allow_hardware": True, "tiptilt": {"kind": "sxao", "port": "/dev/null"}})
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "robofast_core.guider.service",
        "--config",
        str(cfg),
        "--listen",
        "127.0.0.1:0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    t0 = time.monotonic()
    _, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 2, err.decode()
    assert b"allow_hardware" in err and time.monotonic() - t0 < 60


def test_roi_centred_stays_on_the_sensor():
    assert ROI.centred(5, 5, 32, (640, 480)) == ROI(0, 0, 32, 32)
    assert ROI.centred(639, 479, 32, (640, 480)) == ROI(608, 448, 32, 32)
    assert ROI.centred(320, 240, 32, (640, 480)).contains(320, 240)
    assert not math.isnan(ROI.centred(320.4, 240.6, 33, (640, 480)).x)


EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "guider.example.json")
with open(EXAMPLE) as _f:
    EXAMPLE_CONFIG = json.load(_f)


async def test_example_config_runs(tmp_path):
    """deploy/guider.example.json: loads, and guides once calibrated."""
    d = dict(EXAMPLE_CONFIG)
    d.update(listen="127.0.0.1:0", calibration_dir=str(tmp_path / "cal"))
    svc = await GuiderService(GuiderServiceConfig.from_dict(d)).start()
    try:
        await svc.guider.locate_fiber()
        cal = await svc.calibrate(
            {"device": "tiptilt", "step": 150, "exposure": 0.02, "frames": 4, "settle": 0.05}
        )
        assert not cal.flip and cal.residual < 0.6
        # The mount's calibration from the simulator's truth (measuring it is
        # covered above; with this seeing it needs long averages).
        world = svc.parts.world
        svc.guider.mount_cal = Calibration("mount", "fau", world.mount_matrix.tolist())
        svc._rebuild_offloader()
        assert svc.guider.offloader is not None
        svc.guider.start()
        assert await svc.guider.wait_state(("guiding", "failed"), 20) == "guiding"
    finally:
        await svc.stop()
