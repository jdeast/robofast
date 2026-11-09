"""The seeing source (robofast/seeing.py, docs/seeing.md): the
guiders' live FWHM by default, its averaging and staleness, the fallback,
the registry of sources, ``guide: "auto"`` and the ``max_seeing``
constraint in the dispatch scheduler."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from robofast.clock import NightRule
from robofast.guider.base import VirtualClock
from robofast.guider.loop import publish_values
from robofast.plan_record import PlanRecord, read_record
from robofast.registry import ValueRegistry
from robofast.seeing import (
    SEEING_SOURCES,
    GuiderSeeing,
    Seeing,
    SeeingEstimator,
    seeing_from_config,
)
from tests.core.test_dispatch import GUIDING, MINERVA, NIGHT, Clock, group, make, tlimits
from tests.core.test_guider_loop import _guide_for, _rig, _seeing

MINERVA_SITE = Path(__file__).resolve().parents[2] / "sites" / "minerva"


def _feed(src, clock, values, telescope="t1", step=60.0):
    for v in values:
        src.add(telescope, v, clock())
        clock.advance(step)


def test_guider_source_median_window_and_staleness():
    clock = Clock()
    src = GuiderSeeing(window=600, max_age=300, min_samples=3, sample_interval=30, clock=clock)
    assert src.current(clock()) is None  # nothing yet
    _feed(src, clock, [1.0, 1.2])
    assert src.current(clock()) is None  # fewer than min_samples
    _feed(src, clock, [5.0, 1.4])
    est = src.current(clock())
    # The median shrugs off one bad frame; age is since the newest.
    assert est.value == pytest.approx(1.3) and est.samples == 4 and est.age == 60
    # Too soon after the last from the same telescope: not a new sample.
    assert not src.add("t1", 9.0, clock() - timedelta(seconds=50))
    # Another telescope is the same site's seeing.
    assert src.add("t2", 1.5, clock())
    assert src.current(clock()).samples == 5
    # Older than the window: dropped; nothing newer than max_age: stale.
    clock.advance(301)
    assert src.current(clock()) is None
    # Nonsense is not a sample.
    for bad in (float("nan"), -1.0, 0.0, 100.0, True, "1.2", None):
        assert not src.add("t3", bad, clock())


def test_guider_source_collects_from_the_registry():
    """The guide loops publish ``<scope>.guide_seeing`` while guiding; the
    source picks those up (and only those), on its own clock."""
    clock = Clock()
    src = GuiderSeeing(min_samples=2, sample_interval=1, telescopes=["t1", "t2"], clock=clock)
    reg = ValueRegistry()
    assert src.bind(reg)
    publish_values(reg, "t1", "guiding", {"seeing_arcsec": 1.6, "fwhm": 6.0}, **_pv())
    clock.advance(5)
    publish_values(reg, "t2", "guiding", {"seeing_arcsec": 1.8}, **_pv())
    publish_values(reg, "t9", "guiding", {"seeing_arcsec": 9.0}, **_pv())  # not ours
    publish_values(reg, "t1", "acquiring", {"seeing_arcsec": 9.0}, **_pv())  # not guiding
    reg.publish("t1.guide_fwhm", 7.0, max_age=10)  # pixels, not seeing
    est = src.current(clock())
    assert est.value == pytest.approx(1.7) and est.samples == 2 and est.source == "guider"
    assert reg.get("t1.guide_seeing").keyword == "GUIDSEE"


def _pv():
    return {"target": None, "exposure": None, "tiptilt_position": None}


def test_a_failing_listener_never_stops_a_publish():
    reg = ValueRegistry()
    reg.subscribe(lambda v: 1 / 0)
    assert reg.publish("a.b", 1, max_age=None).value == 1


def test_config_fallback_and_registry():
    clock = Clock()
    est = seeing_from_config(None, clock)
    assert isinstance(est.source, GuiderSeeing) and est.unknown == "pass"
    assert est.current(clock()) is None
    # The fallback answers, marked as such; a caller's fallback wins.
    est = seeing_from_config({"fallback": 2.0, "window": 900}, clock)
    assert est.current(clock()) == Seeing(2.0, "fallback", note="guider: nothing recent")
    assert est.current(clock(), fallback=1.4).value == 1.4
    assert seeing_from_config({"source": "fixed", "value": 1.1}).current(NIGHT).value == 1.1
    for bad, match in (
        ({"source": "dimm"}, r"seeing\.source.*'dimm'"),
        ({"source": "fixed"}, r"seeing\.value"),
        ({"window": 0}, r"seeing\.window"),
        ({"min_samples": 0}, r"seeing\.min_samples"),
        ({"bogus": 1}, r"unknown keys \['bogus'\]"),
        ({"unknown": "maybe"}, r"seeing\.unknown"),
        ({"fallback": -1}, r"seeing\.fallback"),
        ("guider", r"seeing: \{source"),
    ):
        with pytest.raises(ValueError, match=match):
            seeing_from_config(bad)

    # A plug-in's source (a DIMM, a weather feed) is one registration away.
    class Dimm:
        name = "dimm"

        def current(self, now):
            raise OSError("no DIMM")  # a broken source falls back

    SEEING_SOURCES.register("test.dimm", lambda d, clock: Dimm())
    try:
        est = seeing_from_config({"source": "test.dimm", "fallback": 1.9})
        assert est.current(NIGHT).value == 1.9
        assert not est.bind(ValueRegistry())  # wants no guider values
    finally:
        SEEING_SOURCES.unregister("test.dimm")


async def test_the_guide_loop_reports_the_seeing():
    """A guide loop on a star reports its FWHM in arcsec as the seeing; on
    a fiber (annulus) only the width learned from a star clear of the
    hole, never a configured one."""
    clock = VirtualClock()
    g, world = _rig(clock, jitter=_seeing(rms=0.1), config={"exposure": 0.01, "plate_scale": 0.5})
    await _guide_for(g, clock, 1.0)
    s = g.summary()
    await g.stop()
    assert s["seeing_arcsec"] == pytest.approx(world.fwhm * 0.5, rel=0.25)

    class Annulus:
        name = "annulus"
        seeing = None

    g._centroider = Annulus()
    assert "seeing_arcsec" not in g.summary()
    Annulus.seeing = 3.0
    assert g.summary()["seeing_arcsec"] == 1.5


def _seeing_src(clock, values):
    src = GuiderSeeing(min_samples=1, clock=clock)
    for v in values:
        src.add("t1", v, clock())
    return src


async def test_max_seeing_follows_the_source_and_is_recorded(tmp_path):
    clock = Clock()
    src = _seeing_src(clock, [2.5])
    rec = PlanRecord(tmp_path, rule=NightRule(longitude=MINERVA.longitude))
    d = make(
        [group("sharp", constraints={"max_seeing": 2.0}, priority=5), group("any")],
        clock=clock,
        limits={"t1": tlimits()},
        seeing=SeeingEstimator(src),
        record=rec,
    )
    cands = {c.request.id: c for c in d.evaluate("t1")}
    assert not cands["sharp-r"].ok and cands["any-r"].ok
    assert 'seeing 2.50" (guider) > max_seeing 2"' in cands["sharp-r"].reasons[0]
    obs = await d.next_observation("t1")
    assert obs.name.startswith("any")
    entries = [e for e in read_record(next(tmp_path.glob("*.jsonl")))]
    decision = next(e for e in entries if e.get("kind") == "decision")
    assert decision["seeing"] == {"value": 2.5, "source": "guider", "samples": 1, "age": 0.0}
    # The seeing improves: the sharp one is observable (and outranks).
    src.add("t1", 1.2, clock() + timedelta(seconds=60))
    src.samples.popleft()
    assert all(c.ok for c in d.evaluate("t1"))
    # No seeing at all: 'pass' (default) lets it through, 'fail' does not.
    for unknown, ok in (("pass", True), ("fail", False)):
        d = make(
            [group("sharp", constraints={"max_seeing": 2.0})],
            clock=clock,
            limits={"t1": tlimits()},
            seeing=SeeingEstimator(GuiderSeeing(clock=clock), unknown=unknown),
        )
        (c,) = d.evaluate("t1")
        assert c.ok is ok, c.reasons
        if not ok:
            assert "seeing unknown" in c.reasons[0]


def test_dispatch_config_takes_a_seeing_entry():
    from robofast.dispatch import dispatch_from_config

    base = {
        "site": {"latitude": 31.68, "longitude": -110.88, "elevation": 2316},
        "telescopes": {"t1": {"instruments": ["imager"], "min_alt": 21, "max_alt": 83}},
    }
    d = dispatch_from_config({**base, "seeing": {"source": "fixed", "value": 1.3}}, ["t1"])
    assert d.seeing_now().as_json() == {"value": 1.3, "source": "fixed"}
    reg = ValueRegistry()
    assert not d.bind_values(reg)
    d = dispatch_from_config(base, ["t1"])
    assert d.bind_values(reg) and d.seeing_now() is None
    with pytest.raises(ValueError, match=r"dispatch plan: seeing\.window"):
        dispatch_from_config({**base, "seeing": {"window": -1}}, ["t1"])


async def test_guide_auto_uses_live_seeing_else_the_model():
    """Bad seeing spills more light round the fiber: a target too faint
    for annulus guiding in the model's seeing is guided on the target
    under the guiders' measured bad seeing; with nothing recent the
    model's seeing is used and the plan says so."""

    def auto(gid, mag):
        g = group(gid, ha=-1)
        g["requests"][0]["configurations"][0]["guiding_config"] = {
            "mode": "ON",
            "extra_params": {"guide": "auto", "target_mag": mag},
        }
        return g

    clock = Clock()
    model = {**GUIDING, "seeing": 1.0}
    from robofast.guider import recommend as rc

    # A magnitude where the choice flips between 1" and 4" seeing.
    gm = rc.GuideModel.from_dict(model)
    mag = next(
        m / 10
        for m in range(80, 250)
        if rc.recommend(gm, m / 10, seeing=1.0).method == "offset"
        and rc.recommend(gm, m / 10, seeing=4.0).method == "annulus"
    )
    for values, on, src, value in (
        ([], "offset", "fallback", 1.0),
        ([4.0], "annulus", "guider", 4),
    ):
        d = make(
            [auto("t", mag)],
            clock=clock,
            limits={"t1": tlimits()},
            instruments={"imager": {"type": "imager", "guiding": model}},
            seeing=SeeingEstimator(_seeing_src(clock, values)),
        )
        visit = d._new_visit(d.requests["t-r"], NIGHT, 0.0)
        d._make_observation(visit)
        plan = visit.guide["t-c"]
        assert plan["on"] == on, plan
        assert plan["seeing"]["source"] == src and plan["seeing"]["value"] == value
        assert plan["recommendation"]["conditions"]["seeing"] == value


async def test_the_sequencer_binds_the_guiders_to_the_seeing_source(tmp_path):
    """The service gives the plan its value registry; a guider then
    publishes there even without a frame pipeline, so the seeing source
    hears every guider."""
    from robofast.config import load_config
    from robofast.sequencer_service import build
    from robofast.timeline import Timeline

    config = load_config(MINERVA_SITE / "aqawan1.json")
    config.pop("images", None)
    config.pop("operator_control", None)
    config["telescopes"]["t1"]["guider"] = "http://127.0.0.1:1"
    config["plan"] = {
        "type": "dispatch",
        "site": {"latitude": 0.0, "longitude": 0.0, "elevation": 0.0},
        "telescopes": {
            t: {"instruments": ["imager"], "min_alt": 21, "max_alt": 83}
            for t in config["telescopes"]
        },
        "seeing": {"min_samples": 1},
    }
    seq, _ = build(config, Timeline(), base=tmp_path)
    guider = seq.units["t1"].guider
    assert guider.registry is seq.registry
    guider._publish({"state": "guiding", "telemetry": {"seeing_arcsec": 1.7}})
    assert seq.plan.seeing_now().value == 1.7
