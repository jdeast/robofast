"""The sky simulator, the photometry model, and solving frames as they are
written (WCSSRC, the zero point, the index)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits

from robofast_core.astrometry import SolverConfig
from robofast_core.catalog import catalog_at, separation
from robofast_core.fits_writer import FrameInfo, FrameStore, Optics
from robofast_core.framesolve import FrameSolver
from robofast_core.imagebytes import ImageArray
from robofast_core.photometry import PortPhotometry, SkyConditions, sky_brightness, snr
from robofast_core.pointing import PointingModel
from robofast_core.registry import ValueRegistry
from robofast_core.testing.fakes import FakeTelescope
from robofast_core.testing.focus_sim import SimFocuser, StarfieldCamera
from robofast_core.testing.skysim import (
    SkyCamera,
    SkyFocusScene,
    SkyScene,
    lst_deg,
    sky_world_stars,
)
from robofast_core.testing.starfield import Detector
from tests.core.skydata import sky_dir

HD185144 = (293.2035, 69.6611)
WHEN = datetime(2026, 4, 23, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def catalog():
    return catalog_at(sky_dir())


def cam(**kw) -> SkyCamera:
    kw.setdefault("detector", Detector(shape=(2048, 2048), gain=1.3, read_noise=6.0, bias=500.0))
    return SkyCamera((2048, 2048), 0.61, **kw)


def test_lst_matches_astropy():
    from astropy import units as u
    from astropy.time import Time

    from robofast_core.sky import offline_iers

    offline_iers()
    ours = lst_deg(WHEN, -110.878977)
    theirs = Time(WHEN).sidereal_time("mean", longitude=-110.878977 * u.deg).deg
    assert abs((ours - theirs + 180) % 360 - 180) * 240 < 0.5  # seconds of time


def test_stars_land_on_their_pixels(catalog):
    c = cam(pa=30.0)
    sc = SkyScene(catalog, c, PortPhotometry(), conditions=SkyConditions(), seeing=1.5)
    frame = sc.render(*HD185144, WHEN, 10.0, noise=False)
    t = frame.truth
    assert t["ra"] == pytest.approx(HD185144[0]) and len(t["stars"]) > 20
    # Every star's pixel maps back to its catalog position.
    ra, dec = c.unproject(t["ra"], t["dec"], t["x"], t["y"])
    assert np.max(separation(ra, dec, t["stars"].ra, t["stars"].dec)) * 3600 < 1e-3
    # And the brightest one really is in the image there.
    k = int(np.argmin(t["stars"].g))
    x, y = round(t["x"][k]), round(t["y"][k])
    if 5 < x < 2040 and 5 < y < 2040:
        assert frame.image[y, x] > frame.truth["sky_adu"] + 100


def test_rotation_and_parity(catalog):
    a = cam(pa=0.0)
    north = a.project(10.0, 20.0, [10.0], [20.01])
    east = a.project(10.0, 20.0, [10.01], [20.0])
    assert north[1][0] > a.centre[1] and abs(north[0][0] - a.centre[0]) < 1e-6  # north up
    assert east[0][0] < a.centre[0]  # east left
    b = cam(pa=90.0)
    north = b.project(10.0, 20.0, [10.0], [20.01])
    assert north[0][0] > b.centre[0]  # +y is east, so north is +x... to the right
    f = cam(flip=True)
    east = f.project(10.0, 20.0, [10.01], [20.0])
    assert east[0][0] > f.centre[0]


def test_pointing_error_moves_the_field(catalog):
    model = PointingModel({"IH": 120.0, "ID": -90.0, "NP": 30.0})
    sc = SkyScene(catalog, cam(), PortPhotometry(), pointing=model, conditions=SkyConditions())
    tra, tdec = sc.true_pointing(*HD185144, WHEN)
    off = separation(*HD185144, tra, tdec) * 3600
    assert 60 < off < 300
    # A perfect pointing model (the same terms) commands the right place.
    cra, cdec = sc.command_for(*HD185144, WHEN)
    back = sc.true_pointing(cra, cdec, WHEN)
    assert separation(*HD185144, *back) * 3600 < 0.01


def test_altaz_pointing_error(catalog):
    model = PointingModel({"IA": 200.0, "IE": 60.0}, mount="altaz")
    sc = SkyScene(catalog, cam(), PortPhotometry(), pointing=model, conditions=SkyConditions())
    tra, tdec = sc.true_pointing(*HD185144, WHEN)
    assert 30 < separation(*HD185144, tra, tdec) * 3600 < 300
    back = sc.true_pointing(*sc.command_for(*HD185144, WHEN), WHEN)
    assert separation(*HD185144, *back) * 3600 < 0.01


def test_sky_brightness():
    dark = sky_brightness(SkyConditions(target_alt=90.0))
    assert dark == pytest.approx(21.7, abs=0.05)
    assert sky_brightness(SkyConditions(target_alt=30.0)) < dark  # brighter low
    full = SkyConditions(moon_alt=50, moon_phase=0, moon_sep=30, target_alt=60)
    assert sky_brightness(full) < 19.0  # full moon nearby: ~18
    assert sky_brightness(SkyConditions(sun_alt=-8.0, target_alt=60)) < 16.0


def test_photometry_chain():
    imager = PortPhotometry(band="V", efficiency={"mirrors": 0.8, "qe": 0.8})
    fau = PortPhotometry(band="V", efficiency={"mirrors": 0.8, "qe": 0.8, "beamsplitter": 0.02})
    # The 2 % beamsplitter costs 2.5 log10(50) = 4.25 mag.
    assert imager.zp - fau.zp == pytest.approx(4.247, abs=1e-3)
    fiber = fau.with_fraction("beamsplitter", 0.98)
    assert fiber.zp - fau.zp == pytest.approx(2.5 * math.log10(49), abs=1e-6)
    measured = PortPhotometry(zero_point=20.1)
    assert measured.zp == 20.1
    assert imager.rate(imager.zp) == pytest.approx(10 ** (-0.4 * imager.extinction))
    s = snr(imager, 12.0, 10.0, sky_per_pixel=5.0, npix=50)
    assert s > snr(imager, 14.0, 10.0, sky_per_pixel=5.0, npix=50) > 0
    with pytest.raises(ValueError):
        PortPhotometry.from_dict({"efficiency": {"x": 1.5}})


def test_sky_focus_scene_follows_the_mount(catalog):
    mount = FakeTelescope(slew_time=0.0)
    mount.Unpark()
    mount.ra, mount.dec = HD185144[0] / 15.0, HD185144[1]
    c = SkyCamera((512, 512), 0.61, detector=Detector(shape=(512, 512)))
    sky = SkyScene(catalog, c, PortPhotometry(), conditions=SkyConditions())
    scene = SkyFocusScene(sky, mount, lambda: WHEN, effective_exposure=5.0)
    foc = SimFocuser(20000)
    camera = StarfieldCamera(foc, scene)
    camera.StartExposure(1.0, True)
    camera._ready = True
    img = camera.ImageArray
    assert img.data.shape == (512, 512)
    assert scene.last_truth["ra"] == pytest.approx(HD185144[0])
    assert len(scene.last_truth["stars"]) >= 1


def test_guider_world_from_the_sky(catalog):
    from robofast_core.guider.base import ROI
    from robofast_core.guider.sim import SimWorld

    c = SkyCamera((1216, 1936), 0.265, detector=Detector(shape=(1216, 1936)))
    sky = SkyScene(catalog, c, PortPhotometry(), conditions=SkyConditions())
    # sigma Draconis (HD 185144), G 4.4, where the catalog has it.
    sig = catalog.cone(*HD185144, 0.1, epoch=WHEN).take([0])
    main, others = sky_world_stars(sky, sig.ra[0], sig.dec[0], WHEN)
    w = SimWorld(sensor=(1936, 1216), star=main[:2], flux=main[2], others=others, noise=False)
    assert abs(main[0] - 967.5) < 1 and abs(main[1] - 607.5) < 1  # at the centre
    assert main[2] > max(f for _, _, f in others)
    assert w.render(ROI(900, 540, 136, 136), 0.0, 0.01).max() > 1000


# -- frames written with a solve -------------------------------------------------------


def _info(tel: str, when: datetime, ra: float, dec: float) -> FrameInfo:
    reg = ValueRegistry()
    for name, v, kw in (
        (f"{tel}.tel.ra", ra / 15.0, None),
        (f"{tel}.tel.dec", dec, None),
        (f"{tel}.tel.alt", 60.0, "ALT"),
        (f"{tel}.camera.last_start", when.isoformat(), None),
        (f"{tel}.camera.last_duration", 10.0, None),
        (f"{tel}.camera.binx", 1, "XBINNING"),
        (f"{tel}.camera.biny", 1, "YBINNING"),
        (f"{tel}.camera.egain", 1.3, "EGAIN"),
        (f"{tel}.camera.max_adu", 65535, "MAXADU"),
    ):
        reg.publish(name, v, max_age=None, keyword=kw)
    snap = reg.snapshot()
    return FrameInfo(
        telescope=tel,
        object="HD185144",
        obs_name="obs",
        imagetyp="LIGHT",
        light=True,
        exptime_requested=10.0,
        host_start=when,
        index=0,
        target=(ra / 15.0, dec),
        start=snap,
        end=snap,
        optics=Optics(plate_scale=0.61),
    )


def test_frame_store_writes_the_solved_wcs_and_zero_point(catalog, tmp_path):
    c = cam(pa=0.0)
    model = PointingModel({"IH": 80.0, "ID": 50.0})
    sc = SkyScene(catalog, c, PortPhotometry(gain=1.3), pointing=model, conditions=SkyConditions())
    frame = sc.render(*HD185144, WHEN, 10.0)
    solver = FrameSolver(catalog, {"t1": SolverConfig(0.61, search_radius=0.1)})
    store = FrameStore(tmp_path, "test", solver=solver)
    data = np.ascontiguousarray(frame.image.T.astype("<i4"))
    w = store.write(_info("t1", WHEN, *HD185144), ImageArray(data, 2, "test", data.nbytes))
    h = fits.getheader(w.path)
    assert h["WCSSRC"] == "robofast"
    assert h["WCSNSTAR"] >= 10
    from astropy.wcs import WCS

    ra, dec = WCS(h).all_pix2world([1023.5], [1023.5], 0)
    tra, tdec = frame.truth["ra"], frame.truth["dec"]
    assert separation(ra[0], dec[0], tra, tdec) * 3600 < 0.5
    assert h["ZPNSTAR"] >= 3 and h["ZPUNIT"] == "e-/s"
    assert "ZPMAG" in h and "AIRMASS" in h
    rec = json.loads(next(w.path.parent.glob("*.frames.jsonl")).read_text().splitlines()[0])
    assert rec["solve"]["solved"] and rec["solve"]["offset_arcsec"] > 30
    assert rec["solve"]["zp"]["n"] >= 3


def test_frame_store_keeps_the_mount_wcs_when_unsolved(catalog, tmp_path):
    solver = FrameSolver(catalog, {"t1": SolverConfig(0.61, search_radius=0.05)})
    store = FrameStore(tmp_path, "test", solver=solver)
    blank = np.random.default_rng(0).normal(500, 5, (256, 256)).astype("<i4")
    w = store.write(_info("t1", WHEN, *HD185144), ImageArray(blank.T.copy(), 2, "t", blank.nbytes))
    h = fits.getheader(w.path)
    assert h["WCSSRC"] == "mount" and "WCSFAIL" in h
    assert w.record["solve"]["solved"] is False


def test_imaging_config_solve_entry(tmp_path):
    from robofast_core.imaging import Imaging, ImagingConfig

    cfg = ImagingConfig.from_dict(
        {
            "data_root": str(tmp_path),
            "telescopes": {"t1": {"plate_scale": 0.61}},
            "solve": {"catalog": {"cache_dir": str(sky_dir()), "fetch": False}},
        }
    )
    im = Imaging(cfg)
    assert isinstance(im.store.solver, FrameSolver)
    assert im.store.solver.configs["t1"].plate_scale == 0.61
    im._publish_solve(
        "t1", {"solved": True, "rms": 0.2, "zp": {"zp": 22.0, "unit": "e-/s", "n": 9}}
    )
    assert im.registry.get("t1.image.zp").value == 22.0


def test_sky_focus_scene_without_catalog_renders_empty(tmp_path):
    mount = FakeTelescope(slew_time=0.0)
    mount.ra, mount.dec = 1.0, 10.0
    c = SkyCamera((64, 64), 0.61, detector=Detector(shape=(64, 64), bias=500.0))
    sky = SkyScene(catalog_at(tmp_path), c, PortPhotometry(), conditions=SkyConditions())
    scene = SkyFocusScene(sky, mount, lambda: WHEN)
    img = scene.render(20000.0, 1.0, (0, 0, 64, 64), 2)
    assert img.shape == (32, 32) and np.all(img == 500)
    assert "not cached" in scene.last_truth["error"]
