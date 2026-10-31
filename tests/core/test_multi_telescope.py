"""Several telescopes in one enclosure (design.md 3.5), on fakes.

One sequencer, one lease, grants per telescope; one telescope's trouble must
not end the other's night. The supervisor's park checks cover every
telescope.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast_core.plan import CallablePlan, Observation, StaticPlan
from robofast_core.sequencer import SlewDenied
from robofast_core.testing.checks import (
    assert_fenced_after_cancel,
    assert_generations_disjoint,
    wait_until,
)

DEVICES = ("t1.telescope", "t1.camera", "t2.telescope", "t2.camera")
BOUND = 3.0


async def _both_observing(rig) -> None:
    await rig.start()
    await wait_until(rig.exposing, 5, what="both cameras exposing")
    assert rig.dome_open() and not rig.parked(0) and not rig.parked(1)


def _frames(rig, telescope: str, since: float = 0.0) -> int:
    return sum(1 for f in rig.sequencer.frames if f["telescope"] == telescope and f["t"] > since)


@pytest.mark.parametrize("lease_path", ["local", "remote"])
async def test_s1_two_telescopes_abort_park_close(make_rig):
    rig = make_rig(telescopes=2)
    await _both_observing(rig)
    t0 = time.monotonic()
    rig.weather.set_safe(False)
    await wait_until(
        lambda: rig.dome_closed() and rig.parked() and rig.camera_idle(),
        BOUND,
        what="closed, both parked, both cameras idle",
    )
    dt = time.monotonic() - t0
    await wait_until(lambda: rig.sequencer.cleanups, 2)
    report = rig.sequencer.cleanups[0]
    assert sorted(report.aborted) == sorted(report.parked) == ["t1", "t2"]
    assert not report.errors and report.all_parked
    assert [c.exposures_aborted for c in rig.cams] == [1, 1]
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)
    print(f"S1 two telescopes (fakes): safe state {dt:.2f} s after unsafe")


async def test_one_camera_hangs_the_other_keeps_observing(make_rig):
    rig = make_rig(telescopes=2, exptime=0.3, count=1000)
    await _both_observing(rig)
    rig.cams[0].hang()
    t_hang = time.monotonic()
    tl = rig.timeline
    await wait_until(lambda: tl.events("telescope_safed"), BOUND, what="t1 safed")
    error = tl.events("telescope_error")[0]
    assert error.detail["telescope"] == "t1" and "did not complete" in error.detail["error"]
    assert rig.parked(0)
    t_safe = time.monotonic()
    await wait_until(lambda: _frames(rig, "t2", since=t_safe) >= 3, 3, what="t2 frames")
    assert rig.dome_open() and not rig.parked(1)
    assert len(tl.events("observe_start")) == 1  # the same generation, same lease
    assert rig.supervisor.lease.live
    # t1 retries after its backoff, fails again, and gives up; t2 carries on.
    await wait_until(lambda: rig.sequencer.units["t1"].gave_up, 5, what="t1 gives up")
    since = time.monotonic()
    await wait_until(lambda: _frames(rig, "t2", since=since) >= 2, 3, what="t2 still going")
    assert not rig.sequencer.gave_up and rig.dome_open()
    print(f"camera hang: t1 safed {t_safe - t_hang:.2f} s after the hang; t2 kept observing")


async def test_one_telescope_errors_repeatedly_gives_up_the_other_continues(make_rig):
    rig = make_rig(telescopes=2, exptime=0.3, count=1000)
    await _both_observing(rig)
    rig.cams[0].fail("camera gone")
    seq, tl = rig.sequencer, rig.timeline
    await wait_until(lambda: seq.units["t1"].gave_up, 5, what="t1 gives up")
    errors = [e for e in tl.events("telescope_error") if e.detail["telescope"] == "t1"]
    assert [e.detail["errors"] for e in errors] == [1, 2]  # max_consecutive_errors = 2
    assert errors[1].t - errors[0].t >= 0.5  # error_backoff
    assert tl.events("giving_up")[0].detail["telescope"] == "t1"
    assert rig.parked(0)
    since = time.monotonic()
    await wait_until(lambda: _frames(rig, "t2", since=since) >= 3, 3, what="t2 frames")
    assert rig.dome_open() and len(tl.events("observe_start")) == 1
    assert not seq.gave_up and seq.needs_operator
    assert not any(e.detail["telescope"] == "t2" for e in tl.events("telescope_error"))
    # Weather now closes the enclosure: the next generation leaves t1 out.
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND, what="closed, parked")
    rig.weather.set_safe(True)
    await wait_until(lambda: len(tl.events("observe_start")) == 2, 5, what="generation 2")
    assert tl.events("observe_start")[1].detail["telescopes"] == ["t2"]


async def test_both_fail_the_enclosure_closes_during_the_backoff(make_rig):
    """With no telescope left working, the generation ends: the lease is
    released (closing the enclosure) and the next waits a full backoff."""
    rig = make_rig(telescopes=2, safety=dict(reopen_hold=0.1))
    await _both_observing(rig)
    for cam in rig.cams:
        cam.fail("camera gone")
    tl = rig.timeline
    await wait_until(lambda: tl.events("lease_released"), BOUND, what="lease released")
    await wait_until(rig.dome_closed, BOUND)
    await wait_until(lambda: rig.sequencer.gave_up, 5, what="every telescope gave up")
    assert len(tl.events("observe_start")) == 2
    gap = tl.events("observe_start")[1].t - tl.events("lease_released")[0].t
    assert gap >= 0.5
    assert tl.events("night_over")
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 2)
    assert_generations_disjoint(tl, "seq")


async def test_a_buggy_plan_for_one_telescope_does_not_stop_the_other(make_rig):
    def plan(telescope: str) -> Observation:
        if telescope == "t1":
            raise RuntimeError("scheduler bug")
        return Observation("field", exptime=0.3, count=1000)

    rig = make_rig(telescopes=2, plan=CallablePlan(plan))
    await rig.start()
    await wait_until(lambda: rig.sequencer.units["t1"].gave_up, 5, what="t1 gives up")
    since = time.monotonic()
    await wait_until(lambda: _frames(rig, "t2", since=since) >= 2, 3, what="t2 frames")
    assert rig.dome_open() and rig.parked(0)


async def test_stale_grants_refused_on_every_telescope(make_rig, monkeypatch):
    """Second-client interference, per telescope: after the generation is
    cancelled, its task tries to slew each telescope (as a legacy thread
    would); every attempt is refused before it reaches the wire."""
    rig = make_rig(telescopes=2, safety=dict(reopen_hold=0.3))
    seq = rig.sequencer
    original = seq._observe

    async def stale(state, unit, tel, cam, obs, taken):
        try:
            await original(state, unit, tel, cam, obs, taken)
        except asyncio.CancelledError:
            try:
                await tel.call("SlewToCoordinatesAsync", 1.0, 1.0)
            finally:
                raise

    monkeypatch.setattr(seq, "_observe", stale)
    await _both_observing(rig)
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)
    tl = rig.timeline
    refused = tl.commands(source_prefix="seq/observe#1", outcome="rejected")
    assert {e.device for e in refused if e.what == "SlewToCoordinatesAsync"} == {
        "t1.telescope",
        "t2.telescope",
    }
    assert_fenced_after_cancel(tl, "seq", 1, DEVICES)
    rig.weather.set_safe(True)
    await wait_until(lambda: len(tl.events("observe_start")) == 2, 5, what="generation 2")
    await wait_until(rig.exposing, 5)
    assert_generations_disjoint(tl, "seq")


class DenyFor:
    """A SlewGuard that refuses every slew of the named telescopes."""

    def __init__(self, *telescopes: str) -> None:
        self.telescopes = set(telescopes)
        self.asked: list[str] = []

    async def check_slew(self, telescope: str, ra_hours: float, dec_deg: float) -> None:
        self.asked.append(telescope)
        if telescope in self.telescopes:
            raise SlewDenied(f"{telescope} would collide")


async def test_denied_slew_never_reaches_the_mount_and_cleanup_still_parks(make_rig):
    guard = DenyFor("t1")
    rig = make_rig(telescopes=2, slew_guard=guard)
    await rig.start()
    tl, seq = rig.timeline, rig.sequencer
    await wait_until(lambda: seq.units["t1"].gave_up, 5, what="t1 gives up")
    assert "would collide" in tl.events("telescope_error")[0].detail["error"]
    sent = tl.commands(device="t1.telescope", outcome="sent")
    assert not [e for e in sent if e.what == "SlewToCoordinatesAsync"]
    assert rig.parked(0)  # it had been unparked for the slew; its safing parked it
    assert rig.exposing(1) and {"t1", "t2"} <= set(guard.asked)
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.parked() and rig.camera_idle(), BOUND)
    await wait_until(lambda: seq.cleanups, 2)
    assert seq.cleanups[0].all_parked and not seq.cleanups[0].errors


async def test_a_single_telescope_denied_closes_and_gives_up(make_rig):
    rig = make_rig(slew_guard=DenyFor("t1"), safety=dict(reopen_hold=0.1))
    await rig.start()
    await wait_until(lambda: rig.sequencer.gave_up, 5, what="gives up")
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)
    sent = rig.timeline.commands(device="t1.telescope", outcome="sent")
    assert not [e for e in sent if e.what == "SlewToCoordinatesAsync"]
    assert any(e.what == "Park" for e in sent)


async def test_static_plan_retries_the_interrupted_target(make_rig):
    targets = [
        {"name": "A", "exptime": 5.0, "count": 1},
        {"name": "B", "exptime": 0.2, "count": 1, "telescopes": ["t2"]},
    ]
    plan = StaticPlan(targets, ["t1", "t2"])
    rig = make_rig(telescopes=2, plan=plan, safety=dict(reopen_hold=0.2))
    await rig.start()
    await wait_until(lambda: rig.exposing(0), 5)
    rig.weather.set_safe(False)  # A is interrupted on t1 (and maybe on t2)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)
    assert ("t1", "A", "interrupted", 0) in plan.history
    rig.weather.set_safe(True)
    # A runs again (5 s); then both telescopes finish and the night ends.
    await wait_until(lambda: rig.timeline.events("plan_exhausted"), 15, what="plan done")
    assert ("t1", "A", "complete", 1) in plan.history
    assert ("t2", "B", "complete", 1) in plan.history
    await wait_until(lambda: rig.dome_closed() and rig.parked(), BOUND)


# -- the supervisor and several telescopes ----------------------------------------


async def test_no_lease_unless_every_telescope_is_parked(make_rig):
    rig = make_rig(telescopes=2, supervise_parking=True, safety=dict(reopen_hold=0.1))
    rig.tels[1].Unpark()
    await rig.start(sequencer=False)
    req = asyncio.create_task(rig.supervisor.request_open("x"))
    await wait_until(lambda: rig.supervisor.alarms, 2, what="alarm")
    await asyncio.sleep(0.5)
    assert not req.done()
    assert "not parked: t2.telescope" in rig.supervisor.alarms[-1]
    rig.tels[1].Park()
    lease = await asyncio.wait_for(req, 3)
    lease.release()


async def test_no_lease_while_a_telescope_is_unreadable(make_rig):
    rig = make_rig(telescopes=2, supervise_parking=True, safety=dict(reopen_hold=0.1))
    rig.tels[0].fail("mount PC down")
    await rig.start(sequencer=False)
    req = asyncio.create_task(rig.supervisor.request_open("x"))
    await wait_until(lambda: rig.supervisor.alarms, 2, what="alarm")
    await asyncio.sleep(0.5)
    assert not req.done()
    alarm = rig.supervisor.alarms[-1]
    assert "cannot confirm parked: t1.telescope" in alarm and "mount PC down" in alarm
    rig.tels[0].unfail()
    lease = await asyncio.wait_for(req, 3)
    lease.release()


async def test_roll_off_parks_every_telescope_in_parallel_before_closing(make_rig):
    rig = make_rig(telescopes=2, safety=dict(park_before_close=True, park_wait=2.0))
    await rig.start(sequencer=False)
    lease = await asyncio.wait_for(rig.supervisor.request_open("x"), 5)
    await asyncio.wait_for(lease.wait_open(), 5)
    for tel in rig.tels:
        tel.Unpark()
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, 4)
    assert rig.parked()
    tl = rig.timeline
    parks = [e for e in tl.commands(source_prefix="safety", outcome="sent") if e.what == "Park"]
    close = min(e.t for e in tl.commands(device="dome", outcome="sent") if e.what == "CloseShutter")
    assert {e.device for e in parks} == {"t1.telescope", "t2.telescope"}
    assert max(e.t for e in parks) < close
    # parallel: the second Park went out before the first telescope had parked (0.3 s)
    assert abs(parks[0].t - parks[1].t) < 0.15
    lease.release()


async def test_roll_off_closes_anyway_when_one_telescope_hangs(make_rig):
    park_wait = 1.0
    rig = make_rig(telescopes=2, safety=dict(park_before_close=True, park_wait=park_wait))
    await rig.start(sequencer=False)
    lease = await asyncio.wait_for(rig.supervisor.request_open("x"), 5)
    await asyncio.wait_for(lease.wait_open(), 5)
    for tel in rig.tels:
        tel.Unpark()
    rig.tels[0].hang()
    t0 = time.monotonic()
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, park_wait + 2)
    # bounded: half park_wait for the sequencer, half for parking (in parallel)
    assert time.monotonic() - t0 < park_wait + 1.0
    assert rig.parked(1) and not rig.parked(0)
    assert any("t1.telescope not confirmed parked" in a for a in rig.supervisor.alarms)
    lease.release()
