"""Calibration: how a device's motion moves the star on the guide camera.

One 2x2 matrix ``M`` per device and port, measured, not assumed::

    d(pixels) = M @ d(device units)

It replaces the legacy scalars -- MINERVA's ``ARCSEC_PER_STEP`` and
``ROTOFFSET`` (``config/ao_*.ini``) and tres-guider's
``STEPS_PER_PIXEL_TIP/TILT``, ``THETA`` and ``FLIP`` -- with the general
linear map: two scales, a rotation, a flip (``det M < 0``) and any
non-orthogonality all come out of one fit, so no sign convention has to be
guessed. :func:`from_sx_legacy` and :func:`from_tres_legacy` turn the old
numbers into a matrix, as a starting point to compare with a measurement.

Measuring (:func:`measure`): with a star in the guide window, the device is
moved to its centre and then by ``+/- step`` on each axis (and back), the
star's centroid averaged over a few frames at each position, and ``pixel =
p0 + M @ (position - centre)`` fitted by least squares; the rms residual is
kept with the result. The mount is calibrated the same way (its units are
arcseconds of offset, east and north).

The sky: :func:`sky_matrix` gives arcseconds (east, north) per pixel from
the plate scale and the position angle (``pa``: of the detector's +y axis,
degrees east of north; ``parity`` -1 for a mirror-imaged field, e.g. after
an odd number of reflections). With it a calibration also maps the device
to the sky.

Calibrations are kept per device and port in a directory of JSON files
(:class:`CalibrationStore`), written atomically.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast_core.guider.base import (
    ROI,
    Camera,
    Centroider,
    Clock,
    GuiderError,
    RealClock,
    TipTilt,
)


def sky_matrix(plate_scale: float, pa_deg: float, parity: int = 1) -> np.ndarray:
    """Arcseconds (east, north) of sky per pixel (dx, dy): at ``pa`` 0 and
    parity +1, north is +y and east is -x (the sky as seen on the sky)."""
    t = math.radians(pa_deg)
    rot = np.array([[math.cos(t), math.sin(t)], [-math.sin(t), math.cos(t)]])
    return plate_scale * rot @ np.diag([-float(parity), 1.0])


def fits_sky_matrix(plate_scale: float, pa_deg: float, flip: bool) -> np.ndarray:
    """:func:`sky_matrix` from a PA and parity in the FITS / plate-solve
    convention (``robofast_core.fits_writer``, ``Solution.pa``/``flip``,
    the PA calibration): there a flipped field's PA is measured the other
    way, so it is the guider's ``sky_matrix(-pa, parity=-1)``."""
    return sky_matrix(plate_scale, -pa_deg if flip else pa_deg, -1 if flip else 1)


@dataclass
class Calibration:
    device: str
    port: str
    #: Pixels per device unit: d(pixels) = matrix @ d(device).
    matrix: list[list[float]]
    units: str = ""
    #: Rms of the fit (pixels).
    residual: float = 0.0
    measured: str = ""
    #: The step used and how many positions were measured.
    step: float = 0.0
    positions: int = 0
    #: Optional sky mapping (plate scale arcsec/pixel, PA degrees, parity).
    plate_scale: float | None = None
    pa: float | None = None
    parity: int = 1
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def m(self) -> np.ndarray:
        return np.asarray(self.matrix, dtype=float)

    def to_pixels(self, d_device: Any) -> np.ndarray:
        return self.m @ np.asarray(d_device, dtype=float)

    def to_device(self, d_pixels: Any) -> np.ndarray:
        """The device motion that moves the star by ``d_pixels``."""
        return np.linalg.solve(self.m, np.asarray(d_pixels, dtype=float))

    @property
    def flip(self) -> bool:
        return float(np.linalg.det(self.m)) < 0

    def describe(self) -> dict[str, float | bool]:
        """Scales (pixels per unit, per device axis), the rotation of
        device axis a on the detector (degrees from +x towards +y), the
        angle between the axes and the flip."""
        m = self.m
        a, b = m[:, 0], m[:, 1]
        ang = math.degrees(math.atan2(a[1], a[0]))
        between = math.degrees(math.acos(float(np.dot(a, b) / (np.hypot(*a) * np.hypot(*b)))))
        return {
            "scale_a": float(np.hypot(*a)),
            "scale_b": float(np.hypot(*b)),
            "rotation_deg": ang,
            "axis_angle_deg": between,
            "flip": self.flip,
        }

    def sky(self) -> np.ndarray | None:
        """Arcseconds (east, north) per device unit, if the plate scale and
        PA are known."""
        if self.plate_scale is None or self.pa is None:
            return None
        return sky_matrix(self.plate_scale, self.pa, self.parity) @ self.m

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["describe"] = self.describe()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Calibration:
        d = {k: v for k, v in d.items() if k != "describe"}
        cal = cls(**d)
        m = cal.m
        if m.shape != (2, 2) or not np.all(np.isfinite(m)) or abs(np.linalg.det(m)) < 1e-12:
            raise ValueError(f"calibration {cal.device}/{cal.port}: singular or bad matrix")
        return cal


def from_sx_legacy(
    arcsec_per_step: float,
    rotoffset_deg: float,
    plate_scale: float,
    pa_deg: float = 0.0,
    parity: int = 1,
    *,
    device: str = "sxao",
    port: str = "",
) -> Calibration:
    """The legacy MINERVA AO model (``minerva_library/ao.py``: ``tip =
    -(E cos r - N sin r)/aps``, ``tilt = (E sin r + N cos r)/aps``) as a
    matrix. A seed only: the legacy unit was never commissioned and its
    sign conventions were never verified."""
    r = math.radians(rotoffset_deg)
    steps_per_sky = np.array([[-math.cos(r), math.sin(r)], [math.sin(r), math.cos(r)]])
    steps_per_sky /= arcsec_per_step
    sky_per_step = np.linalg.inv(steps_per_sky)
    pix_per_step = np.linalg.inv(sky_matrix(plate_scale, pa_deg, parity)) @ sky_per_step
    return Calibration(
        device,
        port,
        pix_per_step.tolist(),
        units="steps",
        plate_scale=plate_scale,
        pa=pa_deg,
        parity=parity,
        notes="from legacy ARCSEC_PER_STEP/ROTOFFSET (unverified)",
    )


def from_tres_legacy(
    steps_per_pixel_tip: float,
    steps_per_pixel_tilt: float,
    theta_deg: float,
    flip: bool,
    *,
    device: str = "pi",
    port: str = "",
) -> Calibration:
    """tres-guider's ``tiptilt.move_x_y`` model (``tip = s*spp_tip*(x cos t
    - y sin t)``, ``tilt = spp_tilt*(x sin t + y cos t)``, ``s = -1`` if
    FLIP) as a matrix."""
    t = math.radians(theta_deg)
    s = -1.0 if flip else 1.0
    dev_per_pix = np.array(
        [
            [s * steps_per_pixel_tip * math.cos(t), -s * steps_per_pixel_tip * math.sin(t)],
            [steps_per_pixel_tilt * math.sin(t), steps_per_pixel_tilt * math.cos(t)],
        ]
    )
    return Calibration(
        device,
        port,
        np.linalg.inv(dev_per_pix).tolist(),
        notes="from tres-guider STEPS_PER_PIXEL/THETA/FLIP",
    )


def fit_matrix(
    positions: list[tuple[float, float]], pixels: list[tuple[float, float]]
) -> tuple[np.ndarray, np.ndarray, float]:
    """Least-squares ``pixel = p0 + M @ position``: (M, p0, rms residual)."""
    p = np.asarray(positions, dtype=float)
    q = np.asarray(pixels, dtype=float)
    if p.shape[0] < 3:
        raise ValueError("need at least three positions")
    a = np.column_stack([np.ones(len(p)), p])
    coef, *_ = np.linalg.lstsq(a, q, rcond=None)
    p0 = coef[0]
    m = coef[1:].T
    resid = q - a @ coef
    rms = float(np.sqrt(np.mean(np.sum(resid**2, axis=1))))
    return m, p0, rms


async def measure(
    camera: Camera,
    centroider: Centroider,
    device: TipTilt,
    *,
    step: float,
    exposure: float,
    roi: ROI | None = None,
    frames: int = 5,
    settle: float = 0.1,
    port: str = "",
    clock: Clock | None = None,
    max_residual: float | None = None,
    gain: float | None = None,
) -> Calibration:
    """Measure ``device``'s matrix (module doc). The star must be in
    ``roi`` (or the frame) and stay there over ``+/- step``. Leaves the
    device at its centre."""
    clock = clock or RealClock()
    pattern = [(0, 0), (1, 0), (-1, 0), (0, 0), (0, 1), (0, -1), (0, 0), (1, 1), (-1, -1)]
    await device.home()
    centre = device.position
    positions: list[tuple[float, float]] = []
    pixels: list[tuple[float, float]] = []
    try:
        for ka, kb in pattern:
            want = (centre[0] + ka * step, centre[1] + kb * step)
            cur = device.position
            res = await device.move_by(want[0] - cur[0], want[1] - cur[1])
            if res.at_limit:
                raise GuiderError(f"calibration: {device.name} reached a limit at {want}")
            await clock.sleep(settle)
            xs, ys = [], []
            for _ in range(frames):
                frame = await camera.expose(exposure, roi, gain)
                m = centroider.measure(frame, None)  # the brightest star
                if not m.valid:
                    raise GuiderError(f"calibration: no star at {want}: {m.reason}")
                xs.append(m.x)
                ys.append(m.y)
            pix = (float(np.mean(xs)), float(np.mean(ys)))
            positions.append((device.position[0] - centre[0], device.position[1] - centre[1]))
            pixels.append(pix)
    finally:
        await device.home()
    m, _, rms = fit_matrix(positions, pixels)
    if abs(np.linalg.det(m)) < 1e-9:
        raise GuiderError(f"calibration: {device.name} does not move the star (matrix {m})")
    # Default bound: a tenth of the smaller axis's motion over one step.
    motion = step * min(float(np.hypot(*m[:, 0])), float(np.hypot(*m[:, 1])))
    bound = max_residual if max_residual is not None else 0.1 * motion
    if rms > bound:
        raise GuiderError(
            f"calibration: residual {rms:.3f} px > {bound:.3f} (seeing? average more frames,"
            " or use a bigger step)"
        )
    return Calibration(
        device.name,
        port,
        m.tolist(),
        units=device.units,
        residual=rms,
        measured=datetime.now(UTC).isoformat(timespec="seconds"),
        step=step,
        positions=len(positions),
        extra={"positions": positions, "pixels": pixels},
    )


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class CalibrationStore:
    """One JSON file per device and port under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, device: str, port: str) -> Path:
        name = f"{_SAFE.sub('_', device)}__{_SAFE.sub('_', port or 'default')}.json"
        return self.root / name

    def load(self, device: str, port: str) -> Calibration | None:
        p = self.path(device, port)
        if not p.exists():
            return None
        with p.open() as f:
            return Calibration.from_dict(json.load(f))

    def save(self, cal: Calibration) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(cal.device, cal.port)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".cal-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cal.as_json(), f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return p
