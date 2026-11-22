"""Whole-night steps: calibrate a camera from a night of frames, and run
the cloud analysis over a night.

:func:`calibrate_night` builds, in order: the static-feature mask, the
geometry (:func:`robofast.allsky.calibrate.solve` on the darkest
star-richest frame, then :func:`~robofast.allsky.calibrate.refine` over
several), and the clear-sky calibration (:func:`robofast.allsky.skycal.calibrate_sky`).
It works on whatever night it is given; on a cloudy night it reports
that it found no clear frames rather than fitting clouds.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast.allsky import calibrate, clouds, imageio, skycal, stars
from robofast.allsky.cameras import MODELS_DIR, CameraSpec
from robofast.allsky.frames import Frame, load_frame
from robofast.allsky.model import CameraModel

log = logging.getLogger(__name__)

__all__ = ["NightFrame", "analyze_night", "calibrate_night"]


@dataclass
class NightFrame:
    path: Path
    time: datetime
    sun_alt: float = math.nan
    moon_alt: float = math.nan
    eph: stars.Ephemeris | None = field(default=None, repr=False)


def with_ephemeris(items: Sequence[tuple[Path, datetime]]) -> list[NightFrame]:
    out = []
    for p, t in items:
        e = stars.ephemeris(t)
        out.append(NightFrame(Path(p), t, e.sun_alt, e.moon_alt, e))
    return out


def _spread(items: list[NightFrame], n: int) -> list[NightFrame]:
    if len(items) <= n:
        return items
    idx = np.linspace(0, len(items) - 1, n).round().astype(int)
    return [items[i] for i in idx]


def _load(nf: NightFrame, camera: str) -> Frame:
    return load_frame(nf.path, camera, nf.time)


def calibrate_night(
    items: Sequence[tuple[Path, datetime]],
    spec: CameraSpec,
    *,
    start: CameraModel | None = None,
    out_dir: Path = MODELS_DIR,
    name: str | None = None,
    n_static: int = 40,
    n_candidates: int = 24,
    n_refine: int = 10,
    n_sky: int = 60,
    search: dict[str, Any] | None = None,
    progress: Callable[[str], None] = log.info,
) -> tuple[CameraModel, dict[str, Any]]:
    """Calibrate ``spec``'s camera from a night's (path, time) frames; the
    model JSON and static mask PNG are written to ``out_dir`` as
    ``<name>.json`` / ``<name>.static.png`` (``name`` defaults to the camera)."""
    name = name or spec.name
    report: dict[str, Any] = {"camera": spec.name, "n_frames": len(items)}
    nights = with_ephemeris(list(items))
    dark = [n for n in nights if n.sun_alt < -15]
    report["n_dark"] = len(dark)
    if len(dark) < 10:
        raise RuntimeError(f"{spec.name}: only {len(dark)} dark frames")
    # 1. Static features.
    progress(f"static mask from {min(n_static, len(dark))} frames")
    resid = skycal.static_residual(_load(n, spec.name) for n in _spread(dark, n_static))
    smask = skycal.static_mask(resid)
    out_dir.mkdir(parents=True, exist_ok=True)
    imageio.write_png(out_dir / f"{name}.static.png", (smask * 255).astype(np.uint8))
    report["static_masked_px"] = int(smask.sum())
    # 2. Geometry: rank moonless dark frames by detected peaks.
    moonless = [n for n in dark if n.moon_alt < -5 and n.sun_alt < -18] or dark
    ranked = []
    for n in _spread(moonless, n_candidates):
        f = _load(n, spec.name)
        v = calibrate.sky_valid(f, spec, start, 10.0, mask=smask)
        ranked.append((len(calibrate.detect(f, v, 5.0).x), n))
    ranked.sort(key=lambda r: -r[0])
    report["peaks_best"] = [r[0] for r in ranked[:n_refine]]
    best = _load(ranked[0][1], spec.name)
    progress(f"solve on {best.path} ({ranked[0][0]} peaks)")
    kw = dict(shift_px=30.0, scales=(0.9, 0.95, 1.0, 1.05, 1.1), n_peaks=120, vmax_search=3.5)
    if start is not None:
        kw = dict(shift_px=12.0, scales=(1.0,), n_peaks=150, vmax_search=3.5)
    kw.update(search or {})
    sol = calibrate.solve(best, spec, start, mask=smask, **kw)
    report["solve"] = dict(sol.fit.summary(), search_matches=sol.search_count, frame=best.path)
    progress(f"solve: {report['solve']}")
    frames = [_load(r[1], spec.name) for r in ranked[:n_refine]]
    res = calibrate.refine(frames, spec, sol.fit.model, mask=smask)
    model = res.model
    report["refine"] = res.summary()
    report["residuals"] = {
        "dx": np.round(res.dx, 3).tolist(),
        "dy": np.round(res.dy, 3).tolist(),
    }
    progress(f"refine: {report['refine']}")
    # 3. Clear-sky calibration.
    model.sky = dict(model.sky or {}, static_mask=str((out_dir / f"{name}.static.png").resolve()))
    sky_frames = [_load(n, spec.name) for n in _spread(dark, n_sky)]
    sky, srep = skycal.calibrate_sky(sky_frames, spec, model, static_name=model.sky["static_mask"])
    model.sky = sky
    report["sky"] = srep
    # 4. Twilight, if the night was clear enough to trust its twilight.
    n_clear = srep.get("n_clear", 0)
    tw_items = [n for n in nights if -16 < n.sun_alt < -10]
    if tw_items and n_clear >= 0.6 * min(n_sky, len(dark)):
        pairs = [(_load(n, spec.name), n.eph) for n in _spread(tw_items, 16)]
        tw, scan = skycal.fit_twilight(pairs, spec, model)
        model.sky["twilight"] = tw
        report["twilight"] = {"term": tw, "scan": scan}
        progress(f"twilight term {tw} ({scan})")
    progress(f"sky: {srep.get('limit')} clear frames {srep.get('n_clear')}")
    model.save(out_dir / f"{name}.json")
    (out_dir / f"{name}.report.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    return model, report


def calibrate_sky_only(
    items: Sequence[tuple[Path, datetime]],
    spec: CameraSpec,
    model: CameraModel,
    *,
    out_dir: Path = MODELS_DIR,
    name: str | None = None,
    n_sky: int = 60,
) -> tuple[CameraModel, dict[str, Any]]:
    """Redo only the clear-sky calibration of ``model`` (geometry and static
    mask kept) from another night's frames."""
    name = name or spec.name
    dark = [n for n in with_ephemeris(list(items)) if n.sun_alt < -15]
    frames = [_load(n, spec.name) for n in _spread(dark, n_sky)]
    sky, rep = skycal.calibrate_sky(
        frames, spec, model, static_name=(model.sky or {}).get("static_mask")
    )
    model.sky = sky
    model.save(out_dir / f"{name}.json")
    return model, rep


def analyze_night(
    items: Sequence[tuple[Path, datetime]],
    spec: CameraSpec,
    model: CameraModel,
    *,
    max_sun_alt: float = -12.0,
    progress: Callable[[str], None] | None = None,
) -> list[clouds.CloudMap]:
    """A cloud map for every frame taken with the Sun below ``max_sun_alt``."""
    out = []
    for p, t in items:
        eph = stars.ephemeris(t)
        if eph.sun_alt > max_sun_alt:
            continue
        try:
            f = load_frame(p, spec.name, t)
            cmap, _ = clouds.analyze(f, spec, model, eph=eph)
        except Exception as exc:  # a corrupt or truncated frame
            log.warning("%s: %s", p, exc)
            continue
        out.append(cmap)
        if progress:
            progress(f"{t:%H:%M:%S} clear {cmap.whole_sky:.2f} cov {cmap.coverage:.2f}")
    return out
