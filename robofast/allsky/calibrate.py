"""Fitting and checking a camera's geometry from the stars.

* :func:`solve`: from a rough guess (:class:`~robofast.allsky.cameras.CameraSpec`)
  or an old model, find the rotation, parity and centre by a brute-force
  search that counts bright catalogue stars landing on detected peaks, then
  refine by iterative nearest-peak matching and :func:`robofast.allsky.model.fit`
  with a shrinking match radius.
* :func:`refine`: one joint fit over the matches of several frames (better
  coverage of the sky and of the distortion).
* :func:`check`: how well a saved model still matches a frame. A bumped
  camera shows as few matches or a large mean offset; :func:`solve` seeded
  with the old model is the refit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from robofast.allsky import image, stars
from robofast.allsky.cameras import CameraSpec
from robofast.allsky.frames import Frame
from robofast.allsky.model import CameraModel, FitResult, fit

__all__ = ["Check", "check", "guess_model", "match", "refine", "sky_valid", "solve"]


def _site(spec: CameraSpec) -> stars.Site:
    if spec.site is None:
        raise ValueError(f"camera {spec.name}: no site (latitude, longitude, elevation)")
    return spec.site


def guess_model(spec: CameraSpec) -> CameraModel:
    f1, f2, f3 = spec.radial or (spec.horizon_px / (math.pi / 2), 0.0, 0.0)
    return CameraModel(
        camera=spec.name,
        width=spec.width,
        height=spec.height,
        x0=spec.x0,
        y0=spec.y0,
        rot=spec.rot,
        parity=spec.parity,
        f1=f1,
        f2=f2,
        f3=f3,
    )


def sky_valid(
    frame: Frame,
    spec: CameraSpec,
    model: CameraModel | None = None,
    min_alt: float = 5.0,
    mask: np.ndarray | None = None,
) -> np.ndarray:
    """Pixels that may be sky: inside the horizon (``min_alt``), not the
    spec's fixed masks, not saturated, not coloured overlay labels."""
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w]
    if model is None:
        inside = np.hypot(xx - spec.x0, yy - spec.y0) < spec.horizon_px * (1 - min_alt / 90)
    else:
        alt, _ = model.altaz_grid(1)
        inside = alt[:h, :w] > min_alt
    valid = inside & ~spec.fixed_mask((h, w))
    valid &= ~image.dilate(frame.grey >= spec.saturation, 2)
    if spec.colour_labels and frame.rgb is not None:
        valid &= ~label_mask(frame.rgb)
    if mask is not None:
        valid &= ~mask
    return valid


def label_mask(rgb: np.ndarray, excess: float = 45.0) -> np.ndarray:
    """Pixels much more strongly coloured than the sky around them (overlay
    text and markers; the twilight sky itself is blue), dilated."""
    c = rgb.astype(np.int16)
    chroma = (c.max(axis=2) - c.min(axis=2)).astype(np.float32)
    local = image.upsample(
        image._fill_nan(image.block_median(chroma, np.ones(chroma.shape, bool), 32)),
        32,
        chroma.shape,
    )
    return image.dilate(chroma - local > excess, 3)


@dataclass
class Detections:
    x: np.ndarray
    y: np.ndarray
    snr: np.ndarray
    bg: image.Background


def detect(frame: Frame, valid: np.ndarray, threshold: float = 4.0) -> Detections:
    bg = image.background(frame.grey, valid)
    x, y, s = image.peaks(bg, threshold)
    return Detections(x, y, s, bg)


def _catalogue(
    frame: Frame, vmax: float, min_alt: float, site: stars.Site
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cat = stars.bright_stars(vmax=vmax)
    alt, az = stars.star_altaz(cat, frame.time, site)
    up = alt > min_alt
    return cat[up], alt[up], az[up]


def _search(
    model: CameraModel,
    alt: np.ndarray,
    az: np.ndarray,
    det: Detections,
    tol: float,
    rots: np.ndarray,
    parities: tuple[int, ...],
    shifts: np.ndarray,
    shape: tuple[int, int],
    scales: tuple[float, ...] = (1.0,),
) -> tuple[int, float, int, float, float, float]:
    """Best (count, rot, parity, dx, dy, scale) for stars landing within
    ``tol`` of a peak."""
    h, w = shape
    occ = np.zeros((h, w), dtype=bool)
    xi = np.clip(np.rint(det.x).astype(int), 0, w - 1)
    yi = np.clip(np.rint(det.y).astype(int), 0, h - 1)
    occ[yi, xi] = True
    occ = image.dilate(occ, round(tol))
    m0 = replace(model, rot=0.0, parity=1)
    x, y = m0.to_pixel(alt, az)
    r0 = np.hypot(x - m0.x0, y - m0.y0)
    phi = -np.arctan2(x - m0.x0, -(y - m0.y0))  # psi = -phi at rot 0, parity 1
    sx = shifts[:, 0][:, None]
    sy = shifts[:, 1][:, None]
    best = (-1, 0.0, 1, 0.0, 0.0, 1.0)
    for scale in scales:
        r = r0 * scale
        for parity in parities:
            for rot in rots:
                psi = np.radians(rot) - parity * phi
                qx = np.rint(model.x0 + r * np.sin(psi) + sx).astype(int)
                qy = np.rint(model.y0 - r * np.cos(psi) + sy).astype(int)
                ok = (qx >= 0) & (qy >= 0) & (qx < w) & (qy < h)
                hit = np.zeros(qx.shape, dtype=bool)
                hit[ok] = occ[qy[ok], qx[ok]]
                n = hit.sum(axis=1)
                k = int(np.argmax(n))
                if n[k] > best[0]:
                    best = (int(n[k]), float(rot), parity, float(sx[k, 0]), float(sy[k, 0]), scale)
    return best


def match(
    model: CameraModel, alt: np.ndarray, az: np.ndarray, det: Detections, tol: float
) -> tuple[np.ndarray, np.ndarray]:
    """For each star the index of the nearest peak within ``tol`` px (-1 if
    none), and the distance."""
    px, py = model.to_pixel(alt, az)
    if det.x.size == 0:
        return np.full(alt.size, -1), np.full(alt.size, np.inf)
    idx = np.full(alt.size, -1)
    dist = np.full(alt.size, np.inf)
    for lo in range(0, alt.size, 512):
        d = np.hypot(px[lo : lo + 512, None] - det.x[None], py[lo : lo + 512, None] - det.y[None])
        j = np.argmin(d, axis=1)
        dd = d[np.arange(j.size), j]
        ok = dd <= tol
        idx[lo : lo + 512] = np.where(ok, j, -1)
        dist[lo : lo + 512] = dd
    # One peak per star: keep the closest claimant.
    order = np.argsort(dist)
    seen: set[int] = set()
    for i in order:
        j = idx[i]
        if j < 0:
            continue
        if j in seen:
            idx[i] = -1
        seen.add(j)
    return idx, dist


@dataclass
class SolveResult:
    fit: FitResult
    search_count: int
    n_bright: int
    alt: np.ndarray
    az: np.ndarray
    x: np.ndarray
    y: np.ndarray


def solve(
    frame: Frame,
    spec: CameraSpec,
    start: CameraModel | None = None,
    *,
    vmax_search: float = 3.5,
    vmax_fit: float = 5.5,
    search_rot: tuple[float, float] = (0.0, 360.0),
    parities: tuple[int, ...] = (1, -1),
    shift_px: float = 0.0,
    scales: tuple[float, ...] = (1.0,),
    n_peaks: int = 250,
    min_alt: float = 12.0,
    mask: np.ndarray | None = None,
) -> SolveResult:
    """Fit a model to one clear frame (module doc). ``mask``: extra pixels
    to ignore (the static-feature mask)."""
    model = start or guess_model(spec)
    valid = sky_valid(frame, spec, None if start is None else model, min_alt=2.0, mask=mask)
    det = detect(frame, valid, 5.0)
    o = np.argsort(-det.snr)[:n_peaks]
    bright = Detections(det.x[o], det.y[o], det.snr[o], det.bg)
    cat, alt, az = _catalogue(frame, vmax_search, min_alt, _site(spec))
    tol = max(6.0, model.f1 * math.radians(1.0))
    step = max(0.25, math.degrees(tol / (0.6 * model.f1 * math.pi / 2)) / 2)
    rots = np.arange(search_rot[0], search_rot[1], step)
    if shift_px > 0:
        g = np.arange(-shift_px, shift_px + 0.1, tol)
        shifts = np.array([(a, b) for a in g for b in g])
    else:
        shifts = np.zeros((1, 2))
    n, rot, parity, dx, dy, scale = _search(
        model, alt, az, bright, tol, rots, parities, shifts, frame.shape, scales
    )
    model = replace(
        model,
        rot=rot % 360,
        parity=parity,
        x0=model.x0 + dx,
        y0=model.y0 + dy,
        f1=model.f1 * scale,
        f2=model.f2 * scale,
        f3=model.f3 * scale,
    )
    # Refine with all detections and fainter stars, shrinking the radius.
    det = detect(frame, valid, 4.0)
    cat, alt, az = _catalogue(frame, vmax_fit, min_alt, _site(spec))
    plan = [
        (tol, ("x0", "y0", "rot", "f1"), vmax_search),
        (tol * 0.7, ("x0", "y0", "rot", "f1", "f2"), vmax_search + 0.5),
        (5.0, ("x0", "y0", "rot", "tilt_e", "tilt_n", "f1", "f2", "f3"), vmax_fit),
        (3.5, ("x0", "y0", "rot", "tilt_e", "tilt_n", "f1", "f2", "f3"), vmax_fit),
        (3.0, ("x0", "y0", "rot", "tilt_e", "tilt_n", "f1", "f2", "f3"), vmax_fit),
    ]
    res = None
    for t, free, vm in plan:
        sel = cat["v"] <= vm
        idx, _ = match(model, alt[sel], az[sel], det, t)
        ok = idx >= 0
        if ok.sum() < 10:
            continue
        res = fit(model, alt[sel][ok], az[sel][ok], det.x[idx[ok]], det.y[idx[ok]], free=free)
        model = res.model
    if res is None:
        raise RuntimeError(f"{spec.name}: too few stars matched ({n} in the search)")
    sel = cat["v"] <= vmax_fit
    idx, _ = match(model, alt[sel], az[sel], det, 3.0)
    ok = idx >= 0
    model.meta.update(
        frame=frame.path, frame_time=frame.time.isoformat(), search_matches=n, n_bright=int(o.size)
    )
    return SolveResult(
        res, n, int(o.size), alt[sel][ok], az[sel][ok], det.x[idx[ok]], det.y[idx[ok]]
    )


def matches_for(
    frame: Frame,
    spec: CameraSpec,
    model: CameraModel,
    vmax: float = 5.5,
    tol: float = 3.0,
    min_alt: float = 12.0,
    mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    valid = sky_valid(frame, spec, model, min_alt=2.0, mask=mask)
    det = detect(frame, valid, 4.0)
    _cat, alt, az = _catalogue(frame, vmax, min_alt, _site(spec))
    idx, _ = match(model, alt, az, det, tol)
    ok = idx >= 0
    return alt[ok], az[ok], det.x[idx[ok]], det.y[idx[ok]]


def refine(
    frames: list[Frame],
    spec: CameraSpec,
    model: CameraModel,
    tol: float = 3.0,
    mask: np.ndarray | None = None,
) -> FitResult:
    """Joint fit over several (clear) frames' matches."""
    for _ in range(2):
        parts = [matches_for(f, spec, model, tol=tol, mask=mask) for f in frames]
        alt, az, x, y = (np.concatenate([p[i] for p in parts]) for i in range(4))
        res = fit(model, alt, az, x, y)
        model = res.model
    model.meta.update(frames=[f.path for f in frames], n_frames=len(frames))
    return res


@dataclass
class Check:
    n_expected: int
    n_matched: int
    rms_px: float
    mean_dx: float
    mean_dy: float
    ok: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def check(
    frame: Frame,
    spec: CameraSpec,
    model: CameraModel,
    *,
    vmax: float = 3.5,
    min_alt: float = 25.0,
    tol: float = 4.0,
    min_frac: float = 0.5,
    max_offset_px: float = 1.5,
    mask: np.ndarray | None = None,
) -> Check:
    """Does ``model`` still fit ``frame``? Uses the bright stars only, so a
    partly cloudy frame can still pass; a frame with few stars returns
    ``ok=False`` with reason 'too few stars' (not evidence of a bump)."""
    valid = sky_valid(frame, spec, model, min_alt=2.0, mask=mask)
    det = detect(frame, valid, 5.0)
    _cat, alt, az = _catalogue(frame, vmax, min_alt, _site(spec))
    px, py = model.to_pixel(alt, az)
    inside = valid[
        np.clip(np.rint(py).astype(int), 0, frame.shape[0] - 1),
        np.clip(np.rint(px).astype(int), 0, frame.shape[1] - 1),
    ]
    alt, az, px, py = alt[inside], az[inside], px[inside], py[inside]
    idx, _ = match(model, alt, az, det, tol)
    ok = idx >= 0
    n = int(ok.sum())
    if n < 5:
        return Check(alt.size, n, math.nan, math.nan, math.nan, False, "too few stars")
    dx = det.x[idx[ok]] - px[ok]
    dy = det.y[idx[ok]] - py[ok]
    rms = float(np.sqrt(np.mean(dx**2 + dy**2)))
    mdx, mdy = float(np.median(dx)), float(np.median(dy))
    frac = n / max(alt.size, 1)
    if math.hypot(mdx, mdy) > max_offset_px:
        return Check(alt.size, n, rms, mdx, mdy, False, "offset: camera moved? refit")
    if frac < min_frac:
        return Check(alt.size, n, rms, mdx, mdy, False, "few matches: clouds, or a bump")
    return Check(alt.size, n, rms, mdx, mdy, True, "ok")
