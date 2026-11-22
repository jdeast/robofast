"""The all-sky cameras on Mt Hopkins: where their images come from, a
starting guess at their geometry, and the parts of the image that are not
sky (burned-in text, the mast).

The guesses only seed :func:`robofast.allsky.calibrate.solve`, which finds
the real centre, rotation and distortion from the stars; the fitted model
is a JSON file (:mod:`robofast.allsky.model`), by default
``robofast/allsky/models/<camera>.json``.

Masks are in pixels of the camera's native frame: rectangles
``(x0, y0, x1, y1)`` (inclusive-exclusive) and thick segments
``(xa, ya, xb, yb, half_width)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = ["CAMERAS", "CameraSpec", "camera_spec"]

MODELS_DIR = Path(__file__).parent / "models"
_MASKS: dict[tuple, np.ndarray] = {}


@dataclass(frozen=True)
class CameraSpec:
    name: str
    title: str
    url: str
    width: int
    height: int
    #: Guess: centre, horizon radius (px), rotation (deg), parity.
    x0: float
    y0: float
    horizon_px: float
    rot: float = 0.0
    parity: int = 1
    #: Guess at the radial polynomial (f1, f2, f3), px; default equidistant
    #: from ``horizon_px``.
    radial: tuple[float, float, float] | None = None
    rects: tuple[tuple[int, int, int, int], ...] = ()
    segments: tuple[tuple[float, float, float, float, float], ...] = ()
    #: Circles drawn on the image: (cx, cy, radius, half_width).
    rings: tuple[tuple[float, float, float, float], ...] = ()
    #: Seconds between frames.
    cadence_s: float = 60.0
    #: Pixels at or above this are saturated (or burned-in white text).
    saturation: float = 250.0
    #: The overlay draws coloured labels (MMTO): mask strongly coloured pixels.
    colour_labels: bool = False
    #: The overlay names the bright stars, planets and Moon: mask a box
    #: (left, right, up, down px) about each, for stars to ``label_vmax``.
    label_box: tuple[int, int, int, int] | None = None
    label_vmax: float = 1.5
    notes: tuple[str, ...] = field(default_factory=tuple)

    def model_path(self) -> Path:
        return MODELS_DIR / f"{self.name}.json"

    def fixed_mask(self, shape: tuple[int, int] | None = None) -> np.ndarray:
        """True where the image is never sky (text, mast), from the spec."""
        h, w = shape or (self.height, self.width)
        key = (self, h, w)
        if key not in _MASKS:
            _MASKS[key] = self._fixed_mask(h, w)
        return _MASKS[key]

    def _fixed_mask(self, h: int, w: int) -> np.ndarray:
        m = np.zeros((h, w), dtype=bool)
        for x0, y0, x1, y1 in self.rects:
            m[max(y0, 0) : min(y1, h), max(x0, 0) : min(x1, w)] = True
        if self.segments:
            yy, xx = np.mgrid[0:h, 0:w]
            for xa, ya, xb, yb, hw in self.segments:
                dx, dy = xb - xa, yb - ya
                t = np.clip(((xx - xa) * dx + (yy - ya) * dy) / (dx * dx + dy * dy), 0, 1)
                d = np.hypot(xx - (xa + t * dx), yy - (ya + t * dy))
                m |= d <= hw
        if self.rings:
            yy, xx = np.mgrid[0:h, 0:w]
            for cx, cy, r, hw in self.rings:
                m |= np.abs(np.hypot(xx - cx, yy - cy) - r) <= hw
        return m


CAMERAS: dict[str, CameraSpec] = {
    "ridge": CameraSpec(
        name="ridge",
        title="SAO ridge all-sky (FLWO ridge)",
        url="http://linmax.sao.arizona.edu/weather/sky/last_sky.jpg",
        width=1040,
        height=1040,
        x0=522.0,
        y0=505.0,
        horizon_px=500.0,
        rects=((470, 0, 640, 32), (0, 995, 440, 1040)),
        # The mast, from the southern horizon toward the centre (2026; in the
        # 2023 frames it crossed the lower right: (612, 650) to (805, 915)).
        segments=((556.0, 650.0, 628.0, 1000.0, 10.0),),
        cadence_s=60.0,
        notes=(
            "1040x1040 grey JPEG converted from FITS at the source;",
            "one a minute, sunset to sunrise.",
            "Text: 'North' at the top, the time and 'East' at the bottom left.",
        ),
    ),
    "mmto": CameraSpec(
        name="mmto",
        title="MMT Observatory all-sky (summit)",
        url="https://skycam.mmto.arizona.edu/skycam/latest_image.jpg",
        width=1422,
        height=1411,
        # The overlay's own altitude rings: centre (710, 705), 7.33 px/deg.
        x0=710.0,
        y0=705.0,
        horizon_px=660.0,
        rects=(
            (0, 0, 110, 34),  # Sun altitude box
            (0, 1340, 70, 1372),  # frame counter
            (0, 1376, 280, 1411),  # local time
            (1255, 1336, 1422, 1372),  # temperature / humidity
            (1160, 1374, 1422, 1411),  # f/ gain exposure
            (680, 10, 740, 70),  # N
            (680, 1342, 740, 1402),  # S
            (14, 676, 74, 736),  # E
            (1346, 676, 1406, 736),  # W
            (686, 680, 790, 730),  # MMT crosshair and label
        ),
        # The overlay: altitude rings every 10 deg (7.33 px/deg), the N-S and
        # E-W lines through the centre, the ring labels along the SE radius.
        segments=(
            (710.0, 40.0, 710.0, 1372.0, 2.0),
            (44.0, 705.0, 1376.0, 705.0, 2.0),
            (650.0, 735.0, 285.0, 1125.0, 16.0),
        ),
        rings=tuple((710.0, 705.0, 73.3 * k, 2.0) for k in range(1, 10)),
        cadence_s=34.0,
        colour_labels=True,
        label_box=(10, 110, 14, 14),
        label_vmax=1.6,
        notes=(
            "1422x1411 colour JPEG with an overlay: altitude rings every 10 deg, N-S and E-W",
            "lines, and labelled bright stars, planets, Moon and Sun (masked per frame by colour",
            "and by the static-feature mask).",
        ),
    ),
    "mmto-png": CameraSpec(
        name="mmto-png",
        title="MMT Observatory all-sky, 2016-2023 (640x480 PNG stacks)",
        url="http://skycam.mmto.arizona.edu/skycam/latest_image.png",
        width=640,
        height=480,
        x0=322.0,
        y0=248.0,
        horizon_px=262.0,
        rects=(
            (0, 0, 150, 62),  # time, gain, frames
            (460, 0, 640, 50),  # logo
            (0, 462, 150, 480),  # IDL time stamp
            (300, 0, 332, 26),  # N
            (300, 456, 332, 480),  # S
            (58, 226, 86, 252),  # E
            (535, 226, 565, 252),  # W
            (300, 232, 332, 264),  # centre marker
        ),
        cadence_s=34.0,
        notes=("256-frame stacks, gain and stamps burned in.",),
    ),
}


def camera_spec(name: str) -> CameraSpec:
    try:
        return CAMERAS[name]
    except KeyError:
        raise KeyError(f"unknown camera {name!r}; known: {', '.join(CAMERAS)}") from None
