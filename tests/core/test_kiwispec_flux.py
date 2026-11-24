"""KiwiSpec as MINERVA runs it now (docs/kiwispec.md, section 6): the flux
record from the FAU guide frames instead of the exposure meter, the raw-file
rewrite, SI Image restarts, the pressure warning, the chiller telemetry.

Fast tier: the spectrograph-PC simulator and the guider simulator on
loopback; nothing here talks to hardware.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from astropy.io import fits

from robofast.devices import kiwispec_fits as kf
from robofast.devices.kiwispec_aux import SimChiller, build_chiller
from robofast.devices.kiwispec_flux import FluxLog, fau_flux, parse_frames
from robofast.devices.kiwispec_service import KiwiSpecConfig, KiwiSpecServer
from robofast.devices.kiwispec_sim import KiwiSpecSim, SimConfig
from tests.core.test_kiwispec import FAST, run_job, service_config

ROOT = Path(__file__).parents[2]


@pytest.fixture
async def sim(tmp_path):
    s = await KiwiSpecSim(SimConfig(**FAST, data_dir=str(tmp_path / "pc"))).start()
    yield s
    await s.stop()


async def start_service(sim: KiwiSpecSim, **over) -> KiwiSpecServer:
    return await KiwiSpecServer(service_config(sim, **over)).start(status_loop=False)


@pytest.fixture
async def fau(sim):
    """The service as MINERVA configures it: FAU flux, no exposure meter."""
    server = await start_service(sim, flux_source="fau")
    async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
        yield server, c
    await server.stop()


# -- the FAU flux math --------------------------------------------------------------


def frames_for(rate, t0: float, t1: float, exp: float = 0.5, gap: float = 0.1):
    """Guide frames of ``exp`` s every ``exp + gap`` s, counts the integral of ``rate``."""
    out, t = [], t0
    while t + exp <= t1:
        n = 64
        dt = exp / n
        c = sum(rate(t + (i + 0.5) * dt) * dt for i in range(n))
        out.append((t, t + exp, c))
        t += exp + gap
    return out


def test_constant_rate_midpoint_is_the_middle():
    fr = frames_for(lambda t: 1000.0, 100.0, 200.0)
    r = fau_flux({"T1": fr}, 120.0, 180.0)
    assert r.midpoint == pytest.approx(150.0, abs=0.05)
    assert r.counts == pytest.approx(1000.0 * 60, rel=0.01)  # gaps bridged
    assert r.telescopes["T1"]["coverage"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("rate", "analytic"),
    [
        (lambda t: t, (2 / 3) * 60.0),  # rate grows linearly from 0 over [0, 60]
        (lambda t: 60.0 - t, 20.0),
        (
            lambda t: math.exp(-t / 20.0),
            20.0 - 60.0 * math.exp(-3.0) / (1 - math.exp(-3.0)),
        ),
    ],
)
def test_midpoint_matches_analytic(rate, analytic):
    fr = frames_for(rate, -5.0, 70.0, exp=0.2, gap=0.05)
    r = fau_flux({"T1": fr}, 0.0, 60.0)
    assert r.midpoint == pytest.approx(analytic, abs=0.15)


def test_telescopes_are_summed():
    """Two telescopes: one bright early, one late: the midpoint is weighted by both."""
    early = frames_for(lambda t: 3000.0 if t < 30 else 0.0, 0.0, 60.0)
    late = frames_for(lambda t: 1000.0 if t >= 30 else 0.0, 0.0, 60.0)
    r = fau_flux({"T1": early, "T2": late}, 0.0, 60.0)
    assert r.midpoint == pytest.approx((3 * 15 + 1 * 45) / 4, abs=0.3)
    assert r.counts == pytest.approx(3000 * 30 + 1000 * 30, rel=0.02)
    assert set(r.telescopes) == {"T1", "T2"}


def test_long_gaps_are_not_bridged_and_coverage_says_so():
    fr = frames_for(lambda t: 100.0, 0.0, 20.0) + frames_for(lambda t: 100.0, 50.0, 60.0)
    r = fau_flux({"T1": fr}, 0.0, 60.0, max_gap=5.0)
    assert r.telescopes["T1"]["coverage"] == pytest.approx(0.5, abs=0.03)
    assert fau_flux({}, 0.0, 60.0).midpoint is None


def test_overlapping_reports_are_not_counted_twice():
    fr = frames_for(lambda t: 100.0, 0.0, 10.0)
    r1 = fau_flux({"T1": fr}, 0.0, 10.0)
    r2 = fau_flux({"T1": fr + fr}, 0.0, 10.0)
    assert r2.counts == pytest.approx(r1.counts)


def test_parse_frames():
    assert parse_frames([[1, 2, 3], [2, 3, -4]]) == [(1.0, 2.0, 3.0), (2.0, 3.0, 0.0)]
    for bad in ([[2, 1, 3]], [[1, 2]], [[1, 2, "x"]], "no", [[1, 2, float("nan")]]):
        with pytest.raises(ValueError):
            parse_frames(bad)


def test_flux_log_ages_out():
    log = FluxLog(max_age=100.0)
    log.add("T1", [(0.0, 1.0, 5.0)], now=50.0)
    log.add("T1", [(200.0, 201.0, 5.0)], now=201.0)
    assert log.window(["T1"], 0.0, 300.0)["T1"] == [(200.0, 201.0, 5.0)]
    assert log.status()["T1"]["frames"] == 2


# -- the service with the FAU flux record ---------------------------------------------


async def report(http, tel: str, rate, t0: float, t1: float) -> None:
    r = await http.post(
        "/flux", json={"telescope": tel, "frames": frames_for(rate, t0, t1, 0.05, 0.01)}
    )
    assert r.status_code == 200, r.text


async def feed(http, tels: dict, stop: asyncio.Event, period: float = 0.1) -> None:
    """Guiders reporting every ``period`` s: each telescope's rate(t) in counts/s."""
    last = time.time()
    while not stop.is_set():
        await asyncio.sleep(period)
        now = time.time()
        for tel, rate in tels.items():
            await report(http, tel, rate, last, now)
        last = now


async def test_minerva_default_is_fau_and_never_touches_the_meter(sim):
    shipped = KiwiSpecConfig.from_dict(
        json.loads((ROOT / "sites/minerva/kiwispec.example.json").read_text())
    )
    assert shipped.flux_source == "fau" and shipped.pressure_alarm == "warning"
    assert shipped.max_spec_pressure == 0.006 and shipped.pc_utc_offset_hours == -7.0
    assert shipped.telescopes == ["T1", "T2", "T3", "T4"]
    base = {"spectrograph": {"host": "h"}, "camera": {"host": "h"}, "telescopes": ["T1"]}
    assert KiwiSpecConfig.from_dict(base).flux_source == "fau"
    server = await start_service(sim, flux_source="fau")
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as http:
            await http.post("/backlight", json={"on": True})
            await http.post("/backlight", json={"on": False})
            job = await run_job(
                http,
                "/expose",
                {"kind": "science", "exptime": 0.4, "telescopes": ["T1"], "object": "x"},
            )
            status = (await http.get("/status", params={"refresh": 1})).json()
    finally:
        await server.stop()
    meter = {
        "get_expmeter_total",
        "reset_expmeter_total",
        "stop_log_expmeter",
        "start_log_expmeter",
    }
    assert not meter & set(sim.commands), sim.commands
    r = job["results"][0]
    assert job["state"] == "done" and r["ended_by"] == "time"
    # no guider reported: no flux midpoint; the barycentric correction will
    # use the middle of the exposure
    assert r["flux_midpoint_utc"] is None and r["midpoint_source"] == "geometric"
    assert any("geometric midpoint" in w for w in r["warnings"])
    assert "expmeter_total" not in status["readings"]
    assert status["flux_source"] == "fau"


async def test_fau_flux_weighted_midpoint_over_telescopes(fau, sim):
    _, http = fau
    stop = asyncio.Event()
    t_start = time.time()
    # T1 brightening, T2 constant, T3 reporting but not feeding this spectrum
    tels = {
        "T1": lambda t: 2000.0 * max(0.0, t - t_start),
        "T2": lambda t: 500.0,
        "T3": lambda t: 1e9,
    }
    feeder = asyncio.create_task(feed(http, tels, stop))
    try:
        job = await run_job(
            http,
            "/expose",
            {"kind": "science", "exptime": 1.5, "telescopes": ["T1", "T2"], "object": "x"},
        )
    finally:
        stop.set()
        await feeder
    r = job["results"][0]
    assert job["state"] == "done", job
    assert r["flux_source"] == "fau" and r["midpoint_source"] == "fau"
    assert set(r["fau"]) == {"T1", "T2"}  # T3 did not feed it
    assert r["fau"]["T1"]["counts"] > r["fau"]["T2"]["counts"] > 0
    # Weighted toward the end by T1's ramp: later than the middle.
    assert r["flux_midpoint_s"] > r["exptime"] / 2 + 0.05
    assert r["flux_midpoint_s"] < r["exptime"]
    assert r["fau_counts"] == pytest.approx(r["fau"]["T1"]["counts"] + r["fau"]["T2"]["counts"])


async def test_fau_target_counts_terminate(fau, sim):
    _, http = fau
    stop = asyncio.Event()
    feeder = asyncio.create_task(feed(http, {"T1": lambda t: 1000.0}, stop))
    try:
        job = await run_job(
            http,
            "/expose",
            {
                "kind": "science",
                "exptime": 10.0,
                "telescopes": ["T1"],
                "target_counts": 400,
                "object": "x",
            },
        )
    finally:
        stop.set()
        await feeder
    r = job["results"][0]
    assert r["ended_by"] == "counts", job
    assert r["exptime"] < 2.0
    assert r["fau_counts"] >= 300


async def test_flux_source_none_runs_fixed_time(sim):
    server = await start_service(sim, flux_source="none")
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as http:
            job = await run_job(
                http,
                "/expose",
                {
                    "kind": "science",
                    "exptime": 0.3,
                    "telescopes": ["T1"],
                    "target_counts": 5,
                    "object": "x",
                },
            )
    finally:
        await server.stop()
    r = job["results"][0]
    assert r["ended_by"] == "time" and r["midpoint_source"] == "geometric"
    assert any("target_counts ignored" in w for w in r["warnings"])


async def test_flux_report_validation(fau):
    _, http = fau
    r = await http.post("/flux", json={"telescope": "T9", "frames": []})
    assert r.status_code == 400
    r = await http.post("/flux", json={"telescope": "T1", "frames": [[2, 1, 3]]})
    assert r.status_code == 400
    r = await http.post("/flux", json={"telescope": "T1", "frames": [[1, 2, 3]]})
    assert r.json() == {"telescope": "T1", "accepted": 1, "kept": 0}  # too old: aged out
    with pytest.raises(ValueError, match="flux_source"):
        KiwiSpecConfig.from_dict(
            {
                "spectrograph": {"host": "h"},
                "camera": {"host": "h"},
                "telescopes": ["T1"],
                "flux_source": "pmt",
            }
        )


def test_sidecar_midpoint_source_follows_the_flux_source(tmp_path):
    from robofast.spectra import SpectrumRecorder
    from tests.core.test_rv_spectra import MTHOPKINS, TARGET

    store = SpectrumRecorder(tmp_path, "minerva", MTHOPKINS, "kiwispec")
    rec = {
        "kind": "science",
        "target": TARGET,
        "start_utc": "2026-11-10T05:00:00+00:00",
        "exptime": 600.0,
        "flux_midpoint_utc": "2026-11-10T05:04:00+00:00",
        "flux_source": "fau",
    }
    assert store.correct(rec)["midpoint_source"] == "fau"
    rec.pop("flux_midpoint_utc")
    assert store.correct(rec)["midpoint_source"] == "geometric"


# -- the raw file ----------------------------------------------------------------------


async def test_raw_file_is_made_legal_in_place(sim, tmp_path):
    server = await start_service(sim, raw_fits={"dir": str(tmp_path / "pc"), "wait": 2})
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as http:
            job = await run_job(
                http,
                "/expose",
                {"kind": "bias", "night": "n20261109"},
            )
            sci = await run_job(
                http,
                "/expose",
                {
                    "kind": "science",
                    "exptime": 0.2,
                    "object": "HD10700",
                    "telescopes": ["T2"],
                    "night": "n20261109",
                    "targets": {"T2": {"ra": 26.0175, "dec": -15.9375, "pm_ra": -1729.7}},
                },
            )
    finally:
        await server.stop()
    # The new keywords survive into the legal file, cards and comments
    # whole (specfast 2026-11-23).
    assert sci["state"] == "done", sci
    with fits.open(tmp_path / "pc" / "n20261109" / sci["results"][0]["file"]) as hdul:
        h = hdul[0].header
        assert h["SIMULATE"] is True
        assert h.comments["SIMULATE"] == "Simulated spectrograph: no light"
        assert h["SPECPRES"] == pytest.approx(0.002)
        assert h.comments["SPECPRES"] == "Spectrograph pressure (mbar)"
        assert h["TARGRA2"] == pytest.approx(26.0175) and h["TARGDEC2"] == -15.9375
        assert h.comments["TARGRA2"] == "Target RA (ICRS deg at TARGEPO2)"
        assert h["PMRA2"] == -1729.7 and h["PMDEC2"] == "UNKNOWN"
        assert h.comments["TARGEPO2"] == "Epoch of TARGRA2/TARGDEC2 (Julian yr)"
        assert "TARGRA1" not in h
    r = job["results"][0]
    assert job["state"] == "done", job
    fits_info = r["fits"]
    assert fits_info["rewritten"] is True, fits_info
    path = tmp_path / "pc" / "n20261109" / r["file"]
    # One copy at rest of each frame.
    assert sorted(p.name for p in path.parent.iterdir()) == sorted(
        [r["file"], sci["results"][0]["file"]]
    )
    with fits.open(path, checksum=True) as hdul:
        hdul.verify("exception")
        data = hdul[0].data
        h = hdul[0].header
        assert np.array_equal(data, sim.files[r["file"]]["pixels"])
        assert h["DATASUM"] == fits_info["datasum"]
        assert h["OBJECT"] == "Bias" and h["FLUXSRC"] == "none"
        assert "PARAM0" not in h
        assert h["SIMULATE"] is True and h["SPECPRES"] == pytest.approx(0.002)
    assert kf.pixel_sha256(data) == fits_info["pixel_sha256"]


def si_file(tmp_path, **cards) -> tuple[Path, np.ndarray]:
    px = np.random.default_rng(1).integers(0, 65536, size=(20, 30), dtype=np.uint16)
    px[0, :3] = [0, 32768, 65535]
    path = tmp_path / "n20261109.x.0001.fits"
    kf.write_si_raw(
        path,
        px,
        {"OBJECT": "x", "DATE-OBS": ("2026-11-09T20:00:00.250", kf.SI_DATE_COMMENT), **cards},
    )
    return path, px


def test_standardize_converts_and_verifies(tmp_path):
    path, px = si_file(tmp_path)
    out = kf.standardize(path, utc_offset_hours=-7.0)
    assert out["rewritten"] and out["shape"] == [20, 30]
    with fits.open(path, checksum=True) as h:
        assert np.array_equal(h[0].data, px) and h[0].data.dtype == np.uint16
        assert h[0].header["DATE-OBS"] == "2026-11-10T03:00:00.250"  # MST + 7 h
        assert h[0].header["RAWSHA"] == out["pixel_sha256"]
    again = kf.standardize(path)
    assert again["rewritten"] is False and again["pixel_sha256"] == out["pixel_sha256"]
    assert list(tmp_path.iterdir()) == [path]


def _unchanged(path: Path, before: bytes) -> None:
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]  # no temporary left behind


def test_standardize_leaves_the_raw_file_on_a_bad_readback(tmp_path, monkeypatch):
    path, _ = si_file(tmp_path)
    before = path.read_bytes()
    real_open = fits.open

    def corrupting_open(*a, **kw):
        hdul = real_open(*a, **kw)
        hdul[0].data[0, 0] ^= 1  # a flipped bit on the way back
        return hdul

    monkeypatch.setattr(fits, "open", corrupting_open)
    with pytest.raises(kf.FitsRewriteError, match="differ"):
        kf.standardize(path)
    _unchanged(path, before)


def test_standardize_leaves_a_file_that_changed_meanwhile(tmp_path, monkeypatch):
    path, _ = si_file(tmp_path)
    before = path.read_bytes()
    stamps = iter([(1, 1), (2, 2)])
    monkeypatch.setattr(kf, "_stamp", lambda p: next(stamps))
    with pytest.raises(kf.FitsRewriteError, match="changed"):
        kf.standardize(path)
    _unchanged(path, before)


@pytest.mark.parametrize("damage", ["truncate", "no_end", "si_header"])
def test_standardize_refuses_damaged_files(tmp_path, damage):
    path, _ = si_file(tmp_path, **({"PARAM0": 1} if damage == "si_header" else {}))
    raw = path.read_bytes()
    if damage == "truncate":
        path.write_bytes(raw[: kf.BLOCK + 100])
    elif damage == "no_end":
        path.write_bytes(raw.replace(b"END" + b" " * 77, b" " * 80))
    before = path.read_bytes()
    with pytest.raises(kf.FitsRewriteError):
        kf.standardize(path)
    _unchanged(path, before)


async def test_rewrite_failure_alarms_and_keeps_the_raw_file(sim, tmp_path, monkeypatch):
    def broken(path, **kw):
        raise kf.FitsRewriteError("simulated verification failure")

    monkeypatch.setattr("robofast.devices.kiwispec_service.standardize", broken)
    server = await start_service(sim, raw_fits={"dir": str(tmp_path / "pc"), "wait": 2})
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as http:
            job = await run_job(http, "/expose", {"kind": "bias", "night": "n20261109"})
            status = (await http.get("/status")).json()
    finally:
        await server.stop()
    r = job["results"][0]
    assert job["state"] == "done"  # the frame is safe on disk, as written
    assert r["fits"]["rewritten"] is False
    assert any("left untouched" in a for a in status["alarms"])
    raw = (tmp_path / "pc" / "n20261109" / r["file"]).read_bytes()
    assert b"SIMPLE  =                    F" in raw[:80]


# -- SI Image restarts ------------------------------------------------------------------


async def test_wedged_camera_is_restarted_at_once(sim):
    server = await start_service(sim, camera_restart_after=2, camera_restart_timeout=5.0)
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=20) as http:
            sim.set_fault("camera_wedged")
            for _ in range(2):
                job = await run_job(http, "/expose", {"kind": "bias"}, wait=20)
                assert job["state"] == "failed", job
            # The job has ended; the spectrograph is held until SI Image is back.
            for _ in range(100):
                status = (await http.get("/status")).json()
                if status["busy"] is None:
                    break
                assert status["busy"] == "restarting SI Image"
                await asyncio.sleep(0.05)
            assert sim.si_kills == 1 and sim.si_starts == 1
            assert sim.cooler_on
            assert status["camera_restarts"][-1]["ok"] is True
            assert status["camera_restarts"][-1]["seconds"] < 5
            job = await run_job(http, "/expose", {"kind": "bias"})
            assert job["state"] == "done", job
    finally:
        await server.stop()


async def test_no_automatic_restart_when_turned_off(sim):
    server = await start_service(sim, camera_restart=False)
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=20) as http:
            sim.set_fault("camera_wedged")
            for _ in range(3):
                await run_job(http, "/expose", {"kind": "bias"}, wait=20)
            assert sim.si_kills == 0
            r = await http.post("/restart_camera", json={"reason": "operator"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert sim.si_kills == 1 and sim.si_starts == 1
    finally:
        await server.stop()


# -- vacuum, chiller, heater --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["alarm", "warning"])
async def test_pressure_alarm_can_be_a_warning(sim, mode):
    sim.config.spec_pressure = 0.5  # isolated from the pump
    server = await start_service(sim, pressure_alarm=mode, max_spec_pressure=0.01)
    try:
        await server.kiwispec.refresh()
        s = server.kiwispec.status()
    finally:
        await server.stop()
    hit = [m for m in s["alarms" if mode == "alarm" else "warnings"] if "pressure" in m]
    assert hit and "0.01" in hit[0]
    other = s["warnings" if mode == "alarm" else "alarms"]
    assert not [m for m in other if "pressure" in m]


async def test_chiller_telemetry(sim):
    server = await start_service(sim, chiller={"kind": "sim", "setpoint": 15.0, "tolerance": 0.5})
    k = server.kiwispec
    try:
        await k.refresh()
        assert k.readings["chiller"]["value"]["temp"] == 15.0
        assert not [m for m in k.status()["alarms"] + k.status()["warnings"] if "chiller" in m]
        assert isinstance(k.chiller, SimChiller)
        k.chiller.temp = 17.0
        await k.refresh()
        assert any("chiller at 17.0" in w for w in k.status()["warnings"])
        k.chiller.fault = "low flow"
        await k.refresh()
        assert any("chiller fault: low flow" in a for a in k.status()["alarms"])
    finally:
        await server.stop()


async def test_a_chiller_without_a_driver_only_warns(sim):
    server = await start_service(sim, chiller={"kind": "oasis3", "port": "COM9"})
    try:
        await server.kiwispec.refresh()
        s = server.kiwispec.status()
    finally:
        await server.stop()
    assert any("oasis3 chiller is read through the aux server" in w for w in s["warnings"])
    assert not [a for a in s["alarms"] if "chiller" in a]
    with pytest.raises(ValueError, match="chiller kind"):
        build_chiller({"kind": "nope"})


async def test_heater_and_backlight_state(fau, sim):
    _, http = fau
    r = await http.post("/configure", json={"heater": True, "heater_setpoint": 55.0})
    assert r.status_code == 200
    await http.post("/backlight", json={"on": True})
    assert sim.backlight_arm == "in"
    s = (await http.get("/status", params={"refresh": 1})).json()
    assert s["heater_on"] is True and s["backlight_on"] is True
    assert s["readings"]["heater_temp"]["value"] == pytest.approx(55.0, abs=1)
    await http.post("/backlight", json={"on": False})
    assert sim.backlight_arm == "out"


# -- MINERVA's nightly plans take no lamp frames ---------------------------------------


def test_no_minerva_plan_expects_lamp_frames():
    """ThAr and fiber flats were only ever taken by hand, and the slit-flat
    LED is dead: nothing MINERVA ships asks for them."""
    banned_kinds = {"thar", "fiberflat", "slitflat"}
    banned_types = {"ARC", "LAMP_FLAT"}

    def walk(x, where):
        if isinstance(x, dict):
            assert x.get("kind") not in banned_kinds, where
            assert x.get("type") not in banned_types, where
            for v in x.values():
                walk(v, where)
        elif isinstance(x, list):
            for v in x:
                walk(v, where)

    for path in sorted((ROOT / "sites/minerva").rglob("*.json")):
        walk(json.loads(path.read_text()), path)
