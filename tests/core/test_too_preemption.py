"""Targets of opportunity through the sequencer (design.md 3.7.1), on fakes.

Two telescopes in one enclosure, the dispatch scheduler as the plan (with a
fixed clock: astronomy is frozen at one instant of a MINERVA night while the
sequencer runs in real time). ``a`` runs on t1 and ``b`` on t2, forever; a
ToO for t1 must interrupt t1 at the point its policy defines, while t2 keeps
observing, the lease stays the same and nothing is parked.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from robofast_core.dispatch import DispatchScheduler
from robofast_core.request_model import Status
from robofast_core.testing.checks import assert_fenced_after_cancel, wait_until
from tests.core.test_dispatch import NIGHT, Clock, group, make, too

DEVICES = ("t1.telescope", "t1.camera", "t2.telescope", "t2.camera")


def scheduler(exptime: float = 1.0, count: int = 100, clock=None) -> DispatchScheduler:
    forever = {"min_interval": 0}
    return make(
        [
            group("a", telescopes=["t1"], exptime=exptime, count=count, cadence=forever),
            group("b", telescopes=["t2"], exptime=exptime, count=count, cadence=forever),
        ],
        clock=clock,
    )


def burst(policy: str, gid: str = "burst", **kw) -> dict:
    kw.setdefault("telescopes", ["t1"])
    return group(gid, too=too(policy, **kw.pop("too", {})), exptime=0.2, count=2, **kw)


def starts(rig, telescope: str, prefix: str):
    return [
        e
        for e in rig.timeline.events("target_start")
        if e.detail["telescope"] == telescope and e.detail["target"].startswith(prefix)
    ]


def frames(rig, telescope: str, since: float) -> int:
    return sum(1 for f in rig.sequencer.frames if f["telescope"] == telescope and f["t"] > since)


async def observing(make_rig, plan, **kw):
    rig = make_rig(telescopes=2, plan=plan, **kw)
    await rig.start()
    await wait_until(rig.exposing, 5, what="both cameras exposing")
    return rig


def assert_t2_undisturbed(rig, since: float) -> None:
    assert rig.cams[1].exposures_aborted == 0
    assert not any(e.detail["telescope"] == "t2" for e in rig.timeline.events("preempted"))
    assert len(starts(rig, "t2", "b ")) == 1  # its one observation, never restarted
    assert len(rig.timeline.events("observe_start")) == 1  # same generation, same lease
    assert not rig.timeline.events("cleanup_start", "seq")[1:]  # only the startup cleanup
    assert rig.supervisor.lease.live and rig.dome_open()
    assert frames(rig, "t2", since) >= 1


async def test_policy_now_aborts_at_once(make_rig):
    plan = scheduler()
    rig = await observing(make_rig, plan)
    await asyncio.sleep(0.3)  # mid-exposure
    t0 = time.monotonic()
    await plan.submit_too(burst("now"))
    await wait_until(lambda: starts(rig, "t1", "burst"), 3, what="t1 starts the ToO")
    t_start = time.monotonic() - t0
    (pre,) = rig.timeline.events("preempted")
    assert pre.detail["telescope"] == "t1" and pre.detail["policy"] == "now"
    assert pre.detail["aborted"]
    assert rig.cams[0].exposures_aborted == 1
    assert not rig.parked(0)
    await wait_until(lambda: plan.status("burst-c") is Status.COMPLETED, 5, what="ToO done")
    assert ("t1", "a [a-c#0]", "interrupted", 0) in plan.history
    assert_t2_undisturbed(rig, t0)
    print(
        f"ToO now: preempted {pre.detail['latency'] * 1e3:.0f} ms after preempt(); "
        f"ToO target_start {t_start * 1e3:.0f} ms after submit"
    )


async def test_policy_now_after_readout_waits_for_the_exposure(make_rig):
    plan = scheduler(exptime=1.0)
    rig = await observing(make_rig, plan)
    await asyncio.sleep(0.3)  # 0.7 s left in t1's exposure
    t0 = time.monotonic()
    await plan.submit_too(burst("now-after-readout"))
    await wait_until(lambda: rig.timeline.events("preempted"), 3, what="preempted")
    (pre,) = rig.timeline.events("preempted")
    assert pre.detail["policy"] == "now-after-readout" and not pre.detail["aborted"]
    assert 0.4 < pre.detail["latency"] < 1.5  # the rest of the exposure, then stop
    done = [
        e
        for e in rig.timeline.events("exposure_complete")
        if e.detail["telescope"] == "t1" and e.t <= pre.t
    ]
    assert done and done[-1].t >= t0  # it waited for the frame in progress
    assert rig.cams[0].exposures_aborted == 0
    await wait_until(lambda: starts(rig, "t1", "burst"), 3, what="t1 starts the ToO")
    interrupted = [h for h in plan.history if h[0] == "t1" and h[2] == "interrupted"]
    assert interrupted and interrupted[0][3] >= 1  # the frame it waited for counts
    assert_t2_undisturbed(rig, t0)
    print(f"ToO now-after-readout: preempted {pre.detail['latency'] * 1e3:.0f} ms after preempt()")


async def test_policy_now_after_readout_stops_a_slew_at_once(make_rig):
    """No exposure in progress (slewing): nothing to wait for."""
    plan = scheduler()
    rig = make_rig(telescopes=2, plan=plan)
    for tel in rig.tels:
        tel._slew_time = 3.0
    await rig.start()
    await wait_until(lambda: rig.tels[0].Slewing and not rig.tels[0]._parked, 5, what="t1 slewing")
    await plan.submit_too(burst("now-after-readout"))
    await wait_until(lambda: rig.timeline.events("preempted"), 2, what="preempted")
    (pre,) = rig.timeline.events("preempted")
    assert pre.detail["latency"] < 0.5
    sent = [c.what for c in rig.timeline.commands(outcome="sent") if c.device == "t1.telescope"]
    assert "AbortSlew" in sent and "Park" not in sent[sent.index("AbortSlew") :]


async def test_policy_next_interrupts_nothing(make_rig):
    plan = scheduler(exptime=0.3, count=3)
    rig = await observing(make_rig, plan)
    t0 = time.monotonic()
    await plan.submit_too(burst("next"))
    await wait_until(lambda: starts(rig, "t1", "burst"), 3, what="t1 starts the ToO")
    assert not rig.timeline.events("preempted")
    assert rig.cams[0].exposures_aborted == 0
    t1_reports = [h for h in plan.history if h[0] == "t1"]
    assert t1_reports[0][2] == "complete" and t1_reports[0][3] == 3  # finished first
    print(f"ToO next: taken {time.monotonic() - t0:.2f} s after submit (end of the observation)")


async def test_too_wakes_an_idle_telescope(make_rig):
    """t2 has nothing to do (idle, tracking off); a ToO for it wakes it at
    once instead of waiting out the idle poll, and t1 is not touched."""
    forever = {"min_interval": 0}
    plan = make([group("a", telescopes=["t1"], exptime=1.0, count=100, cadence=forever)])
    plan.config.idle_poll = 60.0
    rig = make_rig(telescopes=2, plan=plan)
    await rig.start()
    await wait_until(lambda: rig.timeline.events("telescope_idle"), 5, what="t2 idle")
    await wait_until(lambda: rig.exposing(0), 5, what="t1 exposing")
    t0 = time.monotonic()
    await plan.submit_too(burst("now", telescopes=["t2"]))
    await wait_until(lambda: starts(rig, "t2", "burst"), 2, what="t2 starts the ToO")
    dt = time.monotonic() - t0
    print(f"ToO on an idle telescope: target_start {dt * 1e3:.0f} ms after submit")
    assert not rig.timeline.events("preempted")
    assert rig.cams[0].exposures_aborted == 0


async def test_too_during_a_weather_hold_waits_for_the_reopen(make_rig):
    plan = scheduler()
    rig = await observing(make_rig, plan)
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 3, what="closed and parked")
    await wait_until(lambda: rig.timeline.events("lease_released"), 3)
    await plan.submit_too(burst("now"))
    # Nothing is observing, so nothing is asked to stop; and the sequencer
    # would refuse anyway.
    assert not rig.timeline.events("preempt_requested")
    assert rig.sequencer.preempt("t1", "now", "test") is False
    assert "no generation" in rig.timeline.events("preempt_refused")[0].detail["why"]
    assert plan.status("burst-c") is Status.PENDING
    rig.weather.set_safe(True)
    await wait_until(lambda: starts(rig, "t1", "burst"), 5, what="ToO after the reopen")
    gen2 = [e for e in rig.timeline.events("target_start") if e.detail["gen"] == 2]
    first_t1 = next(e for e in gen2 if e.detail["telescope"] == "t1")
    assert first_t1.detail["target"].startswith("burst")  # taken first
    assert not rig.timeline.events("preempted")


async def test_too_expiring_during_a_weather_hold_is_not_taken(make_rig):
    clock = Clock()
    plan = scheduler(clock=clock)
    rig = await observing(make_rig, plan)
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.timeline.events("lease_released"), 3)
    await plan.submit_too(burst("now", too={"deadline": NIGHT + timedelta(seconds=60)}))
    clock.advance(120)
    rig.weather.set_safe(True)
    await wait_until(
        lambda: [e for e in starts(rig, "t1", "a ") if e.detail["gen"] == 2], 5, what="reopen"
    )
    assert plan.status("burst-c") is Status.EXPIRED
    assert not starts(rig, "t1", "burst")


async def test_unobservable_too_expires_and_nothing_is_interrupted(make_rig):
    clock = Clock()
    plan = scheduler(clock=clock)
    rig = await observing(make_rig, plan)
    t0 = time.monotonic()
    # Twelve hours from the meridian: far below the horizon.
    await plan.submit_too(burst("now", ha=12.0, too={"deadline": NIGHT + timedelta(seconds=60)}))
    await asyncio.sleep(0.3)
    plan.check_toos()
    assert plan.status("burst-c") is Status.PENDING
    clock.advance(61)
    plan.check_toos()
    assert plan.status("burst-c") is Status.EXPIRED
    await asyncio.sleep(1.0)
    assert not rig.timeline.events("preempted") and not rig.timeline.events("preempt_requested")
    assert [c.exposures_aborted for c in rig.cams] == [0, 0]
    assert frames(rig, "t1", t0) >= 1 and frames(rig, "t2", t0) >= 1


@pytest.mark.parametrize("delay", [0.0, 0.002, 0.02, 0.1])
async def test_preemption_racing_a_lease_revocation(make_rig, delay):
    """Safety wins: whatever the order, the generation's fence and cleanup
    run as without the preemption (everything aborted and parked, nothing
    sent by the observing grants after the fence), and the ToO waits for the
    reopen."""
    plan = scheduler()
    rig = await observing(make_rig, plan, safety=dict(reopen_hold=1.0))
    await asyncio.sleep(0.2)
    await plan.submit_too(burst("now"))  # preempt() is called synchronously
    if delay:
        await asyncio.sleep(delay)
    rig.weather.set_safe(False)
    rig.supervisor._revoke_lease("weather unsafe (test)")
    await wait_until(
        lambda: rig.dome_closed() and rig.parked() and rig.camera_idle(),
        3,
        what="closed, both parked, both cameras idle",
    )
    await wait_until(lambda: rig.sequencer.cleanups, 3)
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)
    assert not rig.sequencer.quarantined
    assert rig.sequencer.cleanups[0].all_parked
    # Whether or not the ToO started before the revocation, it is not done:
    # it is retried after the reopen.
    assert plan.status("burst-c") in (Status.PENDING, Status.ATTEMPTED)
    rig.weather.set_safe(True)
    await wait_until(lambda: plan.status("burst-c") is Status.COMPLETED, 6, what="ToO after reopen")


async def test_preempt_refused_after_revocation(make_rig):
    plan = scheduler()
    rig = await observing(make_rig, plan)
    rig.weather.set_safe(False)
    rig.supervisor._revoke_lease("weather unsafe (test)")
    assert rig.sequencer.preempt("t1", "now", "test") is False
    assert "lease revoked" in rig.timeline.events("preempt_refused")[0].detail["why"]
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 3)
    assert not rig.timeline.events("preempted")


async def test_repeated_toos(make_rig):
    """A second ToO of the same class waits; a higher class one preempts the
    first ToO; all three complete, in rank order, on t1; t2 is untouched."""
    plan = scheduler()
    rig = await observing(make_rig, plan)
    t0 = time.monotonic()
    first = burst("now", "first")
    first["requests"][0]["configurations"][0]["instrument_configs"] = [{"exptime": 1.0, "count": 3}]
    await plan.submit_too(first)
    await wait_until(lambda: rig.exposing(0) and starts(rig, "t1", "first"), 3)
    await plan.submit_too(burst("now", "same"))
    await asyncio.sleep(0.2)
    assert len(rig.timeline.events("preempted")) == 1  # only "a" was preempted so far
    await plan.submit_too(burst("now", "urgent", too={"cls": 2}))
    for gid in ("urgent", "first", "same"):
        await wait_until(lambda g=gid: plan.status(f"{g}-c") is Status.COMPLETED, 8, what=gid)
    order = [
        e.detail["target"].split()[0]
        for e in rig.timeline.events("target_start")
        if e.detail["telescope"] == "t1" and e.t >= t0
    ]
    assert order[:4] == ["first", "urgent", "first", "same"]
    assert [p["telescope"] for p in rig.sequencer.preemptions] == ["t1", "t1"]
    assert_t2_undisturbed(rig, t0)


async def test_preempted_observation_that_ignores_cancellation_fences_only_its_telescope(
    make_rig,
):
    """A stuck observation (here: a slew guard that never returns) cannot be
    stopped: that telescope's grants are revoked, it gives up, and the other
    keeps observing under the same lease."""

    class StuckGuard:
        def __init__(self) -> None:
            self.armed = False

        async def check_slew(self, telescope, ra, dec):
            while self.armed and telescope == "t1":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    continue  # ignores cancellation

    guard = StuckGuard()
    plan = scheduler(exptime=0.3, count=2)
    rig = make_rig(telescopes=2, plan=plan, slew_guard=guard, seq=dict(cancel_ack_timeout=0.3))
    await rig.start()
    await wait_until(rig.exposing, 5)
    guard.armed = True
    await wait_until(
        lambda: [e for e in rig.timeline.events("target_start") if e.detail["telescope"] == "t1"][
            1:
        ],
        5,
    )
    await asyncio.sleep(0.1)  # t1 now stuck in the guard
    assert rig.sequencer.preempt("t1", "now", "test")
    await wait_until(lambda: rig.timeline.events("preempt_not_acknowledged"), 2)
    assert rig.sequencer.units["t1"].gave_up
    since = time.monotonic()
    await wait_until(lambda: frames(rig, "t2", since) >= 2, 3, what="t2 keeps observing")
    assert rig.supervisor.lease.live and len(rig.timeline.events("observe_start")) == 1
    guard.armed = False
