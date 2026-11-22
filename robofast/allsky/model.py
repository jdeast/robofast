"""A fisheye camera model: pixel <-> (altitude, azimuth).

The model (all angles in degrees in the JSON, radians inside):

1. The direction (alt, az) is a unit vector in the local east-north-up frame.
2. The lens axis is tilted from the zenith: the vector is rotated by
   ``tilt_n`` about the east axis and by ``tilt_e`` about the north axis
   (small angles: a positive ``tilt_e`` moves the axis toward the east).
3. In the lens frame, ``theta`` is the angle from the axis and ``phi`` the
   azimuth about it (east of north).
4. The image radius is a polynomial in theta (radians):
   ``r = f1*theta + f2*theta**2 + f3*theta**3`` pixels (``f1`` alone is the
   equidistant fisheye; the higher terms are its distortion).
5. The image angle, measured from "up" (-y) toward +x, is
   ``psi = rot - parity*phi``; ``parity`` +1 is the view from below (east
   to the left of north, as on the sky), -1 a mirrored image. The pixel is
   ``x = x0 + r sin(psi)``, ``y = y0 - r cos(psi)`` (x right, y down,
   0-based pixel centres).

:func:`fit` refines the parameters from matched (alt, az, x, y) by
Levenberg-Marquardt with a numerical Jacobian (numpy only), clipping
outliers; the residuals are reported in pixels.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["CameraModel", "FitResult", "fit"]

_GRID_CACHE: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}

#: The parameters :func:`fit` adjusts, in order.
FREE = ("x0", "y0", "rot", "tilt_e", "tilt_n", "f1", "f2", "f3")


@dataclass
class CameraModel:
    camera: str
    width: int
    height: int
    x0: float
    y0: float
    rot: float = 0.0  # deg
    parity: int = 1
    tilt_e: float = 0.0  # deg
    tilt_n: float = 0.0  # deg
    f1: float = 300.0  # px per radian
    f2: float = 0.0
    f3: float = 0.0
    #: Fit statistics and provenance (free-form; written to the JSON).
    meta: dict[str, Any] = field(default_factory=dict)
    #: The clear-sky calibration (:mod:`robofast.allsky.skycal`): limiting
    #: magnitude model, clear-sky noise and background, static-mask file.
    sky: dict[str, Any] = field(default_factory=dict)
    #: The directory the model was loaded from (relative files in ``sky``,
    #: the static mask, are found there); not saved.
    source_dir: str | None = field(default=None, compare=False, repr=False)

    # -- geometry -----------------------------------------------------------------

    def _tilt(self) -> np.ndarray:
        """Rotation taking ENU vectors into the lens frame."""
        a = math.radians(self.tilt_n)
        b = math.radians(self.tilt_e)
        # Rotate about east (x) by +a: axis toward north tips the up vector.
        rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
        ry = np.array([[math.cos(b), 0, -math.sin(b)], [0, 1, 0], [math.sin(b), 0, math.cos(b)]])
        # Lens axis in ENU is (ry @ rx).T @ (0,0,1); vectors map into the lens frame by ry @ rx.
        return ry @ rx

    def radius(self, theta: np.ndarray) -> np.ndarray:
        return self.f1 * theta + self.f2 * theta**2 + self.f3 * theta**3

    def theta_of_radius(self, r: np.ndarray) -> np.ndarray:
        """Invert :meth:`radius` by Newton iteration (monotonic in range)."""
        r = np.asarray(r, dtype=float)
        th = r / self.f1
        for _ in range(20):
            f = self.radius(th) - r
            df = self.f1 + 2 * self.f2 * th + 3 * self.f3 * th**2
            step = f / np.where(np.abs(df) < 1e-9, 1e-9, df)
            th = th - step
            if np.all(np.abs(step) < 1e-10):
                break
        return th

    def to_pixel(self, alt: Any, az: Any) -> tuple[np.ndarray, np.ndarray]:
        """Pixel (x, y) of apparent (alt, az) in degrees."""
        alt = np.radians(np.asarray(alt, dtype=float))
        az = np.radians(np.asarray(az, dtype=float))
        v = np.stack([np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)])
        shape = v.shape[1:]
        w = self._tilt() @ v.reshape(3, -1)
        theta = np.arccos(np.clip(w[2], -1, 1))
        phi = np.arctan2(w[0], w[1])
        r = self.radius(theta)
        psi = math.radians(self.rot) - self.parity * phi
        x = self.x0 + r * np.sin(psi)
        y = self.y0 - r * np.cos(psi)
        return x.reshape(shape), y.reshape(shape)

    def to_altaz(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """Apparent (alt, az) in degrees of pixel (x, y)."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        dx, dy = x - self.x0, -(y - self.y0)
        r = np.hypot(dx, dy)
        psi = np.arctan2(dx, dy)
        phi = (math.radians(self.rot) - psi) * self.parity
        theta = self.theta_of_radius(r)
        w = np.stack([np.sin(theta) * np.sin(phi), np.sin(theta) * np.cos(phi), np.cos(theta)])
        shape = w.shape[1:]
        v = self._tilt().T @ w.reshape(3, -1)
        alt = np.degrees(np.arcsin(np.clip(v[2], -1, 1)))
        az = np.degrees(np.arctan2(v[0], v[1])) % 360.0
        return alt.reshape(shape), az.reshape(shape)

    def altaz_grid(self, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """(alt, az) of every ``step``-th pixel, shape (height/step, width/step);
        cached per geometry."""
        key = (step, self.width, self.height, self.parity, *self.params().tolist())
        hit = _GRID_CACHE.get(key)
        if hit is None:
            yy, xx = np.mgrid[0 : self.height : step, 0 : self.width : step]
            hit = self.to_altaz(xx, yy)
            if len(_GRID_CACHE) > 8:
                _GRID_CACHE.clear()
            _GRID_CACHE[key] = hit
        return hit

    def pixel_scale(self, alt: float = 90.0) -> float:
        """Radial scale, pixels per degree, at altitude ``alt``."""
        th = math.radians(90.0 - alt)
        return (self.f1 + 2 * self.f2 * th + 3 * self.f3 * th**2) * math.pi / 180.0

    # -- I/O ----------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source_dir")
        d["model"] = "robofast.allsky.CameraModel/1"
        return d

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1, sort_keys=True) + "\n")

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CameraModel:
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "source_dir"}
        return cls(**d)

    @classmethod
    def load(cls, path: str | Path) -> CameraModel:
        m = cls.from_json(json.loads(Path(path).read_text()))
        m.source_dir = str(Path(path).resolve().parent)
        return m

    def params(self) -> np.ndarray:
        return np.array([getattr(self, k) for k in FREE], dtype=float)

    def with_params(self, p: np.ndarray) -> CameraModel:
        return replace(self, **{k: float(v) for k, v in zip(FREE, p, strict=True)})


@dataclass
class FitResult:
    model: CameraModel
    #: Residuals (pixels) of the points kept.
    dx: np.ndarray
    dy: np.ndarray
    used: np.ndarray  # bool mask over the input points

    @property
    def rms(self) -> float:
        return float(np.sqrt(np.mean(self.dx**2 + self.dy**2))) if self.dx.size else math.nan

    @property
    def median(self) -> float:
        return float(np.median(np.hypot(self.dx, self.dy))) if self.dx.size else math.nan

    @property
    def p90(self) -> float:
        return float(np.percentile(np.hypot(self.dx, self.dy), 90)) if self.dx.size else math.nan

    def summary(self) -> dict[str, float]:
        return {
            "n": int(self.used.sum()),
            "rms_px": round(self.rms, 3),
            "median_px": round(self.median, 3),
            "p90_px": round(self.p90, 3),
        }


def _residuals(model: CameraModel, p: np.ndarray, alt, az, x, y) -> np.ndarray:
    px, py = model.with_params(p).to_pixel(alt, az)
    return np.concatenate([px - x, py - y])


def fit(
    model: CameraModel,
    alt: np.ndarray,
    az: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    *,
    free: tuple[str, ...] = FREE,
    clip: float = 3.0,
    rounds: int = 4,
    tilt_prior_deg: float = 3.0,
) -> FitResult:
    """Least-squares fit of ``model`` to matched points (apparent alt/az in
    degrees, pixels). Parameters not in ``free`` are held. The tilt has a
    weak Gaussian prior (``tilt_prior_deg``) because it is nearly
    degenerate with the centre for a symmetric lens. Each round clips
    points beyond ``clip`` times the robust residual scale (never tighter
    than 1 px)."""
    alt, az, x, y = (np.asarray(a, dtype=float) for a in (alt, az, x, y))
    idx = [FREE.index(k) for k in free]
    used = np.ones(alt.size, dtype=bool)
    p = model.params()
    for _ in range(rounds):
        a, z, xx, yy = alt[used], az[used], x[used], y[used]
        if a.size < len(idx) + 2:
            break
        lam = 1e-3
        for _it in range(50):
            r = _residuals(model, p, a, z, xx, yy)
            prior = np.array([p[FREE.index("tilt_e")], p[FREE.index("tilt_n")]]) / tilt_prior_deg
            cost = r @ r + prior @ prior
            jac = np.empty((r.size, len(idx)))
            for j, k in enumerate(idx):
                h = 1e-4 * max(1.0, abs(p[k])) if FREE[k] not in ("f2", "f3") else 1e-2
                q = p.copy()
                q[k] += h
                jac[:, j] = (_residuals(model, q, a, z, xx, yy) - r) / h
            jtj = jac.T @ jac
            g = jac.T @ r
            for j, k in enumerate(idx):
                if FREE[k] in ("tilt_e", "tilt_n"):
                    jtj[j, j] += 1 / tilt_prior_deg**2
                    g[j] += p[k] / tilt_prior_deg**2
            while True:
                step = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-9), -g)
                q = p.copy()
                q[idx] += step
                r2 = _residuals(model, q, a, z, xx, yy)
                prior2 = np.array([q[FREE.index("tilt_e")], q[FREE.index("tilt_n")]]) / (
                    tilt_prior_deg
                )
                c2 = r2 @ r2 + prior2 @ prior2
                if c2 < cost or lam > 1e8:
                    break
                lam *= 10
            if c2 >= cost:
                break
            p = q
            lam = max(lam / 10, 1e-9)
            if cost - c2 < 1e-8 * cost:
                break
        px, py = model.with_params(p).to_pixel(alt, az)
        d = np.hypot(px - x, py - y)
        scale = np.median(d[used]) / 1.1774  # per-axis sigma from a 2-D (Rayleigh) median
        keep = d < max(clip * scale, 1.0)
        if np.array_equal(keep, used):
            break
        used = keep
    m = model.with_params(p)
    px, py = m.to_pixel(alt, az)
    res = FitResult(m, (px - x)[used], (py - y)[used], used)
    m.meta = dict(m.meta, fit=res.summary())
    return res
