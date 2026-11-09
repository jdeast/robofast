"""The sequencer's guider hook: start on target for ``guide`` observations,
check before each exposure, stop when the observation ends and before every
park; a guider failure is that telescope's ordinary error."""

from __future__ import annotations

import asyncio

from robofast.guider.base import GuiderError
from robofast.plan import StaticPlan
from robofast.request_model import Configuration
from robofast.testing.checks import wait_until


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


async def test_guide_without_a_guider_is_unsupported(make_rig):
    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 1, "guide": True}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1))
    await _run(rig)
    why = _events(rig, "observation_unsupported")[0].detail["reason"]
    assert "has no guider" in why
    assert not _events(rig, "telescope_error")
    assert rig.cams[0].exposures_started == 0 and plan.history[0][2] == "unsupported"


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
        "target": {"type": "ICRS", "name": "HD 1", "ra": 150.0, "dec": 20.0},
        "instrument_configs": [{"exposure_time": 30, "exposure_count": 2}],
        "guiding_config": {"mode": "ON"},
    }
    c = Configuration.from_dict(d, "c")
    assert c.guide and c.to_dict()["guiding_config"] == {"mode": "ON"}
    off = Configuration.from_dict({**d, "guiding_config": {"mode": "OFF"}}, "c")
    assert not off.guide and off.to_dict()["guiding_config"] == {"mode": "OFF"}


def test_sequencer_service_builds_a_guider_client():
    from robofast.guider.service import GuiderRemote
    from robofast.sequencer_service import build_units
    from robofast.timeline import Timeline

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


async def test_an_offset_guided_observation_hands_the_guider_its_offset(make_rig):
    offset = {"star": {"east": 20.0, "north": -5.0}, "target": {"ra": 150.0, "dec": 20.0}}
    plan = StaticPlan(
        [{"name": "x", "exptime": 0.1, "count": 1, "guide": True, "guide_offset": offset}],
        ["t1"],
    )
    rig = make_rig(plan=plan)
    g = rig.sequencer.units["t1"].guider = FakeGuider(rig.timeline)
    await _run(rig)
    start = next(d for what, d in g.calls if what == "start")
    assert start["offset_guide"] == offset
    import pytest

    from robofast.plan import Observation

    with pytest.raises(ValueError, match="guide_offset without guide"):
        Observation(name="y", guide_offset=offset)


def test_configuration_offset_guiding_round_trips():
    import pytest

    base = {
        "id": "c1",
        "target": {"type": "ICRS", "name": "HD 1", "ra": 150.0, "dec": 20.0},
        "instrument_configs": [{"exposure_time": 30, "exposure_count": 2}],
    }
    gc = {"mode": "ON", "extra_params": {"guide": "offset", "offset_star": {"east": 3, "north": 4}}}
    c = Configuration.from_dict({**base, "guiding_config": gc}, "c")
    assert c.guide_on == "offset" and c.offset_star == {"east": 3, "north": 4}
    assert c.to_dict()["guiding_config"] == gc
    c = Configuration.from_dict(
        {**base, "guiding_config": {"mode": "ON", "extra_params": {"guide": "offset"}}}, "c"
    )
    assert c.offset_star is None  # the guider picks it from the catalog
    for extra, why in (
        ({"guide": "sideways"}, "expected one of"),
        ({"offset_star": "auto"}, "only with guide 'offset'"),
        ({"guide": "offset", "offset_star": {"x": 1}}, "offset_star: offset star"),
        ({"guide": "offset", "bogus": 1}, "unknown"),
    ):
        with pytest.raises(ValueError, match=why):
            Configuration.from_dict(
                {**base, "guiding_config": {"mode": "ON", "extra_params": extra}}, "c"
            )
