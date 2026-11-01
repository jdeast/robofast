"""Render the real sky, solve it: the near-blind solver recovers the true
pointing, position angle and scale (MINERVA imager and FAU fields).

The scenes use the test sky (tests/core/skydata.py: real Gaia DR3 tiles, or
the synthetic stand-in until they are pinned), a mount
with a TPOINT-style pointing error (so the commanded position the solver
starts from is arcminutes off the truth), noise, sky and seeing.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from robofast_core import astrometry as ast
from robofast_core.catalog import catalog_at, separation
from robofast_core.photometry import PortPhotometry, SkyConditions
from robofast_core.pointing import PointingModel
from robofast_core.testing.skysim import SkyCamera, SkyScene
from robofast_core.testing.starfield import Detector
from tests.core.skydata import sky_dir

SPARSE = (212.720875, 62.522197)
HD185144 = (293.2035, 69.6611)
DENSE = (285.79486, 25.292175)
WHEN = datetime(2026, 4, 23, 9, 0, tzinfo=UTC)
#: A mount a few arcminutes off (TPOINT terms, arcsec).
ERRORS = PointingModel({"IH": 95.0, "ID": -60.0, "CH": 25.0, "NP": 10.0, "MA": 30.0, "ME": -20.0})
IMAGER_PHOT = PortPhotometry(efficiency={"optics": 0.6, "qe": 0.8}, gain=1.3, read_noise=6.0)
#: The FAU: the same telescope behind a 2 % beamsplitter, a ZWO ASI174.
FAU_PHOT = PortPhotometry(
    efficiency={"optics": 0.6, "qe": 0.7, "beamsplitter": 0.02}, gain=1.3, read_noise=4.0
)


@pytest.fixture(scope="module")
def catalog():
    return catalog_at(sky_dir())


def imager(pa: float = 0.0, flip: bool = False, k1: float = 0.0) -> SkyCamera:
    det = Detector(shape=(2048, 2048), gain=1.3, read_noise=6.0, bias=500.0)
    return SkyCamera((2048, 2048), 0.61, pa=pa, flip=flip, k1=k1, detector=det)


def fau(pa: float = 190.8, flip: bool = False) -> SkyCamera:
    det = Detector(shape=(1216, 1936), gain=1.3, read_noise=4.0, bias=200.0, saturation=65535)
    return SkyCamera((1216, 1936), 0.265, pa=pa, flip=flip, detector=det, pixel_size=5.86)


def scene(catalog, camera: SkyCamera, phot: PortPhotometry, **kw) -> SkyScene:
    kw.setdefault("pointing", ERRORS)
    kw.setdefault("conditions", SkyConditions())
    return SkyScene(catalog, camera, phot, seeing=1.6, **kw)


def centre_truth(frame, cam: SkyCamera) -> tuple[float, float]:
    ny, nx = cam.shape
    ra, dec = cam.unproject(frame.truth["ra"], frame.truth["dec"], [(nx - 1) / 2], [(ny - 1) / 2])
    return float(ra[0]), float(dec[0])


def check(sol: ast.Solution, frame, cam: SkyCamera, *, pos: float, pa: float = 0.02) -> float:
    tra, tdec = centre_truth(frame, cam)
    err = float(separation(sol.ra, sol.dec, tra, tdec)) * 3600
    assert err < pos, sol.summary()
    assert abs((sol.pa - cam.pa + 180) % 360 - 180) < pa, sol.summary()
    assert sol.scale == pytest.approx(cam.plate_scale, rel=2e-3)
    assert sol.flip == cam.flip
    return err


@pytest.mark.parametrize(
    "field,pa,exptime",
    [(SPARSE, 0.0, 30.0), (HD185144, 37.5, 10.0)],
    ids=["sparse", "hd185144"],
)
def test_imager_round_trip(catalog, field, pa, exptime):
    cam = imager(pa=pa)
    sc = scene(catalog, cam, IMAGER_PHOT)
    frame = sc.render(*field, WHEN, exptime)
    # The mount was really off: the solver starts arcminutes from the truth.
    offset = separation(*field, frame.truth["ra"], frame.truth["dec"]) * 3600
    assert offset > 60
    t0 = time.monotonic()
    sol = ast.solve_near(frame.image, catalog, *field, ast.SolverConfig(0.61, search_radius=0.1),
                         epoch=WHEN)  # fmt: skip
    seconds = time.monotonic() - t0
    check(sol, frame, cam, pos=0.5)
    assert sol.n_matched >= 10 and sol.rms < 0.5
    assert seconds < 15.0  # typically ~1 s; generous for a loaded CI runner


@pytest.mark.parametrize(
    "field,exptime", [(DENSE, 5.0), (HD185144, 15.0), (SPARSE, 30.0)], ids=["dense", "hd", "sparse"]
)
def test_fau_round_trip(catalog, field, exptime):
    cam = fau()
    sc = scene(catalog, cam, FAU_PHOT)
    frame = sc.render(*field, WHEN, exptime)
    cfg = ast.SolverConfig(0.265, search_radius=0.05, min_matches=3)
    sol = ast.solve_near(frame.image, catalog, *field, cfg, epoch=WHEN)
    check(sol, frame, cam, pos=0.5, pa=0.1)
    assert sol.n_matched >= 3


def test_unknown_parity_and_angle(catalog):
    cam = imager(pa=123.4, flip=True)
    frame = scene(catalog, cam, IMAGER_PHOT).render(*HD185144, WHEN, 10.0)
    sol = ast.solve_near(frame.image, catalog, *HD185144, ast.SolverConfig(0.61, search_radius=0.1))
    check(sol, frame, cam, pos=0.5)


def test_scale_hint_respected(catalog):
    # Binned 2x2: the solver is told, and the scale comes out doubled.
    cam = imager(pa=10.0)
    frame = scene(catalog, cam, IMAGER_PHOT).render(*HD185144, WHEN, 10.0, binning=2)
    cfg = ast.SolverConfig(0.61, search_radius=0.1)
    sol = ast.solve_near(frame.image, catalog, *HD185144, cfg, binning=2)
    assert sol.scale == pytest.approx(1.22, rel=2e-3)
    assert abs(sol.pa - 10.0) < 0.05


def test_distortion_needs_sip(catalog):
    cam = imager(pa=0.0, k1=0.01)  # ~14 px at the corners
    frame = scene(catalog, cam, IMAGER_PHOT, pointing=PointingModel()).render(*HD185144, WHEN, 10.0)
    plain = ast.solve_near(
        frame.image, catalog, *HD185144, ast.SolverConfig(0.61, search_radius=0.05)
    )
    sip = ast.solve_near(
        frame.image, catalog, *HD185144, ast.SolverConfig(0.61, search_radius=0.05, sip_order=3)
    )
    assert sip.wcs.sip is not None
    assert sip.rms < 0.4 < plain.rms
    # A star near a corner lands where the catalog says.
    x, y = sip.pixel(sip.matches["ra"], sip.matches["dec"])
    assert np.median(np.hypot(x - sip.matches["x"], y - sip.matches["y"])) < 0.5


def test_wrong_scale_fails_clearly(catalog):
    frame = scene(catalog, imager(), IMAGER_PHOT).render(*HD185144, WHEN, 10.0)
    with pytest.raises(ast.SolveError):
        ast.solve_near(frame.image, catalog, *HD185144, ast.SolverConfig(0.80, search_radius=0.1))


def test_nothing_to_solve(catalog):
    blank = np.random.default_rng(0).normal(500, 5, (512, 512))
    with pytest.raises(ast.SolveError, match="no stars detected"):
        ast.solve_near(blank, catalog, *HD185144, ast.SolverConfig(0.61))


def test_few_star_fau_fields(catalog):
    """A FAU field with only the two brightest stars: refused by default
    (two stars can be a chance pair), accepted as a degenerate solution
    with min_matches=2 and a known parity, and still right."""
    cam = fau(pa=190.8)
    sc = scene(catalog, cam, FAU_PHOT, pointing=PointingModel(), mag_limit=None)
    stars = sc.field_stars(*SPARSE, WHEN).sorted()
    # The brightest star and the brightest other star within 100 arcsec.
    d = separation(stars.ra[0], stars.dec[0], stars.ra, stars.dec) * 3600
    k = int(np.nonzero((d > 5) & (d < 100))[0][0])
    two = stars.take([0, k])
    sc2 = replace(sc, catalog=two)
    sc2.rng = np.random.default_rng(1)
    frame = sc2.render(*SPARSE, WHEN, 30.0)
    with pytest.raises(ast.SolveError):
        ast.solve_near(frame.image, catalog, *SPARSE, ast.SolverConfig(0.265, search_radius=0.02))
    cfg = ast.SolverConfig(0.265, search_radius=0.02, min_matches=2, flip=False, max_stars=2)
    sol = ast.solve_near(frame.image, two, *SPARSE, cfg)
    assert sol.degenerate and sol.n_matched == 2
    check(sol, frame, cam, pos=1.0, pa=0.5)


def test_zero_point_recovered(catalog):
    cam = imager(pa=0.0)
    phot = replace(IMAGER_PHOT, extinction=0.0)
    sc = scene(catalog, cam, phot, pointing=PointingModel())
    frame = sc.render(*HD185144, WHEN, 10.0)
    cfg = ast.SolverConfig(0.61, search_radius=0.1)
    sol = ast.solve_near(frame.image - 500.0, catalog, *HD185144, cfg)
    zp = ast.zero_point(frame.image - 500.0, sol, exptime=10.0, gain=1.3, saturation=60000)
    assert zp is not None and zp["n"] >= 5
    # Aperture losses (3 arcsec radius, 1.6 arcsec seeing) are a few per cent.
    assert zp["zp"] == pytest.approx(phot.zp, abs=0.1)
    assert zp["scatter"] < 0.1


def test_wcs_cards_round_trip(catalog):
    from astropy.io import fits
    from astropy.wcs import WCS

    frame = scene(catalog, imager(pa=20.0), IMAGER_PHOT).render(*HD185144, WHEN, 10.0)
    sol = ast.solve_near(frame.image, catalog, *HD185144, ast.SolverConfig(0.61, search_radius=0.1))
    h = fits.Header()
    for k, v, c in ast.wcs_cards(sol):
        h[k] = (v, c)
    assert h["WCSSRC"] == "robofast"
    w = WCS(h)
    ra, dec = w.all_pix2world([1023.5], [1023.5], 0)
    assert separation(ra[0], dec[0], sol.ra, sol.dec) * 3600 < 0.01


@pytest.mark.skipif(
    __import__("shutil").which("solve-field") is None, reason="astrometry.net not installed"
)
def test_solve_field_if_installed(catalog):  # pragma: no cover - needs astrometry.net
    frame = scene(catalog, imager(), IMAGER_PHOT).render(*HD185144, WHEN, 10.0)
    sol = ast.solve_field(frame.image, plate_scale=0.61, ra=HD185144[0], dec=HD185144[1], radius=1)
    assert math.isfinite(sol.ra)


def test_solve_field_missing_is_clear(monkeypatch):
    monkeypatch.setattr(ast.shutil, "which", lambda name: None)
    with pytest.raises(ast.SolveError, match="not installed"):
        ast.solve_field(np.zeros((10, 10)), plate_scale=1.0)
