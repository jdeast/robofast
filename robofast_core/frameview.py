"""Frames for people to look at: binning, a compact array transport, display
scaling and a dependency-free PNG encoder.

Used by the guider service (``GET /frame``: its last frame, binned and
compressed) and by the observer console (docs/console.md, "Images"), which
scales frames on the server to 8-bit PNG so a phone on a slow link gets a
few kilobytes per guider frame rather than the raw pixels.

Nothing here is used to measure anything: display only.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from typing import Any

import numpy as np

#: Display intervals and stretches (``scale_limits``, ``stretch``).
SCALES = ("zscale", "minmax", "99.5", "99", "95")
STRETCHES = ("linear", "asinh", "log", "sqrt")


# -- binning and transport -----------------------------------------------------------


def block_mean(data: np.ndarray, factor: int) -> np.ndarray:
    """Mean of ``factor`` x ``factor`` blocks (the ragged edge is dropped,
    unless that would leave nothing)."""
    a = np.asarray(data, dtype=np.float32)
    if factor <= 1:
        return a
    h, w = a.shape
    hh, ww = h // factor, w // factor
    if hh == 0 or ww == 0:
        return a[::factor, ::factor]
    return a[: hh * factor, : ww * factor].reshape(hh, factor, ww, factor).mean(axis=(1, 3))


def bin_to(data: np.ndarray, max_side: int) -> tuple[np.ndarray, int]:
    """Bin ``data`` by the smallest integer factor that brings both sides
    to at most ``max_side``; (binned, factor)."""
    h, w = data.shape[:2]
    factor = max(1, math.ceil(max(h, w) / max(1, max_side)))
    return block_mean(data, factor), factor


def encode_array(data: np.ndarray) -> dict[str, Any]:
    """A 2-D array as JSON: uint16 when it holds whole numbers in range
    (camera ADU), else float32; zlib-compressed, base64."""
    a = np.asarray(data)
    finite = np.isfinite(a)
    if (
        a.size
        and finite.all()
        and float(a.min()) >= 0
        and float(a.max()) <= 65535
        and np.array_equal(a, np.round(a))
    ):
        raw, dtype = a.astype("<u2").tobytes(), "uint16"
    else:
        raw, dtype = np.where(finite, a, np.nan).astype("<f4").tobytes(), "float32"
    return {
        "shape": list(a.shape),
        "dtype": dtype,
        "data": base64.b64encode(zlib.compress(raw, 6)).decode(),
    }


def decode_array(doc: dict[str, Any], max_pixels: int = 64 * 1024 * 1024) -> np.ndarray:
    """The inverse of :func:`encode_array` (bounded: the sender is a service,
    but a bug must not exhaust memory)."""
    shape = tuple(int(x) for x in doc["shape"])
    if len(shape) != 2 or min(shape) < 1 or shape[0] * shape[1] > max_pixels:
        raise ValueError(f"bad frame shape {shape}")
    dtype = {"uint16": "<u2", "float32": "<f4"}[doc["dtype"]]
    want = shape[0] * shape[1] * np.dtype(dtype).itemsize
    d = zlib.decompressobj()
    raw = d.decompress(base64.b64decode(doc["data"]), want + 1)
    if len(raw) != want:
        raise ValueError("frame data does not match its shape")
    return np.frombuffer(raw, dtype=dtype).reshape(shape).astype(np.float32)


# -- display scaling ---------------------------------------------------------------------


def zscale(data: np.ndarray, contrast: float = 0.25, samples: int = 1000) -> tuple[float, float]:
    """IRAF's zscale interval (approximately): a line fitted to the sorted
    sample, with iterative rejection; its slope divided by ``contrast``."""
    a = np.asarray(data, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 0.0, 1.0
    step = max(1, a.size // samples)
    s = np.sort(a[::step])
    n = s.size
    lo, hi = float(s[0]), float(s[-1])
    if n < 5:
        return lo, hi
    x = np.arange(n, dtype=np.float64)
    keep = np.ones(n, bool)
    slope = intercept = 0.0
    for _ in range(5):
        if keep.sum() < max(5, n // 2):
            break
        slope, intercept = np.polyfit(x[keep], s[keep], 1)
        resid = s - (slope * x + intercept)
        sigma = float(np.std(resid[keep])) or 1e-12
        new = np.abs(resid) < 2.5 * sigma
        if np.array_equal(new, keep):
            break
        keep = new
    if keep.sum() < max(5, n // 2):
        return lo, hi
    mid = (n - 1) / 2
    median = float(np.median(s))
    slope /= contrast
    z1 = max(lo, median - slope * mid)
    z2 = min(hi, median + slope * (n - 1 - mid))
    return (z1, z2) if z2 > z1 else (lo, hi)


def scale_limits(data: np.ndarray, scale: str = "zscale") -> tuple[float, float]:
    a = np.asarray(data)
    if scale == "zscale":
        return zscale(a)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return 0.0, 1.0
    if scale == "minmax":
        lo, hi = float(finite.min()), float(finite.max())
    else:
        p = float(scale)
        sample = finite.ravel()[:: max(1, finite.size // 200000)]
        lo, hi = (float(v) for v in np.percentile(sample, [100 - p, p]))
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


def stretch(norm: np.ndarray, kind: str = "linear") -> np.ndarray:
    """``norm`` in [0, 1] -> [0, 1] (astropy.visualization's forms)."""
    x = np.clip(norm, 0.0, 1.0)
    if kind == "asinh":
        a = 0.1
        return np.arcsinh(x / a) / np.arcsinh(1.0 / a)
    if kind == "log":
        a = 1000.0
        return np.log(a * x + 1.0) / np.log(a + 1.0)
    if kind == "sqrt":
        return np.sqrt(x)
    return x


def to_8bit(data: np.ndarray, lo: float, hi: float, kind: str = "linear") -> np.ndarray:
    a = np.asarray(data, dtype=np.float32)
    norm = (a - lo) / (hi - lo if hi > lo else 1.0)
    out = stretch(np.nan_to_num(norm, nan=0.0), kind)
    return np.round(out * 255).astype(np.uint8)


def png_gray8(img: np.ndarray, level: int = 6) -> bytes:
    """An 8-bit grayscale PNG (no dependencies: zlib and struct)."""
    a = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = a.shape

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    # Filter type 1 (Sub) per row: neighbouring pixels of a sky frame are
    # alike, so the differences compress better than the values.
    diff = a.astype(np.int16)
    diff[:, 1:] -= a[:, :-1]
    rows = np.empty((h, w + 1), np.uint8)
    rows[:, 0] = 1
    rows[:, 1:] = (diff & 0xFF).astype(np.uint8)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows.tobytes(), level))
        + chunk(b"IEND", b"")
    )


def render(
    data: np.ndarray,
    *,
    region: tuple[int, int, int, int] | None = None,
    max_side: int = 512,
    scale: str = "zscale",
    kind: str = "linear",
    limits: tuple[float, float] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """``data`` (rows = y) cut to ``region`` (x0, y0, x1, y1; pixel
    indices, end exclusive), binned to at most ``max_side``, scaled and
    encoded as PNG. ``limits`` (from the whole frame, so panning does not
    change the stretch) else computed from the cut. Returns the PNG and what
    the client needs to map canvas pixels back: the region and the binning.
    """
    h, w = data.shape
    x0, y0, x1, y1 = region if region is not None else (0, 0, w, h)
    x0, x1 = max(0, min(int(x0), w - 1)), max(1, min(int(x1), w))
    y0, y1 = max(0, min(int(y0), h - 1)), max(1, min(int(y1), h))
    if x1 <= x0:
        x1 = x0 + 1
    if y1 <= y0:
        y1 = y0 + 1
    cut = data[y0:y1, x0:x1]
    binned, factor = bin_to(cut, max_side)
    lo, hi = limits if limits is not None else scale_limits(cut, scale)
    png = png_gray8(to_8bit(binned, lo, hi, kind))
    return png, {
        "region": [x0, y0, x1, y1],
        "view_binning": factor,
        "size": [int(binned.shape[1]), int(binned.shape[0])],
        "limits": [lo, hi],
        "scale": scale,
        "stretch": kind,
    }
