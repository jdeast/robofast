"""``python -m robofast.allsky``: cloud maps from all-sky frames.

Commands::

    analyze FRAME [FRAME2]   cloud map PNG + JSON for one frame (two cameras pooled)
    check FRAME              does the saved camera model still fit?
    calibrate FRAMES...      fit the camera model and clear-sky calibration
    series FRAMES...         analyze many frames; JSON lines plus the trend
    watch --config C         loop: newest frames, pooled, to an atomic latest.json
    fetch-catalog            download the Bright Star Catalogue (once)

Frames named ``<camera>_<YYYYMMDDTHHMMSS>Z.jpg`` (the live fetcher's
names) need no ``--camera``/``--time``. Models are read from
``--models`` or ``$ROBOFAST_ALLSKY_MODELS``: a site's directory with
``cameras.yaml`` and ``<camera>.json`` (``sites/<site>/allsky``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from robofast.allsky import cameras, clouds, frames, render, stars
from robofast.allsky.model import CameraModel

_MODELS: str | None = None  # --models


def models_dir() -> Path:
    return cameras.models_dir(_MODELS)


def load_model(camera: str, path: str | None) -> CameraModel:
    p = Path(path) if path else models_dir() / f"{camera}.json"
    if not p.is_file():
        raise SystemExit(f"no camera model {p}: run `python -m robofast.allsky calibrate`")
    return CameraModel.load(p)


def _time(s: str | None) -> datetime | None:
    if not s:
        return None
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _frame(path: str, camera: str | None, when: str | None) -> frames.Frame:
    t = _time(when)
    if t is None and frames.time_from_name(path) is None:
        t = datetime.fromtimestamp(os.path.getmtime(path), UTC)
    return frames.load_frame(path, camera, t)


def cmd_analyze(a: argparse.Namespace) -> int:
    f = _frame(a.frame, a.camera, a.time)
    spec = cameras.camera_spec(f.camera, _MODELS)
    model = load_model(f.camera, a.model)
    eph = stars.ephemeris(f.time, spec.site)
    cmap, meas = clouds.analyze(f, spec, model, eph=eph)
    items = [(meas, model)]
    if a.frame2:
        f2 = _frame(a.frame2, None, None)
        m2 = load_model(f2.camera, None)
        _, meas2 = clouds.analyze(f2, cameras.camera_spec(f2.camera, _MODELS), m2)
        items.append((meas2, m2))
        cmap = clouds.combine(items, max_age_s=a.max_age)
    out = Path(a.out_dir or Path(a.frame).parent)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(a.frame).stem
    render.cloud_map_png(str(out / f"{stem}.cloud.png"), f, cmap)
    doc = cmap.to_json()
    if a.at:
        for spec_s in a.at:
            alt, az, rad = (float(v) for v in spec_s.split(","))
            frac, w, src = cmap.clear_fraction_detail(alt, az, rad)
            doc.setdefault("pointings", []).append(
                {
                    "alt": alt,
                    "az": az,
                    "radius": rad,
                    "clear": None if frac != frac else round(frac, 3),
                    "expected": round(w, 2),
                    "source": src,
                }
            )
    (out / f"{stem}.cloud.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(
        f"{f.camera} {f.time:%Y-%m-%dT%H:%M:%SZ} clear {cmap.whole_sky:.2f} "
        f"coverage {cmap.coverage:.2f} -> {out / (stem + '.cloud.png')}"
    )
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    from robofast.allsky import calibrate as cal
    from robofast.allsky.clouds import static_mask

    f = _frame(a.frame, a.camera, a.time)
    spec = cameras.camera_spec(f.camera, _MODELS)
    model = load_model(f.camera, a.model)
    res = cal.check(f, spec, model, mask=static_mask(model, f.shape))
    print(json.dumps(res.as_dict()))
    return 0 if res.ok else 1


def _items(paths: list[str], camera: str | None) -> list[tuple[Path, datetime]]:
    out = []
    for p in paths:
        t = frames.time_from_name(p) or datetime.fromtimestamp(os.path.getmtime(p), UTC)
        out.append((Path(p), t))
    return sorted(out, key=lambda r: r[1])


def cmd_calibrate(a: argparse.Namespace) -> int:
    from robofast.allsky import pipeline

    items = _items(a.frames, a.camera)
    cam = a.camera or frames.camera_from_name(a.frames[0])
    if not cam:
        raise SystemExit("give --camera")
    start = CameraModel.load(a.start) if a.start else None
    out = Path(a.out_dir or models_dir())
    if a.sky_only:
        if start is None:
            raise SystemExit("--sky-only needs --start MODEL")
        _model, rep = pipeline.calibrate_sky_only(
            items[:: a.step], cameras.camera_spec(cam, _MODELS), start, out_dir=out, name=a.name
        )
        print(json.dumps({k: v for k, v in rep.items() if k != "clear_frames"}, indent=1))
        return 0
    _model, rep = pipeline.calibrate_night(
        items[:: a.step],
        cameras.camera_spec(cam, _MODELS),
        start=start,
        out_dir=out,
        name=a.name,
        progress=lambda s: print(s, flush=True),
    )
    rep.pop("residuals", None)
    print(json.dumps(rep, indent=1, default=str))
    return 0


def cmd_series(a: argparse.Namespace) -> int:
    from robofast.allsky import pipeline

    items = _items(a.frames, a.camera)[:: a.step]
    cam = a.camera or frames.camera_from_name(a.frames[0])
    spec = cameras.camera_spec(cam, _MODELS)
    model = load_model(cam, a.model)
    maps = pipeline.analyze_night(items, spec, model)
    with open(a.out, "w") as fh:
        for m in maps:
            fh.write(json.dumps(m.to_json()) + "\n")
    tr = clouds.trend(maps, n=a.n)
    print(json.dumps(tr.to_json()["whole_sky"]))
    return 0


def cmd_watch(a: argparse.Namespace) -> int:
    from robofast.allsky import watch

    cfg = watch.load_config(a.config)
    w = watch.Watcher(cfg)
    if a.once:
        doc = w.step()
        print(
            json.dumps({k: doc.get(k) for k in ("time", "whole_sky_clear", "coverage", "frames")})
        )
        return 0
    w.run()
    return 0


def cmd_fetch(a: argparse.Namespace) -> int:
    p = stars.fetch_bsc5()
    print(p, stars.bright_stars(p).size, "stars")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m robofast.allsky",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--models",
        help="models directory: cameras.yaml, <camera>.json (or $ROBOFAST_ALLSKY_MODELS)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze", help="cloud map of one frame")
    p.add_argument("frame")
    p.add_argument("frame2", nargs="?", help="a second camera's frame to pool")
    p.add_argument("--camera")
    p.add_argument("--time", help="UTC ISO time (default: from the name, else mtime)")
    p.add_argument("--model")
    p.add_argument("--out-dir")
    p.add_argument("--max-age", type=float, default=180.0)
    p.add_argument(
        "--at",
        action="append",
        metavar="ALT,AZ,RADIUS",
        help="also report clear_fraction at this pointing (deg)",
    )
    p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("check", help="check the camera model on a frame")
    p.add_argument("frame")
    p.add_argument("--camera")
    p.add_argument("--time")
    p.add_argument("--model")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("calibrate", help="fit model + clear-sky calibration from a night")
    p.add_argument("frames", nargs="+")
    p.add_argument("--camera")
    p.add_argument("--start", help="existing model to start from (refit after a bump)")
    p.add_argument("--out-dir")
    p.add_argument("--name")
    p.add_argument("--step", type=int, default=1)
    p.add_argument("--sky-only", action="store_true", help="keep --start's geometry; redo the sky")
    p.set_defaults(fn=cmd_calibrate)
    p = sub.add_parser("series", help="analyze many frames")
    p.add_argument("frames", nargs="+")
    p.add_argument("--camera")
    p.add_argument("--model")
    p.add_argument("--step", type=int, default=1)
    p.add_argument("--n", type=int, default=10, help="frames in the trend")
    p.add_argument("--out", required=True, help="JSON-lines output")
    p.set_defaults(fn=cmd_series)
    p = sub.add_parser("watch", help="loop: newest frames -> latest.json (shadow input)")
    p.add_argument(
        "--config", required=True, help="watch config (YAML; see robofast/allsky/watch.py)"
    )
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("fetch-catalog", help="download the Bright Star Catalogue")
    p.set_defaults(fn=cmd_fetch)
    a = ap.parse_args(argv)
    global _MODELS
    _MODELS = a.models
    logging.basicConfig(level=logging.INFO if a.cmd == "watch" else logging.WARNING)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
