"""The autofocus's coarse mode (robofast.autofocus_coarse): from far out of
focus on the simulated focuser and star field (one bright star, a field of
fainter ones), alone and as the fine mode's fallback."""

from __future__ import annotations

import asyncio
import dataclasses
import re

import numpy as np
import pytest

from robofast.autofocus_coarse import FocusError, Level, approach, sweep_settings
from robofast.testing.focus_sim import FocusScene
from robofast.testing.starfield import Star

from .test_autofocus import OPTICS, Rig, config, scene, tolerance

TRUE = 60000.0
T = 0.01


def _scene(off_px: float = 0.0, true: float = TRUE, faint: bool = True) -> FocusScene:
    base = scene(2.0, seed=13, true_focus=true)
    stars = (list(base.stars) if faint else []) + [Star(256.0 + off_px, 256.0, 3e9)]
    return FocusScene(
        stars, optics=base.optics, detector=base.detector, true_focus=true, seeing=2.0,
        seed=13, effective_exposure=None,
    )  # fmt: skip


def _dark(sc: FocusScene) -> Level:
    empty = dataclasses.replace(sc, stars=[])

    def frame(t: float) -> np.ndarray:
        return empty.render(sc.true_focus, t, (0, 0, 512, 512), 1)

    return Level.of(frame(T), frame(T / 4), T)


async def _approach(sc: FocusScene, start: float, limits=(0.0, 120000.0)):
    pos = [start]

    async def expose(t: float) -> np.ndarray:
        return sc.render(pos[0], t, (0, 0, 512, 512), 1)

    async def move(x: float) -> None:
        pos[0] = x

    lines: list[str] = []
    a = await approach(
        expose, move, start=start, limits=limits, dark=_dark(sc), fwhm_px=2.0,
        step=2000.0, exptime=T, saturation=65535.0, say=lines.append,
    )  # fmt: skip
    return a, lines


@pytest.mark.parametrize(
    "offset, faint", [(40000.0, True), (-20000.0, True), (20000.0, False)]
)  # fmt: skip
async def test_from_far_out_the_approach_and_the_sweep_reach_focus(offset, faint):
    """With a field of fainter stars, and with the bright star alone (a
    planet: the sweep's exposure is chosen from a frame, sweep_settings)."""
    sc = _scene(faint=faint)
    rig = Rig(sc, position=TRUE + offset, max_step=120000)
    pos = rig.focuser

    async def expose(t: float) -> np.ndarray:
        return sc.render(pos.optical, t, (0, 0, 512, 512), 1)

    async def move(x: float) -> None:
        pos.Move(round(x))
        while pos.IsMoving:
            await asyncio.sleep(0.001)

    a = await approach(
        expose, move, start=TRUE + offset, limits=(0.0, 120000.0), dark=_dark(sc),
        fwhm_px=2.0, step=2000.0, exptime=T, saturation=65535.0, say=lambda s: None,
    )  # fmt: skip
    assert abs(a.position - TRUE) < 2000 and a.slope
    optics = dataclasses.replace(OPTICS, focal_plane_factor=a.slope / OPTICS.donut_slope)
    sweep = sweep_settings(await expose(1.0), 1.0, a, fwhm_px=2.0, saturation=60000.0)
    cfg = config(optics=optics, measure={"saturation": 60000.0}, **sweep)
    res = await rig.autofocus(cfg).run("sweep", start=a.position)
    rig.close()
    assert res.success, res.message
    assert abs(res.focus - TRUE) < tolerance(2.0), res.focus


@pytest.mark.parametrize(
    "true, start, words",
    [(130000.0, 100000.0, "upper end"), (-10000.0, 20000.0, "lower end")],
)
async def test_a_focus_beyond_the_travel_says_which_spacer(true, start, words):
    with pytest.raises(FocusError) as exc:
        await _approach(_scene(true=true, faint=False), start)
    assert words in str(exc.value)
    assert ("add a spacer" if words == "upper end" else "remove a spacer") in str(exc.value)


async def test_no_light_at_any_focus_says_so():
    with pytest.raises(FocusError, match="no light at any focus position"):
        await _approach(_scene(off_px=700.0, faint=False), 40000.0)


async def test_a_star_at_the_chips_edge_says_point_at_it_again():
    with pytest.raises(FocusError, match="point at it again"):
        await _approach(_scene(off_px=300.0, faint=False), 40000.0)


def test_a_shortened_frame_is_compared_with_the_dark_at_its_own_exposure():
    """The camera's bias does not shrink with the exposure; the sky does. A
    frame shortened 16x (a bright star near focus) still reads its light, at
    the first exposure's scale, not the bias it lost times 16."""
    rng = np.random.default_rng(1)
    t0, bias, sky, light = 1.0, 1000.0, 50.0, 500.0

    def frame(t: float, lit: bool) -> np.ndarray:
        mean = bias + (sky + (light if lit else 0.0)) * t / t0
        return mean + rng.normal(0.0, 5.0, (256, 256))

    dark = Level.of(frame(t0, False), frame(t0 / 4, False), t0)
    full, short = dark.sigma(frame(t0, True), t0), dark.sigma(frame(t0 / 16, True), t0 / 16)
    assert full > 0 and short > 0, (full, short)
    assert abs(short - full) < 0.05 * full, (full, short)


async def test_a_hot_pixel_does_not_shorten_the_exposure():
    """A pixel at full scale that a shorter exposure does not change (a hot
    pixel) is not the star's light: the exposure stays, and focus is found."""
    sc = _scene(faint=False)
    pos = [TRUE + 20000.0]

    async def expose(t: float) -> np.ndarray:
        img = sc.render(pos[0], t, (0, 0, 512, 512), 1)
        img[10, 10] = 65535.0
        return img

    async def move(x: float) -> None:
        pos[0] = x

    lines: list[str] = []
    a = await approach(
        expose, move, start=pos[0], limits=(0.0, 120000.0), dark=_dark(sc), fwhm_px=2.0,
        step=2000.0, exptime=T, saturation=65535.0, say=lines.append,
    )  # fmt: skip
    # Far out only the hot pixel clips: the exposure stays. (Near focus the
    # star itself clips, and is rightly taken shorter.)
    far = [ln for ln in lines if (m := re.search(r"donut ([-0-9.]+)", ln)) and
           (m.group(1) == "-" or float(m.group(1)) > 50.0)]  # fmt: skip
    assert far and all(f" in {T:g} s" in ln for ln in far), lines
    assert abs(a.position - TRUE) < 2000, (a.position - TRUE, lines)


async def test_every_focus_line_reads_in_plain_words():
    """Each line says how bright, as a multiple of the sky (or of the noise,
    where the sky does not show), never a standard error or a power of ten."""
    _, lines = await _approach(_scene(faint=False), TRUE + 40000.0)
    steps = [ln for ln in lines if ln.startswith("focus: ") and " position " in ln]
    assert steps, lines
    words = ("times the sky", "times the noise", "light on the chip", "no light above")
    for ln in steps:
        assert "sigma" not in ln and "e+" not in ln and " -" not in ln.split("donut")[0], ln
        assert any(w in ln for w in words), ln


def test_the_sweep_is_shortened_only_when_its_stars_all_clip():
    """At the handover, one frame at the sweep's exposure: stars measured
    unclipped keep it; only clipped ones, and it is the approach's last
    exposure times (FWHM / donut)^2 (the star gathers into the seeing's disk)."""
    from robofast.autofocus_coarse import Approach
    from robofast.testing.starfield import Detector, SimOptics, render

    opt = SimOptics(pixel_scale=1.0, pixel_size=13.5, f_ratio=6.5, obstruction=0.47)
    det = Detector(shape=(256, 256))

    def frame(rate: float) -> np.ndarray:
        return render([Star(128.0, 128.0, rate)], opt, det, seeing=2.0, exposure=1.0).image

    a = Approach(0.0, 0.01, [], exptime=0.000625, donut=8.0)
    field = sweep_settings(frame(2e4), 1.0, a, fwhm_px=2.0, saturation=60000.0)
    assert field == {"exptime": 1.0}
    alone = sweep_settings(frame(3e9), 1.0, a, fwhm_px=2.0, saturation=60000.0)
    assert alone["exptime"] == pytest.approx(0.000625 * (2.0 / 8.0) ** 2), alone
    assert alone["min_stars"] == 1


async def test_a_camera_minimum_exposure_is_never_asked_below():
    """A camera refuses an exposure under its ExposureMin (a CCD's is often
    0.1 s): the approach starts there and never shortens below it."""
    from robofast.owner import DeviceError

    sc, emin, asked = _scene(faint=False), 0.1, []
    pos = [TRUE + 20000.0]

    async def expose(t: float) -> np.ndarray:
        asked.append(t)
        if t < emin:
            raise DeviceError(f"InvalidValue: Duration {t} below ExposureMin {emin}")
        return sc.render(pos[0], t, (0, 0, 512, 512), 1)

    async def move(x: float) -> None:
        pos[0] = x

    a = await approach(
        expose, move, start=pos[0], limits=(0.0, 120000.0), dark=_dark(sc), fwhm_px=2.0,
        step=2000.0, exptime=emin, saturation=65535.0, say=lambda s: None, exposure_min=emin,
    )  # fmt: skip
    assert min(asked) >= emin, sorted(asked)[:5]
    assert abs(a.position - TRUE) < 2000, a.position - TRUE


def test_a_planet_too_bright_for_the_shortest_exposure_says_so():
    from robofast.autofocus_coarse import Approach
    from robofast.testing.starfield import Detector, SimOptics, render

    opt = SimOptics(pixel_scale=1.0, pixel_size=13.5, f_ratio=6.5, obstruction=0.47)
    img = render([Star(128.0, 128.0, 3e9)], opt, Detector(shape=(256, 256)), seeing=2.0,
                 exposure=1.0).image  # fmt: skip
    a = Approach(0.0, 0.01, [], exptime=0.1, donut=8.0)
    from robofast.autofocus_coarse import TooBright

    with pytest.raises(TooBright, match="shortest exposure") as exc:
        sweep_settings(img, 1.0, a, fwhm_px=2.0, saturation=60000.0, exposure_min=0.1)
    assert exc.value.need == pytest.approx(0.1 * (2.0 / 8.0) ** 2)


def test_a_target_is_chosen_by_name_and_refused_if_unknown_or_low():
    from datetime import UTC, datetime

    from robofast.autofocus_coarse import bright_target, targets_up

    site, when = (42.38, -71.13, 0.0), datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
    up = targets_up(site, when)
    assert up and bright_target(site, when).name == up[0].name
    other = up[-1]
    assert bright_target(site, when, other.name.lower()).name == other.name
    with pytest.raises(FocusError, match=r"Nostar.*not one robofast knows"):
        bright_target(site, when, "Nostar")
    with pytest.raises(FocusError, match=r"Canopus.*below"):
        bright_target(site, when, "Canopus")


def test_a_too_bright_target_names_a_fainter_star_by_its_rule():
    """The brightest listed star up now at least as many magnitudes fainter
    as the exposure is short: 2.5 log10(minimum / needed)."""
    from robofast.autofocus_coarse import fainter_star

    up = [("Vega", 0.03), ("Deneb", 1.25), ("Mirfak", 1.79)]
    # Jupiter (-2.5) needs 0.01 s on a camera whose shortest is 0.1 s: 2.5 mag fainter.
    assert fainter_star(up, -2.5, need=0.01, shortest=0.1) == ("Vega", 0.03)
    assert fainter_star(up, -2.5, need=0.001, shortest=0.1) is None  # 5 mag: none listed


# -- the coarse mode in robofast.autofocus: alone, and as the fine mode's fallback ---------


def _beside(sc: FocusScene):
    """The sky beside the target: the scene without its stars."""
    empty = dataclasses.replace(sc, stars=[])

    async def sky_beside(t1: float, t2: float) -> tuple[np.ndarray, np.ndarray]:
        return (
            empty.render(sc.true_focus, t1, (0, 0, 512, 512), 1),
            empty.render(sc.true_focus, t2, (0, 0, 512, 512), 1),
        )

    return sky_beside


def _coarse_config(**kw):
    base = dict(
        exptime=1.0, coarse_exptime=T, coarse_step=2000.0, max_seeing=2.0, saturation=65535.0
    )
    base.update(kw)
    return config(**base)


@pytest.mark.parametrize("offset", [40000.0, -20000.0])
async def test_the_coarse_mode_reaches_focus_from_far_out(offset):
    sc = _scene()
    rig = Rig(sc, position=TRUE + offset, max_step=120000)
    af = rig.autofocus(_coarse_config(mode="coarse"), sky_beside=_beside(sc))
    res = await af.run()
    rig.close()
    assert res.success, res.message
    assert res.used == "coarse" and res.method == "coarse"
    assert abs(res.focus - TRUE) < tolerance(2.0), res.focus - TRUE
    assert abs(rig.focuser.optical - TRUE) < tolerance(2.0)
    lines = res.details["coarse"]["lines"]
    assert lines and all(ln.startswith("focus: ") for ln in lines)
    assert [w for w, _ in af.events].count("autofocus_coarse_step") == len(lines)


@pytest.mark.parametrize("method", ["vcurve", "donut", "sweep"])
async def test_a_failed_fine_focus_falls_back_to_the_coarse_mode(method):
    """The fine mode, started 40000 steps out (the stars cover the chip),
    cannot give a focus; the coarse mode takes it from the same start."""
    sc = _scene()
    rig = Rig(sc, position=TRUE + 40000.0, max_step=120000)
    # (fallback false: the fine mode's own sweep fallback is not this test's)
    cfg = _coarse_config(method=method, fallback=False)
    af = rig.autofocus(cfg, sky_beside=_beside(sc))
    res = await af.run()
    rig.close()
    assert res.success, res.message
    assert res.used == "coarse" and res.method == method
    assert res.fallbacks and res.fallbacks[-1].startswith("fine (")
    assert ("autofocus_fallback", "coarse") in [(w, d.get("to")) for w, d in af.events]
    assert abs(res.focus - TRUE) < tolerance(2.0), res.focus - TRUE


@pytest.mark.parametrize("why", ["disabled", "no reference"])
async def test_without_the_fallback_a_failed_fine_focus_fails_back_at_its_start(why):
    sc = _scene()
    start = TRUE + 40000.0
    rig = Rig(sc, position=start, max_step=120000)
    if why == "disabled":
        af = rig.autofocus(_coarse_config(coarse_fallback=False), sky_beside=_beside(sc))
    else:
        af = rig.autofocus(_coarse_config())
    res = await af.run()
    rig.close()
    assert not res.success
    assert "autofocus_coarse_step" not in [w for w, _ in af.events]
    assert rig.focuser.Position == start


async def test_the_coarse_mode_refuses_a_focuser_that_moves_by_steps_before_moving():
    sc = _scene()
    start = TRUE + 40000.0
    rig = Rig(sc, position=start, max_step=120000)
    rig.focuser.Absolute = False
    beside: list[float] = []

    async def sky_beside(t1, t2):
        beside.append(t1)
        return await _beside(sc)(t1, t2)

    res = await rig.autofocus(_coarse_config(mode="coarse"), sky_beside=sky_beside).run()
    rig.close()
    assert not res.success and "moves by steps" in res.message
    assert not beside and not res.points and rig.focuser.Position == start


async def test_a_coarse_focus_with_no_light_fails_back_at_its_start():
    """The bright star off the chip and no field: the whole travel is
    searched, nothing is found, and the focuser goes back where it was."""
    sc = _scene(off_px=700.0, faint=False)
    start = TRUE + 40000.0
    rig = Rig(sc, position=start, max_step=120000)
    res = await rig.autofocus(_coarse_config(mode="coarse"), sky_beside=_beside(sc)).run()
    rig.close()
    assert not res.success and "no light at any focus position" in res.message
    assert rig.focuser.Position == start


async def test_a_camera_error_in_the_coarse_mode_is_the_cameras():
    """Device errors propagate (the sequencer treats them as the
    telescope's), in the coarse mode as in the fine."""
    from robofast.owner import DeviceError

    sc = _scene()
    rig = Rig(sc, position=TRUE + 40000.0, max_step=120000)

    async def sky_beside(t1, t2):
        raise DeviceError("t1.camera: InvalidValue: Duration below ExposureMin")

    with pytest.raises(DeviceError, match="ExposureMin"):
        await rig.autofocus(_coarse_config(mode="coarse"), sky_beside=sky_beside).run()
    rig.close()


def test_a_mode_is_checked_at_startup():
    with pytest.raises(ValueError, match=r"autofocus\.mode"):
        config(mode="rough").validate()
    with pytest.raises(ValueError, match="coarse_step"):
        config(coarse_step=0).validate()


def test_a_target_near_the_sun_is_passed_over_or_refused_by_name():
    from datetime import UTC, datetime

    from robofast.autofocus_coarse import bright_target, targets_up

    site, when = (42.38, -71.13, 0.0), datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
    every = targets_up(site, when)
    assert every and all(0 <= t.sun <= 180 for t in every)
    nearest = min(every, key=lambda t: t.sun)
    kept = targets_up(site, when, min_sun_separation=nearest.sun + 0.1)
    assert nearest.name not in [t.name for t in kept]
    with pytest.raises(FocusError, match=rf"{nearest.name}.*from the Sun"):
        bright_target(site, when, nearest.name, min_sun_separation=nearest.sun + 0.1)
