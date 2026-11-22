"""The clear-sky calibration of a camera: what a clear night looks like.

From a set of frames of one or more nights:

* :func:`static_residual` / :func:`static_mask`: what is bright in the same
  pixels in every frame (hot pixels, overlay lines and text, horizon
  lights). Stars move 15 deg an hour; anything still there in the median
  of frames spread over hours is not a star.
* :func:`select_clear`: frames in which nearly all the bright stars show
  (dark, no Moon).
* :func:`fit_limit`: the limiting-magnitude model of
  :func:`robofast.allsky.clouds.p_detect` from the clear frames' stars, by
  maximum likelihood (the false-positive rate from the control apertures is
  part of the likelihood).
* the clear-sky noise (coarse grid) and background per patch.

:func:`calibrate_sky` runs them all and returns the ``sky`` section of the
camera model's JSON.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import numpy as np

from robofast.allsky import clouds, image
from robofast.allsky.cameras import CameraSpec
from robofast.allsky.frames import Frame
from robofast.allsky.model import CameraModel

__all__ = ["calibrate_sky", "fit_limit", "select_clear", "static_mask", "static_residual"]


def static_residual(frames: Iterable[Frame], block: int = 32) -> np.ndarray:
    """Median over ``frames`` of the background-subtracted frame."""
    stack = []
    for f in frames:
        bg = image.background(f.grey, np.ones(f.shape, dtype=bool), block)
        stack.append(bg.resid.astype(np.float32))
    if not stack:
        raise ValueError("no frames")
    return np.median(np.asarray(stack), axis=0)


def static_mask(resid: np.ndarray, threshold: float | None = None, grow: int = 1) -> np.ndarray:
    """True where the median residual stands out (``threshold`` counts;
    default 5 times its robust scatter, at least 3 counts), grown by ``grow`` px."""
    if threshold is None:
        mad = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
        threshold = max(3.0, 5.0 * mad)
    return image.dilate(resid > threshold, grow)


def bright_fraction(
    m: clouds.Measurement, vmax: float = 2.5, min_alt: float = 30.0
) -> tuple[float, int]:
    """(detected fraction, number) of catalogue stars brighter than ``vmax``
    above ``min_alt`` in a measurement."""
    t = m.table
    sel = (t["v"] <= vmax) & (t["alt"] >= min_alt)
    n = int(sel.sum())
    return (float(t["det"][sel].mean()) if n else math.nan), n


def select_clear(
    measurements: list[clouds.Measurement],
    vmax: float = 2.5,
    min_frac: float = 0.85,
    min_n: int = 8,
    allow_moon: bool = False,
) -> list[int]:
    """Indices of the clear, dark (and, unless ``allow_moon``, moonless)
    measurements."""
    out = []
    for i, m in enumerate(measurements):
        if m.eph.sun_alt > -15 or (m.eph.moon_alt > -5 and not allow_moon):
            continue
        frac, n = bright_fraction(m, vmax)
        if n >= min_n and frac >= min_frac:
            out.append(i)
    return out


def clear_patches(
    table: dict[str, np.ndarray], vmax: float = 2.5, min_n: int = 3, min_frac: float = 0.9
) -> set[int]:
    """Patches of one measurement in which nearly all the bright stars
    (``vmax``) were detected: clear there, whatever the rest of the sky."""
    pid = table["patch"].astype(int)
    bright = (table["v"] <= vmax) & (pid >= 0)
    out = set()
    for k in np.unique(pid[bright]):
        sel = bright & (pid == k)
        if sel.sum() >= min_n and table["det"][sel].mean() >= min_frac:
            out.add(int(k))
    return out


def _loglike(v, x, det, fp, m0, a1, w) -> float:
    p = 1.0 / (1.0 + np.exp(np.clip((v - (m0 + a1 * (x - 1.0))) / w, -50, 50)))
    q = np.clip(fp + (1 - fp) * p, 1e-6, 1 - 1e-6)
    return float(np.sum(det * np.log(q) + (1 - det) * np.log(1 - q)))


def fit_limit(v: np.ndarray, alt: np.ndarray, det: np.ndarray, fp: np.ndarray) -> dict[str, float]:
    """Maximum-likelihood (m0, a1, width) of the logistic detection model,
    by a coarse grid then a finer one around the best point."""
    x = np.minimum(clouds.airmass(alt), 3.0)
    best = (-np.inf, 0.0, 0.0, 0.0)
    grids = [
        (np.arange(1.0, 7.51, 0.25), np.arange(-2.5, 0.51, 0.25), (0.15, 0.25, 0.4, 0.6)),
    ]
    for m0s, a1s, ws in grids:
        for w in ws:
            for m0 in m0s:
                for a1 in a1s:
                    ll = _loglike(v, x, det, fp, m0, a1, w)
                    if ll > best[0]:
                        best = (ll, m0, a1, w)
    _, m0b, a1b, wb = best
    for m0 in np.arange(m0b - 0.25, m0b + 0.26, 0.05):
        for a1 in np.arange(a1b - 0.25, a1b + 0.26, 0.05):
            for w in np.arange(max(0.08, wb - 0.1), wb + 0.11, 0.025):
                ll = _loglike(v, x, det, fp, m0, a1, w)
                if ll > best[0]:
                    best = (ll, m0, a1, w)
    ll, m0, a1, w = best
    return {
        "m0": round(float(m0), 3),
        "a1": round(float(a1), 3),
        "width": round(float(w), 3),
        "loglike": round(ll, 2),
        "n_stars": int(v.size),
    }


def obstruction_mask(
    x: np.ndarray,
    y: np.ndarray,
    v: np.ndarray,
    alt: np.ndarray,
    det: np.ndarray,
    sky: dict[str, Any],
    shape: tuple[int, int],
    block: int = 24,
    min_expected: float = 3.0,
    max_rate: float = 0.25,
) -> np.ndarray:
    """Blocks of ``block`` px where clear frames' stars (expected ``P`` from
    the fitted limit) were seldom detected: at least ``min_expected``
    expected, under ``max_rate`` of that found."""
    p = clouds.p_detect(sky, v, alt, 1.0)
    h, w = shape
    ny, nx = -(-h // block), -(-w // block)
    by = np.clip((y // block).astype(int), 0, ny - 1)
    bx = np.clip((x // block).astype(int), 0, nx - 1)
    e = np.zeros((ny, nx))
    d = np.zeros((ny, nx))
    np.add.at(e, (by, bx), p)
    np.add.at(d, (by, bx), det * p)
    bad = (e >= min_expected) & (d < max_rate * e)
    return np.repeat(np.repeat(bad, block, 0), block, 1)[:h, :w]


def calibrate_sky(
    frames: list[Frame],
    spec: CameraSpec,
    model: CameraModel,
    *,
    static_name: str | None = None,
    radius_px: float | None = None,
    threshold: float = 4.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``sky`` calibration from ``frames`` (several hours of one or
    more nights; the clear dark ones are picked out) and a report."""
    sky = dict(clouds.DEFAULTS)
    sky.update(model.sky or {})
    fit_rms = (model.meta.get("fit") or {}).get("rms_px", 1.5)
    sky["radius_px"] = radius_px or float(np.clip(2.0 * fit_rms, 2.0, 4.0))
    sky["threshold"] = threshold
    if static_name:
        sky["static_mask"] = static_name
    # Measure every frame with everything expected (P=1 to V 6.5).
    probe = dict(sky, m0=20.0, a1=0.0, width=0.3, clear_noise=None)
    tmp = replace(model, sky=probe)
    ms = [clouds.measure(f, spec, tmp, vmax=6.5, settings=probe) for f in frames]
    clear = select_clear(ms)
    report: dict[str, Any] = {"n_frames": len(frames), "n_clear": len(clear)}
    if len(clear) < 3:
        # A moonlit night: calibrate under the Moon. The limit then includes
        # tonight's moonlight (conservative on a dark night) and the Moon
        # term stays 0 until a moonless calibration.
        clear = select_clear(ms, vmax=2.0, min_frac=0.8, allow_moon=True)
        report["n_clear"] = len(clear)
        report["moonlit_calibration"] = True
        sky["calibrated_with_moon"] = True
    patch_level = False
    if len(clear) < 1:
        # Partly cloudy: use the clear patches of the dark frames instead.
        clear = [i for i, m in enumerate(ms) if m.eph.sun_alt < -15]
        patch_level = True
        report["patch_level_calibration"] = True
    cms = [ms[i] for i in clear]
    rows = []
    n_patches = 0
    for m in cms:
        fp = clouds._fp_rates(m.ctrl_n, m.ctrl_k)
        t = m.table
        pid = t["patch"].astype(int)
        keep = np.ones(pid.size, dtype=bool)
        if patch_level:
            good = clear_patches(t)
            n_patches += len(good)
            keep = np.isin(pid, list(good))
        rows.append(
            (
                t["v"][keep],
                t["alt"][keep],
                t["det"][keep],
                fp[np.maximum(pid, 0)][keep],
                t["x"][keep],
                t["y"][keep],
            )
        )
    if patch_level:
        report["n_clear_patches"] = n_patches
    if sum(r[0].size for r in rows) < 200:
        report["error"] = "no clear dark frames or patches"
        return sky, report
    v, alt, det, fp = (np.concatenate([r[i] for r in rows]) for i in range(4))
    lim = fit_limit(v, alt, det, fp)
    sky.update(m0=lim["m0"], a1=lim["a1"], width=lim["width"])
    report["limit"] = lim
    report["fp_rate"] = round(float(np.mean(fp)), 4)
    # Obstructions (buildings, trees, the mast): blocks where stars a clear
    # sky should show never do.
    xs = np.concatenate([r[4] for r in rows])
    ys = np.concatenate([r[5] for r in rows])
    shape = cms[0].frame.shape
    obstructed = obstruction_mask(xs, ys, v, alt, det, sky, shape)
    report["obstructed_px"] = int(obstructed.sum())
    if static_name and obstructed.any():
        from robofast.allsky import imageio

        sm = clouds.static_mask(replace(model, sky=sky), shape)
        merged = obstructed if sm is None else (sm | obstructed)
        imageio.write_png(static_name, (merged * 255).astype(np.uint8))
        clouds._STATIC.clear()
    # Clear-sky noise grid and per-patch background (medians over clear frames).
    sky["clear_noise"] = np.round(
        np.median(np.stack([m.bg.noise_coarse for m in cms]), axis=0), 3
    ).tolist()
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        bgp = np.nanmedian(np.stack([m.bg_patch for m in cms]), axis=0)
    sky["clear_bg_patch"] = [None if not np.isfinite(b) else round(float(b), 2) for b in bgp]
    sky["calibrated_from"] = [cms[0].frame.path, cms[-1].frame.path]
    report["clear_frames"] = [m.frame.path for m in cms]
    return sky, report


def fit_term(
    name: str,
    frames: list[tuple[Frame, Any]],
    spec: CameraSpec,
    model: CameraModel,
    grid: tuple[float, ...],
    target: float = 0.9,
) -> tuple[float, list[tuple[float, float]]]:
    """The smallest value of the limit term ``name`` (``twilight``: mag per
    degree of Sun above -18; ``moon``: mag at full Moon overhead) for which
    clear frames (``(frame, ephemeris)`` pairs) read at least ``target``
    clear (median). The frames must be of a clear sky. Returns the value
    and (value, median) per trial."""
    scan = []
    for val in grid:
        m = replace(model, sky=dict(model.sky, **{name: val}))
        vals = []
        for f, eph in frames:
            cm, _ = clouds.analyze(f, spec, m, eph=eph)
            if np.isfinite(cm.whole_sky):
                vals.append(cm.whole_sky)
        med = float(np.median(vals)) if vals else math.nan
        scan.append((val, round(med, 3)))
        if np.isfinite(med) and med >= target:
            return val, scan
    return grid[-1], scan


TWILIGHT_GRID = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4)
MOON_GRID = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)


def fit_twilight(
    frames: list[tuple[Frame, Any]], spec: CameraSpec, model: CameraModel
) -> tuple[float, list[tuple[float, float]]]:
    return fit_term("twilight", frames, spec, model, TWILIGHT_GRID)


def fit_moon(
    frames: list[tuple[Frame, Any]], spec: CameraSpec, model: CameraModel
) -> tuple[float, list[tuple[float, float]]]:
    return fit_term("moon", frames, spec, model, MOON_GRID)
