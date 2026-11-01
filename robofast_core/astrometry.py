"""Plate solving: where an image points, from its stars and a catalog.

Two solvers (``docs/sky.md``):

* :func:`solve_near` -- robofast's own **near-blind** solver, the default
  for acquisition, pointing models and every frame's WCS. Given an
  approximate pointing (a few arcmin up to a degree off) and the plate scale
  (to a few per cent), it detects the stars
  (:func:`robofast_core.starmeasure.detect`), takes catalog stars around
  the guess (:class:`robofast_core.catalog.Catalog`), matches **triangles**
  of neighbouring stars by their side lengths (the scale is known, so the
  sides themselves are the invariants; rotation and parity are free unless
  hinted), turns each matched triangle into a candidate similarity
  transform, keeps the one most stars agree with, and fits a TAN WCS (and
  optionally SIP distortion) to the matched stars by linear least squares.
  Pure numpy; a 2048x2048 frame solves in about a second.
* :func:`solve_field` -- astrometry.net's ``solve-field``, blind, if it is
  installed, with index files from a local directory.

Both return a :class:`Solution` (an astropy WCS, the centre's RA/Dec, the
position angle, scale, parity, residuals, the matched stars) or raise
:class:`SolveError` saying why.

Conventions (those of :mod:`robofast_core.fits_writer`): pixels are 0-based
``(x, y)`` = (column, row); the FITS WCS is 1-based as FITS requires.
``pa`` is the position angle of the image's +y axis, degrees east of north;
``flip`` False means east is to the left when north is up (the sky as seen
on the sky: a determinant of CD < 0).

Few stars. A small field (the FAU's 8.5 x 5.4 arcmin) can hold only a
handful of catalog stars. With three or more matched stars the triangle
match decides; with two, a pair whose separation matches is accepted only
with ``min_matches=2`` and a known parity; with one, only with
``min_matches=1`` and a known PA (an offset, flagged ``degenerate``). The
default ``min_matches`` is 4, below which a solution can be a chance
alignment.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, Sip

from robofast_core.catalog import Catalog, Stars, _tan, _untan, separation

log = logging.getLogger(__name__)

__all__ = [
    "Detections",
    "Solution",
    "SolveError",
    "SolverConfig",
    "detect_stars",
    "solve_field",
    "solve_near",
    "wcs_cards",
    "zero_point",
]

_DEG = 180.0 / math.pi


class SolveError(RuntimeError):
    """The image could not be solved (the message says why)."""


@dataclass
class Detections:
    """Stars found in an image, brightest first (0-based pixels)."""

    x: np.ndarray
    y: np.ndarray
    flux: np.ndarray
    shape: tuple[int, int]
    noise: float = math.nan

    def __len__(self) -> int:
        return len(self.x)

    def take(self, n: int) -> Detections:
        return Detections(self.x[:n], self.y[:n], self.flux[:n], self.shape, self.noise)


def _interp_matrix(n: int, centres: np.ndarray) -> np.ndarray:
    """(n, len(centres)) weights interpolating linearly between centres
    (constant beyond the ends)."""
    m = np.zeros((n, len(centres)))
    if len(centres) == 1:
        m[:, 0] = 1.0
        return m
    pos = np.clip(np.interp(np.arange(n), centres, np.arange(len(centres))), 0, len(centres) - 1)
    lo = np.minimum(pos.astype(int), len(centres) - 2)
    f = pos - lo
    m[np.arange(n), lo] = 1.0 - f
    m[np.arange(n), lo + 1] += f
    return m


def _box3(a: np.ndarray) -> np.ndarray:
    """3x3 box mean (edges: as if padded with zeros)."""
    p = np.pad(a, 1)
    c = p[:-2] + p[1:-1] + p[2:]
    return (c[:, :-2] + c[:, 1:-1] + c[:, 2:]) / 9.0


def detect_stars(
    image: np.ndarray, *, threshold: float = 5.0, box: int = 64, max_stars: int = 200
) -> Detections:
    """Detect stars fast (a 2048x2048 frame in a few tenths of a second):
    the background from clipped block medians, interpolated bilinearly
    (two small matrix products); a 3x3 box smoothing; pixels above
    ``threshold`` times the smoothed noise grouped into 8-connected pieces
    (:mod:`robofast_core.starmeasure`'s labelling); flux-weighted centroids.
    Pieces under four pixels (hot pixels, cosmic rays) are dropped;
    saturated stars are kept (their centroids are still good), and pieces
    whose peak is far sharper than a star (hot pixels, cosmic rays) are
    dropped."""
    from robofast_core.starmeasure import _runs

    img = np.asarray(image, dtype=np.float32)
    if img.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {img.shape}")
    ny, nx = img.shape
    by, bx = max(1, ny // box), max(1, nx // box)
    hy, hx = ny // by, nx // bx
    blocks = img[: by * hy, : bx * hx].reshape(by, hy, bx, hx).transpose(0, 2, 1, 3)
    blocks = blocks.reshape(by, bx, -1)
    med = np.median(blocks, axis=2)
    mad = np.median(np.abs(blocks - med[..., None]), axis=2)
    # One clipping pass: drop pixels above 3 sigma (stars) and re-take the median.
    clipped = np.where(blocks > (med + 3 * 1.4826 * mad + 1e-6)[..., None], np.nan, blocks)
    grid = np.nanmedian(clipped, axis=2)
    cy = (np.arange(by) + 0.5) * hy - 0.5
    cx = (np.arange(bx) + 0.5) * hx - 0.5
    bkg = (_interp_matrix(ny, cy) @ grid @ _interp_matrix(nx, cx).T).astype(np.float32)
    resid = img - bkg
    sm = _box3(resid)
    step = max(1, int(math.sqrt(sm.size / 65536)))
    sub = sm[::step, ::step].ravel()
    noise = float(np.median(np.abs(sub - np.median(sub)))) * 1.4826
    if not np.isfinite(noise) or noise <= 0:
        noise = 1.0
    mask = sm > threshold * noise
    if not mask.any():
        e = np.zeros(0)
        return Detections(e, e, e, (ny, nx), noise)
    rows, starts, ends, labels = _runs(mask)
    # Every masked pixel, labelled by its run's piece.
    lengths = ends - starts
    pr = np.repeat(rows, lengths)
    pc = np.repeat(starts, lengths) + (
        np.arange(lengths.sum()) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    )
    ids, inv = np.unique(np.repeat(labels, lengths), return_inverse=True)
    w = np.clip(sm[pr, pc], 0, None).astype(float)
    tot = np.bincount(inv, weights=w, minlength=ids.size)
    area = np.bincount(inv, minlength=ids.size)
    sx = np.bincount(inv, weights=w * pc, minlength=ids.size)
    sy = np.bincount(inv, weights=w * pr, minlength=ids.size)
    # Hot pixels and cosmic rays: a peak far sharper than any star (its
    # four neighbours under a fifth of it; a star sampled at FWHM >= 1.5 px
    # keeps them above ~0.3).
    raw = resid[pr, pc].astype(float)
    order_px = np.lexsort((raw, inv))
    last = np.r_[np.nonzero(np.diff(inv[order_px]))[0], len(order_px) - 1]
    top = order_px[last]  # each piece's brightest pixel (pieces in id order)
    yy, xx = pr[top], pc[top]
    pad = np.pad(resid, 1, mode="edge")
    neigh = (pad[yy, xx + 1] + pad[yy + 2, xx + 1] + pad[yy + 1, xx] + pad[yy + 1, xx + 2]) / 4.0
    peak = resid[yy, xx]
    sharp = (peak > 5 * noise) & (neigh < 0.2 * peak)
    ok = (area >= 4) & (tot > 0) & ~sharp
    order = np.argsort(-tot[ok], kind="stable")[:max_stars]
    tot, sx, sy = tot[ok][order], sx[ok][order], sy[ok][order]
    return Detections(
        sx / tot,
        sy / tot,
        tot,
        (ny, nx),
        noise,
    )


@dataclass
class SolverConfig:
    """How to solve one camera's images (a ``solver`` entry in the site
    config, docs/sky.md)."""

    #: arcsec per unbinned pixel, and the fractional uncertainty.
    plate_scale: float
    scale_tolerance: float = 0.03
    #: Pointing uncertainty (deg): how far from the guess to look.
    search_radius: float = 0.25
    #: Position angle hint (deg) and its tolerance (None: any angle).
    pa: float | None = None
    pa_tolerance: float = 180.0
    #: None: either parity; False: east left (normal); True: flipped.
    flip: bool | None = None
    #: Brightest detections and catalog stars used for matching.
    max_stars: int = 40
    max_catalog: int = 400
    #: Fewest matched stars for a solution (module doc), and the fewest as
    #: a fraction of the stars that should match (the fewer of the
    #: detections and the catalog stars that land on the image): a chance
    #: alignment of a few stars in a rich field is not a solution.
    min_matches: int = 4
    min_fraction: float = 0.25
    #: Match radius in pixels for the final fit (and in arcsec, the larger).
    match_radius: float = 3.0
    #: SIP distortion order (0: none; 2 or 3 for a wide field).
    sip_order: int = 0
    #: Faintest catalog magnitude to use (None: the catalog's limit).
    mag_limit: float | None = None
    #: Give up after this long (s).
    timeout: float = 20.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SolverConfig:
        d = dict(d)
        d.pop("_note", None)
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"solver: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


@dataclass
class Solution:
    """A plate solution (module doc for the conventions)."""

    wcs: WCS
    #: RA/Dec (deg) of the image centre (``((nx - 1) / 2, (ny - 1) / 2)``).
    ra: float
    dec: float
    pa: float
    scale: float
    flip: bool
    #: Rms of the matched stars' residuals (arcsec), and how many.
    rms: float
    n_matched: int
    n_detected: int
    n_catalog: int
    method: str
    seconds: float
    #: Fewer than three stars: an offset or a pair, not a full solution.
    degenerate: bool = False
    #: Matched stars: pixel x, y, catalog ra, dec, G, flux (detection).
    matches: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    shape: tuple[int, int] = (0, 0)

    def pixel(self, ra: Any, dec: Any) -> tuple[np.ndarray, np.ndarray]:
        """0-based pixel of sky positions (deg)."""
        x, y = self.wcs.all_world2pix(np.atleast_1d(ra), np.atleast_1d(dec), 0)
        return np.asarray(x), np.asarray(y)

    def sky(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        ra, dec = self.wcs.all_pix2world(np.atleast_1d(x), np.atleast_1d(y), 0)
        return np.asarray(ra), np.asarray(dec)

    def summary(self) -> dict[str, Any]:
        return {
            "ra": round(self.ra, 7),
            "dec": round(self.dec, 7),
            "pa": round(self.pa, 4),
            "scale": round(self.scale, 5),
            "flip": self.flip,
            "rms": round(self.rms, 3),
            "n_matched": self.n_matched,
            "n_detected": self.n_detected,
            "method": self.method,
            "seconds": round(self.seconds, 3),
            "degenerate": self.degenerate,
        }


# -- geometry helpers --------------------------------------------------------------


def _cd(scale_arcsec: float, pa_deg: float, flip: bool) -> np.ndarray:
    """The CD matrix (deg/px) for a scale, PA and parity (fits_writer's)."""
    s = scale_arcsec / 3600.0
    p = math.radians(pa_deg)
    parity = 1.0 if flip else -1.0
    return np.array(
        [[parity * s * math.cos(p), -parity * s * math.sin(p)], [s * math.sin(p), s * math.cos(p)]]
    )


def _describe(cd: np.ndarray) -> tuple[float, float, bool]:
    """(scale arcsec/px, PA deg, flip) of a CD matrix."""
    flip = bool(np.linalg.det(cd) > 0)
    pa = math.degrees(math.atan2(cd[0, 1], cd[1, 1]))
    if flip:
        pa = math.degrees(math.atan2(-cd[0, 1], cd[1, 1]))
    scale = math.sqrt(abs(np.linalg.det(cd))) * 3600.0
    return scale, pa % 360.0, flip


def _triangles(
    x: np.ndarray, y: np.ndarray, k: int, full: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Triangles of each point with pairs of its ``k`` nearest neighbours
    (every triangle when there are no more than ``full`` points):
    vertex indices (n, 3), ordered so vertex i is opposite the i-th
    shortest side, and the sorted side lengths (n, 3)."""
    n = len(x)
    if n < 3:
        return np.zeros((0, 3), int), np.zeros((0, 3))
    d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    np.fill_diagonal(d, np.inf)
    if n <= full:
        # Few stars: every triangle (a faint neighbour missing from one list
        # must not hide the triangles of the bright ones).
        from itertools import combinations

        t = np.array(list(combinations(range(n), 3)), dtype=int)
    else:
        kk = min(k, n - 1)
        nn = np.argsort(d, axis=1)[:, :kk]
        tri = set()
        for i in range(n):
            for a in range(kk):
                for b in range(a + 1, kk):
                    tri.add(tuple(sorted((i, int(nn[i, a]), int(nn[i, b])))))
        t = np.array(sorted(tri), dtype=int)
    # Side opposite each vertex.
    p = np.stack([x[t], y[t]], axis=-1)  # (m, 3, 2)
    opp = np.stack(
        [
            np.hypot(*(p[:, 1] - p[:, 2]).T),
            np.hypot(*(p[:, 0] - p[:, 2]).T),
            np.hypot(*(p[:, 0] - p[:, 1]).T),
        ],
        axis=1,
    )
    order = np.argsort(opp, axis=1)
    t = np.take_along_axis(t, order, axis=1)
    sides = np.take_along_axis(opp, order, axis=1)
    return t, sides


def _similarity(src: np.ndarray, dst: np.ndarray, flip: bool) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares similarity (rotation, scale, translation) mapping image
    pixels ``src`` (n, 2) onto (east, north) tangent-plane pixels ``dst``,
    for a camera of parity ``flip`` (module doc): the normal sky (east
    left) is a *mirror* image of (east, north), so ``flip`` False mirrors."""
    flip = not flip
    s = src.copy()
    if flip:
        s[:, 0] = -s[:, 0]
    sc, dc = s.mean(0), dst.mean(0)
    a, b = s - sc, dst - dc
    # z = (a + ib), solve dst = c * src (complex c).
    za = a[:, 0] + 1j * a[:, 1]
    zb = b[:, 0] + 1j * b[:, 1]
    c = (np.conj(za) @ zb) / max(float(np.real(np.conj(za) @ za)), 1e-12)
    m = np.array([[c.real, -c.imag], [c.imag, c.real]])
    if flip:
        m = m @ np.diag([-1.0, 1.0])
    return m, dc - m @ src.mean(0)


def _nearest(px: np.ndarray, qx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each row of px (n, 2): index of the nearest row of qx and the distance."""
    d = np.hypot(px[:, None, 0] - qx[None, :, 0], px[:, None, 1] - qx[None, :, 1])
    j = np.argmin(d, axis=1)
    return j, d[np.arange(len(px)), j]


def _poly_terms(u: np.ndarray, v: np.ndarray, order: int, lowest: int) -> tuple[np.ndarray, list]:
    cols, powers = [], []
    for total in range(lowest, order + 1):
        for p in range(total, -1, -1):
            q = total - p
            cols.append(u**p * v**q)
            powers.append((p, q))
    return (np.array(cols).T if cols else np.zeros((len(u), 0))), powers


def _fit_wcs(
    x: np.ndarray,
    y: np.ndarray,
    ra: np.ndarray,
    dec: np.ndarray,
    crpix: tuple[float, float],
    guess: tuple[float, float],
    sip_order: int,
) -> tuple[WCS, np.ndarray]:
    """A TAN (or TAN-SIP) WCS through the matched stars, CRPIX fixed
    (0-based ``crpix``); residuals (arcsec) per star."""
    u = x - crpix[0]
    v = y - crpix[1]
    ra0, dec0 = guess
    order = (
        sip_order if sip_order >= 2 and len(x) >= 3 * (sip_order + 1) * (sip_order + 2) // 2 else 1
    )
    for _ in range(4):
        xi, eta = _tan(ra0, dec0, ra, dec)
        xi, eta = np.degrees(xi), np.degrees(eta)
        a, powers = _poly_terms(u, v, order, 0)
        cx, *_ = np.linalg.lstsq(a, xi, rcond=None)
        cy, *_ = np.linalg.lstsq(a, eta, rcond=None)
        # Move the tangent point to the sky position of CRPIX (constant term).
        ra0, dec0 = (
            float(t[0]) for t in _untan(ra0, dec0, np.radians([cx[0]]), np.radians([cy[0]]))
        )
        if math.hypot(cx[0], cy[0]) * 3600.0 < 1e-4:
            break
    xi, eta = _tan(ra0, dec0, ra, dec)
    xi, eta = np.degrees(xi), np.degrees(eta)
    a, powers = _poly_terms(u, v, order, 1)
    cx, *_ = np.linalg.lstsq(a, xi, rcond=None)
    cy, *_ = np.linalg.lstsq(a, eta, rcond=None)
    cd = np.array([[cx[0], cx[1]], [cy[0], cy[1]]])  # powers (1,0), (0,1)
    w = WCS(naxis=2)
    w.wcs.crpix = [crpix[0] + 1.0, crpix[1] + 1.0]
    w.wcs.crval = [ra0, dec0]
    w.wcs.cd = cd
    w.wcs.cunit = ["deg", "deg"]
    if order >= 2:
        # Higher orders: [xi, eta]_nl = CD @ [A, B]  ->  [A, B] = CD^-1 @ nl.
        inv = np.linalg.inv(cd)
        amat = np.zeros((order + 1, order + 1))
        bmat = np.zeros((order + 1, order + 1))
        for k, (p, q) in enumerate(powers[2:], start=2):
            ab = inv @ np.array([cx[k], cy[k]])
            amat[p, q], bmat[p, q] = ab
        # The inverse polynomials (AP, BP) on a grid over the matched area.
        gu, gv = np.meshgrid(np.linspace(u.min(), u.max(), 25), np.linspace(v.min(), v.max(), 25))
        gu, gv = gu.ravel(), gv.ravel()
        fa = sum(amat[p, q] * gu**p * gv**q for p in range(order + 1) for q in range(order + 1))
        fb = sum(bmat[p, q] * gu**p * gv**q for p in range(order + 1) for q in range(order + 1))
        uu, vv = gu + fa, gv + fb
        ia, ipow = _poly_terms(uu, vv, order, 1)
        ca, *_ = np.linalg.lstsq(ia, gu - uu, rcond=None)
        cb, *_ = np.linalg.lstsq(ia, gv - vv, rcond=None)
        apm = np.zeros((order + 1, order + 1))
        bpm = np.zeros((order + 1, order + 1))
        for k, (p, q) in enumerate(ipow):
            apm[p, q], bpm[p, q] = ca[k], cb[k]
        w.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
        w.sip = Sip(amat, bmat, apm, bpm, w.wcs.crpix)
    else:
        w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.set()
    pr, pd = w.all_pix2world(x, y, 0)
    resid = separation(pr, pd, ra, dec) * 3600.0
    return w, resid


# -- the near-blind solver ---------------------------------------------------------


def solve_near(
    image: np.ndarray | Detections,
    catalog: Catalog | Stars,
    ra: float,
    dec: float,
    config: SolverConfig,
    *,
    binning: int = 1,
    epoch: float | datetime | None = None,
    crpix: tuple[float, float] | None = None,
) -> Solution:
    """Solve ``image`` near (ra, dec) deg (module doc). ``catalog`` is a
    :class:`Catalog` (queried around the guess) or a :class:`Stars` table
    already covering the field; ``binning`` scales the plate scale;
    ``crpix`` (0-based) defaults to the image centre."""
    t0 = time.monotonic()
    c = config
    det = image if isinstance(image, Detections) else detect_stars(image)
    ny, nx = det.shape
    scale = c.plate_scale * binning  # arcsec per (binned) pixel
    cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
    if crpix is None:
        crpix = (cx, cy)
    if len(det) == 0:
        raise SolveError("no stars detected")
    half_diag = math.hypot(nx, ny) / 2.0 * scale / 3600.0
    radius = half_diag * (1 + c.scale_tolerance) + c.search_radius
    if isinstance(catalog, Stars):
        stars = catalog.at_epoch(epoch).cone(ra, dec, radius).sorted()
    else:
        stars = catalog.cone(ra, dec, radius, epoch=epoch, mag_limit=c.mag_limit)
    if c.mag_limit is not None:
        stars = stars.brighter(c.mag_limit)
    if len(stars) == 0:
        raise SolveError(f"no catalog stars within {radius * 60:.1f} arcmin of the guess")
    # The catalog in "pixels" on the tangent plane at the guess: east +u, north +v.
    xi, eta = _tan(ra, dec, stars.ra, stars.dec)
    cu = np.degrees(xi) * 3600.0 / scale
    cv = np.degrees(eta) * 3600.0 / scale
    # Depth: about as many catalog stars per area as detections, plus margin.
    area_img = nx * ny
    area_cat = math.pi * (radius * 3600.0 / scale) ** 2
    n_img = min(len(det), c.max_stars)
    n_cat = int(min(len(stars), max(2.0 * n_img * area_cat / area_img, 10), c.max_catalog))
    cat_xy = np.stack([cu, cv], axis=1)
    img = det.take(n_img)
    img_xy = np.stack([img.x - cx, img.y - cy], axis=1)
    flips = [c.flip] if c.flip is not None else [False, True]

    if n_img < 3 or n_cat < 3:
        sol = _few_stars(img_xy, cat_xy, img, stars, ra, dec, scale, c, flips, (cx, cy))
    else:
        sol = _triangle_match(img_xy, cat_xy[:n_cat], c, flips, scale, t0)
        if sol is None and n_img < 3 + 2:
            sol = _few_stars(img_xy, cat_xy, img, stars, ra, dec, scale, c, flips, (cx, cy))
    if sol is None:
        raise SolveError(
            f"no match: {len(det)} stars detected, {len(stars)} catalog stars within "
            f"{radius * 60:.1f} arcmin of the guess (check the plate scale, the pointing, focus)"
        )
    m, off, method, degenerate = sol
    # Refine: match all detections to all catalog stars through the
    # transform, fit the WCS, re-match tighter.
    all_xy = np.stack([det.x - cx, det.y - cy], axis=1)[: max(n_img, 200)]
    tol = max(c.match_radius, 2.0 / scale) * 3.0
    used: tuple[np.ndarray, np.ndarray] | None = None
    w: WCS | None = None
    resid = np.zeros(0)
    for _ in range(3):
        if w is None:
            pred = all_xy @ m.T + off
        else:
            pr, pd = w.all_pix2world(det.x[: len(all_xy)], det.y[: len(all_xy)], 0)
            xi2, eta2 = _tan(ra, dec, pr, pd)
            pred = np.stack([np.degrees(xi2) * 3600 / scale, np.degrees(eta2) * 3600 / scale], 1)
        j, dist = _nearest(pred, cat_xy)
        ok = dist < tol
        # One detection per catalog star (the closest).
        best: dict[int, int] = {}
        for i in np.nonzero(ok)[0]:
            if j[i] not in best or dist[i] < dist[best[j[i]]]:
                best[int(j[i])] = int(i)
        ii = np.array(sorted(best.values()), dtype=int)
        jj = j[ii]
        if len(ii) < max(1, min(c.min_matches, 3)):
            raise SolveError(f"only {len(ii)} stars matched after refinement")
        if len(ii) >= 3:
            w, resid = _fit_wcs(
                det.x[ii],
                det.y[ii],
                stars.ra[jj],
                stars.dec[jj],
                crpix,
                (ra, dec),
                c.sip_order if len(ii) >= 12 else 0,
            )
        else:
            w, resid = _wcs_from_transform(m, off, ra, dec, scale, (cx, cy), crpix), None
            pr, pd = w.all_pix2world(det.x[ii], det.y[ii], 0)
            resid = separation(pr, pd, stars.ra[jj], stars.dec[jj]) * 3600.0
            break
        used = (ii, jj)
        tol = max(c.match_radius, 1.0 / scale)
    if used is None:
        used = (ii, jj)
    ii, jj = used
    n = len(ii)
    if n < c.min_matches:
        raise SolveError(
            f"only {n} stars matched (min_matches {c.min_matches}); "
            f"{len(det)} detected, {len(stars)} in the catalog"
        )
    assert w is not None
    px, py = w.all_world2pix(stars.ra, stars.dec, 0)
    n_in = int(np.sum((px >= 0) & (px <= nx - 1) & (py >= 0) & (py <= ny - 1)))
    need = math.ceil(c.min_fraction * min(len(det), n_in))
    if n < need:
        raise SolveError(
            f"only {n} of {min(len(det), n_in)} stars matched (need {need}): a chance alignment"
        )
    fscale, fpa, fflip = _describe(w.wcs.cd if w.wcs.has_cd() else w.pixel_scale_matrix)
    if abs(fscale / scale - 1.0) > max(3 * c.scale_tolerance, 0.02):
        raise SolveError(f"scale {fscale:.4f} arcsec/px is far from {scale:.4f}: a false match")
    cra, cdec = w.all_pix2world([cx], [cy], 0)
    rms = float(np.sqrt(np.mean(resid**2))) if len(resid) else math.nan
    return Solution(
        wcs=w,
        ra=float(cra[0]) % 360.0,
        dec=float(cdec[0]),
        pa=fpa,
        scale=fscale,
        flip=fflip,
        rms=rms,
        n_matched=n,
        n_detected=len(det),
        n_catalog=len(stars),
        method=method,
        seconds=time.monotonic() - t0,
        degenerate=degenerate or n < 3,
        matches={
            "x": det.x[ii],
            "y": det.y[ii],
            "ra": stars.ra[jj],
            "dec": stars.dec[jj],
            "g": stars.g[jj],
            "bp_rp": stars.bp_rp[jj],
            "flux": det.flux[ii],
            "resid": resid,
        },
        shape=(ny, nx),
    )


def _wcs_from_transform(
    m: np.ndarray,
    off: np.ndarray,
    ra: float,
    dec: float,
    scale: float,
    centre: tuple[float, float],
    crpix: tuple[float, float],
) -> WCS:
    """The WCS of a pixel -> tangent-plane-pixel similarity (few stars)."""
    k = scale / 3600.0
    cd = m * k
    p0 = np.array(crpix) - np.array(centre)
    t = (m @ p0 + off) * k  # deg on the tangent plane at (ra, dec)
    r0, d0 = _untan(ra, dec, np.radians([t[0]]), np.radians([t[1]]))
    w = WCS(naxis=2)
    w.wcs.crpix = [crpix[0] + 1.0, crpix[1] + 1.0]
    w.wcs.crval = [float(r0[0]), float(d0[0])]
    w.wcs.cd = cd
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]
    w.wcs.set()
    return w


def _triangle_match(
    img_xy: np.ndarray,
    cat_xy: np.ndarray,
    c: SolverConfig,
    flips: list[bool],
    scale: float,
    t0: float,
) -> tuple[np.ndarray, np.ndarray, str, bool] | None:
    few = len(img_xy) <= 12
    ti, si = _triangles(img_xy[:, 0], img_xy[:, 1], 5, full=12)
    # With few detections, every triangle of the brightest catalog stars.
    tc, sc = _triangles(cat_xy[:, 0], cat_xy[:, 1], 6, full=40 if few else 0)
    if not len(ti) or not len(tc):
        return None
    # Sides agree within the scale tolerance plus centroid noise (px).
    tol_frac = c.scale_tolerance + 0.005
    cand: list[tuple[int, int]] = []
    for a in range(0, len(si), 256):
        s1 = si[a : a + 256, None, :]
        tol = tol_frac * s1 + 1.5
        ok = np.all(np.abs(s1 - sc[None, :, :]) < tol, axis=2)
        ia, ic = np.nonzero(ok)
        # Tiny triangles carry no information.
        big = si[a + ia, 2] > 15.0
        cand += list(zip((a + ia[big]).tolist(), ic[big].tolist(), strict=True))
    if not cand:
        return None
    if len(cand) > 4000:
        rng = np.random.default_rng(0)
        cand = [cand[i] for i in rng.choice(len(cand), 4000, replace=False)]
    tol_px = max(c.match_radius, 2.0 / scale) * 2.0
    best: tuple[int, float, np.ndarray, np.ndarray] | None = None
    seen: set[tuple] = set()
    for a, b in cand:
        if time.monotonic() - t0 > c.timeout:
            break
        src = img_xy[ti[a]]
        dst = cat_xy[tc[b]]
        for flip in flips:
            m, off = _similarity(src, dst, flip)
            k = math.sqrt(abs(np.linalg.det(m)))
            if abs(k - 1.0) > c.scale_tolerance + 0.01:
                continue
            fit = src @ m.T + off
            if np.max(np.hypot(*(fit - dst).T)) > 3.0:
                continue
            if c.pa is not None and c.pa_tolerance < 180.0:
                _, pa, _ = _describe(m * (scale / 3600.0))
                if abs((pa - c.pa + 180.0) % 360.0 - 180.0) > c.pa_tolerance:
                    continue
            key = (
                flip,
                round(off[0] / 4),
                round(off[1] / 4),
                round(math.atan2(m[1, 0], m[0, 0]), 2),
            )
            if key in seen:
                continue
            seen.add(key)
            j, dist = _nearest(img_xy @ m.T + off, cat_xy)
            good = dist < tol_px
            n = len(set(j[good].tolist()))
            score = n - 0.01 * float(np.mean(np.minimum(dist, tol_px)))
            if best is None or score > best[1]:
                best = (n, score, m, off)
        if best is not None and best[0] >= max(8, 0.6 * len(img_xy)):
            break  # good enough
    if best is None or best[0] < min(3, len(img_xy)):
        return None
    if best[0] < c.min_matches:
        return None
    return best[2], best[3], "triangles", False


def _few_stars(
    img_xy: np.ndarray,
    cat_xy: np.ndarray,
    img: Detections,
    stars: Stars,
    ra: float,
    dec: float,
    scale: float,
    c: SolverConfig,
    flips: list[bool],
    centre: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray, str, bool] | None:
    """One or two stars (module doc)."""
    if c.min_matches > 2 or len(img_xy) == 0:
        return None
    if len(img_xy) >= 2 and c.min_matches <= 2 and c.flip is not None:
        d_img = math.hypot(*(img_xy[0] - img_xy[1]))
        best = None
        n = min(len(cat_xy), 30)
        # The brighter detection is the brighter catalog star (both lists
        # are brightest first): that fixes the pair's direction.
        for i in range(n):
            for j in range(i + 1, n):
                d_cat = math.hypot(*(cat_xy[i] - cat_xy[j]))
                if abs(d_cat - d_img) > c.scale_tolerance * d_img + 1.5:
                    continue
                m, off = _similarity(img_xy[:2], cat_xy[[i, j]], c.flip)
                if c.pa is not None:
                    cd = m * (scale / 3600.0)
                    _, pa, _ = _describe(cd)
                    if abs((pa - c.pa + 180.0) % 360.0 - 180.0) > c.pa_tolerance:
                        continue
                # Prefer the brightest pair nearest the guess.
                cost = (i + j) + float(np.hypot(*off)) * scale / 3600.0 / max(c.search_radius, 1e-6)
                if best is None or cost < best[0]:
                    best = (cost, m, off)
        if best is not None:
            return best[1], best[2], "pair", True
    if c.min_matches <= 1 and c.pa is not None and c.flip is not None:
        # CD maps pixels to (east, north) deg; the tangent plane here is
        # (east, north) in pixels, so the same matrix in pixel units.
        m = _cd(scale, c.pa, c.flip) / (scale / 3600.0)
        pred0 = img_xy[0] @ m.T
        # The brightest catalog star within the search radius of where the
        # guess puts it.
        dist = np.hypot(*(cat_xy - pred0).T) * scale / 3600.0
        near = np.nonzero(dist < c.search_radius)[0]
        if not len(near):
            return None
        k = int(near[0])  # brightest first
        return m, cat_xy[k] - pred0, "single", True
    return None


# -- FITS ---------------------------------------------------------------------------


def wcs_cards(sol: Solution, source: str = "robofast") -> list[tuple[str, Any, str]]:
    """Header cards for a solution: the WCS (TAN or TAN-SIP) plus the
    solve's provenance (``WCSSRC``, ``WCSRMS``, ``WCSNSTAR``, ...)."""
    h = sol.wcs.to_header(relax=True)
    cards: list[tuple[str, Any, str]] = []
    for key in h:
        if key in ("DATEREF", "MJDREFI", "MJDREFF", "WCSAXES", "LATPOLE", "LONPOLE", "MJD-OBS"):
            continue
        if key in ("DATE-OBS",):
            continue
        cards.append((key, h[key], h.comments[key]))
    # astropy writes PC + CDELT; FITS readers take either. Add the summary.
    cards += [
        ("WCSSRC", source, "WCS from a plate solve"),
        ("WCSMETH", sol.method, "Plate-solve method"),
        ("WCSRMS", round(sol.rms, 4), "[arcsec] rms of matched stars"),
        ("WCSNSTAR", sol.n_matched, "Stars matched to the catalog"),
        ("WCSPA", round(sol.pa, 4), "[deg] PA of +y, east of north (solved)"),
        ("WCSSCALE", round(sol.scale, 6), "[arcsec/px] solved plate scale"),
        ("WCSFLIP", sol.flip, "East to the right when north is up"),
    ]
    return cards


# -- photometric zero point -----------------------------------------------------------


def zero_point(
    image: np.ndarray,
    sol: Solution,
    *,
    exptime: float,
    gain: float | None = None,
    aperture: float | None = None,
    saturation: float | None = None,
    min_stars: int = 3,
) -> dict[str, Any] | None:
    """The frame's photometric zero point from the matched stars: the Gaia G
    magnitude of a source giving 1 e-/s (or 1 ADU/s without ``gain``),
    ``ZP = G + 2.5 log10(counts / exptime)``, by aperture photometry
    (radius ``aperture`` px, default 3 arcsec, sky from an annulus). The
    median and a robust scatter over unsaturated, isolated matched stars;
    None with fewer than ``min_stars``."""
    if exptime <= 0 or not sol.matches:
        return None
    img = np.asarray(image, dtype=float)
    ny, nx = img.shape
    r = aperture if aperture is not None else max(3.0, 3.0 / sol.scale)
    x, y, g = sol.matches["x"], sol.matches["y"], sol.matches["g"]
    zps = []
    for k in range(len(x)):
        others = np.hypot(x - x[k], y - y[k])
        if np.any((others > 0) & (others < 2.5 * r)) or not np.isfinite(g[k]):
            continue
        x0, x1 = int(x[k] - 2.5 * r), int(x[k] + 2.5 * r) + 2
        y0, y1 = int(y[k] - 2.5 * r), int(y[k] + 2.5 * r) + 2
        if x0 < 0 or y0 < 0 or x1 > nx or y1 > ny:
            continue
        cut = img[y0:y1, x0:x1]
        yy, xx = np.mgrid[y0:y1, x0:x1]
        rr = np.hypot(xx - x[k], yy - y[k])
        ap = rr <= r
        ann = (rr > 1.6 * r) & (rr <= 2.4 * r)
        if saturation is not None and np.any(cut[rr <= r] >= saturation):
            continue
        sky = float(np.median(cut[ann]))
        flux = float(np.sum(cut[ap] - sky))
        if flux <= 0:
            continue
        counts = flux * (gain if gain else 1.0)
        zps.append(float(g[k]) + 2.5 * math.log10(counts / exptime))
    if len(zps) < min_stars:
        return None
    z = np.array(zps)
    med = float(np.median(z))
    mad = float(np.median(np.abs(z - med))) * 1.4826
    return {
        "zp": med,
        "scatter": mad,
        "error": 1.2533 * mad / math.sqrt(len(z)),
        "n": len(z),
        "unit": "e-/s" if gain else "ADU/s",
        "band": "G",
        "aperture": r,
    }


# -- astrometry.net (optional) --------------------------------------------------------


def solve_field(
    image: np.ndarray,
    *,
    plate_scale: float,
    ra: float | None = None,
    dec: float | None = None,
    radius: float | None = None,
    index_dir: str | Path | None = None,
    scale_tolerance: float = 0.05,
    timeout: float = 60.0,
    executable: str = "solve-field",
) -> Solution:
    """Blind (or hinted) solve with astrometry.net's ``solve-field``.
    ``index_dir`` holds the index files (docs/sky.md lists the ones for
    MINERVA's plate scales). Raises :class:`SolveError` if solve-field is
    not installed or fails."""
    exe = shutil.which(executable)
    if exe is None:
        raise SolveError(f"{executable} is not installed (astrometry.net)")
    t0 = time.monotonic()
    ny, nx = np.asarray(image).shape
    with tempfile.TemporaryDirectory(prefix="robofast-solve-") as tmp:
        d = Path(tmp)
        src = d / "frame.fits"
        fits.PrimaryHDU(np.asarray(image, dtype=np.float32)).writeto(src)
        cmd = [
            exe,
            str(src),
            "--dir",
            str(d),
            "--no-plots",
            "--overwrite",
            "--no-verify",
            "--crpix-center",
            "--new-fits",
            "none",
            "--scale-units",
            "arcsecperpix",
            "--scale-low",
            f"{plate_scale * (1 - scale_tolerance):.5f}",
            "--scale-high",
            f"{plate_scale * (1 + scale_tolerance):.5f}",
            "--cpulimit",
            f"{max(1, int(timeout))}",
        ]
        if ra is not None and dec is not None:
            cmd += ["--ra", f"{ra:.6f}", "--dec", f"{dec:.6f}", "--radius", f"{radius or 2.0:.3f}"]
        if index_dir is not None:
            cfg = d / "astrometry.cfg"
            cfg.write_text(f"inparallel\nadd_path {Path(index_dir).resolve()}\nautoindex\n")
            cmd += ["--config", str(cfg)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout + 30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SolveError("solve-field timed out") from exc
        wfile = d / "frame.wcs"
        if not wfile.exists():
            raise SolveError("solve-field found no solution")
        w = WCS(fits.getheader(wfile))
    cra, cdec = w.all_pix2world([(nx - 1) / 2.0], [(ny - 1) / 2.0], 0)
    scale, pa, flip = _describe(w.pixel_scale_matrix)
    return Solution(
        wcs=w,
        ra=float(cra[0]) % 360.0,
        dec=float(cdec[0]),
        pa=pa,
        scale=scale,
        flip=flip,
        rms=math.nan,
        n_matched=0,
        n_detected=0,
        n_catalog=0,
        method="astrometry.net",
        seconds=time.monotonic() - t0,
        shape=(ny, nx),
    )
