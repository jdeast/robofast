"""S1-S4 against the ASCOM Alpaca OmniSimulators (telescope, camera, dome)
plus the robofast weather server (SafetyMonitor, ObservingConditions).

Ground truth comes from :class:`Truth` (plain alpyca clients reading
OmniSim directly); the "nothing else commanded the devices" assertions come
from the owners' command log.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from robofast_core.testing.checks import (
    assert_fenced_after_cancel,
    assert_generations_disjoint,
    await_until,
    wait_until,
)

from .conftest import BOUND, SIM_SAFETY

pytestmark = pytest.mark.alpaca_sim

DEVICES = ("telescope", "camera")


async def _observing(rig, truth) -> None:
    await rig.start()
    await await_until(truth.exposing, 90, what="OmniSim camera exposing")
    assert truth.open() and not truth.parked()
    await asyncio.sleep(1.0)  # well inside the 60 s exposure


async def _measure(truth, t0: float, bound: float, *, camera: bool = True) -> dict[str, float]:
    """Seconds from ``t0`` until each of closed/parked/(camera idle) first held."""
    probes = {"closed": truth.closed, "parked": truth.parked}
    if camera:
        probes["camera_idle"] = lambda: truth.camera_state() == 0
    seen: dict[str, float] = {}
    while len(seen) < len(probes):
        for name, probe in probes.items():
            if name not in seen and await asyncio.to_thread(probe):
                seen[name] = time.monotonic() - t0
        if time.monotonic() - t0 > bound:
            missing = sorted(set(probes) - set(seen))
            raise AssertionError(f"not reached within {bound} s: {missing} (got {seen})")
        await asyncio.sleep(0.1)
    seen["all"] = max(seen.values())
    return seen


async def _cleanup_done(rig, gen: int = 1) -> None:
    await wait_until(
        lambda: any(e.detail["gen"] == gen for e in rig.timeline.events("cleanup_done")),
        BOUND,
        what="cleanup_done",
    )


async def test_s1_unsafe_mid_exposure(make_sim_rig, truth, record_timing):
    rig = make_sim_rig()
    await _observing(rig, truth)
    t0 = time.monotonic()
    rig.weather.update(safe=False)
    times = await _measure(truth, t0, BOUND)
    await _cleanup_done(rig)
    record_timing(**times)
    assert times["all"] < BOUND
    report = rig.sequencer.cleanups[0]
    assert report.aborted and report.parked and not report.errors, report
    assert not rig.sequencer.frames
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)
    # the observe task's cancellation was acknowledged, not timed out
    assert not rig.sequencer.quarantined


async def test_s2_weather_feed_goes_stale(make_sim_rig, truth, record_timing):
    rig = make_sim_rig()
    await _observing(rig, truth)
    t0 = time.monotonic()
    rig.weather.stop_feed()  # IsSafe stays True; TimeSinceLastUpdate grows
    times = await _measure(truth, t0, BOUND)
    await _cleanup_done(rig)
    record_timing(**times)
    assert times["all"] >= SIM_SAFETY["stale_after"]  # not before it was stale
    assert "stale" in rig.timeline.events("lease_revoked")[0].detail["reason"]
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s2_weather_server_errors(make_sim_rig, truth, record_timing):
    rig = make_sim_rig()
    await _observing(rig, truth)
    t0 = time.monotonic()
    rig.weather.set_mode("error")
    times = await _measure(truth, t0, BOUND)
    await _cleanup_done(rig)
    record_timing(**times)
    assert "read failed" in rig.timeline.events("lease_revoked")[0].detail["reason"]
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s3_hold_then_fresh_generation(make_sim_rig, truth, record_timing):
    rig = make_sim_rig()
    await _observing(rig, truth)
    rig.weather.update(safe=False)
    await _measure(truth, time.monotonic(), BOUND)
    await _cleanup_done(rig)
    await asyncio.sleep(1.0)
    assert truth.closed()
    t_safe = time.monotonic()
    rig.weather.update(safe=True)
    await await_until(lambda: truth.open(), 60, what="reopen")
    t_open = time.monotonic() - t_safe
    await await_until(truth.exposing, 60, what="generation 2 exposing")
    tl = rig.timeline
    reopen_cmd = next(e for e in tl.events("open_commanded") if e.t > t_safe).t - t_safe
    record_timing(open_commanded_after_safe=reopen_cmd, open_after_safe=t_open)
    assert reopen_cmd >= SIM_SAFETY["reopen_hold"] - SIM_SAFETY["poll_interval"]
    assert [e.detail["gen"] for e in tl.events("observe_start")] == [1, 2]
    assert_fenced_after_cancel(tl, "seq", 1, DEVICES)
    assert_generations_disjoint(tl, "seq")


async def test_s4_camera_hangs(make_sim_rig, truth, camera_proxy, record_timing):
    rig = make_sim_rig(camera_address=camera_proxy.address)
    await _observing(rig, truth)
    camera_proxy.set_mode("hang")
    t0 = time.monotonic()
    rig.weather.update(safe=False)
    times = await _measure(truth, t0, BOUND, camera=False)
    await _cleanup_done(rig)
    record_timing(**times, cleanup=rig.sequencer.cleanups[0].duration)
    report = rig.sequencer.cleanups[0]
    assert report.parked and not report.aborted
    assert any("did not complete" in e for e in report.errors), report.errors
    assert rig.timeline.commands(device="camera", outcome="timeout")
    # the abort never reached the camera, and we say so rather than pretend
    assert truth.exposing()
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s4_camera_disappears(make_sim_rig, truth, camera_proxy, record_timing):
    """The camera vanishes mid-exposure with the weather still fine: the
    observing task fails on its own, cleanup parks (and reports the abort it
    could not deliver), the lease is released so the enclosure closes, and
    the sequencer backs off instead of reopening into a dead camera."""
    rig = make_sim_rig(camera_address=camera_proxy.address)
    await _observing(rig, truth)
    t0 = time.monotonic()
    camera_proxy.set_mode("refuse")
    times = await _measure(truth, t0, BOUND, camera=False)
    await _cleanup_done(rig)
    record_timing(**times)
    tl = rig.timeline
    assert tl.events("observe_finished")[0].detail["outcome"] == "error"
    report = rig.sequencer.cleanups[0]
    assert report.parked and not report.aborted and report.errors
    assert tl.events("error_backoff")
    # weather now turns bad as well: still closed, nothing reopens
    rig.weather.update(safe=False)
    await asyncio.sleep(2)
    assert truth.closed() and len(tl.events("observe_start")) == 1
    late = [
        e
        for e in tl.commands(outcome="sent", since=tl.events("cleanup_done")[0].t)
        if e.device in DEVICES
    ]
    assert not late, late


async def test_s4_weather_server_hangs(make_sim_rig, truth, record_timing):
    rig = make_sim_rig()
    await _observing(rig, truth)
    t0 = time.monotonic()
    rig.weather.set_mode("hang")
    times = await _measure(truth, t0, BOUND)
    await _cleanup_done(rig)
    record_timing(**times)
    assert rig.timeline.commands(device="safetymonitor", outcome="timeout")
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


def _blocked_read_latency(omnisim: str, weather_address: str, isolate: bool) -> float:
    """While one client's request to a hung weather server is in flight, how
    long does an unrelated read of the OmniSim dome take?"""
    from alpaca.dome import Dome
    from alpaca.safetymonitor import SafetyMonitor

    from robofast_core.alpaca import isolated_class

    sm_cls = isolated_class(SafetyMonitor) if isolate else SafetyMonitor
    dome_cls = isolated_class(Dome) if isolate else Dome
    sm, dome = sm_cls(weather_address, 0), dome_cls(omnisim, 0)
    dome.Connected = True
    _ = dome.ShutterStatus  # warm the HTTP session
    hung = threading.Thread(target=lambda: _swallow(lambda: sm.IsSafe), daemon=True)
    hung.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    _ = dome.ShutterStatus
    dt = time.monotonic() - t0
    hung.join(10)
    return dt


def _swallow(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def test_stock_alpyca_lock_blocks_other_devices(omnisim, weather, record_timing):
    """Evidence for the alpyca shim: stock alpyca serializes all devices."""
    state, server = weather
    state.set_mode("hang")
    stock = _blocked_read_latency(omnisim, server.address, isolate=False)
    isolated = _blocked_read_latency(omnisim, server.address, isolate=True)
    state.set_mode("ok")
    record_timing(stock_dome_read=stock, isolated_dome_read=isolated)
    assert stock > 3.0  # waited for the hung request's 5 s timeout
    assert isolated < 1.0
