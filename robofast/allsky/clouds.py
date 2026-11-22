"""Per-frame cloud maps: which of the stars that should be visible are.

For one frame (:func:`analyze`):

1. **Mask** what is not sky: below 15 deg, the camera's fixed masks (text,
   mast), saturated pixels, coloured overlay labels, the static-feature
   mask from the clear-sky calibration (hot pixels, overlay lines, lights),
   the Moon (a disc whose radius grows with its phase) and the planets.
2. **Detect**: the background-subtracted, smoothed frame as signal-to-noise
   (:mod:`robofast.allsky.image`). A catalogue star counts as detected if
   the S/N peaks above ``threshold`` within ``radius`` px of where the camera
   model puts it. The same test at **control** positions (offset from each
   star) measures how often noise alone passes, per patch.
3. **Expect**: each star's probability of being detected on a clear night,
   ``P = 1 / (1 + exp((V - m_lim) / w))``, where the limit ``m_lim`` drops
   with airmass (extinction and vignetting together, fitted from clear
   frames: :mod:`robofast.allsky.skycal`) and with the local noise above the
   clear-sky noise (Moon, twilight): ``-2.5 log10(noise / clear noise)``.
4. **Patches** (:data:`RINGS`: 36 near-equal-area cells above 20 deg): the
   clear fraction is the detections in excess of the false-positive rate
   over their clear-sky expectation, each star weighted by its ``P``:
   ``sum(P (det - fp)) / sum(P^2 (1 - fp))``, clipped to [0, 1]. On a clear
   sky the numerator's expectation is the denominator; bright stars (sure
   to show) weigh most, stars near the limit hardly count. A patch
   with too little expected weight is ``unknown``, unless its background
   is well above the clear-sky background on a dark night (cloud lit from
   below), in which case it is ``cloudy`` from the background alone.

:class:`CloudMap` holds the result; :meth:`CloudMap.clear_fraction` answers
for any pointing and radius from the stars themselves; :func:`combine`
pools two cameras' stars; :func:`trend` fits each patch's clear fraction
over time.
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast.allsky import image, stars
from robofast.allsky.calibrate import sky_valid
from robofast.allsky.cameras import CameraSpec
from robofast.allsky.frames import Frame
from robofast.allsky.model import CameraModel

__all__ = [
    "RINGS",
    "CloudMap",
    "analyze",
    "combine",
    "patch_index",
    "patch_stats",
    "patches",
    "trend",
]

#: (alt_lo, alt_hi, number of azimuth sectors): 36 cells of 0.015-0.019 sr.
RINGS = ((20.0, 35.0, 12), (35.0, 50.0, 10), (50.0, 65.0, 8), (65.0, 80.0, 5), (80.0, 90.01, 1))
MIN_ALT = RINGS[0][0]

#: Default analysis settings (overridden by the camera's ``sky`` calibration).
DEFAULTS: dict[str, Any] = {
    "threshold": 4.0,  # S/N for a detection
    "radius_px": 3.0,  # search radius around the predicted position
    "m0": 4.0,  # limiting V at the zenith on a clear dark night
    "a1": -0.6,  # change of the limit per unit airmass above 1
    "width": 0.35,  # logistic width, mag
    "min_weight": 2.0,  # expected detections for a patch to be known
    "bright_ratio": 1.6,  # background / clear-sky background for "lit cloud"
    "clear_noise": None,  # coarse grid of clear-sky noise (list of lists)
    "clear_bg_patch": None,  # clear-sky background per patch
    "noise_block": 32,
    "twilight": 0.0,  # mag per degree of Sun above -18 deg
    "moon": 0.0,  # mag at full Moon in the zenith (moon_factor 1)
}


@dataclass(frozen=True)
class Patch:
    index: int
    alt_lo: float
    alt_hi: float
    az_lo: float
    az_hi: float

    @property
    def alt(self) -> float:
        return 0.5 * (self.alt_lo + self.alt_hi)

    @property
    def az(self) -> float:
        return 0.5 * (self.az_lo + self.az_hi) % 360.0 if self.alt_hi < 90 else 0.0


def patches() -> list[Patch]:
    out = []
    for lo, hi, n in RINGS:
        width = 360.0 / n
        for k in range(n):
            out.append(Patch(len(out), lo, hi, k * width, (k + 1) * width))
    return out


PATCHES = patches()


def patch_index(alt: Any, az: Any) -> np.ndarray:
    """The patch of each (alt, az); -1 below :data:`MIN_ALT`."""
    alt = np.asarray(alt, dtype=float)
    az = np.asarray(az, dtype=float) % 360.0
    out = np.full(alt.shape, -1, dtype=int)
    base = 0
    for lo, hi, n in RINGS:
        sel = (alt >= lo) & (alt < hi)
        out[sel] = base + np.minimum((az[sel] / (360.0 / n)).astype(int), n - 1)
        base += n
    return out


def _sep(alt1, az1, alt2, az2) -> np.ndarray:
    a1, z1, a2, z2 = (np.radians(np.asarray(v, dtype=float)) for v in (alt1, az1, alt2, az2))
    c = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(z1 - z2)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


def moon_radius_deg(illum: float) -> float:
    """Masked radius around the Moon: 4 deg new, 20 deg full."""
    return 4.0 + 16.0 * float(np.clip(illum, 0, 1))


#: Fields of the per-star table (one row per catalogue star considered).
STAR_FIELDS = ("v", "alt", "az", "x", "y", "p", "det", "fp", "snr", "noise_ratio", "patch")


@dataclass
class CloudMap:
    """One frame's (or several cameras' pooled) cloud map."""

    time: datetime
    cameras: list[str]
    #: Per patch: clear fraction (NaN unknown), expected weight, detections,
    #: background ratio, status.
    clear: np.ndarray
    weight: np.ndarray
    detected: np.ndarray
    bg_ratio: np.ndarray
    status: list[str]
    #: Per-star table (:data:`STAR_FIELDS`), for :meth:`clear_fraction`.
    table: dict[str, np.ndarray]
    sun_alt: float = math.nan
    moon_alt: float = math.nan
    moon_illum: float = math.nan
    flags: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def whole_sky(self) -> float:
        """Mean clear fraction over the known patches (equal-area cells)."""
        ok = ~np.isnan(self.clear)
        return float(np.mean(self.clear[ok])) if ok.any() else math.nan

    @property
    def coverage(self) -> float:
        """Fraction of the patches that are known."""
        return float(np.mean(~np.isnan(self.clear)))

    def clear_fraction(
        self, alt: float, az: float, radius: float = 10.0, *, min_weight: float | None = None
    ) -> float:
        """Clear fraction within ``radius`` deg of (alt, az), NaN if unknown."""
        return self.clear_fraction_detail(alt, az, radius, min_weight=min_weight)[0]

    def clear_fraction_detail(
        self, alt: float, az: float, radius: float = 10.0, *, min_weight: float | None = None
    ) -> tuple[float, float, str]:
        """(fraction, expected weight, source): from the stars within
        ``radius`` if they carry ``min_weight``, else from the patches the
        circle touches (weighted by their expected weight), else from the
        background of the patch it is in ('background'), else NaN."""
        mw = self.settings.get("min_weight", 2.0) if min_weight is None else min_weight
        t = self.table
        if t["v"].size:
            d = _sep(alt, az, t["alt"], t["az"])
            sel = d <= radius
            p, fp, det = t["p"][sel], t["fp"][sel], t["det"][sel]
            e = float(np.sum(p * (1 - fp)))
            if e >= mw:
                frac = float(np.sum(p * (det - fp)) / np.sum(p * p * (1 - fp)))
                return float(np.clip(frac, 0, 1)), e, "stars"
        centres_alt = np.array([p.alt for p in PATCHES])
        centres_az = np.array([p.az for p in PATCHES])
        d = _sep(alt, az, centres_alt, centres_az)
        near = (d <= radius + 10.0) & ~np.isnan(self.clear) & (self.weight > 0)
        if near.any():
            w = self.weight[near]
            return float(np.sum(self.clear[near] * w) / np.sum(w)), float(np.sum(w)), "patches"
        k = int(patch_index(alt, az))
        if k >= 0 and self.status[k] == "cloudy":
            return 0.0, 0.0, "background"
        return math.nan, 0.0, "none"

    def to_json(self) -> dict[str, Any]:
        def f(v: float) -> float | None:
            return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, 4)

        return {
            "time": self.time.isoformat(),
            "cameras": self.cameras,
            "frames": self.frames,
            "whole_sky_clear": f(self.whole_sky),
            "coverage": round(self.coverage, 3),
            "sun_alt": f(self.sun_alt),
            "moon_alt": f(self.moon_alt),
            "moon_illum": f(self.moon_illum),
            "flags": self.flags,
            "patches": [
                {
                    "index": p.index,
                    "alt": [p.alt_lo, round(min(p.alt_hi, 90.0), 2)],
                    "az": [p.az_lo, p.az_hi],
                    "clear": f(float(self.clear[p.index])),
                    "expected": round(float(self.weight[p.index]), 2),
                    "detected": round(float(self.detected[p.index]), 2),
                    "bg_ratio": f(float(self.bg_ratio[p.index])),
                    "status": self.status[p.index],
                }
                for p in PATCHES
            ],
            "n_stars": int(self.table["v"].size),
            "settings": {k: v for k, v in self.settings.items() if not isinstance(v, list)},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1) + "\n")


def _disk_offsets(radius: float) -> tuple[np.ndarray, np.ndarray]:
    r = math.ceil(radius)
    dy, dx = np.mgrid[-r : r + 1, -r : r + 1]
    keep = dx * dx + dy * dy <= radius * radius + 1e-9
    return dx[keep], dy[keep]


def _max_in_disk(a: np.ndarray, x: np.ndarray, y: np.ndarray, radius: float) -> np.ndarray:
    """Max of ``a`` within ``radius`` of each (x, y); -inf off the image."""
    h, w = a.shape
    dx, dy = _disk_offsets(radius)
    xi = np.rint(x).astype(int)[:, None] + dx[None]
    yi = np.rint(y).astype(int)[:, None] + dy[None]
    ok = (xi >= 0) & (yi >= 0) & (xi < w) & (yi < h)
    v = np.full(xi.shape, -np.inf, dtype=np.float32)
    v[ok] = a[yi[ok], xi[ok]]
    return v.max(axis=1)


def _all_valid_in_disk(valid: np.ndarray, x, y, radius: float) -> np.ndarray:
    h, w = valid.shape
    dx, dy = _disk_offsets(radius)
    xi = np.rint(x).astype(int)[:, None] + dx[None]
    yi = np.rint(y).astype(int)[:, None] + dy[None]
    ok = (xi >= 0) & (yi >= 0) & (xi < w) & (yi < h)
    v = np.zeros(xi.shape, dtype=bool)
    v[ok] = valid[yi[ok], xi[ok]]
    return v.all(axis=1)


def airmass(alt: Any) -> np.ndarray:
    """Kasten & Young (1989)."""
    a = np.asarray(alt, dtype=float)
    return 1.0 / (np.sin(np.radians(a)) + 0.50572 * (a + 6.07995) ** -1.6364)


def moon_factor(illum: float, moon_alt: float, sep: Any) -> np.ndarray:
    """How much the Moon brightens the sky at angular distance ``sep``
    (deg): ``illum * sqrt(sin(moon_alt)) * (1 + exp(-sep / 30))``, zero
    with the Moon down. Scaled by the calibration's ``moon`` (mag)."""
    if moon_alt <= 0:
        return np.zeros_like(np.asarray(sep, dtype=float))
    base = illum * math.sqrt(math.sin(math.radians(moon_alt)))
    return base * (1.0 + np.exp(-np.asarray(sep, dtype=float) / 30.0))


def limit_mag(
    sky: dict[str, Any],
    alt: Any,
    noise_ratio: Any,
    sun_alt: float | None = None,
    moon: Any = None,
) -> np.ndarray:
    """Limiting V: clear-sky zenith limit, airmass term, noise penalty, the
    twilight term (``twilight`` mag per degree of Sun above -18 deg) and the
    Moon term (``moon`` mag times :func:`moon_factor`). The cameras'
    automatic gain hides twilight and moonlight from the noise penalty,
    hence the two fitted terms."""
    x = np.minimum(airmass(alt), 3.0)
    nr = np.maximum(np.asarray(noise_ratio, dtype=float), 1.0)
    m = sky["m0"] + sky["a1"] * (x - 1.0) - 2.5 * np.log10(nr)
    if sun_alt is not None and sky.get("twilight"):
        m = m - float(sky["twilight"]) * float(np.clip(sun_alt + 18.0, 0.0, 12.0))
    if moon is not None and sky.get("moon"):
        m = m - float(sky["moon"]) * np.asarray(moon, dtype=float)
    return m


def p_detect(
    sky: dict[str, Any],
    v: Any,
    alt: Any,
    noise_ratio: Any,
    sun_alt: float | None = None,
    moon: Any = None,
) -> np.ndarray:
    lim = limit_mag(sky, alt, noise_ratio, sun_alt, moon)
    z = (np.asarray(v, dtype=float) - lim) / sky["width"]
    return 1.0 / (1.0 + np.exp(np.clip(z, -50, 50)))


@dataclass
class Measurement:
    """Everything one frame yields before the patch statistics."""

    frame: Frame
    table: dict[str, np.ndarray]
    #: Control detections (fp) pooled per patch: (trials, passes).
    ctrl_n: np.ndarray
    ctrl_k: np.ndarray
    bg_patch: np.ndarray
    noise_patch: np.ndarray
    eph: stars.Ephemeris
    valid: np.ndarray
    bg: image.Background


def _settings(model: CameraModel) -> dict[str, Any]:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (model.sky or {}).items() if v is not None})
    return s


_STATIC: dict[tuple, np.ndarray] = {}


def static_mask(model: CameraModel, shape: tuple[int, int]) -> np.ndarray | None:
    name = (model.sky or {}).get("static_mask")
    if not name:
        return None
    from robofast.allsky import imageio
    from robofast.allsky.cameras import MODELS_DIR

    p = Path(name)
    if not p.is_absolute():
        p = MODELS_DIR / p
    if not p.is_file():
        return None
    key = (str(p), p.stat().st_mtime)
    if key not in _STATIC:
        _STATIC.clear()
        _STATIC[key] = imageio.read_mask(p)
    m = _STATIC[key]
    return m if m.shape == shape else None


def label_boxes(
    spec: CameraSpec,
    model: CameraModel,
    cat: np.ndarray,
    alt: np.ndarray,
    az: np.ndarray,
    eph: stars.Ephemeris,
    shape: tuple[int, int],
) -> np.ndarray:
    """The overlay's name labels (MMTO): a box ``spec.label_box`` =
    (left, right, up, down) px about each star brighter than
    ``spec.label_vmax``, each planet and the Moon; the label text may be
    white, so the colour mask misses it."""
    h, w = shape
    m = np.zeros(shape, dtype=bool)
    sel = cat["v"] <= spec.label_vmax
    pa = [a for a, _ in eph.planets.values()] + [eph.moon_alt]
    pz = [z for _, z in eph.planets.values()] + [eph.moon_az]
    la = np.concatenate([alt[sel], np.asarray(pa)])
    lz = np.concatenate([az[sel], np.asarray(pz)])
    up = la > 0
    if not up.any():
        return m
    x, y = model.to_pixel(la[up], lz[up])
    left, right, top, bottom = spec.label_box  # type: ignore[misc]
    for xi, yi in zip(x, y, strict=True):
        x0, x1 = int(max(xi - left, 0)), int(min(xi + right, w))
        y0, y1 = int(max(yi - top, 0)), int(min(yi + bottom, h))
        if x1 > x0 and y1 > y0:
            m[y0:y1, x0:x1] = True
    return m


def measure(
    frame: Frame,
    spec: CameraSpec,
    model: CameraModel,
    *,
    vmax: float | None = None,
    eph: stars.Ephemeris | None = None,
    settings: dict[str, Any] | None = None,
) -> Measurement:
    s = settings or _settings(model)
    eph = eph or stars.ephemeris(frame.time)
    h, w = frame.shape
    valid = sky_valid(frame, spec, model, min_alt=MIN_ALT - 5)
    sm = static_mask(model, (h, w))
    if sm is not None:
        valid &= ~sm
    # Background and noise from the sky only.
    bg = image.background(frame.grey, valid, s["noise_block"])
    # Moon and planets.
    alt_px, az_px = model.altaz_grid(4)
    if eph.moon_alt > -2:
        r_moon = moon_radius_deg(eph.moon_illum)
        near = _sep(alt_px, az_px, eph.moon_alt, eph.moon_az) < r_moon
        valid &= ~np.repeat(np.repeat(near, 4, 0), 4, 1)[:h, :w]
    cat = stars.bright_stars(vmax=vmax or min(6.5, s["m0"] + 1.5))
    alt, az = stars.star_altaz(cat, frame.time)
    up = alt > MIN_ALT
    cat, alt, az = cat[up], alt[up], az[up]
    for _name, (pa, pz) in eph.planets.items():
        if pa > 0:
            keep = _sep(alt, az, pa, pz) > 1.5
            cat, alt, az = cat[keep], alt[keep], az[keep]
    if spec.label_box is not None:
        valid = valid & ~label_boxes(spec, model, cat, alt, az, eph, (h, w))
    x, y = model.to_pixel(alt, az)
    rad = float(s["radius_px"])
    inside = _all_valid_in_disk(valid, x, y, rad)
    cat, alt, az, x, y = cat[inside], alt[inside], az[inside], x[inside], y[inside]
    snr = _max_in_disk(bg.snr, x, y, rad)
    det = snr > s["threshold"]
    # Noise relative to the clear-sky noise at the same place.
    cn = s.get("clear_noise")
    xi = np.clip(np.rint(x).astype(int), 0, w - 1)
    yi = np.clip(np.rint(y).astype(int), 0, h - 1)
    if cn is not None:
        cgrid = np.asarray(cn, dtype=np.float32)
        b = int(s["noise_block"])
        clear_noise = cgrid[
            np.minimum(yi // b, cgrid.shape[0] - 1), np.minimum(xi // b, cgrid.shape[1] - 1)
        ]
        noise_ratio = bg.noise[yi, xi] / np.maximum(clear_noise, 1e-3)
    else:
        noise_ratio = np.ones(x.size)
    mf = moon_factor(eph.moon_illum, eph.moon_alt, _sep(alt, az, eph.moon_alt, eph.moon_az))
    p = p_detect(s, cat["v"], alt, noise_ratio, eph.sun_alt, mf)
    keep = p >= 0.02
    cat, alt, az, x, y, snr, det = (
        cat[keep],
        alt[keep],
        az[keep],
        x[keep],
        y[keep],
        snr[keep],
        det[keep],
    )
    p, noise_ratio = p[keep], noise_ratio[keep]
    pidx = patch_index(alt, az)
    # Controls: four positions around each star, 4*radius+3 px away.
    off = 4 * rad + 3
    ctrl_n = np.zeros(len(PATCHES))
    ctrl_k = np.zeros(len(PATCHES))
    for cx, cy in ((off, 0), (-off, 0), (0, off), (0, -off)):
        ok = _all_valid_in_disk(valid, x + cx, y + cy, rad)
        k = _max_in_disk(bg.snr, x[ok] + cx, y[ok] + cy, rad) > s["threshold"]
        np.add.at(ctrl_n, pidx[ok], 1)
        np.add.at(ctrl_k, pidx[ok], k)
    # Background and noise per patch.
    a4, z4 = alt_px, az_px
    pix_patch = patch_index(a4, z4)
    v4 = valid[::4, ::4][: a4.shape[0], : a4.shape[1]]
    bg4 = bg.bg[::4, ::4][: a4.shape[0], : a4.shape[1]]
    nz4 = bg.noise[::4, ::4][: a4.shape[0], : a4.shape[1]]
    bgp = np.full(len(PATCHES), np.nan)
    nzp = np.full(len(PATCHES), np.nan)
    for k in range(len(PATCHES)):
        sel = (pix_patch == k) & v4
        if sel.sum() > 20:
            bgp[k] = float(np.median(bg4[sel]))
            nzp[k] = float(np.median(nz4[sel]))
    table = {
        "v": cat["v"].astype(float),
        "alt": alt,
        "az": az,
        "x": x,
        "y": y,
        "p": p,
        "det": det.astype(float),
        "fp": np.zeros(x.size),
        "snr": snr.astype(float),
        "noise_ratio": noise_ratio.astype(float),
        "patch": pidx.astype(float),
    }
    return Measurement(frame, table, ctrl_n, ctrl_k, bgp, nzp, eph, valid, bg)


def _fp_rates(ctrl_n: np.ndarray, ctrl_k: np.ndarray) -> np.ndarray:
    """Per-patch false-positive rate, shrunk toward the whole-frame rate."""
    total = (ctrl_k.sum() + 0.5) / (ctrl_n.sum() + 1.0)
    prior = 10.0
    return (ctrl_k + prior * total) / (ctrl_n + prior)


def patch_stats(
    table: dict[str, np.ndarray],
    bg_ratio: np.ndarray,
    *,
    min_weight: float,
    bright_ratio: float,
    dark: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """(clear, expected weight, excess detections, status) per patch."""
    n = len(PATCHES)
    pidx = table["patch"].astype(int)
    e = np.zeros(n)
    d = np.zeros(n)
    ok = pidx >= 0
    p, fp, det = table["p"], table["fp"], table["det"]
    num = np.zeros(n)
    den = np.zeros(n)
    np.add.at(e, pidx[ok], (p * (1 - fp))[ok])
    np.add.at(d, pidx[ok], (det - fp)[ok])
    np.add.at(num, pidx[ok], (p * (det - fp))[ok])
    np.add.at(den, pidx[ok], (p * p * (1 - fp))[ok])
    clear = np.full(n, np.nan)
    known = e >= min_weight
    clear[known] = np.clip(num[known] / den[known], 0, 1)
    status = []
    for k in range(n):
        if known[k]:
            c = clear[k]
            status.append("clear" if c >= 0.7 else ("partly" if c >= 0.3 else "cloudy"))
        elif dark and np.isfinite(bg_ratio[k]) and bg_ratio[k] >= bright_ratio:
            status.append("cloudy")
            clear[k] = 0.0
        else:
            status.append("unknown")
    return clear, e, d, status


def _flags(eph: stars.Ephemeris) -> list[str]:
    out = []
    if eph.sun_alt > -6:
        out.append("day")
    elif eph.sun_alt > -15:
        out.append("twilight")
    if eph.moon_alt > 0:
        out.append(f"moon {eph.moon_illum:.0%}")
    return out


def from_measurements(ms: list[Measurement], settings: dict[str, Any]) -> CloudMap:
    """A map from one or more measurements (several cameras pooled)."""
    tables = []
    bgr = []
    for m in ms:
        fp = _fp_rates(m.ctrl_n, m.ctrl_k)
        t = dict(m.table)
        pid = t["patch"].astype(int)
        t["fp"] = np.where(pid >= 0, fp[np.maximum(pid, 0)], 0.0)
        tables.append(t)
        cb = settings.get(f"clear_bg_patch:{m.frame.camera}") or settings.get("clear_bg_patch")
        if cb is not None:
            cbv = np.asarray([np.nan if v is None else v for v in cb], dtype=float)
            bgr.append(m.bg_patch / np.maximum(cbv, 1e-3))
        else:
            bgr.append(np.full(len(PATCHES), np.nan))
    table = {k: np.concatenate([t[k] for t in tables]) for k in STAR_FIELDS}
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        bg_ratio = np.nanmean(np.vstack(bgr), axis=0) if bgr else np.full(len(PATCHES), np.nan)
    eph = ms[0].eph
    dark = eph.sun_alt < -15 and (eph.moon_alt < 0 or eph.moon_illum < 0.3)
    clear, e, d, status = patch_stats(
        table,
        bg_ratio,
        min_weight=settings["min_weight"],
        bright_ratio=settings["bright_ratio"],
        dark=dark,
    )
    flags = _flags(eph)
    if eph.sun_alt > -6:
        clear[:] = np.nan
        status = ["unknown"] * len(PATCHES)
    return CloudMap(
        time=max(m.frame.time for m in ms),
        cameras=[m.frame.camera for m in ms],
        clear=clear,
        weight=e,
        detected=d,
        bg_ratio=bg_ratio,
        status=status,
        table=table,
        sun_alt=eph.sun_alt,
        moon_alt=eph.moon_alt,
        moon_illum=eph.moon_illum,
        flags=flags,
        frames=[m.frame.path for m in ms],
        settings={k: v for k, v in settings.items() if not isinstance(v, (list, dict))},
    )


def analyze(
    frame: Frame, spec: CameraSpec, model: CameraModel, *, eph: stars.Ephemeris | None = None
) -> tuple[CloudMap, Measurement]:
    s = _settings(model)
    m = measure(frame, spec, model, eph=eph, settings=s)
    if s.get("clear_bg_patch") is not None:
        s[f"clear_bg_patch:{frame.camera}"] = s["clear_bg_patch"]
    return from_measurements([m], s), m


def combine(items: list[tuple[Measurement, CameraModel]], max_age_s: float = 180.0) -> CloudMap:
    """Pool the stars of several cameras' measurements taken within
    ``max_age_s`` of the newest; each keeps its own expectations and
    false-positive rates."""
    newest = max(m.frame.time for m, _ in items)
    keep = [(m, mod) for m, mod in items if (newest - m.frame.time).total_seconds() <= max_age_s]
    s = _settings(keep[0][1])
    s["min_weight"] = max(_settings(mod)["min_weight"] for _, mod in keep)
    for m, mod in keep:
        cb = _settings(mod).get("clear_bg_patch")
        if cb is not None:
            s[f"clear_bg_patch:{m.frame.camera}"] = cb
    s.pop("clear_bg_patch", None)
    return from_measurements([m for m, _ in keep], s)


def map_from_json(d: dict[str, Any]) -> CloudMap:
    """A :class:`CloudMap` from :meth:`CloudMap.to_json` (no star table:
    enough for :func:`trend` and the patch fallbacks)."""
    ps = d["patches"]
    nan = float("nan")
    return CloudMap(
        time=datetime.fromisoformat(d["time"]),
        cameras=list(d.get("cameras", [])),
        clear=np.array([nan if p["clear"] is None else p["clear"] for p in ps]),
        weight=np.array([p["expected"] for p in ps], dtype=float),
        detected=np.array([p["detected"] for p in ps], dtype=float),
        bg_ratio=np.array([nan if p["bg_ratio"] is None else p["bg_ratio"] for p in ps]),
        status=[p["status"] for p in ps],
        table={k: np.zeros(0) for k in STAR_FIELDS},
        sun_alt=d.get("sun_alt") or nan,
        moon_alt=d.get("moon_alt") or nan,
        moon_illum=d.get("moon_illum") or nan,
        flags=list(d.get("flags", [])),
        frames=list(d.get("frames", [])),
        settings=dict(d.get("settings", {})),
    )


@dataclass
class Trend:
    #: Per patch: slope (clear fraction per hour), state, number of frames.
    slope: np.ndarray
    state: list[str]
    n: np.ndarray
    whole_slope: float
    whole_state: str

    def to_json(self) -> dict[str, Any]:
        return {
            "whole_sky": {"slope_per_hour": _r(self.whole_slope), "state": self.whole_state},
            "patches": [
                {
                    "index": k,
                    "slope_per_hour": _r(float(self.slope[k])),
                    "state": self.state[k],
                    "n": int(self.n[k]),
                }
                for k in range(len(self.state))
            ],
        }


def _r(v: float) -> float | None:
    return None if math.isnan(v) else round(v, 3)


def _slope(t: np.ndarray, c: np.ndarray, min_n: int, change: float) -> tuple[float, str]:
    ok = np.isfinite(c)
    if ok.sum() < min_n:
        return math.nan, "unknown"
    t, c = t[ok], c[ok]
    span = t.max() - t.min()
    if span <= 0:
        return math.nan, "unknown"
    a, b = np.polyfit(t, c, 1)
    resid = c - (a * t + b)
    se = math.sqrt(max(np.sum(resid**2) / max(c.size - 2, 1), 1e-6) / np.sum((t - t.mean()) ** 2))
    if abs(a) * span >= change and abs(a) > 2.5 * se:
        return float(a), "rising" if a > 0 else "falling"
    return float(a), "stable"


def trend(maps: list[CloudMap], n: int = 10, min_n: int = 4, change: float = 0.15) -> Trend:
    """Per-patch clear-fraction trend over the last ``n`` maps: the slope
    (per hour) of a straight line; 'rising'/'falling' when the fitted change
    over the span is at least ``change`` and the slope is significant
    (2.5 sigma), else 'stable'; 'unknown' with fewer than ``min_n`` known
    values."""
    ms = sorted(maps, key=lambda m: m.time)[-n:]
    t0 = ms[0].time if ms else None
    t = np.array([(m.time - t0).total_seconds() / 3600.0 for m in ms]) if ms else np.zeros(0)
    c = np.array([m.clear for m in ms]) if ms else np.zeros((0, len(PATCHES)))
    slope = np.full(len(PATCHES), np.nan)
    state = []
    cnt = np.zeros(len(PATCHES), dtype=int)
    for k in range(len(PATCHES)):
        col = c[:, k] if c.size else np.zeros(0)
        cnt[k] = int(np.isfinite(col).sum())
        slope[k], st = _slope(t, col, min_n, change)
        state.append(st)
    whole = np.array([m.whole_sky for m in ms]) if ms else np.zeros(0)
    ws, wst = _slope(t, whole, min_n, change)
    return Trend(slope, state, cnt, ws, wst)
