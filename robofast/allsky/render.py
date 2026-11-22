"""Pictures of cloud maps and validation series, drawn with Pillow.

:func:`cloud_map_png` draws a frame with its stars (detected green, missed
red) beside a polar map of the cells (green clear, red cloudy, grey
unknown). :func:`time_series_png` draws simple x-y point plots for the
validation report. Pillow is imported lazily (:mod:`robofast.allsky.imageio`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from robofast.allsky.imageio import pil

__all__ = ["Canvas", "cloud_map_png", "colour_for", "time_series_png"]

Colour = tuple[int, int, int]


class Canvas:
    """A white RGB image with Pillow's ImageDraw and its default font."""

    def __init__(self, width: int, height: int, bg: Colour = (255, 255, 255)) -> None:
        P = pil()
        self.im = P.Image.new("RGB", (width, height), bg)
        self.draw = P.ImageDraw.Draw(self.im)
        self._fonts: dict[int, Any] = {}

    def font(self, size: int = 11) -> Any:
        if size not in self._fonts:
            f = pil().ImageFont
            try:
                self._fonts[size] = f.load_default(size=size)
            except TypeError:  # Pillow < 10.1: one bitmap size
                self._fonts[size] = f.load_default()
        return self._fonts[size]

    def blit(self, img: np.ndarray, x: int, y: int) -> None:
        self.im.paste(pil().Image.fromarray(np.ascontiguousarray(img, dtype=np.uint8)), (x, y))

    def points(self, xs: Any, ys: Any, colour: Colour, size: int = 2) -> None:
        h = size / 2
        for x, y in zip(np.ravel(xs), np.ravel(ys), strict=True):
            self.draw.rectangle([x - h, y - h, x + h, y + h], fill=colour)

    def line(self, x0: Any, y0: Any, x1: Any, y1: Any, colour: Colour, width: int = 1) -> None:
        self.draw.line([(float(x0), float(y0)), (float(x1), float(y1))], fill=colour, width=width)

    def circle(self, x: float, y: float, r: float, colour: Colour, width: int = 1) -> None:
        self.draw.ellipse([x - r, y - r, x + r, y + r], outline=colour, width=width)

    def rect(self, x0: Any, y0: Any, x1: Any, y1: Any, colour: Colour) -> None:
        xa, xb = sorted((float(x0), float(x1)))
        ya, yb = sorted((float(y0), float(y1)))
        self.draw.rectangle([xa, ya, xb, yb], fill=colour)

    def text(self, x: Any, y: Any, s: str, colour: Colour = (0, 0, 0), size: int = 11) -> None:
        self.draw.text((float(x), float(y)), s, fill=colour, font=self.font(size))

    def save(self, path: str) -> None:
        self.im.save(str(path), "PNG")


def colour_for(clear: float | None) -> Colour:
    """Clear fraction -> colour: red (0) through yellow to green (1); grey NaN."""
    if clear is None or not np.isfinite(clear):
        return (150, 150, 150)
    c = float(np.clip(clear, 0, 1))
    if c < 0.5:
        return (215, int(60 + 2 * c * 150), 50)
    return (int(215 - (c - 0.5) * 2 * 175), 200, 50)


def _stretch(grey: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(grey, [2, 99.8])
    return np.clip((grey - lo) / max(hi - lo, 1e-3) * 255, 0, 255).astype(np.uint8)


def polar_xy(alt: Any, az: Any, cx: float, cy: float, rad: float) -> tuple[Any, Any]:
    """(alt, az) on a polar plot: zenith at the centre, horizon at ``rad``,
    north up, east left (as seen from below)."""
    r = (90.0 - np.asarray(alt, dtype=float)) / 90.0 * rad
    a = np.radians(np.asarray(az, dtype=float))
    return cx - r * np.sin(a), cy - r * np.cos(a)


def draw_polar_map(cv: Canvas, cmap: Any, cx: float, cy: float, rad: float) -> None:
    from robofast.allsky.clouds import PATCHES

    # Outer rings first: each ring's wedges cover the inner part of the last.
    for p in sorted(PATCHES, key=lambda p: p.alt_lo):
        r = (90 - p.alt_lo) / 90 * rad
        box = [cx - r, cy - r, cx + r, cy + r]
        col = colour_for(float(cmap.clear[p.index]))
        if p.alt_hi >= 90:
            cv.draw.ellipse(box, fill=col)
        else:
            # Azimuth east of north with east to the left: PIL angle = 270 - az.
            cv.draw.pieslice(box, 270 - p.az_hi, 270 - p.az_lo, fill=col)
    for a in (35, 50, 65, 80):
        cv.circle(cx, cy, (90 - a) / 90 * rad, (255, 255, 255))
    cv.circle(cx, cy, 70 / 90 * rad, (80, 80, 80))
    for p in PATCHES:
        if p.alt_hi < 90:
            x0, y0 = polar_xy(p.alt_lo, p.az_lo, cx, cy, rad)
            x1, y1 = polar_xy(p.alt_hi, p.az_lo, cx, cy, rad)
            cv.line(x0, y0, x1, y1, (255, 255, 255))
        v = float(cmap.clear[p.index])
        if np.isfinite(v):
            tx, ty = polar_xy(p.alt, p.az, cx, cy, rad)
            cv.text(tx - 9, ty - 6, f"{v:.1f}")
    for lab, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        tx, ty = polar_xy(12, az, cx, cy, rad)
        cv.text(tx - 5, ty - 8, lab, size=16)


def cloud_map_png(path: str, frame: Any, cmap: Any, size: int = 520) -> None:
    """The frame (scaled to ``size``) with its stars, beside the cell map."""
    P = pil()
    g = _stretch(frame.grey)
    h, w = g.shape
    s = size / max(h, w)
    small = np.asarray(P.Image.fromarray(g).resize((int(w * s), int(h * s)), P.Image.BILINEAR))
    cv = Canvas(2 * size + 40, size + 70)
    cv.blit(np.stack([small] * 3, axis=-1), 10, 50)
    t = cmap.table
    if t["v"].size:
        sel = t["p"] > 0.3
        for det, col in ((1.0, (0, 230, 0)), (0.0, (255, 40, 40))):
            k = sel & (t["det"] == det)
            for x, y in zip(t["x"][k] * s + 10, t["y"][k] * s + 50, strict=True):
                cv.circle(x, y, 4, col)
    draw_polar_map(cv, cmap, size + 30 + size / 2, 50 + size / 2, size / 2)
    cv.text(10, 6, f"{'+'.join(cmap.cameras)}  {cmap.time:%Y-%m-%d %H:%M:%S}Z", size=18)
    cv.text(
        10,
        30,
        f"clear {cmap.whole_sky:.2f}  coverage {cmap.coverage:.2f}  Sun {cmap.sun_alt:.0f}  "
        f"Moon {cmap.moon_alt:.0f} ({cmap.moon_illum:.0%})  {' '.join(cmap.flags)}",
    )
    cv.text(size + 30, size + 52, "green: detected; red: missed (P > 0.3); cells: clear fraction")
    cv.save(path)


def time_series_png(
    path: str,
    series: Sequence[tuple[np.ndarray, np.ndarray, Colour, str]],
    *,
    title: str = "",
    ylim: tuple[float, float] | None = None,
    xlabel: str = "",
    y2: Sequence[tuple[np.ndarray, np.ndarray, Colour, str]] = (),
    y2lim: tuple[float, float] | None = None,
    bands: Sequence[tuple[float, float, Colour]] = (),
    width: int = 900,
    height: int = 320,
) -> None:
    """Point plots of (x, y, colour, label); ``y2`` on a right-hand axis;
    ``bands``: shaded x ranges (x0, x1, colour). x is in hours."""
    cv = Canvas(width, height)
    left, right, top, bottom = 50, width - 60, 30, height - 40
    allx = np.concatenate([s[0] for s in [*series, *y2] if len(s[0])] or [np.zeros(1)])
    x0, x1 = float(np.nanmin(allx)), float(np.nanmax(allx))
    if x1 <= x0:
        x1 = x0 + 1

    def lims(ss: Sequence[Any], given: tuple[float, float] | None) -> tuple[float, float]:
        if given:
            return given
        ys = np.concatenate([s[1][np.isfinite(s[1])] for s in ss if len(s[1])] or [np.zeros(1)])
        lo, hi = (float(ys.min()), float(ys.max())) if ys.size else (0.0, 1.0)
        return (lo, hi if hi > lo else lo + 1)

    ya = lims(series, ylim)
    yb = lims(y2, y2lim) if y2 else None

    def px(x: Any) -> Any:
        return left + (np.asarray(x) - x0) / (x1 - x0) * (right - left)

    def py(y: Any, lim: tuple[float, float]) -> Any:
        return bottom - (np.asarray(y) - lim[0]) / (lim[1] - lim[0]) * (bottom - top)

    for b0, b1, col in bands:
        cv.rect(px(b0), top, px(b1), bottom, col)
    cv.line(left, bottom, right, bottom, (0, 0, 0))
    cv.line(left, top, left, bottom, (0, 0, 0))
    if yb:
        cv.line(right, top, right, bottom, (0, 0, 0))
    for k in range(5):
        y = py(ya[0] + (ya[1] - ya[0]) * k / 4, ya)
        cv.line(left - 4, y, left, y, (0, 0, 0))
        cv.text(4, y - 6, f"{ya[0] + (ya[1] - ya[0]) * k / 4:.2f}")
        if yb:
            cv.line(right, y, right + 4, y, (0, 0, 0))
            cv.text(right + 6, y - 6, f"{yb[0] + (yb[1] - yb[0]) * k / 4:.0f}")
    for k in range(int(np.ceil(x0)), int(x1) + 1):
        cv.line(px(k), bottom, px(k), bottom + 4, (0, 0, 0))
        cv.text(px(k) - 6, bottom + 6, f"{k % 24:02d}")
    for xs, ys, col, _ in series:
        ok = np.isfinite(ys)
        cv.points(px(xs[ok]), py(ys[ok], ya), col)
    for xs, ys, col, _ in y2:
        ok = np.isfinite(ys)
        cv.points(px(xs[ok]), py(ys[ok], yb), col)
    cv.text(left, 8, title)
    lx = left + 7 * len(title) + 30
    for _, _, col, lab in [*series, *y2]:
        cv.rect(lx, 10, lx + 10, 18, col)
        cv.text(lx + 14, 8, lab)
        lx += 14 + 7 * len(lab) + 16
    cv.text(left + (right - left) // 2 - 3 * len(xlabel), bottom + 22, xlabel)
    cv.save(path)
