"""Twilight sky flats (robofast_core/skyflats.py, docs/skyflats.md): exposure
times predicted on the sky simulator's twilight, evening and morning, the
level guards, the request type, and the sequencer taking them."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from robofast_core.photometry import PortPhotometry, SkyConditions
from robofast_core.plan import Observation, StaticPlan
from robofast_core.skyflats import FlatConfig, flat_spot, take_flats, twilight_rate
from robofast_core.testing.skysim import SkyCamera, SkyScene
from robofast_core.testing.starfield import Detector

FILTERS = {"B": 0.35, "V": 1.0, "R": 1.6}


class Twilight:
    """The simulator's sky through a 64x64 camera, the Sun moving at
    ``rate`` deg/s from ``alt0``; simulated time advances by each exposure
    and its overhead."""

    def __init__(self, alt0: float, rate: float, overhead: float = 15.0) -> None:
        cam = SkyCamera((64, 64), 1.0, detector=Detector(shape=(64, 64), gain=1.5, bias=1000))
        from robofast_core.catalog import Stars

        self.sky = SkyScene(Stars.empty(), cam, PortPhotometry(zero_point=24.0, gain=1.5), seed=2)
        self.alt0, self.rate, self.overhead, self.t = alt0, rate, overhead, 0.0
        self.log: list[tuple[str, float, float]] = []

    def sun_alt(self) -> float:
        return self.alt0 + self.rate * self.t

    async def expose(self, flt: str, exptime: float, off) -> float:
        self.t += self.overhead
        mid = self.alt0 + self.rate * (self.t + exptime / 2)
        zp = 24.0 + 2.5 * math.log10(FILTERS[flt])
        self.sky.photometry = replace(self.sky.photometry, zero_point=zp)
        self.sky.conditions = SkyConditions(sun_alt=mid, target_alt=75.0)
        frame = self.sky.render(0.0, 60.0, datetime(2026, 10, 31, 1, tzinfo=UTC), exptime)
        self.t += exptime
        self.log.append((flt, exptime, mid))
        return float(np.median(frame.image))

    async def wait(self, s: float) -> None:
        self.t += s


def cfg(**kw):
    base = dict(filters=FILTERS, count=3, rate_at_minus6=6e4, min_exptime=1.0, max_exptime=60.0)
    return FlatConfig(**{**base, **kw})


@pytest.mark.parametrize("evening", [True, False])
async def test_twilight_flats_hit_the_target_level(evening):
    tw = Twilight(-2.5 if evening else -11.5, (-1 if evening else 1) * 0.25 / 60)
    c = cfg()
    frames = await take_flats(
        c, evening=evening, sun_alt=tw.sun_alt, expose=tw.expose, wait=tw.wait
    )
    kept = [f for f in frames if f.kept]
    # Evening: least sensitive (B) first; morning: most sensitive (R) first.
    order = list(dict.fromkeys(f.filter for f in frames))
    assert order == (["B", "V", "R"] if evening else ["R", "V", "B"])
    assert {f.filter for f in kept} == {"B", "V", "R"}
    assert all(sum(f.filter == b and f.kept for f in frames) == 3 for b in FILTERS)
    # After the first frame of the night, the levels are near the target.
    levels = [f.median for f in kept[1:]]
    assert all(abs(lv - c.target_adu) / c.target_adu < 0.25 for lv in levels), levels
    # Every kept frame within the level guards and the exposure limits.
    assert all(c.min_adu <= f.median <= c.max_adu for f in kept)
    assert all(c.min_exptime <= f.exptime <= c.max_exptime for f in frames)
    # Dithered: no two frames at the same offset.
    assert len({f.dither for f in frames}) == len(frames)


async def test_a_wrong_first_guess_is_discarded_then_learned():
    tw = Twilight(-4.0, -0.25 / 60)
    frames = await take_flats(
        cfg(rate_at_minus6=6e3, filters={"V": 1.0}),
        evening=True, sun_alt=tw.sun_alt, expose=tw.expose, wait=tw.wait,
    )  # fmt: skip
    assert frames[0].why == "near saturation" and not frames[0].kept
    assert sum(f.kept for f in frames) == 3


async def test_too_dark_ends_the_evening_and_too_early_waits():
    tw = Twilight(-11.9, -0.25 / 60)
    frames = await take_flats(cfg(), evening=True, sun_alt=tw.sun_alt, expose=tw.expose,
                              wait=tw.wait)  # fmt: skip
    assert len(frames) <= 2  # the Sun leaves the window almost at once
    tw = Twilight(1.0, -0.25 / 60)  # the Sun still up: wait for it
    c = cfg(filters={"V": 1.0}, max_wait=3600)
    frames = await take_flats(c, evening=True, sun_alt=tw.sun_alt, expose=tw.expose, wait=tw.wait)
    assert frames and frames[0].sun_alt <= -1.0


def test_model_and_flat_spot():
    assert twilight_rate(-6.0) == pytest.approx(1.0)
    assert twilight_rate(-7.0) == pytest.approx(10 ** (-0.4 * 1.2), rel=1e-3)
    ra, dec, sun = flat_spot(31.680407, -110.878977, datetime(2026, 10, 31, 1, 5, tzinfo=UTC))
    assert -8 < sun < -6 and 0 <= ra < 24 and -90 < dec < 90
    with pytest.raises(ValueError, match="unknown keys"):
        FlatConfig.from_dict({"filterz": {}})
    with pytest.raises(ValueError, match="sky_flat"):
        Observation("x", flats={"count": 1})


def test_sky_flat_request_dispatches():
    import asyncio

    from robofast_core.request_model import load_request_groups

    from .test_dispatch import group, make

    g = group("flats", ics=[{"exposure_time": 1.0, "exposure_count": 4,
                             "optical_elements": {"filter": "V"}}])  # fmt: skip
    g["requests"][0]["configurations"][0]["type"] = "SKY_FLAT"
    load_request_groups([g])

    async def go():
        obs = await make([g], limits=None).next_observation("t1")
        assert obs.kind == "sky_flat" and obs.flats == {"filters": ["V"], "count": 4}

    asyncio.run(go())


async def test_the_sequencer_takes_flats_through_the_filter_wheel(make_rig, tmp_path):
    from robofast_core.owner import DeviceOwner
    from robofast_core.testing.focus_sim import SimFocuser, StarfieldCamera
    from robofast_core.testing.skysim import SkyFocusScene

    from .test_imaging import headers
    from .test_pointmodel_sequencer import run, sky_rig

    when = datetime(2026, 10, 31, 1, 5, tzinfo=UTC)  # the Sun at -6.9 deg
    plan = StaticPlan([{"name": "flats", "kind": "sky_flat", "exptime": 1.0}], ["t1"])
    rig, unit, mount, scn, imaging = sky_rig(make_rig, plan, tmp_path, truth={})
    rig.sequencer.clock = lambda: when
    imaging.set_clock(rig.sequencer.clock)
    unit.pointing = None
    sky = scn.sky
    from robofast_core.catalog import Stars

    sky.catalog = Stars.empty()
    sky.conditions = SkyConditions(sun_alt=-6.9, target_alt=75.0)
    sky.sky_mag = None  # the twilight model, not a fixed sky
    sky.photometry = PortPhotometry(zero_point=24.0, gain=1.5)
    cam = StarfieldCamera(SimFocuser(position=20000), SkyFocusScene(sky, mount, lambda: when))
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=5)
    from robofast_core.testing.fakes import FakeFilterWheel

    wheel = FakeFilterWheel(("V", "R"), move_time=0.01)
    unit.filterwheel = DeviceOwner("t1.filterwheel", lambda: wheel, rig.timeline, default_timeout=5)
    unit.flats = FlatConfig(
        filters={"V": 1.0}, count=2, rate_at_minus6=6e4, overhead=0.0, wait=0.1,
        latitude=31.680407, longitude=-110.878977, min_exptime=0.05, max_exptime=5.0,
    )  # fmt: skip
    await run(rig, imaging, timeout=60)
    assert [h[2] for h in plan.history] == ["complete"]
    kept = [f for t, f in rig.sequencer.flat_frames if f.kept]
    assert len(kept) == 2 and wheel.Names[wheel.Position] == "V"
    flats = [h for _, h in headers(tmp_path, "t1")]
    assert flats and all(h["IMAGETYP"] == "FLAT" and h["FILTER"] == "V" for h in flats)
    assert rig.timeline.events("sky_flats_done")[0].detail["kept"] == 2
