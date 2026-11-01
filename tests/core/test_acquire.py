"""The acquisition cascade (robofast_core/guider/acquire.py, docs/acquisition.md)
on the sky simulator with a realistic FAU: ~2 % of the light, short
exposures, so only the bright stars show -- nu Draconis (an equal pair, 62
arcsec), a closer unequal pair, a triple, a single star; a correct PA
calibration, and a wrong one (180 deg off, or the parity flipped) that must
be caught, not followed."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from robofast_core.catalog import Stars, catalog_at, separation
from robofast_core.guider.acquire import AcqConfig, Acquirer, neighbours_from_catalog
from robofast_core.guider.base import ROI, Frame
from robofast_core.photometry import PortPhotometry, SkyConditions
from robofast_core.testing.skysim import SkyCamera, SkyScene
from robofast_core.testing.starfield import Detector

DATA = Path(__file__).parent / "data" / "catalog"
WHEN = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
FIBER = (948.0, 626.0)
SCALE = 0.265
#: The FAU behind a 2 % beamsplitter (its zero point ~4.25 mag above the imager's).
FAU_PHOT = PortPhotometry(zero_point=20.0, gain=1.5, read_noise=4.0, saturation=65535)


def fau(catalog, pa=30.0, flip=False):
    cam = SkyCamera(
        (1216, 1936), SCALE, pa=pa, flip=flip, crpix=FIBER, detector=Detector(shape=(1216, 1936))
    )
    return SkyScene(catalog, cam, FAU_PHOT, conditions=SkyConditions(), seeing=1.5, seed=3)


def frame_of(sky, ra, dec, exposure, *, miss=(2.0, -1.5)):
    """The FAU frame with the telescope pointed at (ra, dec) give or take
    ``miss`` arcsec (east, north), and the true pixel of every catalog star."""
    cra = ra + miss[0] / 3600.0 / math.cos(math.radians(dec))
    f = sky.render(cra, dec + miss[1] / 3600.0, WHEN, exposure)
    fr = Frame(f.image.astype(float), ROI(0, 0, 1936, 1216), exposure, 0.0, exposure)
    return fr, f.truth


def acquirer(stars, ra, dec, *, pa=30.0, flip=False, sigma=None, mode="AUTO", **kw):
    d = separation(ra, dec, stars.ra, stars.dec) * 3600
    k = int(np.argmin(d))
    others = stars.take(np.arange(len(stars)) != k)
    return Acquirer(
        FIBER,
        target_g=float(stars.g[k]),
        neighbours=neighbours_from_catalog(others, ra, dec, None),
        scale=SCALE,
        pa=pa,
        flip=flip,
        pointing_sigma=sigma,
        mode=mode,
        **kw,
    )


def true_pixel(truth, ra, dec):
    s = truth["stars"]
    k = int(np.argmin(separation(ra, dec, s.ra, s.dec)))
    return float(truth["x"][k]), float(truth["y"][k])


@pytest.fixture(scope="module")
def nu():
    cat = catalog_at(DATA)
    s = cat.cone(263.056, 55.179, 0.02, mag_limit=6.0).at_epoch(2026.5)
    assert len(s) == 2
    return cat, s  # nu1 (brighter), nu2


def make_stars(spec):
    """Stars from (east, north arcsec, G) offsets around (150, 30)."""
    ra0, dec0 = 150.0, 30.0
    ra = [ra0 + e / 3600 / math.cos(math.radians(dec0)) for e, _, _ in spec]
    dec = [dec0 + n / 3600 for _, n, _ in spec]
    n = len(spec)
    return Stars(
        np.array(ra), np.array(dec), np.zeros(n), np.zeros(n),
        np.array([g for *_, g in spec], float), np.full(n, 0.8), -np.arange(1, n + 1),
        epoch=2026.5,
    )  # fmt: skip


# -- nu Draconis: an equal pair ---------------------------------------------------------------


def test_nu_dra_pair_branch_takes_the_target_not_its_twin(nu):
    cat, s = nu
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])  # nu2, the fainter: brightest-star is wrong
    sky = fau(cat)
    fr, truth = frame_of(sky, ra2, dec2, 0.02)
    acq = acquirer(s, ra2, dec2)
    px = acq(fr)
    assert acq.last["stars"] == 2 and acq.last["method"] == "pair", acq.last
    assert np.hypot(*(np.array(px) - true_pixel(truth, ra2, dec2))) < 1.0
    # ... and the brightest star would have been nu1, the wrong one.
    b = Acquirer(FIBER, mode="BRIGHTEST")
    assert np.hypot(*(np.array(b(fr)) - true_pixel(truth, *s.ra[:1], *s.dec[:1]))) < 1.0


@pytest.mark.parametrize(
    "bad,why",
    [({"pa": 210.0}, "pointing put it"), ({"flip": True}, "do not match")],
    ids=["pa-180-off", "parity-flipped"],
)
def test_nu_dra_wrong_calibration_is_caught(nu, bad, why):
    cat, s = nu
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])
    fr, _ = frame_of(fau(cat), ra2, dec2, 0.02)
    acq = acquirer(s, ra2, dec2, sigma=3.0, **bad)
    assert acq(fr) is None
    assert why in acq.last["reason"] and "PA calibration" in acq.last["reason"]


def test_without_a_pa_calibration_the_pair_branch_refuses(nu):
    cat, s = nu
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])
    fr, _ = frame_of(fau(cat), ra2, dec2, 0.02)
    acq = acquirer(s, ra2, dec2)
    acq.pa = None
    assert acq(fr) is None and "no PA calibration" in acq.last["reason"]


# -- a closer, unequal pair; a triple; a single star ----------------------------------------------


def test_unequal_pair_and_its_rotated_calibration():
    # The target is the FAINTER star (G 8.0), its companion G 6.8, 14 arcsec away.
    stars = make_stars([(0, 0, 8.0), (9.0, 10.5, 6.8)])
    sky = fau(stars, pa=100.0, flip=True)
    fr, truth = frame_of(sky, float(stars.ra[0]), float(stars.dec[0]), 0.2)
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), pa=100.0, flip=True)
    px = acq(fr)
    assert acq.last["method"] == "pair"
    assert np.hypot(*(np.array(px) - true_pixel(truth, stars.ra[0], stars.dec[0]))) < 1.0
    # 180 deg off and no pointing uncertainty to lean on: the brightness order
    # contradicts the swapped match, so it is refused, not followed.
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), pa=280.0, flip=True)
    assert acq(fr) is None and "do not match" in acq.last["reason"]


def test_triple_matches_the_whole_pattern():
    stars = make_stars([(0, 0, 7.5), (20, 5, 7.3), (-15, 25, 7.6), (40, -30, 8.0)])
    sky = fau(stars)
    fr, truth = frame_of(sky, float(stars.ra[0]), float(stars.dec[0]), 0.2)
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), sigma=3.0)
    px = acq(fr)
    assert acq.last["method"] == "triangulate" and acq.last["stars"] == 4
    assert np.hypot(*(np.array(px) - true_pixel(truth, stars.ra[0], stars.dec[0]))) < 1.0


def test_single_star_and_a_companion_outside_the_field():
    # A G 6 companion 700 arcsec away: outside the FAU; only the target shows.
    stars = make_stars([(0, 0, 7.0), (700, 0, 6.0)])
    fr, truth = frame_of(fau(stars), float(stars.ra[0]), float(stars.dec[0]), 0.2)
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), sigma=3.0)
    px = acq(fr)
    assert acq.last["method"] == "single" and "flag" not in acq.last
    assert np.hypot(*(np.array(px) - true_pixel(truth, stars.ra[0], stars.dec[0]))) < 1.0
    # Far from where the pointing put it: taken, but flagged.
    fr, _ = frame_of(fau(stars), float(stars.ra[0]), float(stars.dec[0]), 0.2, miss=(40, 30))
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), sigma=3.0)
    assert acq(fr) is not None and "may be missing" in acq.last["flag"]


def test_solve_branch_and_its_fallback_to_the_pattern():
    spec = [
        (0, 0, 7.5),
        (20, 5, 7.3),
        (-15, 25, 7.6),
        (40, -30, 8.0),
        (-50, -20, 7.9),
        (60, 40, 8.2),
    ]
    stars = make_stars(spec)
    fr, truth = frame_of(fau(stars), float(stars.ra[0]), float(stars.dec[0]), 0.2)
    tp = true_pixel(truth, stars.ra[0], stars.dec[0])
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), solve=lambda f: tp)
    assert acq(fr) is not None and acq.last["method"] == "solve" and acq.last["stars"] >= 5
    acq = acquirer(stars, float(stars.ra[0]), float(stars.dec[0]), solve=lambda f: None)
    px = acq(fr)
    assert acq.last["method"] == "triangulate" and "solve failed" in acq.last["reason"]
    assert np.hypot(*(np.array(px) - tp)) < 1.0


# -- explicit modes, fallback, the trace ----------------------------------------------------------


def test_explicit_modes_fail_without_their_preconditions_unless_fallback(nu):
    cat, s = nu
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])
    fr, truth = frame_of(fau(cat), ra2, dec2, 0.02)
    solve = acquirer(s, ra2, dec2, mode="SOLVE")
    assert solve(fr) is None and "no plate solver" in solve.last["reason"]
    fb = acquirer(s, ra2, dec2, mode="SOLVE", fallback=True)
    px = fb(fr)
    assert fb.last["method"] == "pair" and "fallback" in fb.last["reason"]
    assert np.hypot(*(np.array(px) - true_pixel(truth, ra2, dec2))) < 1.0
    # COORDINATES: the star nearest the pointing; the twin is 234 px away.
    co = acquirer(s, ra2, dec2, mode="WCS" if False else "COORDINATES", sigma=3.0)
    assert co(fr) is not None and co.last["method"] == "coordinates"
    loose = acquirer(s, ra2, dec2, mode="COORDINATES", sigma=60.0)
    assert loose(fr) is None and "ambiguous" in loose.last["reason"]
    assert Acquirer(FIBER, mode="WCS").mode == "SOLVE"
    assert Acquirer(FIBER, mode="OFF").mode == "COORDINATES"
    with pytest.raises(ValueError, match="mode"):
        Acquirer(FIBER, mode="GUESS")


def test_trace_records_every_frame(nu):
    cat, s = nu
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])
    acq = acquirer(s, ra2, dec2)
    empty = Frame(np.full((1216, 1936), 100.0), ROI(0, 0, 1936, 1216), 0.02, 0.0, 0.02)
    assert acq(empty) is None
    fr, _ = frame_of(fau(cat), ra2, dec2, 0.02)
    assert acq(fr) is not None
    summ = acq.summary()
    assert [t["method"] for t in summ["trace"]] == ["none", "pair"]
    assert summ["method"] == "pair" and summ["stars"] == 2 and summ["identified"]
    assert AcqConfig.from_dict({"solve_min": 6}).solve_min == 6


@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize("pa", [0.0, 37.0, 101.3, 250.0])
def test_fits_and_guider_conventions_agree(pa, flip):
    """The PA calibration's (FITS, plate-solve) PA and parity give the same
    pixel->sky map as the sky simulator's camera."""
    from robofast_core.guider.calibration import fits_sky_matrix

    cam = SkyCamera(plate_scale=0.5, pa=pa, flip=flip)
    assert np.allclose(cam.cd() * 3600.0, fits_sky_matrix(0.5, pa, flip), atol=1e-9)


# -- in the guide loop, the request, vetting, the sequencer's record ------------------------------


def _loop(nu, *, pa_cal=30.0, flip=False, sigma=None):
    """The guider simulator with nu Dra's real field, a short (0.02 s) FAU
    acquisition frame, and the cascade for nu2."""
    from robofast_core.guider.base import VirtualClock
    from robofast_core.guider.calibration import Calibration
    from robofast_core.guider.loop import GuideConfig, Guider
    from robofast_core.guider.sim import SimCamera, SimMount, SimWorld
    from robofast_core.guider.tiptilt import MountOffset
    from robofast_core.testing.skysim import sky_world_stars

    cat, s = nu
    cam = SkyCamera((1216, 1936), SCALE, pa=30.0, detector=Detector(shape=(1216, 1936)))
    sky = SkyScene(cat, cam, FAU_PHOT, conditions=SkyConditions(), seeing=1.5)
    ra2, dec2 = float(s.ra[1]), float(s.dec[1])
    main, others = sky_world_stars(sky, ra2, dec2, WHEN)
    clock = VirtualClock()
    world = SimWorld(
        clock=clock, sensor=(1936, 1216), star=main[:2], flux=main[2], others=others,
        fwhm=1.5 / SCALE, sky=5.0, gain=1.5, bias=1000.0, read_noise=4.0,
        plate_scale=SCALE, pa=30.0, parity=1,
    )  # fmt: skip
    sm = SimMount(clock=clock, tau=0.2, delay=0.05)
    world.mount = sm
    m = MountOffset(sm, max_offset=120.0)
    m.clock = clock
    cfg = GuideConfig(
        target=FIBER, acq_exposure=0.02, exposure=0.1, roi_size=64, capture_radius=3.0,
        coarse_settle=1.0, max_star_distance=600.0, scope="t2", max_reacquire=0,
    )  # fmt: skip
    g = Guider(SimCamera(world), cfg, mount=m, mount_cal=Calibration("mount", "fau", world.mount_matrix.tolist()), clock=clock)  # noqa: E501
    g.acquirer = acquirer(s, ra2, dec2, pa=pa_cal, flip=flip, sigma=sigma)
    return g, world, clock


async def test_guide_loop_puts_the_right_twin_on_the_fiber(nu):
    g, world, clock = _loop(nu)
    g.start()
    await g.wait_state(("guiding", "failed"), 120)
    assert g.state == "guiding", g.reason
    d2 = float(np.hypot(*(world.star_at(clock.now()) - np.asarray(FIBER))))
    assert d2 < 5.0  # nu2, the fainter twin, is on the fiber
    st = g.status()["acquisition"]
    assert st["method"] == "pair" and st["stars"] == 2 and st["identified"]
    await g.stop()


async def test_guide_loop_with_a_wrong_pa_says_target_not_identified(nu):
    g, _, _ = _loop(nu, flip=True)
    g.start()
    await g.wait_state(("failed",), 120)
    assert "target not identified" in g.reason and "PA calibration" in g.reason
    assert not g.status()["acquisition"]["identified"]


def test_request_acquisition_config_round_trips():
    from robofast_core.request_model import Configuration

    from .test_dispatch import group

    g = group("acq", ics=[{"exposure_time": 30.0, "exposure_count": 1}])
    c = g["requests"][0]["configurations"][0]
    c["acquisition_config"] = {
        "mode": "WCS",
        "extra_params": {"fallback": True, "companions": [{"ra": 1.0, "dec": 2.0, "g": 7.0}]},
    }
    conf = Configuration.from_dict(c, "c")
    assert conf.acquisition == {
        "mode": "SOLVE",
        "fallback": True,
        "companions": [{"ra": 1.0, "dec": 2.0, "g": 7.0}],
    }
    assert Configuration.from_dict(conf.to_dict(), "c").acquisition == conf.acquisition
    c["acquisition_config"] = {"mode": "GUESS"}
    with pytest.raises(ValueError, match="mode"):
        Configuration.from_dict(c, "c")
    c["acquisition_config"] = {}
    assert Configuration.from_dict(c, "c").acquisition == {}


def test_vetting_predicts_the_acquisition_branch(nu):
    from robofast_core.vetting import PortGeometry, vet

    cat, s = nu
    port = PortGeometry(
        shape=(1216, 1936), plate_scale=SCALE, fiber=FIBER, max_star_distance=600.0,
        capture_radius=8.0, exposure=0.02, photometry=FAU_PHOT,
    )  # fmt: skip
    r = vet(cat, "nu2", float(s.ra[1]), float(s.dec[1]), port, when=WHEN)
    assert r.n_acq == 2 and r.acq_branch == "pair"
    assert "acq_relies_on_chip_pa" in r.codes() and r.as_dict()["acq_branch"] == "pair"
    longer = PortGeometry(**{**port.__dict__, "exposure": 60.0})
    r = vet(cat, "nu2", float(s.ra[1]), float(s.dec[1]), longer, when=WHEN)
    assert r.acq_branch == "solve"


def test_sequencer_records_how_the_target_was_identified(make_rig):
    rig = make_rig()
    seq = rig.sequencer
    unit = seq.units["t1"]
    seq._acquired(unit, {"method": "pair", "stars": 2, "trace": []})
    assert seq.registry.get("t1.acq.method").value == "pair"
    assert seq.registry.get("t1.acq.method").keyword == "ACQMETH"
    assert seq.registry.get("t1.acq.stars").value == 2 and unit.acquisition["method"] == "pair"
