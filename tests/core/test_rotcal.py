"""Rotator / PA calibration (robofast_core/rotcal.py, docs/rotcal.md): the
fit, the store, the routine on the simulated sky (a rotator port and a
flipped fixed port), the sequencer, and the consumers -- the frame
pipeline's WCS and header, the plate solver's hint, the guider's seed."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits

from robofast_core.astrometry import SolverConfig, solve_near
from robofast_core.config import load_config
from robofast_core.guider.service import GuiderService
from robofast_core.guider.sim import SimWorld
from robofast_core.owner import DeviceOwner
from robofast_core.plan import Observation, StaticPlan
from robofast_core.request_model import load_request_groups
from robofast_core.rotcal import (
    PACalibration,
    PAMeasurement,
    PAStore,
    RotatorSetup,
    calibrate,
    fit,
    main,
    seed_mount_matrix,
)
from robofast_core.sequencer_service import build
from robofast_core.testing.pointing_sim import SimRotator, SyntheticCatalog
from robofast_core.testing.skysim import SkyCamera, SkyScene, lst_deg
from robofast_core.testing.starfield import Detector
from robofast_core.timeline import Timeline

from .test_guider_loop import _service_config
from .test_imaging import headers
from .test_pointmodel import LON
from .test_pointmodel_sequencer import SITES, now, run, sky_rig

T0 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)


# The sequencer tests point at the sky overhead *now*, so each run solves a
# different field. Over 40 fields at Dec 40 the solved PA scattered 0.01 deg
# rms with a tail to 0.04 deg (and 0.053 deg once in CI), so 0.05 deg failed
# now and then. 0.1 deg is still tight next to the errors these tests catch
# (a flip, a sign, a calibration 85 deg off).
SOLVE_TOL = 0.1


def measurements(offset, sense, flip, angles, noise=0.01, seed=0):
    rng = np.random.default_rng(seed)
    return [
        PAMeasurement(a, (offset + sense * a + rng.normal(0, noise)) % 360, flip, 0.61)
        for a in angles
    ]


# -- the fit ------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [37.5, 359.8, 101.3])
@pytest.mark.parametrize("sense", [1, -1])
@pytest.mark.parametrize("flip", [False, True])
def test_fit_recovers_offset_sense_and_parity(offset, sense, flip):
    cal = fit(measurements(offset, sense, flip, [0, 90, 180, 270]), telescope="t1", port="1")
    assert abs((cal.offset - offset + 180) % 360 - 180) < 0.02
    assert cal.sense == sense and cal.flip == flip and not cal.sense_assumed
    assert cal.rms < 0.03 and cal.n == 4 and cal.plate_scale == 0.61
    assert abs((cal.pa(45.0) - (offset + sense * 45.0) + 180) % 360 - 180) < 0.02


def test_one_angle_assumes_the_sense_and_mixed_parity_fails():
    cal = fit(measurements(101.3, 1, True, [0.0]), telescope="t2", port="fau")
    assert cal.sense == 1 and cal.sense_assumed and cal.flip
    assert cal.pa(None) == pytest.approx(101.3, abs=0.05)
    mixed = measurements(10, 1, False, [0, 90]) + measurements(10, 1, True, [180])
    with pytest.raises(ValueError, match="parity"):
        fit(mixed)
    with pytest.raises(ValueError, match="no PA"):
        fit([])


def test_store_keeps_current_and_history(tmp_path):
    store = PAStore(tmp_path / "pa.json")
    assert store.get("t1", "1") is None and store.wcs_for("t1", "1", 0) is None
    a = fit(measurements(10, 1, False, [0, 90]), telescope="t1", port="1")
    b = fit(measurements(12, 1, False, [0, 90]), telescope="t1", port="1")
    store.save(a)
    store.save(b)
    store.save(fit(measurements(100, 1, True, [0]), telescope="t2", port=None))
    assert store.get("t1", "1").offset == b.offset
    assert [c.offset for c in store.history("t1", "1")] == [a.offset, b.offset]
    pa, flip = store.wcs_for("t1", "1", 30.0)
    assert pa == pytest.approx(42.0, abs=0.05) and flip is False
    assert store.get("t2", None).flip and store.get("t2", "default").flip
    assert PACalibration.from_dict(json.loads(json.dumps(a.as_dict()))).offset == a.offset


def test_mount_seed_matches_the_guider_simulator():
    cal = fit(measurements(101.3, 1, True, [0.0]), telescope="t2", port="fau")
    world = SimWorld(plate_scale=0.2655, pa=cal.guider_pa(0.0), parity=-1)
    assert np.allclose(seed_mount_matrix(cal, 0.2655), world.mount_matrix, atol=1e-3)


# -- on the simulated sky --------------------------------------------------------------


def scene(pa=0.0, flip=False, scale=1.5):
    cam = SkyCamera(
        shape=(768, 768), plate_scale=scale, pa=pa, flip=flip, detector=Detector(shape=(768, 768))
    )
    return SkyScene(SyntheticCatalog(), camera=cam, sky_mag=21.0, seeing=2.5)


def field(t):
    return (lst_deg(t, LON) + 15.0) % 360.0, 35.0


@pytest.mark.parametrize(
    "offset,sense,flip,angles",
    [(250.0, -1, False, [0.0, 60.0, 120.0]), (101.3, 1, True, [0.0])],
    ids=["rotator-port", "flipped-fixed-port"],
)
async def test_routine_on_the_simulated_sky(offset, sense, flip, angles):
    sky = scene(flip=flip)
    ra, dec = field(T0)
    state = {"mech": 0.0}
    cfg = SolverConfig(plate_scale=1.5)

    async def move(a):
        state["mech"] = a

    async def expose():
        pa = (offset + sense * state["mech"]) % 360
        return sky.render(ra, dec, T0, 10.0, pa=pa).image.astype(float)

    def solve(image):
        return solve_near(image, sky.catalog, ra, dec, cfg, epoch=T0)

    cal = await calibrate(
        angles,
        move=move if len(angles) > 1 else None,
        expose=expose,
        solve=solve,
        telescope="t1",
        port="p",
        min_measurements=len(angles),
    )
    assert abs((cal.offset - offset + 180) % 360 - 180) < 0.05
    assert cal.flip == flip and cal.plate_scale == pytest.approx(1.5, rel=1e-3)
    if len(angles) > 1:
        assert cal.sense == sense and not cal.sense_assumed


def test_command_line_on_fits_frames(tmp_path, capsys, monkeypatch):
    """Legacy-style frames (header RA/Dec in JNow) solved and fitted."""
    from astropy import units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    sky = scene(pa=101.3, flip=True)
    cat = sky.catalog
    monkeypatch.setattr("robofast_core.catalog.catalog_from_config", lambda *a, **k: cat)
    paths = []
    for k in range(2):
        t = T0 + timedelta(minutes=10 * k)
        ra, dec = field(t)
        img = sky.render(ra, dec, t, 10.0).image.astype(np.int32)
        jnow = SkyCoord(ra * u.deg, dec * u.deg).transform_to(FK5(equinox=Time(t)))
        h = fits.Header()
        h["DATE-OBS"] = t.isoformat()
        h["RA"], h["DEC"] = jnow.ra.to_string(u.hourangle, sep=":"), jnow.dec.to_string(sep=":")
        p = tmp_path / f"f{k}.fits"
        fits.PrimaryHDU(img, h).writeto(p)
        paths.append(str(p))
    store = tmp_path / "pa.json"
    args = ["solve", "--store", str(store), "--telescope", "t2", "--port", "fau"]
    assert main([*args, "--plate-scale", "1.5", "--jnow", *paths]) == 0
    cal = PAStore(store).get("t2", "fau")
    assert cal.n == 2 and cal.flip and abs(cal.offset - 101.3) < 0.05
    assert main(["show", "--store", str(store), "--telescope", "t2", "--port", "fau"]) == 0
    assert "flipped" in capsys.readouterr().out


# -- in the sequencer, and its consumers ------------------------------------------------------


async def test_sequencer_calibrates_saves_and_the_next_frames_use_it(make_rig, tmp_path):
    ra_h, dec = ((lst_deg(now(), LON) + 20.0) % 360) / 15.0, 40.0
    obs = {"ra_hours": ra_h, "dec_deg": dec, "exptime": 0.02}
    plan = StaticPlan(
        [
            {"name": "pa", "kind": "rotator_calibration", **obs, "rotator": {"angles": [10, 80]}},
            {"name": "field", **obs, "count": 2},
        ],
        ["t1"],
    )
    rig, unit, _, scn, imaging = sky_rig(make_rig, plan, tmp_path, truth={})
    unit.pointing = None
    rot = SimRotator(position=200.0)
    scn.sky.camera = replace(scn.sky.camera, flip=True)
    scn.pa = rot.image_pa(33.0, -1)
    unit.rotator = DeviceOwner("t1.rotator", lambda: rot, rig.timeline, default_timeout=5)
    imaging._aux["t1"] = [(unit.rotator.grant("telemetry/t1"), {
        "MechanicalPosition": ("rotator.mechanical", "ROTMECH", "deg", "Rotator mechanical")
    }, "rotator")]  # fmt: skip
    store = PAStore(tmp_path / "pa.json")
    unit.pa_calibration = RotatorSetup(
        store, solver=SolverConfig(plate_scale=1.5), catalog=scn.sky.catalog, settle=0
    )
    await run(rig, imaging)
    assert [h[2] for h in plan.history] == ["complete", "complete"]
    cal = store.get("t1", None)
    assert cal is not None and cal.flip and cal.sense == -1
    assert abs((cal.offset - 33.0 + 180) % 360 - 180) < SOLVE_TOL
    assert rot.moves == [10.0, 80.0, 200.0]  # measured, then back where it was
    [ev] = rig.timeline.events("pa_calibrated")
    assert ev.detail["n"] == 2
    # The field frames: PACALIB/PAFLIP from the calibration at the rotator's
    # angle (200), and the solved PA agrees.
    light = [h for _, h in headers(tmp_path, "t1") if h.get("OBJECT") == "field"]
    assert len(light) == 2
    for h in light:
        assert h["ROTMECH"] == 200.0
        assert h["PACALIB"] == pytest.approx((33.0 - 200.0) % 360, abs=SOLVE_TOL)
        assert h["PAFLIP"] is True and h["WCSFLIP"] is True
        assert abs((h["WCSPA"] - h["PACALIB"] + 180) % 360 - 180) < SOLVE_TOL


async def test_wrong_calibration_falls_back_to_an_unhinted_solve(make_rig, tmp_path):
    ra_h, dec = ((lst_deg(now(), LON) + 20.0) % 360) / 15.0, 40.0
    plan = StaticPlan(
        [{"name": "field", "ra_hours": ra_h, "dec_deg": dec, "exptime": 0.02, "count": 1}],
        ["t1"],
    )
    rig, unit, _, scn, imaging = sky_rig(make_rig, plan, tmp_path, truth={})
    unit.pointing = None
    scn.pa = lambda: 75.0
    store = PAStore(tmp_path / "pa.json")
    store.save(fit(measurements(160.0, 1, False, [0.0]), telescope="t1"))  # wrong by 85 deg
    unit.pa_calibration = RotatorSetup(store)
    await run(rig, imaging)
    [(_, h)] = headers(tmp_path, "t1")
    assert h["PACALIB"] == pytest.approx(160.0, abs=0.1)
    assert h["WCSSRC"] == "robofast" and abs(h["WCSPA"] - 75.0) < SOLVE_TOL


async def test_guider_seeds_its_mount_matrix_from_the_pa_calibration(tmp_path):
    store = PAStore(tmp_path / "pa.json")
    cal = fit(measurements(101.3, 1, True, [0.0]), telescope="t2", port="fau")
    store.save(cal)
    cfg = _service_config(
        tmp_path,
        calibrations={},
        pa_calibration={"store": str(tmp_path / "pa.json"), "telescope": "t2"},
    )
    svc = GuiderService(cfg)
    mc = svc.guider.mount_cal
    assert mc is not None and "PA calibration" in mc.notes
    world = SimWorld(plate_scale=0.5, pa=cal.guider_pa(0.0), parity=-1)
    assert np.allclose(mc.m, world.mount_matrix, atol=1e-6)


async def test_request_type_and_service_config(tmp_path):
    from .test_dispatch import group, make

    g = group("pa", ics=[{"exptime": 5.0}])
    g["requests"][0]["configurations"][0]["type"] = "ROTATOR_CALIBRATION"
    load_request_groups([g])
    obs = await make([g], limits=None).next_observation("t1")
    assert obs.kind == "rotator_calibration" and obs.count == 1
    with pytest.raises(ValueError, match="rotator"):
        Observation("x", rotator={"angles": [0]})
    config = load_config(SITES / "minerva" / "aqawan1.json")
    t1 = config["telescopes"]["t1"]
    t1["rotator"] = "127.0.0.1:11998"
    t1["pa_calibration"] = {"store": "pa.json", "angles": [0, 90], "solver": {"plate_scale": 0.6}}
    seq, remote = build(config, Timeline(), base=tmp_path)
    try:
        u = seq.units["t1"]
        assert u.rotator is not None and u.pa_calibration.store.path == tmp_path / "pa.json"
        assert u.pa_calibration.angles == [0, 90] and seq.units["t2"].rotator is None
    finally:
        await remote.aclose()
    t1["pa_calibration"]["bogus"] = 1
    with pytest.raises(ValueError, match="t1"):
        build(config, Timeline(), base=tmp_path)


@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize("pa", [0.0, 37.0, 101.3, 250.0])
def test_fits_and_guider_conventions_agree(pa, flip):
    """The PA calibration's (FITS, plate-solve) PA and parity give the same
    pixel->sky map as the sky simulator's camera and the guider's matrix."""
    from robofast_core.guider.calibration import fits_sky_matrix, sky_matrix

    cam = SkyCamera(plate_scale=0.5, pa=pa, flip=flip)
    assert np.allclose(cam.cd() * 3600.0, fits_sky_matrix(0.5, pa, flip), atol=1e-9)
    cal = fit(measurements(pa, 1, flip, [0.0], noise=0.0), telescope="t", port="p")
    assert np.allclose(
        sky_matrix(0.5, cal.guider_pa(0.0), cal.parity), fits_sky_matrix(0.5, pa, flip), atol=1e-5
    )
