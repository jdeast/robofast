"""Focus from far out of focus (robofast.first_focus) on the simulated
focuser and star field: one bright star, a field of fainter ones."""

from __future__ import annotations

import asyncio
import dataclasses
import re

import numpy as np
import pytest

from robofast.first_focus import FocusError, Level, approach, walk_through
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
    return Level.of(empty.render(sc.true_focus, T, (0, 0, 512, 512), 1))


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


@pytest.mark.parametrize("offset", [40000.0, -20000.0])
async def test_from_far_out_the_approach_and_the_sweep_reach_focus(offset):
    sc = _scene()
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
    cfg = config(optics=optics, exptime=1.0, measure={"saturation": 60000.0})
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
