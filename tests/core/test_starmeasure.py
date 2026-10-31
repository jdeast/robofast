"""Star measurement (robofast_core.starmeasure) and the focus fits
(robofast_core.focusfit), on synthetic images and curves."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from robofast_core.focusfit import (
    FitError,
    fit_hyperbola,
    fit_line,
    fit_parabola,
    two_point,
)
from robofast_core.starmeasure import MeasureConfig, background, measure
from robofast_core.testing.starfield import Detector, SimOptics, Star, random_stars, render

OPTICS = SimOptics(pixel_scale=1.0, pixel_size=13.5, f_ratio=6.5, obstruction=0.47)
K = math.sqrt((1 + 0.47**2) / 2)  # HFD of the annulus / its diameter


def field(seeing, defocus, *, n=6, seed=0, shape=(512, 512), profile="moffat", **kw):
    det = Detector(shape=shape)
    rng = np.random.default_rng(seed)
    stars = kw.pop("stars", None) or random_stars(
        n, shape, flux=(1.5e5, 6e5), margin=60, min_separation=110, rng=rng
    )
    return render(
        stars, OPTICS, det, seeing=seeing, defocus=defocus, profile=profile, rng=rng, **kw
    )


@pytest.mark.parametrize("seeing", [2.0, 3.0, 4.0])
def test_in_focus_fwhm_and_hfd(seeing):
    f = field(seeing, 0.0, profile="gaussian")
    m = measure(f.image)
    assert m.n == 6
    # Gaussian: adaptive moments are exact; HFD = FWHM for a Gaussian (the
    # pixels themselves add a little to a 2-px star's growth curve).
    assert m.fwhm == pytest.approx(seeing, rel=0.08)
    assert m.hfd == pytest.approx(seeing, rel=0.15)


@pytest.mark.parametrize("seeing", [1.0, 4.0])
@pytest.mark.parametrize("blur", [24.0, 40.0])
def test_defocused_hfd_is_geometric_and_donut_edge_is_exact(seeing, blur):
    """Far from focus (blur >= 6 FWHM) the HFD is k x the blur diameter and
    the donut's outer edge is the blur diameter, whatever the seeing."""
    dz = blur * OPTICS.f_ratio * OPTICS.pixel_size
    f = field(seeing, dz)
    m = measure(f.image, MeasureConfig(smooth=max(1.0, K * blur / 4)))
    assert m.n >= 5
    assert m.hfd == pytest.approx(K * blur, rel=0.025)
    assert m.donut == pytest.approx(blur, rel=0.02)
    assert m.hfd_err < 0.02 * m.hfd


def test_rejections():
    det = Detector(shape=(300, 300), saturation=20000)
    stars = [
        Star(150, 150, 5e4),  # fine
        Star(60, 60, 5e7),  # saturated
        Star(3, 200, 2e5),  # off the edge
        Star(240, 230, 5e4),  # blended with the next
        Star(246, 236, 4e4),
    ]
    f = render(stars, OPTICS, det, seeing=2.0, rng=np.random.default_rng(1))
    m = measure(f.image, MeasureConfig(saturation=19000))
    assert m.n == 1 and m.stars[0].x == pytest.approx(150, abs=0.1)
    assert m.rejected.get("saturated") == 1
    assert m.rejected.get("edge") == 1
    # Two stars 8.5 px apart are one elongated (or blended) detection.
    assert m.rejected.get("elongated", 0) + m.rejected.get("blended", 0) >= 1


def test_region_and_faint_and_empty():
    f = field(2.0, 0.0)
    xs = sorted(s.x for s in measure(f.image).stars)
    half = measure(f.image, MeasureConfig(region=(0, 0, 256, 512)))
    assert 0 < half.n < 6 and all(s.x < 256 for s in half.stars)
    assert half.rejected.get("edge", 0) >= 6 - half.n - 1
    assert len(xs) == 6
    dim = render(
        [Star(100, 100, 300)], OPTICS, Detector(shape=(200, 200)), rng=np.random.default_rng(2)
    )
    assert measure(dim.image).n == 0
    flat = np.full((128, 128), 1000.0)
    m = measure(flat)
    assert m.n == 0 and math.isnan(m.hfd)


def test_background_follows_a_gradient_and_ignores_donuts():
    f = field(2.0, 3000.0, shape=(512, 512))
    _, xx = np.indices((512, 512))
    img = f.image + 0.2 * xx  # a 100 ADU gradient across the frame
    bkg, _ = background(img, 128)
    sky = f.truth["sky_adu"]
    assert np.median(bkg - (sky + 0.2 * xx)) == pytest.approx(0.0, abs=1.5)
    m = measure(img, MeasureConfig(smooth=8))
    assert m.n >= 3
    assert m.donut == pytest.approx(f.truth["blur_px"], rel=0.02)


# -- the fits -----------------------------------------------------------------------


def hyperbola(x, x0, a, mp, mm):
    m = np.where(x > x0, mp, mm)
    return np.sqrt(a**2 + (m * (x - x0)) ** 2)


def test_hyperbola_fit_with_asymmetric_arms_and_an_outlier():
    rng = np.random.default_rng(0)
    x = np.linspace(-3000, 3000, 13) + 20250
    h = hyperbola(x, 20200.0, 3.0, 0.013, 0.011) * (1 + 0.01 * rng.normal(size=x.size))
    e = 0.01 * h
    h[3] *= 1.5  # a cloud, a satellite...
    fit = fit_hyperbola(x, h, e)
    assert fit.x0 == pytest.approx(20200, abs=30)
    assert fit.m_plus == pytest.approx(0.013, rel=0.05)
    assert fit.m_minus == pytest.approx(0.011, rel=0.05)
    assert fit.a == pytest.approx(3.0, rel=0.2)
    assert 3 not in fit.used and 0 < fit.x0_err < 50
    assert float(fit(np.array([fit.x0]))[0]) == pytest.approx(fit.a)


def test_hyperbola_and_parabola_refuse_unbracketed_minima():
    x = np.linspace(21000, 24000, 7)
    h = hyperbola(x, 20000.0, 3.0, 0.013, 0.013)
    with pytest.raises(FitError):
        fit_hyperbola(x, h)
    with pytest.raises(FitError):
        fit_parabola(x, h)
    with pytest.raises(FitError):
        fit_hyperbola(x[:3], h[:3])
    with pytest.raises(FitError, match="upside down"):
        fit_parabola(x, 50 - ((x - 22500) / 1000) ** 2)


def test_parabola_keeps_its_weights():
    """The legacy fit dropped the weights on refitting; a badly measured
    point (large error) must not drag the vertex."""
    x = np.linspace(-1000, 1000, 9)
    h = 2 + (x / 500) ** 2
    e = np.full_like(h, 0.02)
    h[0] += 1.2
    e[0] = 5.0
    fit = fit_parabola(x, h, e)
    assert fit.x0 == pytest.approx(0, abs=5)
    assert fit.x0_err < 20


def test_line_and_two_point():
    line = fit_line([1000, 2000, 3000], [9.0, 18.0, 27.0])
    assert line.slope == pytest.approx(0.009) and line.zero == pytest.approx(0, abs=1e-6)
    m = 0.01
    hp = lambda u: math.hypot(3.0, m * u)  # noqa: E731
    tp = two_point(23000, hp(3000), 0.1, 22000, hp(2000), 0.1, m, +1)
    assert tp.zero == pytest.approx(20000, abs=1e-6) and tp.a2 == pytest.approx(9.0)
    assert tp.slope_ratio == pytest.approx((hp(3000) - hp(2000)) / 1000 / m)
    # On the other side the secant is reversed.
    tm = two_point(23000, m * 1000, 0.1, 22000, m * 2000, 0.1, m, +1)
    assert tm.slope_ratio == pytest.approx(-1)
    with pytest.raises(FitError):
        two_point(1, 1, 1, 1, 1, 1, m, 1)


# -- real data: MINERVA T2's FAU, 2026-04-23 ------------------------------------------

REAL = Path(__file__).parent / "data" / "minerva_t2_fau_20260423.json"


def _sweep(name):
    seq = json.loads(REAL.read_text())["sequences"][name]
    p = seq["points"]
    x = np.array([q["position"] for q in p])
    h = np.array([q["hfd_px"] for q in p])
    e = np.array([q["hfd_err_px"] for q in p])
    return seq, x, h, e


@pytest.mark.parametrize("name,tol", [("0003", 60), ("0531", 40), ("0602", 120)])
def test_real_sweeps_agree_with_the_legacy_focus(name, tol):
    """Our HFDs of the legacy sweeps, fitted with the hyperbola, land on the
    legacy result (well inside the ~+-110 um depth of focus at f/6.5)."""
    seq, x, h, e = _sweep(name)
    fit = fit_hyperbola(x, h, e)
    assert abs(fit.x0 - seq["legacy_new"]) < tol
    # Seeing from the fit: 2-2.4" FWHM that night.
    assert 1.5 < fit.a / 1.2 * 0.265 < 2.8


def test_real_two_point_vcurve_calibrated_on_the_other_sweep():
    """Calibrate on one sweep, compute from two same-side points of the
    other: the linear V-curve lands within the depth of focus even though
    these points are only ~2x the seeing out (the method wants 6x)."""
    errors = []
    for cal_name, run_name in (("0531", "0003"), ("0003", "0531")):
        _, cx, ch, ce = _sweep(cal_name)
        cfit = fit_hyperbola(cx, ch, ce)
        _, x, h, e = _sweep(run_name)
        run = fit_hyperbola(x, h, e)
        for s in (1, -1):
            sel = (cx - cfit.x0) * s >= 500
            line = fit_line(cx[sel], ch[sel], ce[sel])
            m, off = abs(line.slope), line.zero - cfit.x0
            pts = np.flatnonzero((x - run.x0) * s >= 500)
            far_near = pts[np.argsort(-np.abs(x[pts] - run.x0))][:2]
            focus = np.mean([x[i] - s * h[i] / m for i in far_near]) - off
            errors.append(focus - run.x0)
    assert max(abs(v) for v in errors) < 110, errors
