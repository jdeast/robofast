"""Two telescopes in one enclosure, against OmniSim.

OmniSim serves one telescope and one camera, so the second pair comes from a
second OmniSim (``omnisim2``). The enclosure is the first one's dome.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast_core import Observation, SafetyConfig, SafetySupervisor, Sequencer, SequencerConfig
from robofast_core.alpaca import alpaca_owner
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.sequencer import TelescopeUnit
from robofast_core.testing.checks import assert_fenced_after_cancel, await_until, wait_until
from robofast_core.timeline import Timeline

from .conftest import BOUND, COMMAND_TIMEOUT, SIM_SAFETY, SIM_SEQ, SimRig

pytestmark = pytest.mark.alpaca_sim

DEVICES = ("t1.telescope", "t1.camera", "t2.telescope", "t2.camera")


@pytest.fixture
async def make_two(omnisim, omnisim2, weather, truth, truth2, request):
    rigs: list[SimRig] = []
    state, server = weather

    def build(*, t1_camera: str | None = None, exptime: float = 60.0, plan=None) -> SimRig:
        tl = Timeline()

        def owner(name, kind, address, **kw):
            return alpaca_owner(name, kind, address, tl, default_timeout=COMMAND_TIMEOUT, **kw)

        owners = {
            "dome": owner("dome", "dome", omnisim, privileged_members=ENCLOSURE_MEMBERS),
            "t1.telescope": owner("t1.telescope", "telescope", omnisim),
            "t1.camera": owner("t1.camera", "camera", t1_camera or omnisim),
            "t2.telescope": owner("t2.telescope", "telescope", omnisim2),
            "t2.camera": owner("t2.camera", "camera", omnisim2),
            "safetymonitor": owner("safetymonitor", "safetymonitor", server.address),
            "observingconditions": owner(
                "observingconditions", "observingconditions", server.address
            ),
        }
        sup = SafetySupervisor(
            "safety",
            dome=owners["dome"],
            safety_monitor=owners["safetymonitor"],
            observing_conditions=owners["observingconditions"],
            telescopes=[owners["t1.telescope"], owners["t2.telescope"]],
            timeline=tl,
            config=SafetyConfig(**SIM_SAFETY),
        )
        seq = Sequencer(
            "seq",
            supervisor=sup,
            telescopes=[
                TelescopeUnit(n, owners[f"{n}.telescope"], owners[f"{n}.camera"])
                for n in ("t1", "t2")
            ],
            plan=plan or (lambda: Observation("field", exptime=exptime, count=1000)),
            timeline=tl,
            config=SequencerConfig(**SIM_SEQ),
        )
        rig = SimRig(tl, sup, seq, owners, state)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        await rig.stop()
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print("\n--- timeline ---\n" + rig.timeline.dump())


async def _both_observing(rig, truth, truth2) -> None:
    await rig.start()
    for t in (truth, truth2):
        await await_until(t.exposing, 90, what="OmniSim camera exposing")
    assert truth.open() and not truth.parked() and not truth2.parked()


async def test_s1_two_telescopes(make_two, truth, truth2, record_timing):
    rig = make_two()
    await _both_observing(rig, truth, truth2)
    await asyncio.sleep(1.0)
    t0 = time.monotonic()
    rig.weather.update(safe=False)
    seen: dict[str, float] = {}
    probes = {
        "closed": truth.closed,
        "t1_parked": truth.parked,
        "t2_parked": truth2.parked,
        "t1_camera_idle": lambda: truth.camera_state() == 0,
        "t2_camera_idle": lambda: truth2.camera_state() == 0,
    }
    while len(seen) < len(probes):
        for name, probe in probes.items():
            if name not in seen and await asyncio.to_thread(probe):
                seen[name] = time.monotonic() - t0
        assert time.monotonic() - t0 < BOUND, f"not all reached within {BOUND} s: {seen}"
        await asyncio.sleep(0.1)
    seen["all"] = max(seen.values())
    await wait_until(lambda: rig.sequencer.cleanups, BOUND, what="cleanup")
    record_timing(**seen)
    report = rig.sequencer.cleanups[0]
    assert sorted(report.aborted) == sorted(report.parked) == ["t1", "t2"], report
    assert not report.errors
    assert_fenced_after_cancel(rig.timeline, "seq", 1, DEVICES)


async def test_one_camera_hangs_the_other_keeps_observing(
    make_two, truth, truth2, camera_proxy, record_timing
):
    rig = make_two(t1_camera=camera_proxy.address, exptime=5.0)
    await _both_observing(rig, truth, truth2)
    tl, seq = rig.timeline, rig.sequencer
    t0 = time.monotonic()
    camera_proxy.set_mode("hang")
    await wait_until(lambda: tl.events("telescope_safed"), BOUND, what="t1 safed")
    safed = time.monotonic() - t0
    assert tl.events("telescope_error")[0].detail["telescope"] == "t1"
    await await_until(truth.parked, BOUND, what="t1 parked")
    t_after = time.monotonic()
    await wait_until(
        lambda: sum(f["telescope"] == "t2" and f["t"] > t_after for f in seq.frames) >= 2,
        60,
        what="t2 frames after t1's error",
    )
    assert truth.open() and not truth2.parked()
    assert len(tl.events("observe_start")) == 1 and rig.supervisor.lease.live
    record_timing(t1_safed=safed)
    rig.weather.update(safe=False)
    await await_until(lambda: truth.closed() and truth2.parked(), BOUND, what="closed, parked")


class _ToOPlan:
    """ "field" forever; once ``too_pending`` is set, one "too" on t1."""

    def __init__(self, exptime: float) -> None:
        self.exptime = exptime
        self.too_pending = False
        self.history: list[tuple[str, str, str, int]] = []

    async def next_observation(self, telescope: str) -> Observation:
        if telescope == "t1" and self.too_pending:
            self.too_pending = False
            return Observation("too", exptime=1.0, count=1)
        return Observation("field", exptime=self.exptime, count=1000)

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        self.history.append((telescope, obs.name, outcome, frames))


async def test_preempt_one_telescope_now(make_two, truth, truth2, record_timing):
    """A ToO preemption ("now") of t1 against real Alpaca devices: t1's
    exposure is aborted and it starts the ToO without parking; t2 keeps
    exposing; same lease (design.md 3.7.1)."""
    plan = _ToOPlan(exptime=30.0)
    rig = make_two(plan=plan)
    await _both_observing(rig, truth, truth2)
    await asyncio.sleep(1.0)
    tl, seq = rig.timeline, rig.sequencer
    plan.too_pending = True
    t0 = time.monotonic()
    assert seq.preempt("t1", "now", "sim ToO")
    await wait_until(lambda: tl.events("preempted"), BOUND, what="t1 preempted")
    preempted = time.monotonic() - t0
    await wait_until(
        lambda: any(e.detail["target"] == "too" for e in tl.events("target_start")),
        60,
        what="t1 starts the ToO",
    )
    too_start = time.monotonic() - t0
    assert tl.events("preempted")[0].detail["aborted"]
    assert ("t1", "field", "interrupted", 0) in plan.history
    assert not await asyncio.to_thread(truth.parked)
    assert await asyncio.to_thread(truth2.exposing)  # t2's exposure never stopped
    assert len(tl.events("observe_start")) == 1 and rig.supervisor.lease.live
    assert not any(h[0] == "t2" and h[2] != "complete" for h in plan.history)
    record_timing(preempted=preempted, too_target_start=too_start)
