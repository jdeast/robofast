"""S1-S4 end to end on pure-Python fakes (fast; no simulator).

Each runs twice: with the sequencer holding the supervisor in-process
("local"), and through the supervisor's HTTP lease API ("remote"). The same
scenarios run against OmniSim in tests/sim/.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast_core.testing.checks import (
    assert_fenced_after_cancel,
    assert_generations_disjoint,
    wait_until,
)

pytestmark = pytest.mark.parametrize("lease_path", ["local", "remote"])

DEVICES = ("t1.telescope", "t1.camera")
BOUND = 3.0  # seconds, for the fast fake timings in conftest


async def _observing(rig):
    await rig.start()
    await wait_until(rig.exposing, 5, what="camera exposing")
    assert rig.dome_open() and not rig.parked()


async def _safe_state_reached(rig, bound=BOUND) -> float:
    t0 = time.monotonic()
    await wait_until(
        lambda: rig.dome_closed() and rig.parked() and rig.camera_idle(),
        bound,
        what="closed, parked and camera idle",
    )
    return time.monotonic() - t0


async def test_s1_unsafe_aborts_parks_closes(make_rig):
    rig = make_rig()
    await _observing(rig)
    rig.weather.set_safe(False)
    dt = await _safe_state_reached(rig)
    assert rig.cam.exposures_aborted == 1
    await wait_until(lambda: rig.timeline.events("cleanup_done"), 2)
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)
    report = rig.sequencer.cleanups[0]
    assert report.aborted and report.parked and not report.errors
    assert not rig.sequencer.frames  # the 5 s exposure never completed
    print(f"S1 (fakes): safe state {dt:.2f} s after unsafe")


async def test_s2_stale_feed(make_rig):
    rig = make_rig()
    await _observing(rig)
    rig.weather.freeze()
    await _safe_state_reached(rig, BOUND + 1)
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s2_weather_error(make_rig):
    rig = make_rig()
    await _observing(rig)
    rig.sm.fail("HTTP 500")
    await _safe_state_reached(rig)
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s3_reopen_after_hold_and_fresh_generation(make_rig):
    rig = make_rig(safety=dict(reopen_hold=0.8))
    await _observing(rig)
    rig.weather.set_safe(False)
    await _safe_state_reached(rig)
    await asyncio.sleep(0.3)
    t_safe = time.monotonic()
    rig.weather.set_safe(True)
    await wait_until(lambda: rig.timeline.events("observe_start", "seq")[1:], 5)
    await wait_until(rig.exposing, 5)
    tl = rig.timeline
    reopen = [e for e in tl.events("open_commanded") if e.t > t_safe]
    assert reopen and reopen[0].t - t_safe >= 0.8 - 0.06
    assert_fenced_after_cancel(tl, "seq", 1, DEVICES)
    assert_generations_disjoint(tl, "seq")
    # generation 2 used new grants; generation 1's were all revoked
    assert tl.commands(source_prefix="seq/observe#2", outcome="ok")


async def test_s3_stuck_task_is_quarantined_and_never_revived(make_rig, monkeypatch):
    """An observing task that ignores cancellation: no reopen, no new task."""
    rig = make_rig(seq=dict(cancel_ack_timeout=0.3), safety=dict(reopen_hold=0.2))
    seq = rig.sequencer
    original = seq._observe

    async def stubborn(*args):
        try:
            await original(*args)
        except asyncio.CancelledError:
            await asyncio.sleep(3)  # misbehaves: swallows the cancel for a while

    monkeypatch.setattr(seq, "_observe", stubborn)
    await _observing(rig)
    rig.weather.set_safe(False)
    await _safe_state_reached(rig)
    rig.weather.set_safe(True)
    await asyncio.sleep(1.5)  # far past the hold
    assert seq.quarantined
    await wait_until(lambda: rig.supervisor.lease.quarantined, 1, what="supervisor told")
    assert rig.timeline.events("cancel_not_acknowledged")
    assert len(rig.timeline.events("observe_start")) == 1
    assert rig.dome_closed()
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s3_new_generation_waits_for_slow_cleanup(make_rig):
    rig = make_rig(safety=dict(reopen_hold=0.1), command_timeout=1.0)
    await _observing(rig)
    rig.tel.hang()  # cleanup's park will block until we let it go
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, BOUND)
    rig.weather.set_safe(True)
    await asyncio.sleep(0.5)
    assert len(rig.timeline.events("observe_start")) == 1
    rig.tel.unhang()
    await wait_until(lambda: len(rig.timeline.events("observe_start")) == 2, 5)
    assert_generations_disjoint(rig.timeline, "seq")


async def test_s4_camera_hangs_mid_exposure(make_rig):
    rig = make_rig(command_timeout=0.5)
    await _observing(rig)
    rig.cam.hang()
    t0 = time.monotonic()
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)
    await wait_until(lambda: rig.timeline.events("cleanup_done"), BOUND)
    dt = time.monotonic() - t0
    report = rig.sequencer.cleanups[0]
    assert report.parked
    assert any("did not complete" in e for e in report.errors), report.errors
    assert rig.timeline.commands(device="t1.camera", outcome="timeout")
    assert dt < BOUND
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_s4_telescope_disappears(make_rig):
    rig = make_rig()
    await _observing(rig)
    rig.tel.fail("connection refused")
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.camera_idle(), BOUND)
    await wait_until(lambda: rig.timeline.events("cleanup_done"), BOUND)
    report = rig.sequencer.cleanups[0]
    assert not report.parked
    assert any("connection refused" in e for e in report.errors)


async def test_s4_dome_hangs_alarm_raised(make_rig):
    """If the dome itself stops answering, all we can do is alarm and retry."""
    rig = make_rig()
    await _observing(rig)
    rig.dome.hang()
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.parked() and rig.camera_idle(), BOUND)
    await wait_until(lambda: rig.supervisor.alarms, BOUND)
    rig.dome.unhang()
    await wait_until(rig.dome_closed, BOUND)


async def test_s4_camera_dies_on_its_own_backoff_then_give_up(make_rig):
    """A device failure (weather fine) ends the generation: park, release the
    lease (so the enclosure closes), back off, retry, and give up rather than
    cycling the enclosure forever."""
    rig = make_rig(safety=dict(reopen_hold=0.1))
    await _observing(rig)
    rig.cam.fail("camera gone")
    # (the abort cannot reach a dead camera, so only closed + parked here)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)
    tl = rig.timeline
    assert tl.events("observe_finished")[0].detail["outcome"] == "error"
    assert not tl.events("cancel_requested")  # nobody had to cancel it
    assert rig.sequencer.cleanups[0].parked
    await wait_until(lambda: rig.sequencer.gave_up, 5, what="giving up")
    assert len(tl.events("observe_start")) == 2  # one retry after the backoff
    gap = tl.events("observe_start")[1].t - tl.events("lease_released")[0].t
    assert gap >= 0.5
    await wait_until(rig.dome_closed, 2)
    assert_generations_disjoint(tl, "seq")
