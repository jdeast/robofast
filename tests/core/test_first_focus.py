"""Focus from far out of focus (robofast.first_focus) on the simulated
focuser and star field: one bright star, a field of fainter ones."""

from __future__ import annotations

import asyncio
import dataclasses
import re

import numpy as np
import pytest

from robofast.first_focus import FocusError, Level, approach, sweep_settings, walk_through
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


async def test_the_walk_through_brings_a_person_to_focus():
    """A person who does what each line says (4000 steps a knob turn)."""
    sc = _scene(faint=False)
    turn_steps = 4000.0
    state = {"x": TRUE + 10000.0, "last": 0.0}
    lines: list[str] = []

    async def expose(t: float) -> np.ndarray:
        return sc.render(state["x"], t, (0, 0, 512, 512), 1)

    async def ask(prompt: str) -> str:
        adv, last = lines[-1], state["last"]
        if "stop here" in adv:
            return "q"
        if "quarter turn, either way" in adv:
            turn = 0.25 * turn_steps
        elif m := re.search(r"turn (the same way|back), ([0-9.]+) times your last turn", adv):
            turn = float(m.group(2)) * last * (1 if m.group(1) == "the same way" else -1)
        elif m := re.search(r"turn a quarter turn, (the same way|back)", adv):
            sign = 1 if m.group(1) == "the same way" else -1
            turn = 0.25 * turn_steps * (1 if last >= 0 else -1) * sign
        elif "turn back, half your last turn" in adv:
            turn = -0.5 * last
        elif "the same way, as far as your last turn" in adv:
            turn = last
        else:
            turn = turn_steps * (1.0 if last >= 0 else -1.0)  # no light: a full turn
        state["x"] += turn
        state["last"] = turn
        return ""

    await walk_through(
        expose, ask, dark=_dark(sc), fwhm_px=2.0, exptime=T, saturation=65535.0,
        say=lines.append, max_frames=30,
    )  # fmt: skip
    assert "stop here" in lines[-1], lines[-3:]
    assert abs(state["x"] - TRUE) < tolerance(2.0), state["x"] - TRUE


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
    from robofast.first_focus import Approach
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


async def test_the_kind_refuses_a_focuser_that_moves_by_steps_before_anything_moves():
    """first_focus in any plan: a relative focuser (Absolute false) is
    refused before a slew, an exposure or a move."""
    from types import SimpleNamespace

    from robofast.first_focus import run_first_focus
    from robofast.sequencer import ObservationFailed

    done: list[str] = []

    class Focuser:
        async def get(self, name):
            done.append(f"get {name}")
            return {"Absolute": False}.get(name, 0)

        async def call(self, name, *args):
            done.append(f"call {name}")

    class Camera:
        async def call(self, name, *args):
            done.append(f"camera {name}")
            raise AssertionError("exposed")

        get = call

    async def goto(*args):
        done.append("slew")

    seq = SimpleNamespace(_goto=goto, _event=lambda *a, **k: None)
    r = SimpleNamespace(
        sequencer=seq, state=SimpleNamespace(focusers={"t1": Focuser()}),
        unit=SimpleNamespace(name="t1"), tel=None, cam=Camera(),
        obs=SimpleNamespace(
            autofocus={"fwhm_px": 2.0, "saturation": 65535.0}, ra_hours=1.0, dec_deg=10.0
        ),
    )  # fmt: skip
    with pytest.raises(ObservationFailed, match="moves by steps"):
        await run_first_focus(r)
    assert done == ["get Absolute"], done


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
    from robofast.first_focus import Approach
    from robofast.testing.starfield import Detector, SimOptics, render

    opt = SimOptics(pixel_scale=1.0, pixel_size=13.5, f_ratio=6.5, obstruction=0.47)
    img = render([Star(128.0, 128.0, 3e9)], opt, Detector(shape=(256, 256)), seeing=2.0,
                 exposure=1.0).image  # fmt: skip
    a = Approach(0.0, 0.01, [], exptime=0.1, donut=8.0)
    from robofast.first_focus import TooBright, too_bright_line

    with pytest.raises(TooBright, match="shortest exposure") as exc:
        sweep_settings(img, 1.0, a, fwhm_px=2.0, saturation=60000.0, exposure_min=0.1)
    # The stop says what to run next, naming a fainter star up now.
    line = too_bright_line(exc.value, {"mag": -2.5, "stars_up": [["Vega", 0.03], ["Mirfak", 1.79]]})
    # 0.1 s / 0.0063 s is 3.0 mag: fainter than -2.5 + 3.0 = +0.5, so Mirfak, not Vega.
    assert "--focus-on Mirfak" in line, line
    assert "--focus-on NAME" in too_bright_line(exc.value, {"mag": -2.5, "stars_up": []})


async def test_a_camera_error_in_the_focus_step_stops_it_in_the_cameras_words():
    from types import SimpleNamespace

    from robofast.first_focus import run_first_focus
    from robofast.owner import DeviceError
    from robofast.sequencer import ObservationFailed

    events: list[tuple[str, dict]] = []

    class Focuser:
        async def get(self, name):
            return {"Absolute": True}.get(name, 0)

        async def call(self, name, *args):
            pass

    class Camera:
        async def call(self, name, *args):
            raise DeviceError("InvalidValue: Duration 0.01 below ExposureMin 0.1")

        get = call

    async def goto(*args):
        pass

    seq = SimpleNamespace(_goto=goto, _event=lambda kind, **k: events.append((kind, k)))
    r = SimpleNamespace(
        sequencer=seq, state=SimpleNamespace(focusers={"t1": Focuser()}),
        unit=SimpleNamespace(name="t1"), tel=None, cam=Camera(),
        obs=SimpleNamespace(
            autofocus={"fwhm_px": 2.0, "saturation": 65535.0}, ra_hours=1.0, dec_deg=10.0
        ),
    )  # fmt: skip
    with pytest.raises(ObservationFailed, match=r"ExposureMin 0\.1"):
        await run_first_focus(r)
    assert [k for k, _ in events] == ["first_focus_failed"], events


def test_a_target_is_chosen_by_name_and_refused_if_unknown_or_low():
    from datetime import UTC, datetime

    from robofast.first_focus import bright_target, targets_up

    site, when = (42.38, -71.13, 0.0), datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
    up = targets_up(site, when)
    assert up and bright_target(site, when).name == up[0].name
    other = up[-1]
    assert bright_target(site, when, other.name.lower()).name == other.name
    with pytest.raises(FocusError, match=r"Nostar.*not one first light knows"):
        bright_target(site, when, "Nostar")
    with pytest.raises(FocusError, match=r"Canopus.*below"):
        bright_target(site, when, "Canopus")


def test_a_too_bright_target_names_a_fainter_star_by_its_rule():
    """The brightest listed star up now at least as many magnitudes fainter
    as the exposure is short: 2.5 log10(minimum / needed)."""
    from robofast.first_focus import fainter_star

    up = [("Vega", 0.03), ("Deneb", 1.25), ("Mirfak", 1.79)]
    # Jupiter (-2.5) needs 0.01 s on a camera whose shortest is 0.1 s: 2.5 mag fainter.
    assert fainter_star(up, -2.5, need=0.01, shortest=0.1) == ("Vega", 0.03)
    assert fainter_star(up, -2.5, need=0.001, shortest=0.1) is None  # 5 mag: none listed
