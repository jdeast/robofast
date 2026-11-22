"""The all-sky cameras: where their frames come from, where they stand, a
starting guess at their geometry, and the pixels that are never sky.

The cameras are site data (docs/design.md 3.10): a ``cameras.yaml`` in the
**models directory**, beside the fitted models (``<camera>.json``) and
their static masks. The directory is ``$ROBOFAST_ALLSKY_MODELS``, or the
one a command or the watcher's config names; MINERVA's is
``sites/minerva/allsky/``. The guesses only seed
:func:`robofast.allsky.calibrate.solve`, which finds the real centre,
rotation and distortion from the stars.

Masks are in pixels of the camera's native frame: ``rects``
``(x0, y0, x1, y1)`` (inclusive-exclusive), thick ``segments``
``(xa, ya, xb, yb, half_width)`` and ``rings`` ``(cx, cy, radius, half_width)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["CAMERAS", "CameraSpec", "camera_spec", "load_cameras", "models_dir"]

#: Environment variable naming the models directory.
MODELS_ENV = "ROBOFAST_ALLSKY_MODELS"
_MASKS: dict[tuple, np.ndarray] = {}


def models_dir(given: str | Path | None = None) -> Path:
    """``given``, else ``$ROBOFAST_ALLSKY_MODELS``; an error names both."""
    d = given or os.environ.get(MODELS_ENV)
    if not d:
        raise FileNotFoundError(
            f"no all-sky models directory: pass one or set ${MODELS_ENV} "
            "(a site's: sites/<site>/allsky)"
        )
    return Path(d).expanduser()


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
    #: Where the camera stands: (latitude deg N, longitude deg E, elevation m).
    site: tuple[float, float, float] | None = None

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> CameraSpec:
        known = set(cls.__dataclass_fields__) - {"name"}
        extra = set(d) - known
        if extra:
            raise ValueError(f"camera {name}: unknown keys {sorted(extra)}")
        kw: dict[str, Any] = dict(d)
        for key in ("rects", "segments", "rings"):
            kw[key] = tuple(tuple(v) for v in d.get(key) or ())
        for key in ("radial", "label_box", "site"):
            if d.get(key) is not None:
                kw[key] = tuple(d[key])
        kw["notes"] = tuple(d.get("notes") or ())
        spec = cls(name=name, **kw)
        if spec.site is None or len(spec.site) != 3:
            raise ValueError(f"camera {name}: site must be [latitude, longitude, elevation]")
        return spec

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


#: Camera specs loaded so far, by name (:func:`load_cameras` fills it).
CAMERAS: dict[str, CameraSpec] = {}


def load_cameras(directory: str | Path | None = None) -> dict[str, CameraSpec]:
    """The cameras of a models directory's ``cameras.yaml``; also registers
    them in :data:`CAMERAS`."""
    import yaml

    path = models_dir(directory) / "cameras.yaml"
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: the top level must be a mapping of cameras")
    specs = {name: CameraSpec.from_dict(name, d or {}) for name, d in raw.items()}
    CAMERAS.update(specs)
    return specs


def camera_spec(name: str, directory: str | Path | None = None) -> CameraSpec:
    """A camera by name: from :data:`CAMERAS`, else the models directory."""
    if name not in CAMERAS:
        load_cameras(directory)
    try:
        return CAMERAS[name]
    except KeyError:
        raise KeyError(f"unknown camera {name!r}; known: {', '.join(CAMERAS)}") from None
