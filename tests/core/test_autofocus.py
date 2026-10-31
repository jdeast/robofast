"""Autofocus (robofast_core.autofocus) on the simulated focuser and star field.

The scene: a 512x512 camera at 1"/px, 13.5 um pixels, f/6.5 with a 47 %
central obstruction; the focuser moves the instrument (1 um of focal plane
per step). Accuracy is judged against the depth of focus that matters at
that seeing: the focus error must leave a geometric blur under a quarter of
the seeing FWHM (``tolerance``), and never more than ~90 steps.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import functools
import math

import numpy as np
import pytest

from robofast_core.autofocus import (
    Autofocus,
    AutofocusConfig,
    AutofocusSetup,
    FocuserError,
    FocusModel,
    FocusOptics,
    FocusStore,
    publish_result,
)
from robofast_core.owner import DeviceError, DeviceOwner
from robofast_core.registry import ValueRegistry
from robofast_core.testing.focus_sim import FocusScene, SimFocuser, StarfieldCamera
from robofast_core.testing.starfield import Detector, SimOptics, random_stars
from robofast_core.timeline import Timeline

PIXEL_SCALE, PIXEL_SIZE, F_RATIO, OBSTRUCTION = 1.0, 13.5, 6.5, 0.47
OPTICS = FocusOptics(
    f_ratio=F_RATIO, pixel_size=PIXEL_SIZE, pixel_scale=PIXEL_SCALE, obstruction=OBSTRUCTION
)
SIM_OPTICS = SimOptics(
    pixel_scale=PIXEL_SCALE, pixel_size=PIXEL_SIZE, f_ratio=F_RATIO, obstruction=OBSTRUCTION
)
DETECTOR = Detector(shape=(512, 512))
TRUE_FOCUS = 20000.0
START_ERROR = 300.0


def tolerance(seeing: float) -> float:
    """Steps of focus error that blur a star by a quarter of the seeing FWHM."""
    return max(20.0, 0.25 * (seeing / PIXEL_SCALE) * F_RATIO * PIXEL_SIZE)


def config(**kw) -> AutofocusConfig:
    base = dict(
        optics=OPTICS,
        exptime=0.001,
        poll_interval=0.001,
        max_start_error=START_ERROR,
        readout_margin=5.0,
        move_timeout=2.0,
    )
    base.update(kw)
    return AutofocusConfig(**base)


def scene(
    seeing: float = 2.0, *, seed: int = 3, true_focus: float = TRUE_FOCUS, **kw
) -> FocusScene:
    rng = np.random.default_rng(seed)
    stars = random_stars(
        6, DETECTOR.shape, flux=(1.5e5, 6e5), margin=60, min_separation=110, rng=rng
    )
    return FocusScene(
        stars,
        optics=kw.pop("optics", SIM_OPTICS),
        detector=DETECTOR,
        true_focus=true_focus,
        seeing=seeing,
        seed=seed,
        **kw,
    )


class Rig:
    def __init__(self, sc: FocusScene, *, position: float = TRUE_FOCUS, **foc) -> None:
        self.timeline = Timeline()
        self.scene = sc
        self.focuser = SimFocuser(position=round(position), **foc)
        self.camera = StarfieldCamera(self.focuser, sc)
        self.fo = DeviceOwner("t1.focuser", lambda: self.focuser, self.timeline, default_timeout=1)
        self.co = DeviceOwner("t1.camera", lambda: self.camera, self.timeline, default_timeout=5)
        self.fg = self.fo.grant("af")
        self.cg = self.co.grant("af")

    def autofocus(self, cfg: AutofocusConfig | None = None, **kw) -> Autofocus:
        events: list[tuple[str, dict]] = []
        af = Autofocus(
            "t1",
            cfg or config(),
            self.fg,
            self.cg,
            event=lambda what, **d: events.append((what, d)),
            **kw,
        )
        af.events = events  # type: ignore[attr-defined]
        return af

    def close(self) -> None:
        self.fo.close()
        self.co.close()


@functools.cache
def _calibration_store() -> FocusStore:
    """One calibration sweep (2" seeing), shared by the tests."""

    async def go() -> FocusStore:
        store = FocusStore()
        rig = Rig(scene(2.0, seed=11))
        res = await rig.autofocus(store=store).calibrate(start=TRUE_FOCUS + 150)
        rig.close()
        assert res.success, res.message
        return store

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(go())
    with concurrent.futures.ThreadPoolExecutor(1) as pool:  # from inside a test's loop
        return pool.submit(lambda: asyncio.run(go())).result()


@pytest.fixture
def calibrated() -> FocusStore:
    """A store holding only the calibration (not the zero point and last
    focus that the calibration run also recorded)."""
    store = copy.deepcopy(_calibration_store())
    store.data["models"], store.data["last"], store.data["history"] = {}, {}, []
    return store


# -- the calibration --------------------------------------------------------------


def test_calibration_measures_the_optics(calibrated):
    cal = calibrated.calibration(FocusStore.key("t1"))
    assert cal is not None
    # The arms' slopes are the geometric ones (k |dz| / N per pixel).
    assert cal.slope_plus == pytest.approx(OPTICS.hfd_slope, rel=0.03)
    assert cal.slope_minus == pytest.approx(OPTICS.hfd_slope, rel=0.03)
    assert cal.donut_plus == pytest.approx(OPTICS.donut_slope, rel=0.03)
    # No aberrations in the simulator: the arms meet at the focus.
    assert abs(cal.focus - TRUE_FOCUS) < tolerance(2.0)
    assert abs(cal.offset_plus) < 40 and abs(cal.offset_minus) < 40
    assert cal.seeing_hfd == pytest.approx(1.2 * 2.0 / PIXEL_SCALE, rel=0.15)


def test_calibration_persists_and_reloads(tmp_path, calibrated):
    path = tmp_path / "focus.json"
    store = FocusStore(path)
    key = FocusStore.key("t1", "imager", "V")
    store.set_calibration(key, calibrated.calibration(FocusStore.key("t1")))
    again = FocusStore(path)
    assert again.calibration(key) == store.calibration(key)
    assert again.calibration(FocusStore.key("t1", "fau", "V")) is None


# -- accuracy, every method, seeing 1-4" ----------------------------------------------


@pytest.mark.parametrize("seeing", [1.0, 2.0, 3.0, 4.0])
@pytest.mark.parametrize(
    "method,exposures",
    [("vcurve", 2), ("donut", 1), ("sweep", 9)],
    ids=["vcurve", "donut", "sweep"],
)
async def test_true_focus_recovered(method, exposures, seeing, calibrated):
    # The prediction is off by up to START_ERROR, either way.
    for seed, offset in ((5, -0.9 * START_ERROR), (6, 0.8 * START_ERROR)):
        rig = Rig(scene(seeing, seed=seed), position=TRUE_FOCUS + offset)
        af = rig.autofocus(store=calibrated)
        res = await af.run(method, start=TRUE_FOCUS + offset)
        rig.close()
        assert res.success, res.message
        assert res.used == method and res.fallbacks == []
        assert res.exposures == exposures
        assert abs(res.focus - TRUE_FOCUS) < tolerance(seeing), (seed, res.focus)
        # The optics end where the result says (no backlash here).
        assert rig.focuser.optical == pytest.approx(round(res.focus), abs=1)
        assert res.focus_err is not None and 0 < res.focus_err < 3 * tolerance(seeing)


async def test_donut_needs_no_calibration():
    rig = Rig(scene(3.0, seed=8), position=TRUE_FOCUS - 200)
    res = await rig.autofocus().run("donut", start=TRUE_FOCUS - 200)
    assert res.success and abs(res.focus - TRUE_FOCUS) < tolerance(3.0)
    assert res.exposures == 1


async def test_parabola_sweep_uses_its_fit(calibrated):
    rig = Rig(scene(2.0, seed=9), position=TRUE_FOCUS + 250)
    res = await rig.autofocus(config(sweep_fit="parabola"), store=calibrated).run(
        "sweep", start=TRUE_FOCUS + 250
    )
    assert res.success and res.details["sweep"]["fit"] == "parabola"
    # A parabola is the wrong shape for a V, but bracketed it still finds it.
    assert abs(res.focus - TRUE_FOCUS) < 3 * tolerance(2.0)


# -- the V-curve's two points ---------------------------------------------------------


async def test_vcurve_points_are_blur_dominated_and_seeing_does_not_bias(calibrated):
    """Calibrated in 2" seeing, run in 1" and 4": both exposures are far from
    focus (geometric blur >= 6x the worst seeing), so the answer does not
    move with the seeing."""
    results = {}
    for seeing in (1.0, 4.0):
        rig = Rig(scene(seeing, seed=12))
        af = rig.autofocus(store=calibrated)
        res = await af.run("vcurve", start=TRUE_FOCUS)
        assert res.success
        near, _ = af.offsets()
        for optical in rig.camera.exposed_at:
            blur = abs(optical - TRUE_FOCUS) * OPTICS.donut_slope
            assert blur >= 6 * 4.0 / PIXEL_SCALE - 1
            assert abs(optical - TRUE_FOCUS) >= near - 1
        results[seeing] = res.focus
    assert abs(results[1.0] - results[4.0]) < tolerance(4.0)


async def test_wrong_side_is_detected_and_recovered(calibrated):
    # Prediction 4000 steps too low: both points land below focus.
    rig = Rig(scene(2.0, seed=13), position=TRUE_FOCUS - 4000)
    af = rig.autofocus(config(max_start_error=0), store=calibrated)
    near, far = af.offsets()
    assert far + 4000 > 0 and near < 4000
    res = await af.run("vcurve", start=TRUE_FOCUS - 4000)
    assert res.success and res.used == "vcurve"
    assert any(f.startswith("wrong side") for f in res.fallbacks)
    assert res.exposures == 4  # two to find the side, two from the right one
    assert abs(res.focus - TRUE_FOCUS) < tolerance(2.0)
    assert [e for e, _ in af.events].count("autofocus_wrong_side") == 1


async def test_inconsistent_slope_falls_back_to_a_sweep(calibrated):
    rig = Rig(scene(2.0, seed=14))
    af = rig.autofocus(store=calibrated)
    real = af.exposer
    n = 0

    async def wrong_star(exptime):
        # The second frame shows the first one's star sizes (a different
        # star, a saturated one, a passing cloud...): the slope is wrong.
        nonlocal n
        n += 1
        if n == 2:
            rig.scene.true_focus = TRUE_FOCUS - 1200
            try:
                return await real(exptime)
            finally:
                rig.scene.true_focus = TRUE_FOCUS
        return await real(exptime)

    af.exposer = wrong_star
    res = await af.run("vcurve", start=TRUE_FOCUS)
    assert res.success and res.used == "sweep"
    assert res.fallbacks and "secant/slope" in res.fallbacks[0]
    assert res.exposures == 2 + 9
    assert abs(res.focus - TRUE_FOCUS) < tolerance(2.0)


async def test_inconsistency_without_fallback_fails_and_returns(calibrated):
    rig = Rig(scene(2.0, seed=15), position=TRUE_FOCUS + 100)
    rig.scene.transparency = 0.0  # clouds: no stars at all
    af = rig.autofocus(config(fallback=False), store=calibrated)
    before = af.model.zero_point  # from the calibration run
    res = await af.run("vcurve", start=TRUE_FOCUS + 100)
    assert not res.success and res.focus is None
    assert "stars" in res.message
    # Back where it started, and no measurement claimed.
    assert rig.focuser.Position == TRUE_FOCUS + 100
    assert af.model.zero_point == before
    assert calibrated.last_focus(FocusStore.key("t1")) is None


async def test_no_stars_in_a_sweep_fails_cleanly(calibrated):
    rig = Rig(scene(2.0, seed=15), position=TRUE_FOCUS)
    rig.scene.transparency = 0.0
    res = await rig.autofocus(store=calibrated).run("sweep", start=TRUE_FOCUS)
    assert not res.success and "usable points" in res.message
    assert rig.focuser.Position == TRUE_FOCUS


# -- backlash ----------------------------------------------------------------------


async def test_backlash_is_taken_out_by_approaching_from_the_side(calibrated):
    lash = 150
    for cfg_lash, ok in ((0, False), (300, True)):
        rig = Rig(scene(1.5, seed=16), position=TRUE_FOCUS, backlash=lash)
        af = rig.autofocus(config(backlash=cfg_lash), store=calibrated)
        res = await af.run("sweep", start=TRUE_FOCUS)
        assert res.success
        # Where the optics really are, after the final move.
        error = abs(rig.focuser.optical - TRUE_FOCUS)
        if ok:
            assert error < tolerance(1.5)
            # Every exposure and the final position were reached moving down.
            moves = rig.focuser.moves
            final = moves[-1]
            assert moves[-2] > final  # the overshoot came from above
        else:
            # Arriving from below leaves the optics a backlash short.
            assert error > 0.7 * lash


async def test_vcurve_with_backlash(calibrated):
    rig = Rig(scene(2.0, seed=17), position=TRUE_FOCUS - 1000, backlash=120)
    af = rig.autofocus(config(backlash=250), store=calibrated)
    res = await af.run("vcurve", start=TRUE_FOCUS)
    assert res.success
    assert abs(rig.focuser.optical - TRUE_FOCUS) < tolerance(2.0)


# -- focuser faults ------------------------------------------------------------------


async def test_focuser_timeout_raises_and_halts(calibrated):
    rig = Rig(scene(2.0), position=TRUE_FOCUS)
    rig.focuser.stuck = True
    halted = []
    real_halt = rig.focuser.Halt
    rig.focuser.Halt = lambda: (halted.append(1), real_halt())[1]  # type: ignore[method-assign]
    af = rig.autofocus(config(move_timeout=0.2), store=calibrated)
    with pytest.raises(FocuserError, match="still moving"):
        await af.run("vcurve", start=TRUE_FOCUS)
    assert halted


async def test_focuser_failure_and_wrong_position_raise(calibrated):
    rig = Rig(scene(2.0), position=TRUE_FOCUS)
    rig.focuser.fail_moves = 1
    with pytest.raises(DeviceError):
        await rig.autofocus(store=calibrated).run("vcurve", start=TRUE_FOCUS)
    rig = Rig(scene(2.0), position=TRUE_FOCUS)
    rig.focuser.arrival_error = 40
    with pytest.raises(FocuserError, match="commanded"):
        await rig.autofocus(store=calibrated).run("vcurve", start=TRUE_FOCUS)


async def test_a_hung_focuser_times_out_through_the_owner(calibrated):
    rig = Rig(scene(2.0), position=TRUE_FOCUS)
    rig.focuser.hang()
    with pytest.raises(DeviceError):
        await asyncio.wait_for(rig.autofocus(store=calibrated).run("vcurve"), 10)
    rig.focuser.unhang()


async def test_positions_beyond_the_focuser_range_fail_without_moving(calibrated):
    rig = Rig(scene(2.0), position=1000, max_step=3000)
    res = await rig.autofocus(store=calibrated).run("vcurve", start=1000)
    assert not res.success and "outside" in res.message
    assert rig.focuser.moves == []


# -- the temperature model -----------------------------------------------------------


async def test_model_predicts_the_start_and_only_its_zero_point_learns(calibrated):
    model = FocusModel(
        terms={"{tel}.focuser.temperature": -50.0},
        reference={"{tel}.focuser.temperature": 10.0},
        zero_point=TRUE_FOCUS + 250,  # stale: 250 steps off
    )
    temps = {"{tel}.focuser.temperature": 12.0}
    rig = Rig(scene(2.0, seed=18), position=TRUE_FOCUS)
    af = rig.autofocus(store=calibrated, model=model, values=lambda: temps)
    assert af.model.predict(temps) == TRUE_FOCUS + 250 - 100
    res = await af.run("vcurve")
    assert res.predicted == TRUE_FOCUS + 150 and res.start == TRUE_FOCUS + 150
    # The measurement wins; the model is re-anchored on it.
    assert abs(res.focus - TRUE_FOCUS) < tolerance(2.0)
    assert af.model.predict(temps) == pytest.approx(res.focus)
    # The zero point persists in the store under this telescope's key...
    zp, _ = calibrated.model_zero_point(FocusStore.key("t1"))
    assert zp == pytest.approx(res.focus + 100)
    # ...and a later run (2 C warmer) starts from the updated model.
    af2 = rig.autofocus(
        store=calibrated, model=model, values=lambda: {"{tel}.focuser.temperature": 14.0}
    )
    assert af2.model.predict({"{tel}.focuser.temperature": 14.0}) == pytest.approx(res.focus - 100)
    # The shared model object itself is untouched (zero points are per key).
    assert model.zero_point == TRUE_FOCUS + 250


async def test_a_failed_run_never_moves_the_model(calibrated):
    model = FocusModel(zero_point=TRUE_FOCUS)
    rig = Rig(scene(2.0, seed=19), position=TRUE_FOCUS)
    rig.scene.transparency = 0.0
    af = rig.autofocus(config(fallback=False), store=calibrated, model=model)
    res = await af.run("vcurve")
    assert not res.success and af.model.zero_point == TRUE_FOCUS


def test_missing_model_values_count_as_reference():
    m = FocusModel(terms={"a": 2.0, "b": 3.0}, reference={"a": 1.0, "b": 1.0}, zero_point=100)
    corr, missing = m.correction({"a": 2.0, "b": None})
    assert corr == 2.0 and missing == ["b"]
    assert FocusModel().predict({}) is None


# -- binning, region, registry -----------------------------------------------------------


async def test_binning_and_roi_are_applied_and_restored(calibrated):
    rig = Rig(scene(2.0, seed=20))
    cfg = config(binning=2, roi=(0, 0, 512, 512))
    res = await rig.autofocus(cfg, store=calibrated).run("vcurve", start=TRUE_FOCUS)
    assert res.success and abs(res.focus - TRUE_FOCUS) < tolerance(2.0)
    assert rig.camera.BinX == 1 and rig.camera.NumX == 512


async def test_result_is_published_to_the_registry(calibrated):
    rig = Rig(scene(2.0, seed=21))
    res = await rig.autofocus(config(verify=True), store=calibrated).run("vcurve", start=TRUE_FOCUS)
    assert res.exposures == 3 and res.seeing == pytest.approx(2.0, rel=0.2)
    reg = ValueRegistry()
    publish_result(reg, "t1", res)
    assert reg.get("t1.autofocus.focus").keyword == "AFFOCUS"
    assert reg.get("t1.autofocus.method").value == "vcurve"
    assert reg.get("t1.autofocus.nexp").value == 3


# -- configuration ----------------------------------------------------------------------


def test_config_validation_and_overrides(tmp_path):
    base = {
        "optics": {"f_ratio": 6.5, "pixel_size": 9, "pixel_scale": 0.6, "obstruction": 0.47},
        "exptime": 5,
        "model": {"terms": {"{tel}.focuser.temperature": -52.94}},
        "overrides": {"-/V": {"exptime": 10}, "fau/-": {"optics": {"pixel_scale": 0.12}}},
        "store": "focus_t1.json",
    }
    setup = AutofocusSetup.from_dict(base, tmp_path)
    assert setup.store.path == tmp_path / "focus_t1.json"
    assert setup.config_for(None, "V").exptime == 10
    assert setup.config_for("fau", None).optics.pixel_scale == 0.12
    assert setup.config_for("fau", "V").exptime == 10
    assert setup.config_for(None, None, {"method": "sweep"}).method == "sweep"
    for bad in (
        {"optics": {"f_ratio": 6.5, "pixel_size": 9, "pixel_scale": 0.6}, "method": "guess"},
        {"optics": {"f_ratio": 6.5, "pixel_size": 9, "pixel_scale": 0.6}, "side": 0},
        {"optics": {"f_ratio": 6.5, "pixel_size": 9}},
        {"optics": {"f_ratio": 6.5, "pixel_size": 9, "pixel_scale": 0.6}, "bogus": 1},
        {"exptime": 1},
    ):
        with pytest.raises((ValueError, TypeError)):
            AutofocusSetup.from_dict(bad)


def test_theory():
    o = FocusOptics(f_ratio=8.0, pixel_size=10.0, pixel_scale=1.0, obstruction=0.0)
    assert o.donut_slope == pytest.approx(1 / 80)
    assert o.hfd_slope == pytest.approx(math.sqrt(0.5) / 80)
    secondary = FocusOptics(
        f_ratio=8.0, pixel_size=10.0, pixel_scale=1.0, um_per_step=0.5, focal_plane_factor=-20
    )
    assert secondary.donut_slope == pytest.approx(10 / 80)
