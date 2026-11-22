"""robofast.allsky: the fisheye model, the patch statistics, the codecs.

All synthetic: no camera frames, no catalogue download."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from robofast.allsky import clouds, image
from robofast.allsky.model import CameraModel, fit

# -- the camera model ---------------------------------------------------------------


def _model(**kw) -> CameraModel:
    base = dict(camera="test", width=1000, height=1000, x0=503.2, y0=497.5, rot=12.0,
                parity=1, tilt_e=1.2, tilt_n=-0.7, f1=310.0, f2=-8.0, f3=-6.0)  # fmt: skip
    base.update(kw)
    return CameraModel(**base)


def _sky(n=400, seed=1, min_alt=10.0):
    rng = np.random.default_rng(seed)
    alt = np.degrees(np.arcsin(rng.uniform(math.sin(math.radians(min_alt)), 1.0, n)))
    az = rng.uniform(0, 360, n)
    return alt, az


@pytest.mark.parametrize("parity", [1, -1])
def test_pixel_altaz_round_trip(parity):
    m = _model(parity=parity)
    alt, az = _sky()
    x, y = m.to_pixel(alt, az)
    a2, z2 = m.to_altaz(x, y)
    assert np.max(np.abs(a2 - alt)) < 1e-6
    dz = (z2 - az + 180) % 360 - 180
    assert np.max(np.abs(dz[alt < 89.9])) < 1e-5


def test_orientation_conventions():
    """Parity +1 is the view from below: north up, east left."""
    m = _model(rot=0.0, tilt_e=0.0, tilt_n=0.0, f2=0.0, f3=0.0)
    xn, yn = m.to_pixel(45.0, 0.0)
    xe, ye = m.to_pixel(45.0, 90.0)
    assert abs(xn - m.x0) < 1e-9 and yn < m.y0
    assert xe < m.x0 and abs(ye - m.y0) < 1e-9
    # Equidistant: radius f1 * zenith angle.
    assert math.isclose(m.y0 - yn, m.f1 * math.radians(45.0), rel_tol=1e-12)
    xz, yz = m.to_pixel(90.0, 123.0)
    assert math.hypot(xz - m.x0, yz - m.y0) < 1e-9
    mirrored = replace(m, parity=-1)
    assert mirrored.to_pixel(45.0, 90.0)[0] > m.x0


def test_tilt_moves_zenith_toward_tilt():
    m = _model(rot=0.0, tilt_e=2.0, tilt_n=0.0, f2=0.0, f3=0.0)
    xz, _yz = m.to_pixel(90.0, 0.0)
    # Axis tipped east: the zenith lies west of the axis, i.e. to the right.
    assert xz > m.x0
    assert math.isclose(xz - m.x0, m.f1 * math.radians(2.0), rel_tol=1e-6)


def test_fit_recovers_parameters_and_reports_pixel_residuals():
    truth = _model()
    alt, az = _sky(300, seed=3)
    x, y = truth.to_pixel(alt, az)
    rng = np.random.default_rng(4)
    x = x + rng.normal(0, 0.5, x.size)
    y = y + rng.normal(0, 0.5, y.size)
    # Ten gross outliers (wrong matches) must be clipped.
    x[:10] += 25.0
    start = _model(x0=490.0, y0=510.0, rot=10.0, tilt_e=0.0, tilt_n=0.0, f1=300.0, f2=0.0, f3=0.0)
    res = fit(start, alt, az, x, y)
    assert not res.used[:10].any()
    assert res.used[10:].mean() > 0.97
    assert 0.5 < res.rms < 0.9  # 2-D rms of 0.5 px per axis is 0.71
    xp, yp = res.model.to_pixel(alt, az)
    xt, yt = truth.to_pixel(alt, az)
    assert np.max(np.hypot(xp - xt, yp - yt)) < 0.5
    assert res.model.meta["fit"]["n"] == int(res.used.sum())


def test_model_json_round_trip(tmp_path):
    m = _model()
    m.sky = {"m0": 4.2, "clear_noise": [[1.0, 2.0]]}
    p = tmp_path / "cam.json"
    m.save(p)
    m2 = CameraModel.load(p)
    assert m2 == m
    assert np.allclose(m2.to_pixel(40.0, 200.0), m.to_pixel(40.0, 200.0))


# -- patches --------------------------------------------------------------------------


def test_patches_tile_the_sky_above_20_deg_in_near_equal_areas():
    ps = clouds.patches()
    assert len(ps) == 36
    alt = np.arange(20.0, 90.0, 0.25)
    az = np.arange(0.0, 360.0, 0.5)
    aa, zz = np.meshgrid(alt, az)
    idx = clouds.patch_index(aa, zz)
    assert set(np.unique(idx)) == set(range(36))
    assert clouds.patch_index(19.99, 10.0) == -1
    assert clouds.patch_index(90.0, 10.0) == 35
    areas = [
        math.radians(p.az_hi - p.az_lo)
        * (math.sin(math.radians(min(p.alt_hi, 90))) - math.sin(math.radians(p.alt_lo)))
        for p in ps
    ]
    assert max(areas) / min(areas) < 1.35
    # Azimuth wraps.
    assert clouds.patch_index(25.0, 361.0) == clouds.patch_index(25.0, 1.0)


def _table(n_per_patch=10, det=1.0, p=1.0, fp=0.0, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for pt in clouds.PATCHES:
        alt = rng.uniform(pt.alt_lo, min(pt.alt_hi, 90), n_per_patch)
        az = rng.uniform(pt.az_lo, pt.az_hi, n_per_patch)
        rows.append((alt, az, np.full(n_per_patch, pt.index)))
    alt, az, pid = (np.concatenate([r[i] for r in rows]) for i in range(3))
    n = alt.size
    return {
        "v": np.full(n, 3.0), "alt": alt, "az": az, "x": np.zeros(n), "y": np.zeros(n),
        "p": np.full(n, p), "det": np.full(n, det), "fp": np.full(n, fp),
        "snr": np.zeros(n), "noise_ratio": np.ones(n), "patch": pid.astype(float),
    }  # fmt: skip


def _stats(t, bg=None, dark=True, min_weight=2.0):
    bg = np.ones(36) if bg is None else bg
    return clouds.patch_stats(t, bg, min_weight=min_weight, bright_ratio=1.6, dark=dark)


def test_patch_stats_all_seen_and_none_seen():
    clear, w, _d, st = _stats(_table(det=1.0))
    assert np.allclose(clear, 1.0) and set(st) == {"clear"}
    assert np.allclose(w, 10.0)
    clear, *_, st = _stats(_table(det=0.0))
    assert np.allclose(clear, 0.0) and set(st) == {"cloudy"}


def test_patch_stats_weights_by_expectation_and_corrects_false_positives():
    t = _table(n_per_patch=4)
    # In each patch: two bright stars (P=1) seen, two faint (P=0.1) missed.
    t["p"] = np.tile([1.0, 1.0, 0.1, 0.1], 36)
    t["det"] = np.tile([1.0, 1.0, 0.0, 0.0], 36)
    clear, w, *_ = _stats(t)
    assert np.allclose(w, 2.2)
    assert np.allclose(clear, 2.0 / 2.02)
    # A 20% chance-coincidence rate: half the stars "seen" is 37.5% clear.
    t = _table(n_per_patch=8, fp=0.2)
    t["det"] = np.tile([1.0, 0.0], 36 * 4)
    clear, *_ = _stats(t)
    assert np.allclose(clear, (4 - 8 * 0.2) / (8 * 0.8))


def test_patch_stats_unknown_without_weight_and_cloudy_from_background():
    t = _table(n_per_patch=1)  # weight 1 < 2: unknown
    bg = np.ones(36)
    bg[3] = 2.5  # lit cloud in patch 3
    clear, _, _, st = _stats(t, bg)
    assert st[0] == "unknown" and np.isnan(clear[0])
    assert st[3] == "cloudy" and clear[3] == 0.0
    # Not on a moonlit or twilight sky: the background then says nothing.
    clear, _, _, st = _stats(t, bg, dark=False)
    assert st[3] == "unknown"


def _map(table, t0=None, clear=None):
    clear_p, w, d, st = _stats(table)
    if clear is not None:
        clear_p = np.full(36, clear)
    return clouds.CloudMap(
        time=t0 or datetime(2026, 11, 22, 5, tzinfo=UTC), cameras=["test"], clear=clear_p,
        weight=w, detected=d, bg_ratio=np.ones(36), status=st, table=table,
        settings={"min_weight": 2.0},
    )  # fmt: skip


def test_clear_fraction_at_a_pointing():
    t = _table(n_per_patch=20)
    # Cloud over the north-east: stars with az 0-90 below 50 deg unseen.
    cloud = (t["az"] < 90) & (t["alt"] < 50)
    t["det"] = np.where(cloud, 0.0, 1.0)
    m = _map(t)
    assert m.clear_fraction(35.0, 45.0, 8.0) == pytest.approx(0.0)
    assert m.clear_fraction(35.0, 225.0, 8.0) == pytest.approx(1.0)
    frac, w, src = m.clear_fraction_detail(35.0, 85.0, 10.0)
    assert 0.2 < frac < 0.8 and src == "stars" and w > 2
    # Too few stars in a tiny circle: falls back to the patches.
    frac, w, src = m.clear_fraction_detail(60.0, 200.0, 0.1)
    assert src == "patches" and frac == pytest.approx(1.0)
    assert 0.6 < m.whole_sky < 0.95
    assert m.coverage == 1.0
    doc = m.to_json()
    assert len(doc["patches"]) == 36 and doc["patches"][0]["status"] == "cloudy"


def test_trend_rising_falling_stable():
    t0 = datetime(2026, 11, 22, 5, tzinfo=UTC)
    tab = _table()
    up = [_map(tab, t0 + timedelta(minutes=5 * k), clear=0.1 * k) for k in range(8)]
    tr = clouds.trend(up, n=8)
    assert set(tr.state) == {"rising"} and tr.whole_state == "rising"
    assert tr.slope[0] == pytest.approx(1.2)  # 0.1 per 5 min
    down = [_map(tab, m.time, clear=1 - m.clear[0]) for m in up]
    assert clouds.trend(down, n=8).whole_state == "falling"
    rng = np.random.default_rng(0)
    flat = [_map(tab, m.time, clear=0.8 + rng.normal(0, 0.02)) for m in up]
    assert clouds.trend(flat, n=8).whole_state == "stable"
    assert clouds.trend(up[:2], n=8).whole_state == "unknown"


def test_detection_probability_falls_with_magnitude_airmass_and_noise():
    sky = {"m0": 4.0, "a1": -0.5, "width": 0.3}
    p = clouds.p_detect(sky, [2.0, 4.0, 6.0], 90.0, 1.0)
    assert p[0] > 0.99 and p[1] == pytest.approx(0.5, abs=1e-3) and p[2] < 0.01
    assert clouds.p_detect(sky, 3.5, 25.0, 1.0) < clouds.p_detect(sky, 3.5, 80.0, 1.0)
    # Noise twice the clear-sky noise costs 0.75 mag.
    assert clouds.limit_mag(sky, 90.0, 2.0) == pytest.approx(4.0 - 2.5 * math.log10(2), abs=1e-3)
    assert clouds.limit_mag(sky, 90.0, 0.5) == pytest.approx(4.0, abs=1e-3)


# -- image operations -----------------------------------------------------------------


def test_background_and_peaks_find_point_sources():
    rng = np.random.default_rng(5)
    img = 40 + rng.normal(0, 2.0, (256, 256)) + np.linspace(0, 20, 256)[None, :]
    stars = [(50.3, 60.7), (200.0, 30.2), (128.6, 190.4)]
    yy, xx = np.mgrid[0:256, 0:256]
    for x, y in stars:
        img += 60 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.0**2))
    bg = image.background(img.astype(np.float32), np.ones(img.shape, bool), 32)
    assert np.median(bg.noise) == pytest.approx(2.0 * 0.375, rel=0.3)  # 1-2-1 smoothing
    x, y, _s = image.peaks(bg, 6.0)
    assert len(x) == 3
    for sx, sy in stars:
        d = np.hypot(x - sx, y - sy)
        assert d.min() < 0.5


# -- images (Pillow) -----------------------------------------------------------------


def test_image_round_trip_and_mask(tmp_path):
    pytest.importorskip("PIL")
    from robofast.allsky import imageio

    rng = np.random.default_rng(2)
    grey = rng.integers(0, 256, (17, 23), dtype=np.uint8)
    rgb = rng.integers(0, 256, (9, 11, 3), dtype=np.uint8)
    for a in (grey, rgb):
        p = tmp_path / "a.png"
        imageio.write_png(p, a)
        assert np.array_equal(imageio.decode(p.read_bytes()), a)
    imageio.write_png(tmp_path / "m.png", (grey > 100).astype(np.uint8) * 255)
    assert np.array_equal(imageio.read_mask(tmp_path / "m.png"), grey > 100)


def test_frames_need_pillow_only_when_reading(monkeypatch, tmp_path):
    import builtins

    from robofast.allsky import frames, imageio

    real = builtins.__import__

    def no_pil(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL here")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    p = tmp_path / "mmto_20261122T030000Z.jpg"
    p.write_bytes(b"\xff\xd8not really")
    with pytest.raises(imageio.PillowMissing, match="robofast\\[allsky\\]"):
        frames.load_frame(p)


def test_cloud_map_png_draws(tmp_path):
    pytest.importorskip("PIL")
    from robofast.allsky import imageio, render
    from robofast.allsky.frames import Frame

    m = _map(_table(det=1.0))
    f = Frame("test", m.time, np.random.default_rng(0).normal(40, 3, (300, 320)).astype(np.float32))
    out = tmp_path / "map.png"
    render.cloud_map_png(str(out), f, m)
    img = imageio.decode(out.read_bytes())
    assert img.ndim == 3 and img.shape[1] > 600
    render.time_series_png(
        str(tmp_path / "ts.png"),
        [(np.array([1.0, 2.0, 3.0]), np.array([0.1, 0.5, 0.9]), (0, 150, 0), "camera")],
        ylim=(0, 1),
        title="test",
    )
    assert (tmp_path / "ts.png").stat().st_size > 0


# -- end to end on a synthetic frame ---------------------------------------------------


@pytest.fixture
def fake_sky(monkeypatch):
    """A random catalogue (no download) and an offline ephemeris with the
    Sun and Moon down."""
    from robofast.allsky import stars as st

    rng = np.random.default_rng(11)
    n = 3000
    cat = np.zeros(n, dtype=st.STAR_DTYPE)
    cat["hr"] = np.arange(n)
    cat["ra"] = rng.uniform(0, 360, n)
    cat["dec"] = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    cat["v"] = np.sort(rng.uniform(0.0, 6.5, n) ** 0.7 * 6.5**0.3)
    monkeypatch.setattr(st, "bright_stars", lambda path=None, vmax=6.5: cat[cat["v"] <= vmax])
    eph = st.Ephemeris(sun_alt=-40.0, sun_az=0.0, moon_alt=-30.0, moon_az=0.0, moon_illum=0.5,
                       planets={})  # fmt: skip
    monkeypatch.setattr(st, "ephemeris", lambda when, site=st.SITE: eph)
    return cat, eph


def _render(model, cat, when, cloud=None, seed=0):
    from robofast.allsky import stars as st

    rng = np.random.default_rng(seed)
    alt, az = st.star_altaz(cat, when)
    up = alt > 5
    x, y = model.to_pixel(alt[up], az[up])
    amp = 120 * 10 ** (-0.4 * (cat["v"][up] - 1.0))
    if cloud is not None:
        amp = np.where(cloud(alt[up], az[up]), 0.0, amp)
    img = 30 + rng.normal(0, 2.0, (model.height, model.width))
    yy, xx = np.mgrid[0 : model.height, 0 : model.width]
    for xi, yi, a in zip(x, y, amp, strict=True):
        if a < 1:
            continue
        i0, j0 = int(yi) - 4, int(xi) - 4
        if i0 < 0 or j0 < 0 or i0 + 9 > model.height or j0 + 9 > model.width:
            continue
        sy, sx = yy[i0 : i0 + 9, j0 : j0 + 9], xx[i0 : i0 + 9, j0 : j0 + 9]
        img[i0 : i0 + 9, j0 : j0 + 9] += a * np.exp(-((sx - xi) ** 2 + (sy - yi) ** 2) / 2.0)
    return np.clip(img, 0, 255).astype(np.float32)


def test_synthetic_frame_solve_and_cloud_map(fake_sky):
    from robofast.allsky import calibrate, cameras
    from robofast.allsky.frames import Frame

    cat, _ = fake_sky
    spec = cameras.CameraSpec(name="synth", title="", url="", width=400, height=400,
                              x0=200.0, y0=200.0, horizon_px=185.0)  # fmt: skip
    truth = CameraModel(camera="synth", width=400, height=400, x0=203.0, y0=196.0, rot=37.0,
                        f1=185.0 / (math.pi / 2), f2=-3.0, f3=-2.0)  # fmt: skip
    when = datetime(2026, 11, 22, 6, tzinfo=UTC)
    frame = Frame("synth", when, _render(truth, cat, when))
    sol = calibrate.solve(frame, spec, shift_px=10.0, vmax_search=3.0, vmax_fit=4.5)
    xs, ys = sol.fit.model.to_pixel(np.array([30.0, 60.0, 85.0]), np.array([10.0, 200.0, 300.0]))
    xt, yt = truth.to_pixel(np.array([30.0, 60.0, 85.0]), np.array([10.0, 200.0, 300.0]))
    assert np.max(np.hypot(xs - xt, ys - yt)) < 0.5
    assert sol.fit.rms < 0.6
    model = sol.fit.model
    model.sky = {"m0": 4.0, "a1": -0.3, "width": 0.3, "radius_px": 2.0}
    clear_map, _ = clouds.analyze(frame, spec, model)
    assert clear_map.whole_sky > 0.85
    # A cloud over the western half (az 180-360).
    cloudy = Frame("synth", when, _render(truth, cat, when, cloud=lambda a, z: (z % 360) >= 180))
    cmap, _ = clouds.analyze(cloudy, spec, model)
    west = cmap.clear_fraction(45.0, 270.0, 20.0)
    east = cmap.clear_fraction(45.0, 90.0, 20.0)
    assert west < 0.15 and east > 0.85
    chk = calibrate.check(frame, spec, model)
    assert chk.ok, chk
    bumped = replace(model, x0=model.x0 + 6.0)
    assert not calibrate.check(frame, spec, bumped).ok


def test_map_json_round_trip_keeps_what_the_trend_needs():
    m = _map(_table(det=1.0))
    m2 = clouds.map_from_json(m.to_json())
    assert m2.time == m.time
    assert np.allclose(m2.clear, m.clear) and m2.status == m.status
    assert m2.whole_sky == pytest.approx(m.whole_sky)
    assert m2.clear_fraction(50.0, 100.0, 5.0) == pytest.approx(1.0)  # from the patches


def test_validation_classes_and_confusion():
    from robofast.allsky import validate

    assert validate.sensor_class(-45.0) == "clear"
    assert validate.sensor_class(-30.0) == "between"
    assert validate.sensor_class(-5.0) == "cloudy"
    assert validate.sensor_class(998.0) == "cloudy"  # rain
    assert validate.sensor_class(39.0) == "none"
    assert validate.sensor_class(None) == "none"
    assert validate.camera_class(0.9, 0.8) == "clear"
    assert validate.camera_class(0.9, 0.1) == "unknown"
    assert validate.camera_class(0.5, 0.8) == "partly"
    recs = [
        {"sun_alt": -30, "clear": 0.95, "coverage": 0.8, "MearthCloud": -45.0},
        {"sun_alt": -30, "clear": 0.05, "coverage": 0.8, "MearthCloud": -5.0},
        {"sun_alt": -30, "clear": 0.90, "coverage": 0.8, "MearthCloud": -5.0},
        {"sun_alt": -15, "clear": 0.10, "coverage": 0.8, "MearthCloud": -45.0},  # twilight
        {"time": "x", "error": "corrupt"},
    ]
    c = validate.confusion(recs)
    assert c[("clear", "clear")] == 1 and c[("cloudy", "cloudy")] == 1
    s = validate.scores(c)
    assert s["decided"] == 3 and s["agreement"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["camera_clear_when_sensor_cloudy"] == pytest.approx(0.5)
    assert validate.confusion(recs, sun_range=(-18, -12))[("clear", "cloudy")] == 1


def test_site_log_parsing_both_formats(tmp_path):
    from robofast.allsky import archive

    p = tmp_path / "site_mtHopkins.log"
    p.write_text(
        "2016-01-08T23:10:12 [env.py:250 - getWeather()] DEBUG: MearthCloud=-21.0\n"
        "2016-11-02T23:03:54.639 [env.py:227 - getWeather()] DEBUG: T4: MearthCloud=-51.3\n"
        "2016-11-02T23:03:54.639 [env.py:227 - getWeather()] DEBUG: T4: Requesting URL: x\n"
        "2016-11-02T23:04:00 [env.py:298 - oktoopen()] INFO: Not OK to open: MearthCloud=-1.0\n"
    )
    log = archive.read_site_log(p)
    assert log["MearthCloud"][1].tolist() == [-21.0, -51.3]
    t = datetime(2016, 11, 2, 23, 5, tzinfo=UTC)
    assert archive.series_at(log["MearthCloud"], t) == -51.3
    assert math.isnan(archive.series_at(log["MearthCloud"], t + timedelta(hours=1)))


def test_twilight_term_lowers_the_limit_only_above_minus_18():
    sky = {"m0": 5.0, "a1": 0.0, "width": 0.3, "twilight": 0.25}
    assert clouds.limit_mag(sky, 90.0, 1.0, -30.0) == pytest.approx(5.0, abs=1e-3)
    assert clouds.limit_mag(sky, 90.0, 1.0, -12.0) == pytest.approx(5.0 - 1.5, abs=1e-3)
    assert clouds.limit_mag(sky, 90.0, 1.0, None) == pytest.approx(5.0, abs=1e-3)


def test_obstruction_mask_marks_blocks_where_bright_stars_never_show():
    from robofast.allsky import skycal

    rng = np.random.default_rng(3)
    n = 4000
    x = rng.uniform(0, 240, n)
    y = rng.uniform(0, 240, n)
    v = np.full(n, 2.0)
    alt = np.full(n, 70.0)
    det = np.ones(n)
    behind = (x > 192) & (y > 192)  # a building in the corner
    det[behind] = 0.0
    sky = {"m0": 5.0, "a1": 0.0, "width": 0.3}
    m = skycal.obstruction_mask(x, y, v, alt, det, sky, (240, 240), block=24)
    assert m[200:, 200:].all()
    assert not m[:180, :].any()


def test_moon_term_grows_with_phase_altitude_and_closeness():
    f_full_high = clouds.moon_factor(1.0, 90.0, np.array([10.0, 90.0]))
    assert f_full_high[0] > f_full_high[1] > 1.0
    assert clouds.moon_factor(0.5, 90.0, 90.0) < clouds.moon_factor(1.0, 90.0, 90.0)
    assert np.all(clouds.moon_factor(1.0, -5.0, np.array([10.0])) == 0)
    sky = {"m0": 5.0, "a1": 0.0, "width": 0.3, "moon": 2.0}
    assert clouds.limit_mag(sky, 90.0, 1.0, None, 1.5) == pytest.approx(2.0, abs=1e-3)


def test_clear_patches_picks_patches_where_bright_stars_all_show():
    from robofast.allsky import skycal

    t = _table(n_per_patch=4)
    t["v"][:] = 2.0
    t["det"][:] = 1.0
    t["det"][t["patch"] == 5] = 0.0
    t["det"][np.flatnonzero(t["patch"] == 6)[0]] = 0.0  # 3 of 4 seen: not clear enough
    good = skycal.clear_patches(t)
    assert 5 not in good and 6 not in good and 0 in good and len(good) == 34


# -- the shadow watcher ----------------------------------------------------------------


def test_watch_config_reads_a_directory_unless_fetch_is_opted_in(tmp_path):
    from robofast.allsky import watch

    cfg = watch.WatchConfig.parse(
        {"output": str(tmp_path / "latest.json"), "cameras": {"mmto": {"dir": str(tmp_path)}}}
    )
    assert cfg.fetch is False and cfg.cameras["mmto"]["dir"] == tmp_path
    with pytest.raises(ValueError, match="needs a dir"):
        watch.WatchConfig.parse({"cameras": {"mmto": {"url": "https://example.invalid/x.jpg"}}})
    ok = watch.WatchConfig.parse(
        {"fetch": True, "cameras": {"mmto": {"url": "https://example.invalid/x.jpg"}}}
    )
    assert ok.fetch
    with pytest.raises(ValueError, match="unknown"):
        watch.WatchConfig.parse({"cameras": {"mmto": {"dir": "x"}}, "loop": 1})
    with pytest.raises(KeyError):
        watch.WatchConfig.parse({"cameras": {"nosuch": {"dir": "x"}}})


def test_write_atomic_replaces_whole(tmp_path):
    from robofast.allsky import watch

    p = tmp_path / "sub" / "latest.json"
    watch.write_atomic(p, "one")
    watch.write_atomic(p, "two")
    assert p.read_text() == "two"
    assert [q.name for q in p.parent.iterdir()] == ["latest.json"]


def test_watch_step_writes_latest_json_from_the_newest_frames(fake_sky, tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from robofast.allsky import cameras, imageio, watch

    cat, _ = fake_sky
    spec = cameras.CameraSpec(name="synth", title="", url="", width=400, height=400,
                              x0=200.0, y0=200.0, horizon_px=185.0)  # fmt: skip
    monkeypatch.setitem(cameras.CAMERAS, "synth", spec)
    model = CameraModel(camera="synth", width=400, height=400, x0=200.0, y0=200.0,
                        f1=185.0 / (math.pi / 2))  # fmt: skip
    model.sky = {"m0": 4.0, "a1": -0.3, "width": 0.3, "radius_px": 2.0}
    models = tmp_path / "models"
    models.mkdir()
    model.save(models / "synth.json")
    when = datetime(2026, 11, 22, 6, tzinfo=UTC)
    day = tmp_path / "synth" / "20261122"
    day.mkdir(parents=True)
    old = when - timedelta(minutes=30)
    for t in (old, when):
        img = np.clip(_render(model, cat, t), 0, 255).astype(np.uint8)
        imageio.write_png(day / f"synth_{t:%Y%m%dT%H%M%S}Z.png", img)
    assert watch.latest_frame(tmp_path / "synth", "synth").name == "synth_20261122T060000Z.png"
    out = tmp_path / "shadow" / "latest.json"
    cfg = watch.WatchConfig.parse(
        {
            "output": str(out),
            "history": str(tmp_path / "shadow" / "history.jsonl"),
            "models": str(models),
            "cameras": {"synth": {"dir": str(tmp_path / "synth")}},
        }
    )
    w = watch.Watcher(cfg)
    doc = w.step(now=when + timedelta(seconds=45))
    on_disk = json.loads(out.read_text())
    assert on_disk == json.loads(json.dumps(doc))
    assert on_disk["whole_sky_clear"] > 0.85 and on_disk["coverage"] > 0.3
    assert len(on_disk["cells"]) == 36
    assert on_disk["frames"] == [
        {"camera": "synth", "frame": "synth_20261122T060000Z.png", "age_s": 45.0, "used": True}
    ]
    assert on_disk["trend"]["whole_sky"]["state"] in ("unknown", "stable")
    # Much later: the camera is down; nothing is analysed, the file says so.
    doc = w.step(now=when + timedelta(hours=2))
    assert doc["whole_sky_clear"] is None and doc["frames"][0]["used"] is False
