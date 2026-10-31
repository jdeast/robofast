"""The sequencer's guider hook: start on target for ``guide`` observations,
check before each exposure, stop when the observation ends and before every
park; a guider failure is that telescope's ordinary error."""

from __future__ import annotations

import asyncio

from robofast_core.guider.base import GuiderError
from robofast_core.plan import StaticPlan
from robofast_core.request_model import Configuration
from robofast_core.testing.checks import wait_until


class FakeGuider:
    """Records calls into the rig's timeline (so their order against the
    telescope's commands can be checked)."""

    def __init__(self, timeline, *, fail_start=None, fail_check_after=None):
        self.timeline = timeline
        self.fail_start = fail_start
        self.fail_check_after = fail_check_after
        self.calls: list[tuple[str, object]] = []
        self.guiding = False
        self.checks = 0

    def _log(self, what, **detail):
        self.calls.append((what, detail))
        self.timeline.event("guider", what, **detail)

    async def start(self, **params):
        self._log("start", **params)
        if self.fail_start:
            raise GuiderError(self.fail_start)
        self.guiding = True

    async def check(self):
        self.checks += 1
        self._log("check")
        if self.fail_check_after is not None and self.checks > self.fail_check_after:
            raise GuiderError("guider t1: failed: star lost 4 times")

    async def stop(self, reason):
        self._log("stop", reason=reason)
        self.guiding = False
        return True


def _events(rig, what, source="seq"):
    return rig.timeline.events(what, source)


async def _run(rig, timeout=10):
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), timeout)


async def test_guided_observation_starts_checks_and_stops_the_guider(make_rig):
    plan = StaticPlan(
        [
            {"name": "x", "exptime": 0.1, "count": 3, "guide": True},
            {"name": "y", "exptime": 0.1, "count": 1},
        ],
        ["t1"],
    )
    rig = make_rig(plan=plan)
    g = rig.sequencer.units["t1"].guider = FakeGuider(rig.timeline)
    await _run(rig)
    assert plan.history == [("t1", "x", "complete", 3), ("t1", "y", "complete", 1)]
    kinds = [c[0] for c in g.calls]
    # The startup cleanup stops it (a crashed predecessor may have left it
    # guiding); then start, a check per exposure, stop at the end of x; y is
    # not guided; the final cleanup stops it again (idempotent) before
    # parking.
    assert kinds == ["stop", "start", "check", "check", "check", "stop", "stop"]
    assert g.calls[0][1]["reason"] == g.calls[-1][1]["reason"] == "cleanup"
    assert g.calls[5][1]["reason"] == "observation ended"
    on_target = _events(rig, "on_target")[0].t
    start = _events(rig, "start", "guider")[0].t
    first_exposure = _events(rig, "exposure_started")[0].t
    assert on_target < start < first_exposure
    assert "dec" in g.calls[1][1]
    stop = _events(rig, "stop", "guider")[1].t
    assert stop < _events(rig, "exposure_started")[3].t  # before y's exposure (and slew)


async def test_cleanup_stops_the_guider_before_parking(make_rig):
    plan = StaticPlan([{"name": "x", "exptime": 1.0, "count": 50, "guide": True}], ["t1"])
    rig = make_rig(plan=plan)
    g = rig.sequencer.units["t1"].guider = FakeGuider(rig.timeline)
    await rig.start()
    await wait_until(lambda: g.guiding and rig.exposing(), 10, what="guiding and exposing")
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.timeline.events("lease_released"), 10)
    stops = [e for e in rig.timeline.events("stop", "guider")]
    parks = rig.timeline.commands("t1.telescope", outcome="sent")
    parks = [e for e in parks if e.what == "Park"]
    assert stops and parks
    assert min(e.t for e in stops) < parks[0].t
    assert not g.guiding


async def test_guide_without_a_guider_fails_the_observation(make_rig):
    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 1, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1))
    await _run(rig)
    err = _events(rig, "telescope_error")[0].detail["error"]
    assert "has no guider" in err
    assert rig.cams[0].exposures_started == 0 and plan.history[0][2] == "failed"


async def test_guider_failing_to_acquire_is_the_telescopes_error(make_rig):
    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 1, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1))
    g = rig.sequencer.units["t1"].guider = FakeGuider(rig.timeline, fail_start="no star found")
    await _run(rig)
    assert "no star found" in _events(rig, "telescope_error")[0].detail["error"]
    assert rig.cams[0].exposures_started == 0
    assert rig.parked()
    assert [c[0] for c in g.calls][-1] == "stop"


async def test_guider_failing_mid_observation_is_the_telescopes_error(make_rig):
    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 5, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1))
    g = rig.sequencer.units["t1"].guider = FakeGuider(rig.timeline, fail_check_after=2)
    await _run(rig)
    assert "star lost" in _events(rig, "telescope_error")[0].detail["error"]
    assert plan.history == [("t1", "x", "failed", 2)]
    assert not g.guiding and rig.parked()


def test_configuration_guide_flag_round_trips():
    d = {
        "id": "c1",
        "target": {"name": "HD 1", "ra_hours": 10.0, "dec_deg": 20.0},
        "instrument_configs": [{"exptime": 30, "count": 2}],
        "guide": True,
    }
    c = Configuration.from_dict(d, "c")
    assert c.guide and c.to_dict()["guide"] is True
    assert "guide" not in Configuration.from_dict({**d, "guide": False}, "c").to_dict()


def test_sequencer_service_builds_a_guider_client():
    from robofast_core.guider.service import GuiderRemote
    from robofast_core.sequencer_service import build_units
    from robofast_core.timeline import Timeline

    entry = {"telescope": "127.0.0.1:1", "camera": "127.0.0.1:1"}
    units = build_units(
        {
            "telescopes": {
                "t1": entry,
                "t2": {**entry, "guider": {"url": "http://127.0.0.1:8140", "acquire_timeout": 60}},
            }
        },
        Timeline(),
        1.0,
    )
    t1, t2 = units
    assert t1.guider is None
    assert isinstance(t2.guider, GuiderRemote)
    assert t2.guider.scope == "t2" and t2.guider.acquire_timeout == 60
