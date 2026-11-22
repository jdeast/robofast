"""Image operations for 8-bit all-sky frames, numpy only.

Background: medians in blocks, smoothed and interpolated back to full
resolution (stars are a few pixels, so a block median ignores them).
Noise: the robust scatter of the smoothed residual in the same blocks.
Peaks: local maxima of the smoothed residual, as signal-to-noise.
"""

from __future__ import annotations

import warnings

import numpy as np

__all__ = [
    "Background",
    "background",
    "dilate",
    "local_max",
    "smooth121",
    "upsample",
]


def smooth121(a: np.ndarray) -> np.ndarray:
    """Separable [1, 2, 1]/4 smoothing (edges replicated)."""
    p = np.pad(a.astype(np.float32), 1, mode="edge")
    r = 0.25 * p[:, :-2] + 0.5 * p[:, 1:-1] + 0.25 * p[:, 2:]
    return 0.25 * r[:-2] + 0.5 * r[1:-1] + 0.25 * r[2:]


def _shift_max(a: np.ndarray, half: int, axis: int) -> np.ndarray:
    p = np.pad(a, [(half, half) if ax == axis else (0, 0) for ax in range(a.ndim)], mode="edge")
    n = a.shape[axis]
    out = a.copy()
    for k in range(2 * half + 1):
        out = np.maximum(out, np.take(p, np.arange(k, k + n), axis=axis))
    return out


def max_filter(a: np.ndarray, half: int) -> np.ndarray:
    """Maximum over a (2*half+1)^2 box."""
    return _shift_max(_shift_max(a, half, 0), half, 1)


def local_max(a: np.ndarray, half: int = 2) -> np.ndarray:
    """True where ``a`` is the maximum of its (2*half+1)^2 neighbourhood."""
    return a >= max_filter(a, half)


def dilate(mask: np.ndarray, half: int) -> np.ndarray:
    if half <= 0:
        return mask.copy()
    return max_filter(mask.astype(np.uint8), half).astype(bool)


def _fill_nan(grid: np.ndarray) -> np.ndarray:
    """Replace NaN cells by the mean of valid neighbours, repeatedly."""
    g = grid.copy()
    if np.all(np.isnan(g)):
        return np.zeros_like(g)
    while np.isnan(g).any():
        p = np.pad(g, 1, mode="constant", constant_values=np.nan)
        stack = np.stack([p[1 + dy : p.shape[0] - 1 + dy, 1 + dx : p.shape[1] - 1 + dx]
                          for dy in (-1, 0, 1) for dx in (-1, 0, 1)])  # fmt: skip
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            m = np.nanmean(stack, axis=0)
        holes = np.isnan(g)
        g[holes] = m[holes]
    return g


def upsample(grid: np.ndarray, block: int, shape: tuple[int, int]) -> np.ndarray:
    """Bilinear interpolation of a block grid (cell centres) to ``shape``."""
    ny, nx = grid.shape
    yc = (np.arange(ny) + 0.5) * block - 0.5
    xc = (np.arange(nx) + 0.5) * block - 0.5
    yi = np.clip(np.interp(np.arange(shape[0]), yc, np.arange(ny)), 0, ny - 1)
    xi = np.clip(np.interp(np.arange(shape[1]), xc, np.arange(nx)), 0, nx - 1)
    y0 = np.floor(yi).astype(int)
    x0 = np.floor(xi).astype(int)
    y1 = np.minimum(y0 + 1, ny - 1)
    x1 = np.minimum(x0 + 1, nx - 1)
    fy = (yi - y0)[:, None]
    fx = (xi - x0)[None, :]
    top = grid[y0][:, x0] * (1 - fx) + grid[y0][:, x1] * fx
    bot = grid[y1][:, x0] * (1 - fx) + grid[y1][:, x1] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def _blocks(a: np.ndarray, block: int) -> np.ndarray:
    ny, nx = -(-a.shape[0] // block), -(-a.shape[1] // block)
    p = np.full((ny * block, nx * block), np.nan, dtype=np.float32)
    p[: a.shape[0], : a.shape[1]] = a
    return p.reshape(ny, block, nx, block).transpose(0, 2, 1, 3).reshape(ny, nx, -1)


def block_median(a: np.ndarray, valid: np.ndarray, block: int, min_frac: float = 0.3) -> np.ndarray:
    v = np.where(valid, a, np.nan).astype(np.float32)
    b = _blocks(v, block)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        n = np.sum(~np.isnan(b), axis=2)
        med = np.nanmedian(b, axis=2)
    med[n < min_frac * block * block] = np.nan
    return med


class Background:
    """Background, residual, smoothed residual, noise and S/N of a frame."""

    def __init__(self, img: np.ndarray, valid: np.ndarray, block: int = 32) -> None:
        img = img.astype(np.float32)
        self.block = block
        coarse = _fill_nan(block_median(img, valid, block))
        self.coarse = coarse
        self.bg = upsample(coarse, block, img.shape)
        self.resid = img - self.bg
        self.smooth = smooth121(self.resid)
        sm = np.where(valid, self.smooth, np.nan)
        b = _blocks(sm, block)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(b, axis=2)
            mad = np.nanmedian(np.abs(b - med[:, :, None]), axis=2)
        # JPEG flattens dark noise to zero: floor it at a quarter count.
        sig = np.maximum(1.4826 * mad, 0.25)
        n = np.sum(~np.isnan(b), axis=2)
        sig[n < 0.3 * block * block] = np.nan
        self.noise_coarse = _fill_nan(sig)
        self.noise = upsample(self.noise_coarse, block, img.shape)
        self.snr = self.smooth / self.noise
        self.valid = valid


def background(img: np.ndarray, valid: np.ndarray, block: int = 32) -> Background:
    return Background(img, valid, block)


def peaks(
    bg: Background, threshold: float = 4.0, half: int = 2
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, snr) of local maxima of the S/N image above ``threshold``,
    refined to a sub-pixel centroid over 3x3."""
    s = np.where(bg.valid, bg.snr, -np.inf)
    lm = local_max(s, half) & (s > threshold)
    yy, xx = np.nonzero(lm)
    h, w = s.shape
    ok = (xx > 0) & (yy > 0) & (xx < w - 1) & (yy < h - 1)
    yy, xx = yy[ok], xx[ok]
    sm = bg.smooth
    tot = np.zeros(xx.size)
    cx = np.zeros(xx.size)
    cy = np.zeros(xx.size)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            v = np.maximum(sm[yy + dy, xx + dx], 0)
            tot += v
            cx += v * dx
            cy += v * dy
    tot = np.where(tot > 0, tot, 1)
    return xx + cx / tot, yy + cy / tot, s[yy, xx]
