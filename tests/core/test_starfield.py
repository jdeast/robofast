"""The shared synthetic star-image renderer (robofast_core/testing/starfield.py)."""

from __future__ import annotations

import numpy as np
import pytest

from robofast_core.testing.starfield import (
    Detector,
    SimOptics,
    Star,
    blur_diameter,
    random_stars,
    render,
    render_stamp,
)


@pytest.mark.parametrize("blur", [0.0, 0.1, 4.0, 25.0])
@pytest.mark.parametrize("dx,dy", [(0.0, 0.0), (0.3, -0.2), (-0.45, 0.49)])
def test_stamp_unit_flux_and_exact_centroid(blur, dx, dy):
    st = render_stamp(61, dx, dy, fwhm_px=3.0, blur_px=blur, obstruction=0.4)
    assert st.sum() == pytest.approx(1.0)
    assert st.min() >= 0
    y, x = np.indices(st.shape)
    assert (st * x).sum() - 30 == pytest.approx(dx, abs=2e-3)
    assert (st * y).sum() - 30 == pytest.approx(dy, abs=2e-3)


def test_donut_has_a_hole_and_the_right_diameter():
    st = render_stamp(81, 0.0, 0.0, fwhm_px=1.0, blur_px=40.0, obstruction=0.5)
    c = 40
    assert st[c, c] < 0.2 * st.max()  # central obstruction
    row = st[c]
    lit = np.flatnonzero(row > 0.5 * row.max())
    assert lit.max() - lit.min() + 1 == pytest.approx(40, abs=2)


def test_blur_diameter_scales_with_defocus_and_f_ratio():
    o = SimOptics(f_ratio=6.5, pixel_size=9.0)
    assert blur_diameter(1170.0, o) == pytest.approx(20.0)
    assert blur_diameter(-1170.0, o) == pytest.approx(20.0)
    a = SimOptics(arm_asymmetry=0.1)
    assert blur_diameter(100.0, a) / blur_diameter(-100.0, a) == pytest.approx(1.1 / 0.9)
    assert blur_diameter(10.0, SimOptics(blur_offset=-20.0)) == 0.0


def test_render_flux_roi_binning_offset():
    d = Detector(shape=(200, 300), sky=0.0, read_noise=0.0, bias=0.0, gain=1.0)
    stars = [Star(150.2, 90.7, 1e5)]
    f = render(stars, SimOptics(), d, seeing=2.0, defocus=500.0, noise=False)
    assert f.image.shape == (200, 300)
    assert f.image.sum() == pytest.approx(1e5, rel=1e-6)
    _, x = np.indices(f.image.shape)
    assert (f.image * x).sum() / f.image.sum() == pytest.approx(150.2, abs=0.01)
    # ROI + 2x2 binning + an offset.
    g = render(
        stars,
        SimOptics(),
        d,
        seeing=2.0,
        roi=(100, 40, 100, 100),
        binning=2,
        offset=(3.0, -2.0),
        noise=False,
    )
    assert g.image.shape == (50, 50)
    yy, xx = np.indices(g.image.shape)
    cx = (g.image * xx).sum() / g.image.sum()
    cy = (g.image * yy).sum() / g.image.sum()
    assert g.to_full(cx, cy) == pytest.approx((153.2, 88.7), abs=0.02)


def test_noise_saturation_and_reproducibility():
    d = Detector(shape=(64, 64), saturation=5000.0)
    stars = [Star(32, 32, 1e7)]
    a = render(stars, SimOptics(), d, seeing=1.0, rng=np.random.default_rng(3))
    b = render(stars, SimOptics(), d, seeing=1.0, rng=np.random.default_rng(3))
    assert a.image.dtype == np.int32
    assert np.array_equal(a.image, b.image)
    assert a.image.max() == 5000
    sky = np.median(a.image)
    assert sky == pytest.approx(d.bias + d.sky / d.gain, abs=3)


def test_jitter_moves_the_field():
    d = Detector(shape=(100, 100))
    f = render([Star(50, 50, 1e5)], SimOptics(), d, jitter=1.0, rng=np.random.default_rng(1))
    assert f.truth["shift"] != (0.0, 0.0)


def test_random_stars_respect_margin_and_separation():
    stars = random_stars(20, (300, 400), margin=30, min_separation=25, rng=np.random.default_rng(0))
    assert len(stars) == 20
    for s in stars:
        assert 30 <= s.x <= 369 and 30 <= s.y <= 269
    for i, s in enumerate(stars):
        for t in stars[i + 1 :]:
            assert np.hypot(s.x - t.x, s.y - t.y) >= 25
