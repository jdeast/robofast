"""robofast's pointing model (robofast_core/pointmodel.py, docs/pointing_model.md):
term selection, cross-validation, persistence, the local correction, drift,
and a pointing run on the catalog-based sky simulator with injected truth."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from robofast_core import pointing as pm
from robofast_core.astrometry import SolverConfig
from robofast_core.catalog import separation
from robofast_core.pointmodel import (
    SIDEREAL_DEG_PER_S,
    BuiltModel,
    DriftMonitor,
    LocalCorrection,
    ModelStore,
    PointingCorrector,
    Sample,
    SampleLog,
    build,
    grid_radec,
    main,
    sky_grid,
    solve_sample,
)
from robofast_core.testing.pointing_sim import SyntheticCatalog
from robofast_core.testing.skysim import SkyCamera, SkyScene, lst_deg
from robofast_core.testing.starfield import Detector

LAT, LON = 31.680407, -110.878977
EQ = {"IH": 120.0, "ID": -45.0, "CH": 30.0, "NP": -12.0, "MA": 40.0, "ME": -25.0, "TF": 15.0}
AZ = {"IA": 300.0, "IE": -60.0, "CA": 20.0, "NPAE": 10.0, "AN": 35.0, "AW": -18.0, "TF": 12.0}
T0 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)


def synthetic(terms, mount="equatorial", n=60, noise=0.5, seed=0, port="1"):
    """Samples of a mount with ``terms`` (truth), ``noise`` arcsec of solver noise."""
    rng = np.random.default_rng(seed)
    m = pm.PointingModel(terms, mount=mount, latitude=LAT)
    out = []
    zmin, zmax = math.sin(math.radians(25)), math.sin(math.radians(85))
    for k in range(n):
        az, alt = rng.uniform(0, 360), math.degrees(math.asin(rng.uniform(zmin, zmax)))
        ha, dec = (float(x) for x in pm.altaz_to_hadec(az, alt, LAT))
        lst = rng.uniform(0, 360)
        if mount == "altaz":
            m1, m2 = m.mount_from_true(az, alt)
            mha, mdec = (float(x) for x in pm.altaz_to_hadec(m1, m2, LAT))
        else:
            mha, mdec = (float(x) for x in m.mount_from_true(ha, dec))
        tra = (lst - ha) % 360 + rng.normal(0, noise) / 3600 / math.cos(math.radians(dec))
        tdec = dec + rng.normal(0, noise) / 3600
        when = (T0 + timedelta(minutes=k)).isoformat()
        out.append(Sample(when, "t1", port, lst, (lst - mha) % 360, mdec, tra, tdec, "run"))
    return out


# -- fitting -------------------------------------------------------------------------


@pytest.mark.parametrize("mount,terms", [("equatorial", EQ), ("altaz", AZ)])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_term_selection_recovers_the_injected_terms(mount, terms, seed):
    built = build(synthetic(terms, mount, seed=seed), mount=mount, latitude=LAT)
    assert sorted(built.model.terms) == sorted(terms)
    for k, v in terms.items():
        assert built.model.terms[k] == pytest.approx(v, abs=max(5 * built.errors[k], 0.5))
    # Two coordinates of 0.5" noise: 0.7" rms; cross-validation is honest.
    assert built.rms_before > 100
    assert built.rms_after < 0.85
    assert built.rms_after <= built.cv_rms < 1.0


EQ_EXTRA = {"IH": 120.0, "ID": -45.0, "CH": 30.0, "MA": 40.0, "ME": -25.0, "FO": 20.0, "HCES": 15.0}
AZ_EXTRA = {"IA": 300.0, "IE": -60.0, "AN": 35.0, "AW": -18.0, "TF": 12.0, "ACEC": 25.0}


@pytest.mark.parametrize("mount,terms", [("equatorial", EQ_EXTRA), ("altaz", AZ_EXTRA)])
@pytest.mark.parametrize("seed", [0, 1])
def test_term_selection_drops_absent_basic_terms_and_adds_extras(mount, terms, seed):
    built = build(synthetic(terms, mount, seed=seed), mount=mount, latitude=LAT)
    assert sorted(built.model.terms) == sorted(terms)
    assert built.cv_rms < 1.0


def test_no_terms_invented_from_noise():
    built = build(synthetic({"IH": 60.0, "ID": 20.0}, seed=4), latitude=LAT)
    assert sorted(built.model.terms) == ["ID", "IH"]


def test_outliers_are_rejected():
    s = synthetic(EQ, seed=5)
    s[3].true_dec += 60 / 3600  # a misidentified star field
    s[11].true_ra += 90 / 3600
    built = build(s, latitude=LAT)
    assert built.n_rejected == 2 and built.n == len(s) - 2
    assert sorted(built.model.terms) == sorted(EQ)
    assert built.cv_rms < 1.0


def test_fixed_terms_and_too_few_points():
    built = build(synthetic(EQ, seed=6), latitude=LAT, terms=["IH", "ID"])
    assert sorted(built.model.terms) == ["ID", "IH"] and built.rms_after > 10
    with pytest.raises(ValueError, match="at least"):
        build(synthetic(EQ, n=5), latitude=LAT)


# -- persistence ---------------------------------------------------------------------


def test_store_keeps_history_and_provenance(tmp_path):
    store = ModelStore(tmp_path)
    a = build(synthetic(EQ, seed=1), latitude=LAT, telescope="t1", port="1", when=T0)
    b = build(synthetic(EQ, seed=2), latitude=LAT, telescope="t1", port="1", when=T0 + timedelta(1))
    store.save(a)
    store.save(b)
    cur = store.load("t1", "1")
    assert cur is not None and cur.built == b.built and cur.model == b.model
    assert [m.built for m in store.history("t1", "1")] == [a.built, b.built]
    assert cur.sources == {"run": 60} and cur.first and cur.last
    assert store.load("t1", "2") is None
    assert BuiltModel.from_dict(json.loads(json.dumps(a.as_dict()))).model == a.model


def test_sample_log_and_command_line(tmp_path, capsys):
    log = SampleLog(tmp_path / "samples")
    for s in synthetic(EQ, seed=3):
        log.append(s)
    assert len(log.read("t1", "1")) == 60
    assert log.read("t1", "1", sources=["frame"]) == []
    assert len(log.read("t1", "1", since=T0 + timedelta(minutes=30))) == 30
    args = ["build", "--store", str(tmp_path), "--telescope", "t1", "--port", "1"]
    assert main([*args, "--latitude", str(LAT)]) == 0
    assert "rms" in capsys.readouterr().out
    assert main(["show", "--store", str(tmp_path), "--telescope", "t1", "--port", "1"]) == 0
    assert ModelStore(tmp_path / "models").load("t1", "1") is not None


# -- applying it -----------------------------------------------------------------------


def corrector(terms=EQ, **kw) -> PointingCorrector:
    model = pm.PointingModel(terms, latitude=LAT)
    built = BuiltModel(model, "t1", "1", {}, 100.0, 0.5, 0.6, 50)
    return PointingCorrector("t1", latitude=LAT, longitude=LON, models={"1": built}, **kw)


def test_command_and_reported_are_inverses():
    pc = corrector()
    ra_h, dec, lst_h = 3.0, 25.0, 4.2
    cra, cdec, detail = pc.command(ra_h, dec, lst_h, "1", T0)
    assert detail["model"] and detail["offset_arcsec"] > 60
    tra, tdec = pc.reported(cra, cdec, lst_h)
    assert separation(tra * 15, tdec, ra_h * 15, dec) * 3600 < 0.01
    # Another port without a model, or the model disabled: commanded as asked.
    assert pc.command(ra_h, dec, lst_h, "2", T0)[:2] == pytest.approx((ra_h, dec))
    pc.enabled = False
    assert pc.command(ra_h, dec, lst_h, "1", T0)[:2] == pytest.approx((ra_h, dec))


def test_local_correction_decays_with_distance_and_time():
    lc = LocalCorrection(scale=10.0, tau=1800.0)
    lc.update(100.0, 20.0, 6.0, -8.0, T0)
    assert lc.offset(100.0, 20.0, T0) == pytest.approx((6.0, -8.0))
    near = lc.offset(100.0, 25.0, T0)
    assert near == pytest.approx((6.0 * math.exp(-0.5), -8.0 * math.exp(-0.5)))
    later = lc.offset(100.0, 20.0, T0 + timedelta(seconds=1800))
    assert later == pytest.approx((6.0 / math.e, -8.0 / math.e))
    lc.update(0.0, 0.0, 900.0, 0.0, T0)  # absurd: ignored
    assert lc.dx == 6.0


def test_local_correction_from_the_first_unguided_frame():
    pc = corrector(local=LocalCorrection())
    lst_h = 4.0
    cra, cdec, _ = pc.command(3.0, 25.0, lst_h, "1", T0)
    # The frame shows the telescope 4" north of the target (the model's residual).
    solve = {
        "solved": True,
        "n_matched": 30,
        "rms": 0.3,
        "axis_ra": 45.0,
        "axis_dec": 25.0 + 4 / 3600,
        "mount_ra": cra * 15,
        "mount_dec": cdec,
        "lst": lst_h * 15,
        "port": "1",
        "objra": 45.0,
        "objdec": 25.0,
        "date_obs": T0.isoformat(),
    }
    out = pc.frame_solved(solve)
    assert out["local_update"] == pytest.approx([0.0, 4.0], abs=0.01)
    # The next slew nearby aims 4" south; a second frame does not update it again.
    _, _, d2 = pc.command(3.0, 25.1, lst_h, "1", T0 + timedelta(seconds=60))
    assert d2["local_arcsec"] == pytest.approx(4.0 * math.exp(-0.1 / 15 - 60 / 3600), abs=0.01)
    assert "local_update" not in (pc.frame_solved(solve) or {})


def test_drift_alarm_fires_on_an_injected_shift():
    alarms = []
    pc = corrector(drift={"min_samples": 6})
    pc.on_alarm = alarms.append
    model = pc.models["1"].model

    def frame(ra, dec, lst, shift=0.0):
        # The telescope's true pointing, then the mount's position through the
        # model, plus an extra shift of the true pointing (the drift).
        ha = lst - ra
        mha, mdec = model.mount_from_true(ha, dec)
        return {
            "solved": True,
            "n_matched": 20,
            "rms": 0.3,
            "axis_ra": ra,
            "axis_dec": dec + shift / 3600,
            "mount_ra": float(lst - mha) % 360,
            "mount_dec": float(mdec),
            "lst": lst,
            "port": "1",
        }

    for k in range(10):
        pc.frame_solved(frame(30.0 + 10 * k, 20.0 + 3 * k, 60.0 + 5 * k))
    assert not alarms and pc.night_summary()["1"]["shift"] < 0.01
    for k in range(10):
        pc.frame_solved(frame(30.0 + 10 * k, 20.0 + 3 * k, 60.0 + 5 * k, shift=40.0))
    for k in range(10):
        pc.frame_solved(frame(30.0 + 10 * k, 20.0 + 3 * k, 60.0 + 5 * k, shift=40.0))
    assert len(alarms) == 1 and "pointing drift" in alarms[0] and "t1 port 1" in alarms[0]
    assert pc.night_summary()["1"]["alarmed"]


def test_drift_monitor_threshold():
    mon = DriftMonitor(min_samples=3, factor=5.0, floor=2.0)
    assert mon.limit(1.0) == 5.0 and mon.limit(0.1) == 2.0
    for _ in range(3):
        mon.add(1.0, 1.0)
    assert mon.check(1.0) is None


def test_sky_grid_spreads_points_and_avoids_the_moon():
    lst = 120.0
    moon = (lst, LAT, 20.0)  # the Moon at the zenith
    pts = sky_grid(40, latitude=LAT, lst=lst, avoid=[moon], min_alt=30, max_alt=85)
    assert len(pts) == 40
    alts = [alt for _, alt in pts]
    assert min(alts) >= 30 and max(alts) <= 85
    for az, alt in pts:
        ra, dec = grid_radec(az, alt, LAT, lst)
        assert separation(ra, dec, moon[0], moon[1]) > 20
    # Every quadrant of azimuth is covered.
    assert {int(az // 90) for az, _ in pts} == {0, 1, 2, 3}
    eq = sky_grid(30, latitude=LAT, lst=lst, max_hour_angle=3.0, min_dec=-20)
    for az, alt in eq:
        ha, dec = pm.altaz_to_hadec(az, alt, LAT)
        assert abs(ha) <= 45.0 + 1e-6 and dec >= -20


def test_config_entry(tmp_path):
    pc = PointingCorrector.from_config(
        "t1",
        {
            "latitude": LAT,
            "longitude": LON,
            "store": "pointing",
            "local_correction": {"scale": 5.0},
            "drift": {"floor": 3.0},
            "vendor_off": {"action": "pointing_model_off"},
            "solver": {"plate_scale": 0.61},
        },
        base=tmp_path,
    )
    assert pc.store.root == tmp_path / "pointing" / "models"
    assert pc.local.scale == 5.0 and pc.solver.plate_scale == 0.61
    with pytest.raises(ValueError, match="unknown keys"):
        PointingCorrector.from_config("t1", {"latitude": 0, "colour": 1})
    with pytest.raises(ValueError, match="drift"):
        PointingCorrector.from_config("t1", {"latitude": 0, "drift": {"x": 1}})


# -- on the simulated sky ------------------------------------------------------------------


def sky_scene(truth, mount="equatorial", **kw):
    cam = SkyCamera(shape=(768, 768), plate_scale=1.5, detector=Detector(shape=(768, 768)))
    return SkyScene(
        SyntheticCatalog(),
        camera=cam,
        pointing=pm.PointingModel(truth, mount=mount),
        sky_mag=21.0,
        seeing=2.5,
        **kw,
    )


@pytest.mark.parametrize("mount,truth", [("equatorial", EQ), ("altaz", AZ)])
def test_pointing_run_on_the_simulated_sky(mount, truth):
    """Injected TPOINT truth -> grid, render, plate solve, build: the model
    recovers the injected terms and pointing errors drop to the solver's noise."""
    sky = sky_scene(truth, mount)
    cat = sky.catalog
    pc = PointingCorrector(
        "t1",
        latitude=LAT,
        longitude=LON,
        mount=mount,
        solver=SolverConfig(plate_scale=1.5),
        catalog=cat,
    )
    t = T0
    samples = []
    for az, alt in sky_grid(28, latitude=LAT, lst=lst_deg(t, LON)):
        lst = lst_deg(t, LON)
        ra, dec = grid_radec(az, alt, LAT, lst)
        frame = sky.render(ra, dec, t, 10.0)
        mid = lst + 5.0 * SIDEREAL_DEG_PER_S
        samples.append(solve_sample(frame.image.astype(float), pc, ra, dec, mid, t, "1"))
        t += timedelta(seconds=90)
    built = build(samples, mount=mount, latitude=LAT, telescope="t1", port="1")
    assert sorted(built.model.terms) == sorted(truth)
    for k, v in truth.items():
        assert built.model.terms[k] == pytest.approx(v, abs=1.0)
    assert built.cv_rms < 0.5
    pc.set_model(built, save=False)
    rng = np.random.default_rng(7)
    before, after = [], []
    for _ in range(25):
        lst = lst_deg(t, LON)
        ra, dec = grid_radec(rng.uniform(0, 360), rng.uniform(30, 80), LAT, lst)
        tra, tdec = sky.true_pointing(ra, dec, t)
        before.append(separation(ra, dec, tra, tdec) * 3600)
        cra, cdec, _ = pc.command(ra / 15, dec, lst / 15, "1", t)
        tra, tdec = sky.true_pointing(cra * 15, cdec, t)
        after.append(separation(ra, dec, tra, tdec) * 3600)
        t += timedelta(seconds=60)
    rms_before = math.sqrt(np.mean(np.square(before)))
    rms_after = math.sqrt(np.mean(np.square(after)))
    assert rms_before > 60
    assert rms_after < 0.5, (rms_before, rms_after)
