"""The sequencer and the spectrograph's health, the console's holds and its
single-telescope park (fakes; docs/rv_night.md, docs/console.md):

* a spectrograph that keeps failing is marked down (an alarm), the plan
  stops getting spectra, imaging goes on, and no telescope burns its
  give-up counter on it; a recovery check or the operator's clear brings it
  back;
* a spectrograph busy with another job (another enclosure) re-queues the
  spectrum, nobody's error;
* a held telescope is not waited for by a leader;
* the console parks one telescope now, never touching the roof.
"""

from __future__ import annotations

import asyncio

from robofast_core.console.hold import OperatorHold, send_command
from robofast_core.devices.kiwispec_client import KiwiSpecBusy
from robofast_core.plan import Observation, StaticPlan
from robofast_core.testing.checks import wait_until

from .test_rv_spectra import FakeSpectrograph, events, run_to_end, rv_rig, spectrum_target

IMAGE = {"name": "field", "exptime": 0.2, "count": 1, "ra_hours": 1.0, "dec_deg": 10.0}


class SpecPlan:
    """Spectra for t1 while the spectrograph is available, then one image;
    nothing for t2 (a minimal stand-in for the dispatch scheduler)."""

    def __init__(self, spectra: int = 6) -> None:
        self.available = True
        self.calls: list[tuple[bool, str]] = []
        self.history: list[tuple[str, str, str]] = []
        self.spectra_left = spectra
        self.imaged = False

    async def next_observation(self, telescope):
        if telescope != "t1":
            return None
        if self.available and self.spectra_left > 0:
            self.spectra_left -= 1
            return Observation.from_dict(spectrum_target(f"rv{self.spectra_left}", feed=["t1"]))
        if not self.imaged:
            self.imaged = True
            return Observation.from_dict(IMAGE)
        return None

    async def peek(self, telescope):
        if telescope != "t1":
            return None
        if self.available and self.spectra_left > 0:
            return Observation.from_dict(spectrum_target(f"rv{self.spectra_left - 1}", feed=["t1"]))
        return None if self.imaged else Observation.from_dict(IMAGE)

    def report(self, telescope, obs, outcome, frames, files=None):
        self.history.append((telescope, obs.name, outcome))

    def set_spectrograph_available(self, available, reason=""):
        self.available = available
        self.calls.append((available, reason))


class FailingSpectrograph(FakeSpectrograph):
    """Every job fails; ``status`` answers with alarms until ``healthy``."""

    def __init__(self, timeline) -> None:
        super().__init__(timeline)
        self.fail_next = "SI Image not answering"
        self.healthy = False

    async def status(self, refresh=False):
        return {"job": None, "alarms": [] if self.healthy else ["ccd_temp: timeout"]}


def health_rig(make_rig, tmp_path, plan, **seq):
    rig = rv_rig(make_rig, tmp_path, plan, seq=dict(spectrograph_max_failures=2, **seq))
    rig.spec = rig.sequencer.spectrograph = FailingSpectrograph(rig.timeline)
    return rig


async def test_a_failing_spectrograph_is_marked_down_and_imaging_goes_on(make_rig, tmp_path):
    plan = SpecPlan()
    rig = health_rig(make_rig, tmp_path, plan, spectrograph_recovery_interval=60.0)
    await run_to_end(rig)
    seq = rig.sequencer
    errors = events(rig, "spectrograph_error")
    assert [e.detail["failures"] for e in errors] == [1, 2]
    [down] = events(rig, "spectrograph_down")
    assert down.detail["level"] == "critical"
    assert seq.spectrograph_down and "2 faults in a row" in seq.spectrograph_down
    assert plan.calls and plan.calls[0][0] is False
    # The spectra were re-queued, not failed; then the image was taken.
    assert [h[2] for h in plan.history] == ["interrupted", "interrupted", "complete"]
    assert plan.history[-1][1] == "field"
    # Not the telescope's fault: no error, no backoff, no give-up.
    assert not events(rig, "telescope_error")
    assert not events(rig, "telescope_backoff")
    assert all(not u.gave_up and u.consecutive_errors == 0 for u in seq.units.values())


async def test_the_recovery_check_brings_the_spectrograph_back(make_rig, tmp_path):
    plan = SpecPlan(spectra=3)
    rig = health_rig(make_rig, tmp_path, plan, spectrograph_recovery_interval=0.2)
    plan.imaged = True  # nothing else to do: t1 idles until the spectrograph is back

    async def idle_then(telescope, inner=plan.next_observation):
        obs = await inner(telescope)
        if obs is None and telescope == "t1" and not plan.available:
            from robofast_core.plan import Idle

            return Idle(0.2, "waiting for the spectrograph")
        return obs

    plan.next_observation = idle_then
    await rig.start()
    await wait_until(lambda: events(rig, "spectrograph_down"), 10, what="marked down")
    await asyncio.sleep(0.5)
    assert not events(rig, "spectrograph_up")  # the check fails while it alarms
    checks = events(rig, "spectrograph_check")
    assert checks and not checks[-1].detail["ok"]
    rig.spec.healthy, rig.spec.fail_next = True, None
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    [up] = events(rig, "spectrograph_up")
    assert up.detail["why"] == "recovery check passed"
    assert plan.calls[-1][0] is True
    # A spectrum, taken once it was back.
    assert [r["object"] for r in rig.sequencer.spectrum_records] == ["rv0"]


async def test_the_operator_clears_a_spectrograph_marked_down(make_rig, tmp_path):
    inner = SpecPlan(spectra=2)
    plan = OperatorHold(inner, ["t1", "t2"], tmp_path / "control")
    rig = health_rig(
        make_rig,
        tmp_path,
        plan,
        spectrograph_recovery_interval=60.0,
        spectrograph_auto_recover=False,
    )
    await run_to_end(rig)
    assert rig.sequencer.spectrograph_down is not None
    send_command(tmp_path / "control", "clear_spectrograph", [], operator="jde", reason="fixed")
    [result] = plan.take_in()
    assert result["ok"] is True
    assert rig.sequencer.spectrograph_down is None
    [up] = events(rig, "spectrograph_up")
    assert "cleared by jde" in up.detail["why"]
    assert inner.calls[-1] == (True, up.detail["why"])


async def test_a_busy_spectrograph_requeues_the_spectrum(make_rig, tmp_path):
    plan = SpecPlan(spectra=2)
    plan.imaged = True
    rig = rv_rig(make_rig, tmp_path, plan)
    expose = rig.spec.expose
    busy = [1]

    async def maybe_busy(**frame):
        if busy:
            busy.pop()
            raise KiwiSpecBusy("POST /expose: busy (job 7) for the 600 s this request waited")
        return await expose(**frame)

    rig.spec.expose = maybe_busy
    await run_to_end(rig)
    assert [h[2] for h in plan.history] == ["interrupted", "complete"]
    [b] = events(rig, "spectrograph_busy")
    assert "busy" in b.detail["error"]
    assert not events(rig, "spectrograph_error") and not events(rig, "telescope_error")


# -- operator holds ---------------------------------------------------------------------


async def test_a_held_member_is_not_waited_for(make_rig, tmp_path):
    inner = StaticPlan([spectrum_target()], ["t1", "t2"])
    plan = OperatorHold(inner, ["t1", "t2"], tmp_path / "control", poll=0.05)
    rig = rv_rig(make_rig, tmp_path, plan, seq=dict(spectrum_ready_timeout=8.0))
    rig.guiders[1].start_delay = 5.0  # t2 is slow, then held while the leader waits
    await rig.start()
    serve = asyncio.create_task(plan.serve())
    try:
        await wait_until(lambda: events(rig, "spectrum_session"), 10, what="session")
        send_command(tmp_path / "control", "pause", ["t2"], operator="obs", reason="FAU")
        t_held = asyncio.get_running_loop().time()
        await wait_until(lambda: rig.spec.science(), 5, what="spectrum started")
        assert asyncio.get_running_loop().time() - t_held < 2.0  # not the 8 s timeout
        [frame] = rig.spec.science()
        assert frame["telescopes"] == ["t1"]
        assert [e.detail["telescope"] for e in events(rig, "spectrum_member_held")] == ["t2"]
    finally:
        serve.cancel()


async def test_the_console_parks_one_telescope_now(make_rig, tmp_path):
    control = tmp_path / "control"
    inner = StaticPlan([{**IMAGE, "exptime": 1.0, "count": 1000}], ["t1", "t2"])
    plan = OperatorHold(inner, ["t1", "t2"], control, poll=0.05, hold_idle=0.3)
    rig = make_rig(telescopes=2, plan=plan, seq=dict(idle_release_after=None))
    await rig.start()
    serve = asyncio.create_task(plan.serve())
    try:
        await wait_until(
            lambda: (
                {e.detail["telescope"] for e in events(rig, "exposure_started")} == {"t1", "t2"}
            ),
            10,
            what="both exposing",
        )
        assert rig.dome.ShutterStatus == 0  # open
        send_command(control, "park", ["t2"], operator="obs", reason="check the mount")
        await wait_until(lambda: events(rig, "operator_parked"), 10, what="t2 parked")
        [p] = events(rig, "operator_parked")
        assert p.detail["telescope"] == "t2" and p.detail["parked"] is True
        assert rig.tels[1].AtPark and not rig.tels[0].AtPark
        # Only the mount moved: the roof is open, the lease held, t1 observing.
        assert rig.dome.ShutterStatus == 0
        closes = [e for e in rig.timeline.entries() if e.what in ("CloseShutter", "close")]
        assert not closes
        n1 = len([e for e in events(rig, "exposure_started") if e.detail["telescope"] == "t1"])
        await asyncio.sleep(1.5)
        assert (
            len([e for e in events(rig, "exposure_started") if e.detail["telescope"] == "t1"]) > n1
        )
        assert rig.tels[1].AtPark  # still held and parked
        # Resume: t2 unparks and observes again.
        send_command(control, "resume", ["t2"], operator="obs", reason="mount fine")
        n2 = len([e for e in events(rig, "exposure_started") if e.detail["telescope"] == "t2"])
        await wait_until(
            lambda: (
                len([e for e in events(rig, "exposure_started") if e.detail["telescope"] == "t2"])
                > n2
            ),
            10,
            what="t2 observing again",
        )
        assert not rig.tels[1].AtPark
        assert plan.holds == {}
    finally:
        serve.cancel()
