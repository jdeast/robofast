"""Validation against archived nights and the site cloud sensor.

``python -m robofast.allsky.validate run NIGHT_DIR --camera C --model M --out X.jsonl``
analyzes a night's frames and writes one JSON line per frame with the
camera's clear fractions and the cloud sensors' readings nearest in time.
``... report X.jsonl ... --out-dir D`` draws a time-series PNG per night
and prints the confusion table of camera versus sensor.

The log's keys are the caller's (``--sensor`` decides; ``--log-keys``
are recorded too): they are a site's (docs/allsky.md). Sensor classes,
for a sky-minus-ambient cloud sensor in deg C: at or below ``CLEAR_MAX``
(-38) clear, above ``CLOUDY_MIN`` (-28) cloudy, in between "between";
39.0, 0, 999 and -999.9 are no reading, 998 is rain (cloudy).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast.allsky import archive, cameras, clouds, frames, stars
from robofast.allsky.model import CameraModel

__all__ = ["camera_class", "confusion", "run_night", "sensor_class"]

CLEAR_MAX = -38.0
CLOUDY_MIN = -28.0
NO_READING = (39.0, 0.0, 999.0, -999.9, -999.0)
RAIN = 998.0


def sensor_class(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "none"
    if v == RAIN:
        return "cloudy"
    if v in NO_READING:
        return "none"
    if v <= CLEAR_MAX:
        return "clear"
    if v > CLOUDY_MIN:
        return "cloudy"
    return "between"


def camera_class(clear: float | None, coverage: float, min_coverage: float = 0.3) -> str:
    if clear is None or (isinstance(clear, float) and math.isnan(clear)) or coverage < min_coverage:
        return "unknown"
    if clear >= 0.7:
        return "clear"
    if clear < 0.3:
        return "cloudy"
    return "partly"


def _nearest(series: tuple[np.ndarray, np.ndarray], t: datetime, max_gap: float = 300.0):
    ts, vs = series
    if ts.size == 0:
        return None
    i = int(np.argmin(np.abs(ts - t.timestamp())))
    if abs(ts[i] - t.timestamp()) > max_gap:
        return None
    return float(vs[i])


def run_night(
    night_dir: Path,
    spec: cameras.CameraSpec,
    model: CameraModel,
    *,
    step: int = 4,
    max_sun_alt: float = -12.0,
    log_path: Path | None = None,
    log_keys: tuple[str, ...] = (),
) -> Iterable[dict[str, Any]]:
    from robofast.allsky.calibrate import _site

    paths = archive.frame_paths(night_dir)
    times = archive.frame_times(paths)
    log = archive.read_site_log(log_path, log_keys) if log_path and log_path.is_file() else {}
    for p, t in list(zip(paths, times, strict=True))[::step]:
        eph = stars.ephemeris(t, _site(spec))
        if eph.sun_alt > max_sun_alt:
            continue
        try:
            f = frames.load_frame(p, spec.name, t)
            cmap, _ = clouds.analyze(f, spec, model, eph=eph)
        except Exception as exc:  # truncated or corrupt frame
            yield {"time": t.isoformat(), "frame": str(p), "error": str(exc)}
            continue
        z45 = cmap.clear_fraction_detail(90.0, 0.0, 45.0)
        rec = {
            "time": t.isoformat(),
            "frame": p.name,
            "clear": _r(cmap.whole_sky),
            "coverage": round(cmap.coverage, 3),
            "zenith45": _r(z45[0]),
            "zenith45_weight": round(z45[1], 1),
            "patches": [_r(float(c)) for c in cmap.clear],
            "status": [s[0] for s in cmap.status],
            "bg_ratio": [_r(float(b)) for b in cmap.bg_ratio],
            "sun_alt": round(eph.sun_alt, 2),
            "moon_alt": round(eph.moon_alt, 2),
            "moon_illum": round(eph.moon_illum, 3),
            "n_stars": int(cmap.table["v"].size),
            "n_det": int(cmap.table["det"].sum()),
        }
        for k, series in log.items():
            rec[k] = _nearest(series, t)
        yield rec


def _r(v: float | None) -> float | None:
    return None if v is None or math.isnan(v) else round(float(v), 4)


def attach_sensors(
    recs: list[dict[str, Any]], log_path: str | Path, keys: tuple[str, ...] | list[str]
) -> list[dict[str, Any]]:
    """(Re)fill each record's readings of ``keys`` from a site log."""
    log = archive.read_site_log(log_path, keys)
    for r in recs:
        if "error" in r:
            continue
        t = datetime.fromisoformat(r["time"])
        for k, series in log.items():
            r[k] = _nearest(series, t)
    return recs


def load(path: str | Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def confusion(
    recs: list[dict[str, Any]],
    sensor: str,
    key: str = "clear",
    sun_range: tuple[float, float] = (-90.0, -18.0),
    min_coverage: float = 0.3,
) -> Counter:
    """(sensor class, camera class) counts over the records with the Sun in
    ``sun_range`` (default: astronomical night)."""
    c: Counter = Counter()
    for r in recs:
        if "error" in r:
            continue
        if not (sun_range[0] <= r["sun_alt"] <= sun_range[1]):
            continue
        cov = r["coverage"] if key == "clear" else (1.0 if r.get(key) is not None else 0.0)
        c[(sensor_class(r.get(sensor)), camera_class(r.get(key), cov, min_coverage))] += 1
    return c


def format_confusion(c: Counter, title: str = "") -> str:
    rows = ["clear", "between", "cloudy", "none"]
    cols = ["clear", "partly", "cloudy", "unknown"]
    out = [
        f"| sensor \\ camera{(' (' + title + ')') if title else ''} | "
        + " | ".join(cols)
        + " | total |",
        "|---|" + "---:|" * (len(cols) + 1),
    ]
    for r in rows:
        n = [c.get((r, k), 0) for k in cols]
        if sum(n):
            out.append(f"| {r} | " + " | ".join(str(v) for v in n) + f" | {sum(n)} |")
    return "\n".join(out)


def scores(c: Counter) -> dict[str, float]:
    """Agreement on frames where both sides are decided (sensor clear or
    cloudy; camera clear or cloudy, partly counted as neither)."""
    tp = c.get(("clear", "clear"), 0)
    tn = c.get(("cloudy", "cloudy"), 0)
    fp = c.get(("cloudy", "clear"), 0)  # camera says clear, sensor cloudy
    fn = c.get(("clear", "cloudy"), 0)
    n = tp + tn + fp + fn
    s_clear = sum(v for (s, _), v in c.items() if s == "clear")
    s_cloudy = sum(v for (s, _), v in c.items() if s == "cloudy")
    return {
        "decided": n,
        "agreement": round((tp + tn) / n, 4) if n else math.nan,
        "camera_clear_when_sensor_cloudy": round(fp / s_cloudy, 4) if s_cloudy else math.nan,
        "camera_cloudy_when_sensor_clear": round(fn / s_clear, 4) if s_clear else math.nan,
        "sensor_clear_frames": s_clear,
        "sensor_cloudy_frames": s_cloudy,
    }


def plot_night(recs: list[dict[str, Any]], path: str, title: str, sensor: str) -> None:
    from robofast.allsky import render

    ok = [r for r in recs if "error" not in r]
    if not ok:
        return
    t0 = datetime.fromisoformat(ok[0]["time"]).replace(hour=0, minute=0, second=0)
    h = np.array([(datetime.fromisoformat(r["time"]) - t0).total_seconds() / 3600 for r in ok])
    clear = np.array([np.nan if r["clear"] is None else r["clear"] for r in ok])
    z45 = np.array([np.nan if r["zenith45"] is None else r["zenith45"] for r in ok])
    me = np.array(
        [np.nan if sensor_class(r.get(sensor)) == "none" else min(r[sensor], 10) for r in ok]
    )
    bands = []
    for r, hh in zip(ok, h, strict=True):
        if r["moon_alt"] > 0 and r["moon_illum"] > 0.3:
            bands.append((hh - 0.02, hh + 0.02, (245, 240, 205)))
        if r["sun_alt"] > -15:
            bands.append((hh - 0.02, hh + 0.02, (225, 225, 240)))
    render.time_series_png(
        path,
        [
            (h, clear, (0, 150, 0), "camera whole sky"),
            (h, z45, (120, 200, 120), "camera zenith 45"),
        ],
        y2=[(h, me, (30, 60, 220), f"{sensor} (right, deg C)")],
        ylim=(0.0, 1.0),
        y2lim=(-60.0, 10.0),
        title=title,
        xlabel="UTC hour (yellow: moon up, blue: twilight)",
        bands=bands,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast.allsky.validate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("night_dir")
    p.add_argument("--camera", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--step", type=int, default=4)
    p.add_argument("--out", required=True)
    p.add_argument("--log", help="the night's site log")
    p.add_argument("--log-keys", default="", help="comma-separated log keys to record")
    p.add_argument("--models", help="models directory (default $ROBOFAST_ALLSKY_MODELS)")
    p = sub.add_parser("report")
    p.add_argument("jsonl", nargs="+")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--sensor", required=True, help="the log key to compare with")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        spec = cameras.camera_spec(a.camera, a.models)
        model = CameraModel.load(a.model)
        keys = tuple(k for k in a.log_keys.split(",") if k)
        log = Path(a.log) if a.log else None
        with open(a.out, "w") as fh:
            for rec in run_night(
                Path(a.night_dir), spec, model, step=a.step, log_path=log, log_keys=keys
            ):
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
        return 0
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total: Counter = Counter()
    for j in a.jsonl:
        recs = load(j)
        name = Path(j).stem
        plot_night(recs, str(out / f"{name}.png"), name, a.sensor)
        c = confusion(recs, a.sensor)
        total += c
        print(name, json.dumps(scores(c)))
    print(format_confusion(total))
    print(json.dumps(scores(total)))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def fit_residuals(
    model: CameraModel, spec: cameras.CameraSpec, frame_list: list[frames.Frame], tol: float = 3.0
) -> dict[str, np.ndarray]:
    """Matched catalogue stars' residuals (pixels) under ``model``."""
    from robofast.allsky import calibrate

    mask = clouds.static_mask(model, frame_list[0].shape)
    parts = [calibrate.matches_for(f, spec, model, tol=tol, mask=mask) for f in frame_list]
    alt, az, x, y = (np.concatenate([p[i] for p in parts]) for i in range(4))
    px, py = model.to_pixel(alt, az)
    return {"alt": alt, "az": az, "dx": x - px, "dy": y - py}


def residual_png(path: str, res: dict[str, np.ndarray], title: str) -> dict[str, float]:
    """Scatter of dx, dy and of |d| against altitude; returns summary stats."""
    from robofast.allsky import render

    d = np.hypot(res["dx"], res["dy"])
    cv = render.Canvas(760, 360)
    cx, cy, s = 180, 190, 40.0  # 40 px per pixel of residual
    for k in (1, 2, 3):
        cv.circle(cx, cy, k * s, (200, 200, 200))
    cv.line(cx - 150, cy, cx + 150, cy, (160, 160, 160))
    cv.line(cx, cy - 150, cx, cy + 150, (160, 160, 160))
    cv.points(cx + res["dx"] * s, cy + res["dy"] * s, (30, 60, 200), 2)
    cv.text(20, 345, "DX, DY (RINGS 1, 2, 3 PX)", (0, 0, 0))
    # |d| against altitude
    left, right, top, bottom = 400, 740, 40, 320
    cv.line(left, bottom, right, bottom, (0, 0, 0))
    cv.line(left, top, left, bottom, (0, 0, 0))
    ymax = 3.0
    xs = left + (res["alt"] - 10) / 80 * (right - left)
    ys = bottom - np.minimum(d, ymax) / ymax * (bottom - top)
    cv.points(xs, ys, (30, 60, 200), 2)
    for a in (10, 30, 50, 70, 90):
        x = left + (a - 10) / 80 * (right - left)
        cv.text(int(x) - 6, bottom + 6, str(a), (0, 0, 0))
    for v in (0, 1, 2, 3):
        y = bottom - v / ymax * (bottom - top)
        cv.text(left - 12, int(y) - 3, str(v), (0, 0, 0))
    cv.text(left + 60, bottom + 20, "ALTITUDE (DEG) VS RESIDUAL (PX)", (0, 0, 0))
    stats = {
        "n": int(d.size),
        "rms_px": round(float(np.sqrt(np.mean(d**2))), 3) if d.size else math.nan,
        "median_px": round(float(np.median(d)), 3) if d.size else math.nan,
        "p90_px": round(float(np.percentile(d, 90)), 3) if d.size else math.nan,
    }
    cv.text(
        20,
        10,
        f"{title}  N {stats['n']}  RMS {stats['rms_px']}  MEDIAN {stats['median_px']}",
        (0, 0, 0),
    )
    cv.save(path)
    return stats
