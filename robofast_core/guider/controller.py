"""The guide loop's controller: a two-axis PI(D) with persistent state.

The legacy guiders (MINERVA ``control.fauguide`` and tres-guider ``tres.py``)
called ``PID.setPoint()`` every frame, and ``setPoint`` zeroed the
integrator and the derivative memory, so they ran as proportional-only
loops. Here:

* **Changing the setpoint never resets state.** :meth:`Controller.set_target`
  only moves the target; the integrator and the derivative memory are kept,
  and the derivative acts on the *measurement* (not the error), so a
  setpoint step gives no derivative kick. Only :meth:`reset` clears state
  (start of guiding, re-acquisition).
* **Output is an incremental correction** in pixels: how far to move the
  image this frame. A tip/tilt mirror (or the mount) holds its position, so
  the actuator itself integrates the corrections: ``kp`` is the classic
  "integrator gain" of an adaptive-optics tip/tilt loop (0 < kp < 1; ~0.3-0.6
  is typical), ``ki`` adds a second integration that removes a steady drift
  (a tracking ramp) with no lag, ``kd`` damps.
* **Deadband**: errors smaller than ``deadband`` pixels are treated as zero
  and larger ones are reduced by it (continuous, as the legacy code did),
  so the loop does not chase centroid noise.
* **Integrator limit and anti-windup**: the integrator is clamped to
  ``i_max`` pixels, and it does not integrate further in a direction in
  which the last output was clipped (by the step or rate limit, or by the
  actuator reporting a range limit: :meth:`saturated`).
* **Step and rate limits**: the correction's length is at most ``max_step``
  pixels per frame and ``max_rate`` pixels per second.

Units: everything is pixels on the guide camera, per frame; the conversion
to device units is the calibration's (:mod:`robofast_core.guider.calibration`).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields

import numpy as np


@dataclass
class Gains:
    kp: float = 0.5
    ki: float = 0.0
    kd: float = 0.0
    #: Errors below this (pixels) are ignored.
    deadband: float = 0.0
    #: Integrator clamp (pixels, per axis).
    i_max: float = 5.0
    #: Correction length limits: pixels per frame and pixels per second
    #: (inf: none).
    max_step: float = math.inf
    max_rate: float = math.inf

    @classmethod
    def from_dict(cls, d: dict) -> Gains:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown gains {sorted(unknown)}")
        g = cls(**{k: float(v) for k, v in d.items()})
        g.validate()
        return g

    def validate(self) -> None:
        for name in ("kp", "ki", "kd", "deadband", "i_max", "max_step", "max_rate"):
            v = getattr(self, name)
            if not (v >= 0) or (math.isinf(v) and name not in ("max_step", "max_rate", "i_max")):
                raise ValueError(f"gain {name} must be a finite number >= 0, not {v}")
        if self.kp > 2.0:
            raise ValueError(f"kp {self.kp} > 2 is unstable for an integrating actuator")

    def as_json(self) -> dict:
        return {k: (None if math.isinf(v) else v) for k, v in asdict(self).items()}


@dataclass
class Controller:
    """Two-axis PI(D) (module doc). Feed :meth:`update` the measured star
    position each frame; it returns the image motion to apply (pixels)."""

    gains: Gains = field(default_factory=Gains)
    target: np.ndarray = field(default_factory=lambda: np.zeros(2))
    integrator: np.ndarray = field(default_factory=lambda: np.zeros(2), init=False)
    last_measurement: np.ndarray | None = field(default=None, init=False)
    last_error: np.ndarray = field(default_factory=lambda: np.zeros(2), init=False)
    last_output: np.ndarray = field(default_factory=lambda: np.zeros(2), init=False)
    #: Per axis: -1/+1 if the last output was clipped in that direction.
    _clipped: np.ndarray = field(default_factory=lambda: np.zeros(2), init=False)

    def __post_init__(self) -> None:
        self.target = np.asarray(self.target, dtype=float).copy()

    def reset(self) -> None:
        """Clear the integrator and derivative memory (new acquisition)."""
        self.integrator = np.zeros(2)
        self.last_measurement = None
        self.last_error = np.zeros(2)
        self.last_output = np.zeros(2)
        self._clipped = np.zeros(2)

    def set_target(self, x: float, y: float) -> None:
        """Move the setpoint. Keeps every bit of state (module doc)."""
        self.target = np.array([float(x), float(y)])

    def set_gains(self, gains: Gains) -> None:
        gains.validate()
        # Keep the integrator's *contribution* continuous when ki changes.
        if self.gains.ki > 0 and gains.ki > 0:
            self.integrator = self.integrator * (self.gains.ki / gains.ki)
        self.gains = gains
        self.integrator = np.clip(self.integrator, -gains.i_max, gains.i_max)

    def saturated(self, axis_signs: tuple[float, float]) -> None:
        """The actuator could not apply the last output on these axes (sign
        of the clipped part per axis; 0: fine): stop integrating that way."""
        self._clipped = np.sign(np.asarray(axis_signs, dtype=float))

    def update(self, measured: tuple[float, float] | np.ndarray, dt: float) -> np.ndarray:
        g = self.gains
        m = np.asarray(measured, dtype=float)
        err = self.target - m
        # Deadband, continuous.
        mag = np.abs(err)
        err = np.where(mag < g.deadband, 0.0, err - np.sign(err) * g.deadband)
        # Integrate, except further into a clipped direction (anti-windup).
        grow = np.sign(err)
        allow = ~((self._clipped != 0) & (grow == self._clipped))
        self.integrator = np.where(allow, self.integrator + err, self.integrator)
        self.integrator = np.clip(self.integrator, -g.i_max, g.i_max)
        # Derivative on the measurement: no kick when the target moves.
        if self.last_measurement is None or g.kd == 0:
            deriv = np.zeros(2)
        else:
            deriv = -(m - self.last_measurement)
        out = g.kp * err + g.ki * self.integrator + g.kd * deriv
        # Step and rate limits on the vector's length.
        limit = g.max_step
        if dt > 0 and math.isfinite(g.max_rate):
            limit = min(limit, g.max_rate * dt)
        norm = float(np.hypot(*out))
        clipped = np.zeros(2)
        if norm > limit:
            scaled = out * (limit / norm)
            clipped = np.sign(out - scaled)
            out = scaled
        self._clipped = clipped
        self.last_measurement = m
        self.last_error = err
        self.last_output = out
        return out

    def state(self) -> dict:
        return {
            "target": [float(v) for v in self.target],
            "integrator": [float(v) for v in self.integrator],
            "last_error": [float(v) for v in self.last_error],
            "last_output": [float(v) for v in self.last_output],
            "gains": self.gains.as_json(),
        }
