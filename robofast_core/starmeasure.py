"""Measure star sizes in an image: in focus, defocused, and as donuts.

Used by :mod:`robofast_core.autofocus` (and meant for the image pipeline's
FWHM monitoring). Only numpy, deterministic, no external extractor::

    m = measure(image, MeasureConfig(saturation=60000))
    m.hfd, m.hfd_err, m.fwhm, m.donut, m.n

Steps:

1. **Background**: sigma-clipped medians in ``background_box`` tiles,
   interpolated bilinearly; the noise is the robust (MAD) scatter of the
   background-subtracted image.
2. **Detection**: the image smoothed by a Gaussian of ``smooth`` pixels
   (pick it near a quarter of the expected star size, so a defocused donut
   is one detection rather than a ring of fragments), thresholded at
   ``threshold`` times the smoothed noise, grown by ``grow`` pixels and
   split into connected pieces (8-connected).
3. **Rejection**, with the reason counted in :attr:`FieldMeasurement.rejected`:
   ``saturated`` (any pixel within the aperture at or above
   ``saturation``), ``edge`` (aperture off the image or out of
   ``region``), ``blended`` (another detection inside the aperture),
   ``faint`` (S/N below ``min_snr``), ``elongated`` (moment ellipticity
   above ``max_ellipticity``; trailed or double stars), ``small`` (fewer
   than ``min_area`` pixels: hot pixels, cosmic rays).
4. **Per star** (the brightest ``max_stars`` survivors), on a cutout around
   the detection, with the local background re-estimated in an annulus:

   * **HFD**, the half-flux diameter: twice the radius enclosing half the
     flux within the aperture (each pixel split into 4x4 sub-pixels, so the
     growth curve is smooth for small stars). Linear in defocus far from
     focus, for a disk or a donut alike, so it is what the V-curve uses.
   * **FWHM**, from adaptive Gaussian moments (exact for a Gaussian; a
     Moffat reads a few percent low; meaningless for a donut).
   * **Donut outer diameter**: twice the radius where the azimuthally
     averaged profile falls to half of its plateau on the outside. For a
     pupil image much larger than the seeing, this edge does not move
     with the seeing (a symmetric blur leaves a straight edge's 50 % point
     where it was), so it measures the geometric blur directly.

5. **Field**: medians over the stars, with the standard error of the median
   (``1.2533 * 1.4826 * MAD / sqrt(N)``).

Coordinates are 0-based pixel centres of the image as given, ``(x, y)`` =
(column, row).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "FieldMeasurement",
    "MeasureConfig",
    "StarMeasurement",
    "background",
    "measure",
]

_MAD = 1.4826


@dataclass
class MeasureConfig:
    #: Detection threshold, in units of the smoothed image's noise.
    threshold: float = 5.0
    #: Gaussian sigma (pixels) of the detection smoothing.
    smooth: float = 1.0
    #: Pixels added around each detection before it is measured.
    grow: int = 2
    #: Fewest pixels above threshold for a detection.
    min_area: int = 5
    #: ADU; a star with an aperture pixel at or above it is rejected (None: off).
    saturation: float | None = None
    #: Aperture radius = ``aperture_scale`` x the detection's extent (half
    #: its grown bounding box) + ``aperture_pad`` + 2 ``smooth``.
    aperture_scale: float = 1.2
    aperture_pad: float = 3.0
    #: Width of the local background annulus outside the aperture, pixels.
    annulus: float = 6.0
    #: Only stars whose aperture lies inside (x0, y0, x1, y1) (None: the image).
    region: tuple[float, float, float, float] | None = None
    #: Measure at most this many (the brightest).
    max_stars: int = 40
    min_snr: float = 30.0
    max_ellipticity: float = 0.35
    #: Background tile size, pixels.
    background_box: int = 128


@dataclass
class StarMeasurement:
    x: float
    y: float
    #: Background-subtracted flux in the aperture (image units).
    flux: float
    snr: float
    #: Peak pixel (image units, background included).
    peak: float
    hfd: float
    fwhm: float
    ellipticity: float
    #: Outer diameter of the profile's half-plateau edge (NaN if no plateau).
    donut: float
    #: Aperture radius used.
    radius: float


@dataclass
class FieldMeasurement:
    stars: list[StarMeasurement]
    #: Why detections were not used: reason -> count.
    rejected: dict[str, int] = field(default_factory=dict)
    sky: float = math.nan
    noise: float = math.nan

    @property
    def n(self) -> int:
        return len(self.stars)

    def _median(self, attr: str) -> float:
        v = np.array([getattr(s, attr) for s in self.stars], dtype=float)
        v = v[np.isfinite(v)]
        return float(np.median(v)) if v.size else math.nan

    def _err(self, attr: str) -> float:
        v = np.array([getattr(s, attr) for s in self.stars], dtype=float)
        v = v[np.isfinite(v)]
        if v.size == 0:
            return math.nan
        if v.size == 1:
            return float(0.05 * v[0])  # one star: a nominal 5 %
        mad = float(np.median(np.abs(v - np.median(v))))
        # Floor at 1 % per star (identical stars would claim zero error).
        sigma = max(_MAD * mad, 0.01 * float(np.median(v)))
        return 1.2533 * sigma / math.sqrt(v.size)

    @property
    def hfd(self) -> float:
        return self._median("hfd")

    @property
    def hfd_err(self) -> float:
        return self._err("hfd")

    @property
    def fwhm(self) -> float:
        return self._median("fwhm")

    @property
    def donut(self) -> float:
        return self._median("donut")

    @property
    def donut_err(self) -> float:
        return self._err("donut")

    def summary(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "hfd": round(self.hfd, 3),
            "hfd_err": round(self.hfd_err, 3),
            "fwhm": round(self.fwhm, 3),
            "donut": round(self.donut, 3),
            "rejected": dict(self.rejected),
        }


# -- background ----------------------------------------------------------------


def _clipped_mean(v: np.ndarray, k: float = 3.0) -> float:
    """Sigma-clipped mean: the background *level* (a median of integer,
    Poisson-noisy pixels is biased by up to half an ADU, which a large
    aperture multiplies into a large flux error)."""
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan
    med, sig = _clipped_median(v)
    if not np.isfinite(sig) or sig <= 0:
        return med
    for _ in range(3):
        keep = np.abs(v - med) < k * sig
        if keep.sum() < 10:
            break
        med = float(v[keep].mean())
    return med


def _clipped_median(v: np.ndarray, iters: int = 3, k: float = 3.0) -> tuple[float, float]:
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan, math.nan
    med = float(np.median(v))
    sig = _MAD * float(np.median(np.abs(v - med)))
    for _ in range(iters):
        if sig <= 0:
            break
        keep = np.abs(v - med) < k * sig
        if keep.sum() < 10:
            break
        v = v[keep]
        med = float(np.median(v))
        sig = _MAD * float(np.median(np.abs(v - med)))
    return med, sig


def background(
    image: np.ndarray, box: int = 64, mask: np.ndarray | None = None
) -> tuple[np.ndarray, float]:
    """The background map (bilinear between tile medians) and the noise;
    ``mask`` (True: a source) is left out of the tile medians."""
    img = np.asarray(image, dtype=float)
    if mask is not None:
        img = np.where(mask, np.nan, img)
    ny, nx = img.shape
    by, bx = max(1, ny // max(1, box)), max(1, nx // max(1, box))
    ys = np.linspace(0, ny, by + 1).astype(int)
    xs = np.linspace(0, nx, bx + 1).astype(int)
    grid = np.empty((by, bx))
    for i in range(by):
        for j in range(bx):
            tile = img[ys[i] : ys[i + 1], xs[j] : xs[j + 1]].ravel()
            ok = np.isfinite(tile)
            # A tile mostly covered by sources is filled in from the others.
            grid[i, j] = _clipped_mean(tile[ok]) if ok.mean() > 0.2 else np.nan
    if not np.any(np.isfinite(grid)):
        grid[:] = _clipped_median(np.asarray(image, dtype=float).ravel())[0]
    elif not np.all(np.isfinite(grid)):
        grid[~np.isfinite(grid)] = np.nanmedian(grid)
    # A tile dominated by a big donut reads high: replace outliers by the
    # median of the grid (robust for a few bright tiles).
    gmed, gsig = _clipped_median(grid.ravel())
    if np.isfinite(gsig) and gsig > 0 and grid.size >= 4:
        grid[np.abs(grid - gmed) > 5 * gsig] = gmed
    cy = (ys[:-1] + ys[1:] - 1) / 2.0
    cx = (xs[:-1] + xs[1:] - 1) / 2.0
    rows = np.array([np.interp(np.arange(nx), cx, g) for g in grid]) if bx > 1 else None
    if rows is None:
        rows = np.repeat(grid, nx, axis=1)
    bkg = (
        np.array([np.interp(np.arange(ny), cy, rows[:, x]) for x in range(nx)]).T
        if by > 1
        else np.repeat(rows, ny, axis=0)
    )
    resid = img - bkg  # NaN where masked
    _, noise = _clipped_median(resid[:: max(1, ny // 256), :: max(1, nx // 256)].ravel())
    if not np.isfinite(noise) or noise <= 0:
        noise = 1.0
    return bkg, noise


# -- detection -----------------------------------------------------------------


def _smooth(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img
    half = max(1, math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2)
    k /= k.sum()
    out = img
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (half, half)
        p = np.pad(out, pad)  # zeros: the residual has zero mean
        acc = np.zeros_like(out)
        n = out.shape[axis]
        for i, w in enumerate(k):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(i, i + n)
            acc += w * p[tuple(sl)]
        out = acc
    return out


def _dilate(mask: np.ndarray, n: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(max(0, n)):
        m = out.copy()
        m[1:, :] |= out[:-1, :]
        m[:-1, :] |= out[1:, :]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        out = m
    return out


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Horizontal runs of ``mask``: row, start, end (exclusive), and their
    8-connected component labels (union-find over runs of adjacent rows)."""
    m = mask.astype(np.int8)
    d = np.diff(m, axis=1, prepend=0, append=0)
    rs, cs = np.nonzero(d == 1)
    re_, ce = np.nonzero(d == -1)
    # np.nonzero is row-major, so starts and ends pair up in order.
    assert np.array_equal(rs, re_)
    n = rs.size
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if n:
        row_start = np.searchsorted(rs, np.arange(mask.shape[0] + 1))
        rows = rs.tolist()
        starts, ends = cs.tolist(), ce.tolist()
        prev_lo = prev_hi = 0
        last_row = -2
        for row in sorted(set(rows)):
            lo, hi = int(row_start[row]), int(row_start[row + 1])
            if last_row == row - 1:
                j = prev_lo
                for i in range(lo, hi):
                    # Runs of the previous row touching run i (diagonals count).
                    while j < prev_hi and ends[j] < starts[i]:
                        j += 1
                    k = j
                    while k < prev_hi and starts[k] <= ends[i]:
                        a, b = find(i), find(k)
                        if a != b:
                            parent[max(a, b)] = min(a, b)
                        k += 1
            prev_lo, prev_hi, last_row = lo, hi, row
    labels = np.array([find(i) for i in range(n)], dtype=np.int64)
    return rs, cs, ce, labels


@dataclass
class _Detection:
    x: float
    y: float
    area: int
    flux: float
    #: Half the larger side of the detection's (grown) bounding box.
    extent: float = 0.0


def _detect(resid: np.ndarray, noise: float, cfg: MeasureConfig) -> list[_Detection]:
    sm = _smooth(resid, cfg.smooth)
    # The smoothed image's own noise (it includes background structure the
    # smoothing does not average away).
    step = max(1, int(math.sqrt(sm.size / 65536)))
    _, sm_noise = _clipped_median(sm[::step, ::step].ravel())
    if not np.isfinite(sm_noise) or sm_noise <= 0:
        sm_noise = noise
    core = sm > cfg.threshold * sm_noise
    if not core.any():
        return []
    rows, starts, ends, labels = _runs(_dilate(core, cfg.grow))
    ids, inv = np.unique(labels, return_inverse=True)
    # Per-run sums from row-wise cumulative sums (pixels in [start, end)).
    w = np.clip(sm, 0, None)
    x = np.arange(sm.shape[1], dtype=float)

    def run_sums(a: np.ndarray) -> np.ndarray:
        c = np.concatenate([np.zeros((a.shape[0], 1)), np.cumsum(a, axis=1)], axis=1)
        return c[rows, ends] - c[rows, starts]

    tot = np.bincount(inv, weights=run_sums(w), minlength=ids.size)
    sx = np.bincount(inv, weights=run_sums(w * x[None, :]), minlength=ids.size)
    sy = np.bincount(inv, weights=run_sums(w) * rows, minlength=ids.size)
    area = np.bincount(inv, weights=run_sums(core.astype(float)), minlength=ids.size)
    big = np.iinfo(np.int64).max
    x0 = np.full(ids.size, big)
    y0 = np.full(ids.size, big)
    x1 = np.full(ids.size, -1)
    y1 = np.full(ids.size, -1)
    np.minimum.at(x0, inv, starts)
    np.maximum.at(x1, inv, ends - 1)
    np.minimum.at(y0, inv, rows)
    np.maximum.at(y1, inv, rows)
    ext = np.maximum(x1 - x0, y1 - y0) / 2.0 + 0.5
    out = []
    for i in range(ids.size):
        if tot[i] <= 0:
            continue
        out.append(
            _Detection(sx[i] / tot[i], sy[i] / tot[i], int(area[i]), float(tot[i]), float(ext[i]))
        )
    out.sort(key=lambda d: -d.flux)
    return out


# -- per star --------------------------------------------------------------------

_SUB = 4  # sub-pixels per pixel side for the growth curve


def _growth_hfd(r: np.ndarray, f: np.ndarray, rmax: float) -> tuple[float, float]:
    """Half-flux diameter from per-sub-pixel radii and fluxes; total flux."""
    inside = r <= rmax
    r, f = r[inside], f[inside]
    order = np.argsort(r)
    r, f = r[order], f[order]
    cum = np.cumsum(f)
    total = float(cum[-1]) if cum.size else 0.0
    if total <= 0:
        return math.nan, total
    k = int(np.searchsorted(cum, 0.5 * total))
    k = min(max(k, 1), cum.size - 1)
    c0, c1 = cum[k - 1], cum[k]
    t = 0.0 if c1 == c0 else (0.5 * total - c0) / (c1 - c0)
    return 2.0 * float(r[k - 1] + t * (r[k] - r[k - 1])), total


def _ellipticity(cut: np.ndarray, x: float, y: float) -> float:
    """Unweighted second-moment ellipticity (fine for donuts too)."""
    yy, xx = np.indices(cut.shape, dtype=float)
    w = np.clip(cut, 0, None)
    sw = w.sum()
    if sw <= 0:
        return math.nan
    dx, dy = xx - x, yy - y
    mxx = float((w * dx * dx).sum() / sw)
    myy = float((w * dy * dy).sum() / sw)
    mxy = float((w * dx * dy).sum() / sw)
    tr = mxx + myy
    return math.sqrt((mxx - myy) ** 2 + 4 * mxy**2) / tr if tr > 0 else math.nan


def _moments(cut: np.ndarray, x: float, y: float) -> float:
    """FWHM from adaptive Gaussian moments."""
    yy, xx = np.indices(cut.shape, dtype=float)
    dx, dy = xx - x, yy - y
    s2 = 4.0
    for _ in range(20):
        w = np.exp(-0.5 * (dx**2 + dy**2) / s2) * cut
        sw = w.sum()
        if sw <= 0:
            return math.nan
        new = float((w * (dx * dx + dy * dy)).sum() / sw)
        if new <= 0:
            return math.nan
        # With a Gaussian weight matched to a Gaussian star (sigma^2 per
        # axis), the weighted second moment summed over both axes is sigma^2.
        if abs(new - s2) < 1e-4 * s2:
            s2 = new
            break
        s2 = new
    return 2.3548 * math.sqrt(s2)


def _donut_edge(r: np.ndarray, f: np.ndarray, rmax: float) -> float:
    """Outer half-plateau diameter of the azimuthally averaged profile."""
    width = 0.5
    nb = math.ceil(rmax / width)
    if nb < 4:
        return math.nan
    idx = np.minimum((r / width).astype(int), nb)
    keep = idx < nb
    sums = np.bincount(idx[keep], weights=f[keep], minlength=nb)
    cnts = np.bincount(idx[keep], minlength=nb).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        prof = sums / cnts
    prof = np.where(cnts > 0, prof, 0.0)
    # Light smoothing over 3 bins before taking the plateau.
    sm = np.convolve(prof, np.ones(3) / 3.0, mode="same")
    peak_i = int(np.argmax(sm))
    plateau = sm[peak_i]
    if plateau <= 0:
        return math.nan
    half = 0.5 * plateau
    for i in range(peak_i, nb - 1):
        if prof[i] >= half > prof[i + 1]:
            t = (prof[i] - half) / (prof[i] - prof[i + 1])
            return 2.0 * ((i + 0.5) * width + t * width)
    return math.nan


def _light_edge(cut: np.ndarray, rr: np.ndarray, rmax: float, noise: float) -> float | None:
    nb = int(rmax)
    if nb < 3:
        return None
    idx = rr.astype(int)
    keep = idx < nb
    sums = np.bincount(idx[keep], weights=cut[keep], minlength=nb)
    cnts = np.bincount(idx[keep], minlength=nb).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        prof = np.where(cnts > 0, sums / np.maximum(cnts, 1), 0.0)
        err = noise / np.sqrt(np.maximum(cnts, 1))
    peak = float(prof.max())
    if peak <= 0:
        return None
    above = np.flatnonzero((prof > 3 * err) & (prof > 0.005 * peak))
    return float(above.max() + 1) if above.size else None


def _aperture(det: _Detection, cfg: MeasureConfig) -> float:
    """Aperture radius: the detection's extent (which follows a donut's
    outer edge, unlike its area), scaled and padded for the seeing wings."""
    return cfg.aperture_scale * det.extent + cfg.aperture_pad + 2.0 * cfg.smooth


def _measure_one(
    img: np.ndarray, resid: np.ndarray, det: _Detection, cfg: MeasureConfig, noise: float
) -> tuple[StarMeasurement | None, str | None]:
    ny, nx = img.shape
    if det.area < cfg.min_area:
        return None, "small"
    radius = _aperture(det, cfg)  # generous: the smoothing inflates it
    outer = radius + cfg.annulus
    x, y = det.x, det.y
    x0, x1 = max(0, math.floor(x - outer)), min(nx, math.ceil(x + outer) + 1)
    y0, y1 = max(0, math.floor(y - outer)), min(ny, math.ceil(y + outer) + 1)
    cut = resid[y0:y1, x0:x1]
    raw = img[y0:y1, x0:x1]
    yy, xx = np.indices(cut.shape, dtype=float)
    cx, cy = x - x0, y - y0
    rr = np.hypot(xx - cx, yy - cy)
    ann = (rr > radius) & (rr <= outer)
    local = _clipped_mean(cut[ann]) if ann.sum() >= 10 else 0.0
    local = 0.0 if not np.isfinite(local) else local
    cut = cut - local
    # Shrink the aperture to where the star's light ends (the outermost
    # 1-px ring above both 3 sigma and 0.5 % of the profile's peak) plus a
    # margin for the wings, recentring on the light inside it.
    for _ in range(3):
        edge = _light_edge(cut, rr, radius, noise)
        if edge is not None:
            radius = min(radius, edge + cfg.aperture_pad + cfg.smooth)
        ap = rr <= radius
        w = np.clip(cut, 0, None) * ap
        sw = w.sum()
        if sw <= 0:
            return None, "faint"
        cx, cy = float((w * xx).sum() / sw), float((w * yy).sum() / sw)
        rr = np.hypot(xx - cx, yy - cy)
    ap = rr <= radius
    sx_, sy_ = cx + x0, cy + y0
    if sx_ - radius < 0 or sy_ - radius < 0 or sx_ + radius > nx - 1 or sy_ + radius > ny - 1:
        return None, "edge"
    if cfg.region is not None:
        rx0, ry0, rx1, ry1 = cfg.region
        if sx_ - radius < rx0 or sy_ - radius < ry0 or sx_ + radius > rx1 or sy_ + radius > ry1:
            return None, "edge"
    if cfg.saturation is not None and np.any(raw[ap] >= cfg.saturation):
        return None, "saturated"
    flux = float(cut[ap].sum())
    npix = int(ap.sum())
    snr = flux / math.sqrt(max(npix * noise**2 + max(flux, 0.0), 1e-12))
    if snr < cfg.min_snr:
        return None, "faint"
    # Sub-pixel growth curve.
    off = (np.arange(_SUB) + 0.5) / _SUB - 0.5
    sx = (xx[..., None, None] + off[None, None, None, :]) - cx
    sy = (yy[..., None, None] + off[None, None, :, None]) - cy
    sr = np.hypot(sx, sy).ravel()
    sf = np.repeat(cut.ravel() / _SUB**2, _SUB * _SUB)
    hfd, _ = _growth_hfd(sr, sf, radius)
    fwhm = _moments(np.where(ap, cut, 0.0), cx, cy)
    ell = _ellipticity(np.where(ap, cut, 0.0), cx, cy)
    donut = _donut_edge(sr, sf, radius)
    if np.isfinite(ell) and ell > cfg.max_ellipticity:
        return None, "elongated"
    star = StarMeasurement(
        x=cx + x0,
        y=cy + y0,
        flux=flux,
        snr=snr,
        peak=float(raw[ap].max()),
        hfd=hfd,
        fwhm=fwhm,
        ellipticity=ell,
        donut=donut,
        radius=radius,
    )
    return star, None


def measure(image: np.ndarray, config: MeasureConfig | None = None) -> FieldMeasurement:
    """Detect and measure the stars in ``image`` (module doc)."""
    cfg = config or MeasureConfig()
    img = np.asarray(image, dtype=float)
    if img.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {img.shape}")
    bkg, noise = background(img, cfg.background_box)
    # Again with the sources masked (generously), so no star or donut
    # pulls its own background up.
    sm = _smooth(img - bkg, cfg.smooth)
    _, sm_noise = _clipped_median(
        sm[:: max(1, img.shape[0] // 256), :: max(1, img.shape[1] // 256)].ravel()
    )
    if np.isfinite(sm_noise) and sm_noise > 0:
        mask = _dilate(sm > 2.0 * sm_noise, cfg.grow + math.ceil(2 * cfg.smooth) + 2)
        bkg, noise = background(img, cfg.background_box, mask)
    resid = img - bkg
    dets = _detect(resid, noise, cfg)
    rejected: dict[str, int] = {}
    stars: list[StarMeasurement] = []
    # A blend: another real detection (not a noise blob: at least 5 % of
    # this one's flux) whose light reaches into this star's aperture.
    sizes = [math.sqrt(d.area / math.pi) for d in dets]
    for i, d in enumerate(dets):
        if len(stars) >= cfg.max_stars:
            break
        star, why = _measure_one(img, resid, d, cfg, noise)
        if star is None:
            assert why is not None
            rejected[why] = rejected.get(why, 0) + 1
            continue
        if any(
            j != i
            and dets[j].area >= cfg.min_area
            and dets[j].flux > 0.05 * d.flux
            and math.hypot(dets[j].x - star.x, dets[j].y - star.y) < star.radius + sizes[j]
            for j in range(len(dets))
        ):
            rejected["blended"] = rejected.get("blended", 0) + 1
            continue
        stars.append(star)
    return FieldMeasurement(stars, rejected, float(np.median(bkg)), noise)
