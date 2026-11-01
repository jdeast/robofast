"""The throughput monitor (robofast_core/throughput.py, docs/throughput.md).

A year of zero points with a known history (robofast_core/testing/throughput_sim.py:
the port's zero point times an injected throughput -- a slow decline, a
cleaning, a sudden loss -- times random clouds, less extinction): the
monitor recovers the trend, alarms when the clear-night envelope is 10 %
below the baseline, and resets the baseline at the cleaning. One path runs
through real rendered and plate-solved frames (the sky simulator, the
frame solver, the frame index) and one through a legacy MINERVA-style FITS
file solved by the backfill.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pytest
from astropy.io import fits

from robofast_core import throughput as tp
from robofast_core.photometry import PortPhotometry, SkyConditions
from robofast_core.testing.throughput_sim import History, Weather, simulate

#: T2's FAU as sky.example.json has it (the efficiency chain; G band).
FAU = PortPhotometry(
    band="G",
    efficiency={"mirrors": 0.73, "optics": 0.9, "qe": 0.6, "beamsplitter": 0.02},
    extinction=0.15,
)
#: The scenario: 2.5 % lost a month, a cleaning on night 180 back to 98 %, a
#: week of patchy cloud just before the alarm is due.
HISTORY = History(decline_per_30d=0.025, restores={180: 0.98})
WEATHER = Weather(runs=[(118, 6)])
NIGHTS = 280


def config(**kw) -> tp.ThroughputConfig:
    ports = {"t2/fau": {**FAU.__dict__, "efficiency": dict(FAU.efficiency)}}
    return tp.ThroughputConfig.from_dict({"ports": ports, **kw})


def series(records, **kw):
    return tp.analyze(records, config(**kw))["series"]["t2/fau/-"]


def index(truth):
    return {t["night"]: t["i"] for t in truth}


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_injected_trend_recovered(seed):
    recs, truth = simulate(FAU, HISTORY, WEATHER, nights=NIGHTS, seed=seed)
    s = series(recs)
    idx = index(truth)
    # The baseline: the first 30 days' trend at the first night (n0's truth).
    first = s["nights"][0]["night"]
    base_true = HISTORY(idx[first])
    # When the injected throughput fell 10 % below it.
    cross = next(i for i in range(NIGHTS) if HISTORY(i) / base_true <= 0.9)
    assert 120 < cross < 135
    # One alarm, raised near the crossing, cleared by the cleaning.
    drops = [a for a in s["alarms"] if a["kind"] == "drop"]
    assert len(drops) == 1, s["alarms"]
    a = drops[0]
    assert abs(idx[a["start"]] - cross) <= 20, (a, cross)
    assert a["cleared"] == "step up: baseline reset"
    # Exactly one step: the cleaning, dated at the first clear night after
    # it (the weather permitting) and found the next steady night.
    assert [e["what"] for e in s["events"]] == ["step_up"], s["events"]
    up = s["events"][0]
    clear = next(t["i"] for t in truth[180:] if t["weather"] == "clear")
    assert 180 <= idx[up["night"]] <= clear + 3, (up, clear)
    assert idx[up["detected_on"]] - idx[up["night"]] <= 6
    # Its size (+14 %), roughly: post-cleaning cirrus can pull the old trend up.
    assert up["change"] == pytest.approx(0.98 / HISTORY(179) - 1.0, abs=0.07)
    # The level follows the injected throughput (relative to the baseline)
    # to about 1 %, through the clouds, before and after the cleaning.
    errs = []
    for n in s["nights"]:
        i = idx[n["night"]]
        if n["change"] is None or i in range(176, 215):
            continue
        ref = idx[first] if i < 180 else idx[up["night"]]
        errs.append(n["change"] - (HISTORY(i) / HISTORY(ref) - 1.0))
    errs = np.array(errs)
    assert abs(errs.mean()) < 0.01 and errs.std() < 0.01, (errs.mean(), errs.std())
    # The current slope (2.5 % a month) and no alarm (the truth is -7 %).
    st = s["status"]
    assert st["slope_per_30d"] == pytest.approx(-0.025, abs=0.01)
    assert not st["alarm"]
    assert st["change"] == pytest.approx(HISTORY(NIGHTS - 1) / 0.98 - 1, abs=0.02)
    # Clouds are flagged in the last window, clear nights are not.
    w = {t["night"]: t["weather"] for t in truth}
    flagged = [(w[n["night"]], n["cloudy"]) for n in s["nights"] if n["cloudy"] is not None]
    assert all(c for k, c in flagged if k == "patchy")
    clear = [c for k, c in flagged if k == "clear"]
    assert sum(clear) <= 0.1 * len(clear)  # a few at the 2.5-sigma clip


def test_efficiency_against_the_optics():
    """Against the efficiency chain the ratio is the injected throughput (the
    envelope sits ~1.3 sigma of the frame noise above the clear level: the
    80th percentile of a night)."""
    recs, _ = simulate(FAU, History(decline_per_30d=0.0, start=0.8), WEATHER, nights=60)
    st = series(recs)["status"]
    assert st["efficiency"] == pytest.approx(0.8, rel=0.04)
    assert abs(st["slope_per_30d"]) < 0.01 and st["baseline"] is not None


def test_sudden_loss_alarms_and_recovers():
    """A cover or dew: 25 % gone for two weeks. A step down (an alarm at
    once), then back to the old trend: 'recovered', the baseline kept."""
    hist = History(decline_per_30d=0.01, losses=[(100, 115, 0.75)])
    recs, truth = simulate(FAU, hist, Weather(), nights=160, seed=5)
    s = series(recs)
    idx = index(truth)
    whats = [(e["what"], idx[e["night"]]) for e in s["events"]]
    assert [w for w, _ in whats] == ["step_down", "recovered"], whats
    assert 98 <= whats[0][1] <= 110 and 115 <= whats[1][1] <= 125
    down = next(a for a in s["alarms"] if a["kind"] == "step_down")
    assert idx[down["raised_on"]] <= 112 and down["cleared"] == "recovered"
    assert not s["status"]["alarm"] and "step_down" not in s["status"]
    assert s["status"]["baseline_from"].startswith(s["nights"][0]["night"])


def test_permanent_loss_keeps_alarming():
    hist = History(decline_per_30d=0.01, losses=[(100, 999, 0.8)])
    recs, _ = simulate(FAU, hist, Weather(), nights=160, seed=4)
    s = series(recs)
    st = s["status"]
    assert st["alarm"] and st["step_down"]
    assert st["change"] == pytest.approx(hist(159) / hist(0) - 1, abs=0.03)
    doc = tp.status_document(tp.analyze(recs, config()))
    texts = {a["text"]: a["level"] for a in doc["alarms"]}
    assert texts["t2/fau/-: throughput below baseline by more than its limit"] == "warning"
    assert any(t.startswith("t2/fau/-: sudden throughput drop") and lv == "alarm"
               for t, lv in texts.items())  # fmt: skip


def test_annotation_resets_the_baseline(tmp_path):
    """A cleaning a person records: the baseline restarts there (no step is
    'detected' on top of it), and the annotation is in the events."""
    recs, truth = simulate(FAU, HISTORY, WEATHER, nights=NIGHTS, seed=2)
    store = tp.ThroughputStore(tmp_path / "t.jsonl")
    assert store.append(recs) == len(recs)
    assert store.append(recs[:10]) == 0  # zero points are stored once
    tp.annotate(store, telescope="T2", night=truth[180]["night"], what="cleaning", note="CO2 snow")
    s = tp.analyze(store.read(), config())["series"]["t2/fau/-"]
    assert [(e["what"], e["source"]) for e in s["events"]] == [("cleaning", "annotation")]
    assert s["events"][0]["night"] >= truth[180]["night"]
    assert s["alarms"][0]["cleared"] == "cleaning (annotated)"
    # A cleaning of another telescope does not touch this one.
    tp.annotate(store, telescope="t1", night=truth[100]["night"], what="cleaning")
    assert tp.analyze(store.read(), config())["series"]["t2/fau/-"]["events"] == s["events"]


def test_extinction_fit():
    sim = replace(FAU, extinction=0.22)
    recs, _ = simulate(sim, History(decline_per_30d=0.0), Weather(), nights=90, seed=5)
    fixed = series(recs)
    fitted = series(recs, fit_extinction=True)
    assert fitted["extinction"]["source"] == "fitted"
    assert fitted["extinction"]["k"] == pytest.approx(0.22, abs=0.01)
    # With the wrong k the efficiency is low by ~ dk * <X>; fitted it is right.
    assert fitted["status"]["efficiency"] == pytest.approx(1.0, abs=0.04)
    assert fixed["status"]["efficiency"] < 0.95
    direct = tp.fit_extinction([r for r in recs if r["kind"] == "zp"])
    assert direct is not None and direct["nights"] > 30


def test_config_validation(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        tp.ThroughputConfig.from_dict({"alarm": 0.1})
    with pytest.raises(ValueError, match="fraction"):
        tp.ThroughputConfig.from_dict({"alarm_drop": 10})
    with pytest.raises(ValueError, match="expected"):
        tp.ThroughputConfig.from_dict({"expected": "model"})
    c = config(extinction={"default": 0.1, "V": 0.2, "t2/fau": 0.12})
    assert c.extinction_for("t2/fau/-") == 0.12
    assert c.extinction_for("t1/imager/V") == 0.2
    assert c.extinction_for("t1/imager/gp") == 0.1
    assert config().extinction_for("t9/x/-") == 0.15
    # ADU/s through the port's gain (e- per ADU).
    fau2 = {**FAU.__dict__, "gain": 2.0, "efficiency": dict(FAU.efficiency)}
    g = tp.ThroughputConfig.from_dict({"ports": {"fau": fau2}})
    assert g.expected_zp("t1", "fau", "ADU/s") == pytest.approx(
        FAU.zp_estimated - 2.5 * math.log10(2.0)
    )


# -- through the files: the frame index, the console, the morning report --------------


def _write_index(root, night, frames):
    d = root / "minerva" / "t2" / night
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, f in enumerate(frames):
        lines.append(json.dumps({
            "file": f"minerva/t2/{night}/{night}.t2.x.{k:04d}.fits", "telescope": "t2",
            "imagetyp": "LIGHT", "date_obs": f["t"], "exptime": 10.0, "filter": None,
            "solve": {"solved": True, "port": "fau", "context": {"temperature": 8.5},
                      "zp": {"zp": f["zp"], "unit": "e-/s", "n": 9, "error": 0.01,
                             "scatter": 0.05, "airmass": f["airmass"]}},
        }))  # fmt: skip
    lines.append(json.dumps({"telescope": "t2", "imagetyp": "BIAS", "exptime": 0.0}))
    (d / f"{night}.t2.frames.jsonl").write_text("\n".join(lines) + "\n")


def test_update_console_and_morning_report(tmp_path):
    """Months of history in the store, then tonight's frame index: update
    ingests it, raises the alarm on the night it starts, writes the status
    file; the console shows the series and the alarm; the morning report
    has the section and the QA line."""
    from robofast_core.console.aggregate import Aggregator, ConsoleEnclosure
    from robofast_core.morning import main as morning_main

    hist = History(decline_per_30d=0.04)
    recs, _ = simulate(FAU, hist, Weather(runs=[]), nights=200, seed=3)
    s = series(recs)
    start = s["alarms"][0]["raised_on"]
    # Store everything before the alarm night; the alarm night comes as an index.
    before = [r for r in recs if r["night"] < start]
    tonight = [r for r in recs if r["night"] == start]
    data = tmp_path / "data"
    _write_index(data, start, tonight)
    cfgd = {
        "store": str(tmp_path / "tp" / "throughput.jsonl"),
        "status": str(tmp_path / "tp" / "status.json"),
        "ports": {"t2/fau": {**FAU.__dict__, "efficiency": dict(FAU.efficiency)}},
    }
    cpath = tmp_path / "throughput.json"
    cpath.write_text(json.dumps(cfgd))
    cfg = tp.ThroughputConfig.load(cpath)
    tp.ThroughputStore(cfg.store).append(before)
    # The night before: no alarm yet.
    prev = tp.update(cfg, before[-1]["night"], data)
    assert prev.added == 0 and prev.new_alarms == []
    upd = tp.update(cfg, start, data)
    assert upd.added == len(tonight)
    assert [a["kind"] for a in upd.new_alarms] == ["drop"]
    stored = [r for r in tp.ThroughputStore(cfg.store).read() if r["night"] == start]
    assert stored[0]["port"] == "fau" and stored[0]["context"] == {"temperature": 8.5}
    status = json.loads(cfg.status.read_text())
    assert status["series"]["t2/fau/-"]["alarm"] and status["alarms"]

    # The console.
    url = "http://127.0.0.1:1"
    enc = ConsoleEnclosure("aqawan1", url, ["t1", "t2"], throughput_status=cfg.status)
    alarms = []
    view = Aggregator._throughput_view(enc, lambda *a: alarms.append(a))
    assert list(view["series"]) == ["t2/fau/-"]
    assert alarms == [("throughput", "t2/fau/-: throughput below baseline by more than its limit",
                       "warning", status["alarms"][0]["detail"])]  # fmt: skip
    other = ConsoleEnclosure("aqawan2", url, ["t3"], throughput_status=cfg.status)
    alarms.clear()
    assert Aggregator._throughput_view(other, lambda *a: alarms.append(a))["series"] == {}
    assert alarms == []
    missing = ConsoleEnclosure("x", url, ["t2"], throughput_status=tmp_path / "no")
    assert Aggregator._throughput_view(missing, lambda *a: None)["detail"] == "no status yet"

    # The morning report (it runs update itself: nothing new to ingest).
    out = tmp_path / "reports"
    assert morning_main(["--night", start, "--data-root", str(data), "--out", str(out),
                         "--throughput", str(cpath)]) == 0  # fmt: skip
    md = (out / f"report-{start}.md").read_text()
    rep = json.loads((out / f"report-{start}.json").read_text())
    assert "## Throughput" in md and "t2/fau/-" in md
    assert any("throughput" in x and "below its baseline" in x for x in rep["qa"]["look_at"])
    assert rep["throughput"]["series"]["t2/fau/-"]["tonight"]["night"] == start


def test_cli_status_annotate_extinction(tmp_path, capsys):
    recs, _ = simulate(FAU, HISTORY, WEATHER, nights=120, seed=1)
    store = tmp_path / "t.jsonl"
    tp.ThroughputStore(store).append(recs)
    cpath = tmp_path / "c.json"
    ports = {"t2/fau": {**FAU.__dict__, "efficiency": dict(FAU.efficiency)}}
    cpath.write_text(json.dumps({"store": "t.jsonl", "ports": ports}))
    assert tp.main(["--config", str(cpath), "status"]) == 0
    assert "t2/fau/-" in capsys.readouterr().out
    assert tp.main(["--config", str(cpath), "annotate", "--telescope", "t2", "--port", "fau",
                    "--night", "2026-03-01", "--what", "cleaning"]) == 0  # fmt: skip
    capsys.readouterr()
    assert tp.main(["--config", str(cpath), "status", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["series"]["t2/fau/-"]["events"][0]["what"] == "cleaning"
    assert tp.main(["--config", str(cpath), "extinction"]) == 0
    assert "fitted" in capsys.readouterr().out


# -- real pixels: the sky simulator, the frame solver, the backfill --------------------

HD185144 = (293.2035, 69.6611)


@pytest.fixture(scope="module")
def sky():
    from robofast_core.catalog import catalog_at
    from tests.core.skydata import sky_dir

    return catalog_at(sky_dir())


def _scene(catalog, thr: float):
    from robofast_core.testing.skysim import SkyCamera, SkyScene
    from robofast_core.testing.starfield import Detector

    phot = PortPhotometry(band="V", efficiency={"mirrors": 0.73, "qe": 0.8, "dust": thr},
                          gain=1.3, read_noise=6.0, extinction=0.15)  # fmt: skip
    det = Detector(shape=(1024, 1024), gain=1.3, read_noise=6.0, bias=500.0)
    cam = SkyCamera((1024, 1024), 0.61, detector=det)
    return SkyScene(catalog, cam, phot, conditions=SkyConditions())


def _frame_info(scene, when):
    from robofast_core.fits_writer import FrameInfo, Optics
    from robofast_core.registry import ValueRegistry

    ra, dec = HD185144
    reg = ValueRegistry()
    for name, v, kw in (
        ("t1.tel.ra", ra / 15.0, None),
        ("t1.tel.dec", dec, None),
        ("t1.tel.alt", scene.altitude(ra, dec, when), "ALT"),
        ("t1.camera.last_start", when.isoformat(), None),
        ("t1.camera.last_duration", 10.0, None),
        ("t1.camera.egain", 1.3, "EGAIN"),
        ("t1.camera.max_adu", 65535, "MAXADU"),
        ("t1.focuser.temperature", 6.5, "FOCTEMP"),
    ):
        reg.publish(name, v, max_age=None, keyword=kw)
    snap = reg.snapshot()
    return FrameInfo(
        telescope="t1", object="HD185144", obs_name="obs", imagetyp="LIGHT", light=True,
        exptime_requested=10.0, host_start=when, index=0, target=(ra / 15.0, dec),
        start=snap, end=snap, optics=Optics(plate_scale=0.61), filter="V", port="imager",
    )  # fmt: skip


def test_rendered_frames_to_efficiency(sky, tmp_path):
    """Frames rendered with an injected throughput (100, 90, 80 %), solved
    and measured as they are written, ingested from the frame index: the
    efficiency against the optics' chain is the injected throughput (times
    the aperture loss), and the per-night ratios match to ~2 %."""
    from robofast_core.astrometry import SolverConfig
    from robofast_core.fits_writer import FrameStore
    from robofast_core.framesolve import FrameSolver
    from robofast_core.imagebytes import ImageArray

    solver = FrameSolver(sky, {"t1": SolverConfig(0.61, search_radius=0.1)})
    store = FrameStore(tmp_path / "data", "test", solver=solver)
    nights = []
    for day, thr in ((23, 1.0), (24, 0.9), (25, 0.8)):
        sc = _scene(sky, thr)
        for hour in (4, 8, 11):  # three airmasses
            when = datetime(2026, 4, day, hour, 0, tzinfo=UTC)
            frame = sc.render(*HD185144, when, 10.0)
            data = np.ascontiguousarray(frame.image.T.astype("<i4"))
            w = store.write(_frame_info(sc, when), ImageArray(data, 2, "t", data.nbytes))
            assert w.record["solve"]["context"]["focus_temp"] == 6.5
        nights.append(f"n{w.night:%Y%m%d}")
    phot = _scene(sky, 1.0).photometry
    cfg = tp.ThroughputConfig.from_dict(
        {"store": str(tmp_path / "t.jsonl"),
         "ports": {"t1/imager": {**phot.__dict__, "efficiency": {"mirrors": 0.73, "qe": 0.8}}}}
    )  # fmt: skip
    st = tp.ThroughputStore(cfg.store)
    for n in nights:
        assert tp.ingest_night(st, tmp_path / "data", n) == 3
    s = tp.analyze(st.read(), cfg)["series"]["t1/imager/V"]
    eff = [n["efficiency"] for n in s["nights"]]
    assert eff[0] == pytest.approx(1.0, abs=0.06)  # aperture loss + the 80th percentile
    assert eff[1] / eff[0] == pytest.approx(0.9, abs=0.02)
    assert eff[2] / eff[0] == pytest.approx(0.8, abs=0.02)


def test_backfill_legacy_fits(sky, tmp_path):
    """A legacy-style frame (TELESCOP 'T2', PORT 1, JNow-free RA/Dec here,
    no ZPMAG, the spectrograph's GAIN): the backfill solves it, measures the
    zero point in ADU/s, maps port 1 to T2's FAU and keeps the conditions;
    a robofast frame with ZPMAG is taken as it is."""
    sc = _scene(sky, 1.0)
    when = datetime(2026, 4, 23, 9, 0, tzinfo=UTC)
    frame = sc.render(*HD185144, when, 10.0)
    alt = sc.altitude(*HD185144, when)
    h = fits.Header()
    for k, v in (("TELESCOP", "T2"), ("PORT", 1), ("EXPTIME", 10.0), ("GAIN", 1.3),
                 ("DATE-OBS", when.isoformat()[:19]), ("RA", HD185144[0]), ("DEC", HD185144[1]),
                 ("AIRMASS", 1 / math.sin(math.radians(alt))), ("IMAGETYP", "UNKNOWN"),
                 ("OUTTEMP", 10.8), ("CCD-TEMP", "UNKNOWN"), ("MOONDIS", 72.0)):  # fmt: skip
        h[k] = v
    legacy = tmp_path / "n20260422.T2.FAU.autofocus.0003.fits"
    fits.PrimaryHDU(frame.image.astype(np.int32), h).writeto(legacy)
    h2 = fits.Header({"TELESCOP": "t1", "PORT": "imager", "FILTER": "V", "AIRMASS": 1.2,
                      "DATE-OBS": "2026-04-23T05:00:00", "NIGHT": "n20260422",
                      "EXPTIME": 5.0, "ZPMAG": 22.5, "ZPUNIT": "e-/s", "ZPNSTAR": 30,
                      "IMAGETYP": "LIGHT"})  # fmt: skip
    robo = tmp_path / "robofast.fits"
    fits.PrimaryHDU(np.zeros((8, 8), np.int16), h2).writeto(robo)
    cpath = tmp_path / "c.json"
    cpath.write_text(json.dumps({"store": "t.jsonl"}))
    inst = tmp_path / "instruments.json"
    inst.write_text(json.dumps({"telescopes": {"t2": {"ports": {"imager": 2, "fau": 1}}}}))
    from tests.core.skydata import sky_dir

    assert tp.main(["--config", str(cpath), "backfill", "--solve", "--plate-scale", "0.61",
                    "--catalog", str(sky_dir()), "--search-radius", "0.1",
                    "--instruments", str(inst), str(legacy), str(robo)]) == 0  # fmt: skip
    recs = {r["telescope"]: r for r in tp.ThroughputStore(tmp_path / "t.jsonl").read()}
    old = recs["t2"]
    assert old["port"] == "fau" and old["night"] == "n20260422" and old["unit"] == "ADU/s"
    assert old["context"] == {"temperature": 10.8, "moon_sep": 72.0}
    # Counts are ADU (gain 1.3 e-/ADU in the simulator): the e- zero point less 2.5 log 1.3.
    true = sc.photometry.zp - 0.15 * old["airmass"] - 2.5 * math.log10(1.3)
    assert old["zp"] == pytest.approx(true, abs=0.08)
    new = recs["t1"]
    got = (new["port"], new["filter"], new["zp"], new["night"])
    assert got == ("imager", "V", 22.5, "n20260422")
    # Again: nothing new.
    assert tp.main(["--config", str(cpath), "backfill", str(robo)]) == 0
    assert len(tp.ThroughputStore(tmp_path / "t.jsonl").read()) == 2


def test_header_context_and_nights():
    h = {"OUTTEMP": 10.8, "AMBTEMP": "UNKNOWN", "M1TEMP": 16.9, "MOONSEP": 40.0, "FWHM": "nan"}
    assert tp.header_context(h) == {"temperature": 10.8, "m1_temp": 16.9, "moon_sep": 40.0}
    assert tp.series_key("T2", None, None) == "t2/-/-"
    r = tp.record_from_header({"TELESCOP": "t1", "DATE-OBS": "2026-04-23T05:00:00", "AIRMASS": 1.1},
                              {"zp": 22.0}, file="x.fits", longitude=-110.88)  # fmt: skip
    assert r["night"] == "n20260422"  # 22:36 local mean time on the 22nd
    assert tp._night_str(date(2026, 1, 2)) == "n20260102"
