"""Self-correcting mount calibration (docs/guider.md, "Self-correcting mount
calibration").

Fine acquisition drives the star onto the fiber pixel through the mount's
calibration ``M`` (:mod:`robofast.guider.calibration`: ``d_pixels = M @
d_mount``). When ``M`` is rotated from the truth (almost always a poorly
known rotator angle: the PA), a north correction bleeds into east and the
star spirals in slowly, walks away, or never gets under the capture radius
(legacy MINERVA, Dec 2025 - Apr 2026: 57% of spectra hit the 750 s
acquisition timeout).

Every mount move the acquisition makes is a measurement: the move ``u``
actually sent (arcsec east, north) and the star's displacement ``d``
(pixels) between the frame before and the frame after it. With ``p = M @
u`` the predicted displacement, :func:`estimate` fits ``d = Z p`` with
``Z`` a rotation by ``theta`` and a scale ``s`` (the complex least squares
``z = sum(conj(p) d) / sum(|p|^2)``), and also the mirrored model ``d = W
conj(p)``: with two or more moves in clearly different directions, a mirror
that fits much better than any rotation is a parity flip.

Pairs are kept only when well measured (:meth:`SelfCalConfig`): the
predicted move at least ``min_move`` pixels (centroid noise and seeing are
~1 px, so a 15 px move fixes theta to a few degrees and a 100 px one to
~0.5 deg), the star found in both frames and not saturated, the move not
clipped, and ``|d| / |p|`` within ``max_scale_error`` of 1 (else it was
probably another star).

:class:`SelfCal` keeps one port's state:

* during an acquisition (:meth:`SelfCal.begin`, :meth:`SelfCal.add`): the
  estimate from this acquisition's own moves; :meth:`SelfCal.corrected`
  gives ``Z @ M`` once ``|theta| >= correct_deg``, for the rest of this
  acquisition (the guider records the ``mount_cal_corrected`` event);
  :meth:`SelfCal.parity_flipped` says when the moves are mirrored (the
  guider alarms ``mount_cal_parity`` and fails: it never fixes a parity);
* after it (:meth:`SelfCal.finish`): a converged acquisition's estimate is
  kept; ``persist_count`` in a row above ``correct_deg`` that agree within
  ``persist_tolerance_deg`` (and 5% in scale) give the corrected matrix to
  save, with provenance (``extra.source = "online"``). One outlier never
  rewrites the stored calibration;
* :attr:`SelfCal.suspect`: why the stored calibration is suspect (None:
  not suspect) -- an estimate above ``correct_deg`` not (yet) persisted, an
  unstable estimate, a parity flip, or ``not_converging``. Cleared when an
  acquisition's moves agree with the stored matrix, when one is persisted,
  or when the mount is calibrated again (:meth:`SelfCal.reset`).

The rotator: when the stored matrix came from a PA (``Calibration.pa``), a
consistent ``theta`` is a PA error: the guider's PA is off by ``-parity *
theta`` and the FITS / PA-calibration PA (robofast.rotcal, ``pa = offset +
sense * mechanical``) by ``-theta`` -- fix the PA calibration's offset
rather than only the matrix (:func:`pa_advice`).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast.guider.calibration import Calibration


@dataclass
class SelfCalConfig:
    """The guide config's ``mount_selfcal`` entry (``false``: off)."""

    #: Smallest predicted move (pixels) that counts as a measurement.
    min_move: float = 15.0
    #: |d| / |p| must lie within [1/(1+e), 1+e] (else: another star?).
    max_scale_error: float = 1.0
    #: Correct the matrix during an acquisition from |theta| (deg) on.
    correct_deg: float = 5.0
    #: Alarm ``mount_cal_suspect`` ("run the full calibration") from here.
    alarm_deg: float = 20.0
    #: Persist after this many converged acquisitions in a row agree...
    persist_count: int = 3
    #: ... within this spread of theta (deg).
    persist_tolerance_deg: float = 3.0
    #: Parity: two or more pairs whose predicted directions span at least
    #: this angle (deg), and the rotation fit's rms worse than ``parity_ratio``
    #: times the mirror's and than ``parity_floor`` of the mean move.
    parity_span_deg: float = 30.0
    parity_ratio: float = 3.0
    parity_floor: float = 0.15
    #: A centroid peak plus background at or above this (ADU) is saturated.
    saturation: float = 60000.0

    @classmethod
    def from_any(cls, v: Any) -> SelfCalConfig | None:
        if v is False or v is None:
            return None
        if v is True:
            return cls()
        if isinstance(v, SelfCalConfig):
            return v
        if not isinstance(v, dict):
            raise ValueError("mount_selfcal: true, false or a mapping")
        known = {f.name for f in fields(cls)}
        unknown = set(v) - known
        if unknown:
            raise ValueError(f"mount_selfcal: unknown keys {sorted(unknown)}")
        c = cls(**v)
        if not (0 < c.correct_deg <= c.alarm_deg) or c.persist_count < 2 or c.min_move <= 0:
            raise ValueError("mount_selfcal: 0 < correct_deg <= alarm_deg, persist_count >= 2")
        return c


@dataclass
class Pair:
    """One mount move: ``u`` sent (device units), ``d`` observed (pixels)."""

    u: tuple[float, float]
    d: tuple[float, float]


@dataclass
class Estimate:
    #: Rotation (deg, from +x towards +y on the detector) and scale of the
    #: truth relative to the stored matrix: d = s R(theta) M u.
    theta: float
    scale: float
    n: int
    #: Rms residual (pixels) of the rotation fit and of the mirrored fit.
    rms: float
    rms_mirror: float
    #: The predicted directions span this much (deg; 0 for one pair).
    span: float
    mean_move: float

    @property
    def z(self) -> np.ndarray:
        """The 2x2 correction ``Z`` (the truth is ``Z @ M``)."""
        t = math.radians(self.theta)
        c, s = self.scale * math.cos(t), self.scale * math.sin(t)
        return np.array([[c, -s], [s, c]])

    def as_json(self) -> dict[str, Any]:
        return {
            "theta_deg": round(self.theta, 3),
            "scale": round(self.scale, 4),
            "pairs": self.n,
            "rms_px": round(self.rms, 3),
            "rms_mirror_px": round(self.rms_mirror, 3),
            "span_deg": round(self.span, 1),
        }


def estimate(m: np.ndarray, pairs: list[Pair]) -> Estimate | None:
    """The rotation and scale that best map ``m @ u`` onto ``d`` (module doc)."""
    if not pairs:
        return None
    p = np.array([complex(*(m @ np.asarray(q.u, float))) for q in pairs])
    d = np.array([complex(*q.d) for q in pairs])
    norm = float(np.sum(np.abs(p) ** 2))
    if norm <= 0:
        return None
    z = complex(np.sum(np.conj(p) * d) / norm)
    w = complex(np.sum(p * d) / norm)
    rms = float(np.sqrt(np.mean(np.abs(d - z * p) ** 2)))
    rms_m = float(np.sqrt(np.mean(np.abs(d - w * np.conj(p)) ** 2)))
    ang = np.angle(p)
    span = 0.0
    for i in range(len(ang)):
        for j in range(i + 1, len(ang)):
            # Directions, not senses: a move and its reverse span nothing.
            a = abs(math.degrees(ang[i] - ang[j])) % 180.0
            span = max(span, min(a, 180.0 - a))
    return Estimate(
        math.degrees(math.atan2(z.imag, z.real)),
        abs(z),
        len(pairs),
        rms,
        rms_m,
        span,
        float(np.mean(np.abs(p))),
    )


def pa_advice(cal: Calibration, theta: float) -> dict[str, Any] | None:
    """What ``theta`` means for the PA, when the matrix came from one."""
    if cal.pa is None:
        return None
    parity = -1 if cal.parity < 0 else 1
    d_guider = -parity * theta
    return {
        "pa_stored": round(float(cal.pa), 3),
        "pa_measured": round((float(cal.pa) + d_guider) % 360.0, 3),
        "pa_change_deg": round(d_guider, 3),
        # The FITS / PA-calibration convention (robofast.rotcal): its
        # offset (pa = offset + sense * mechanical) is off by this much.
        "rotcal_offset_change_deg": round(-theta, 3),
    }


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


@dataclass
class SelfCal:
    """One port's online estimate (module doc)."""

    config: SelfCalConfig = field(default_factory=SelfCalConfig)
    pairs: list[Pair] = field(default_factory=list)
    rejected: int = 0
    #: This acquisition's estimate, and whether it corrected the matrix.
    current: Estimate | None = None
    applied: bool = False
    #: Converged acquisitions' estimates above ``correct_deg``, newest last.
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=10))
    suspect: str | None = None
    suspect_since: str | None = None
    persisted: int = 0
    last_persisted: dict[str, Any] | None = None

    # -- one acquisition ------------------------------------------------------

    def begin(self) -> None:
        self.pairs = []
        self.rejected = 0
        self.current = None
        self.applied = False

    def add(
        self,
        m: np.ndarray,
        u: Any,
        d: Any,
        *,
        clipped: bool = False,
        saturated: bool = False,
    ) -> str | None:
        """Offer one move; returns why it was rejected (None: kept)."""
        c = self.config
        u = np.asarray(u, float)
        d = np.asarray(d, float)
        p = m @ u
        why = None
        lp, ld = float(np.hypot(*p)), float(np.hypot(*d))
        if clipped:
            why = "move clipped"
        elif saturated:
            why = "star saturated"
        elif not (np.all(np.isfinite(u)) and np.all(np.isfinite(d))):
            why = "no measurement"
        elif lp < c.min_move:
            why = f"move {lp:.1f} px < {c.min_move:g}"
        elif not (1.0 / (1.0 + c.max_scale_error) <= ld / lp <= 1.0 + c.max_scale_error):
            why = f"star moved {ld:.1f} px for {lp:.1f} predicted (another star?)"
        if why is not None:
            self.rejected += 1
            return why
        self.pairs.append(Pair((float(u[0]), float(u[1])), (float(d[0]), float(d[1]))))
        self.current = estimate(m, self.pairs)
        return None

    def parity_flipped(self) -> bool:
        """The moves are unambiguously mirrored (module doc)."""
        e, c = self.current, self.config
        if e is None or e.n < 2 or e.span < c.parity_span_deg:
            return False
        return e.rms > c.parity_ratio * e.rms_mirror and e.rms > c.parity_floor * e.mean_move

    def corrected(self, base: Calibration) -> Calibration | None:
        """``Z @ M`` for the rest of this acquisition, once |theta| is
        at least ``correct_deg`` (None: keep the stored matrix)."""
        e = self.current
        if e is None or abs(e.theta) < self.config.correct_deg:
            return None
        return _with_matrix(base, e.z @ base.m, notes="corrected during this acquisition")

    # -- across acquisitions --------------------------------------------------

    def finish(self, base: Calibration, converged: bool, now: str | None = None) -> dict[str, Any]:
        """Close one acquisition. Returns what happened: ``persist`` (a
        :class:`Calibration` to save), ``alarm`` (text) and ``suspect``."""
        c = self.config
        e = self.current
        out: dict[str, Any] = {}
        now = now or datetime.now(UTC).isoformat(timespec="seconds")
        if not converged or e is None:
            return out
        if abs(e.theta) < c.correct_deg:
            # The stored matrix is right (within the threshold).
            self.history.clear()
            self._clear()
            return out
        self.history.append({**e.as_json(), "time": now})
        last = list(self.history)[-c.persist_count :]
        if len(last) >= c.persist_count:
            thetas = [h["theta_deg"] for h in last]
            ref = thetas[-1]
            spread = max(abs(_wrap(t - ref)) for t in thetas)
            scales = [h["scale"] for h in last]
            mean_s = float(np.mean(scales))
            if spread <= c.persist_tolerance_deg and max(scales) - min(scales) <= 0.05 * mean_s:
                theta = ref + float(np.mean([_wrap(t - ref) for t in thetas]))
                t = math.radians(theta)
                z = mean_s * np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
                prov = {
                    "source": "online",
                    "theta_deg": round(theta, 3),
                    "scale": round(mean_s, 4),
                    "pairs": int(sum(h["pairs"] for h in last)),
                    "acquisitions": len(last),
                    "estimates": last,
                    "time": now,
                    "previous_matrix": base.matrix,
                    "previous_measured": base.measured,
                }
                advice = pa_advice(base, theta)
                if advice is not None:
                    prov["pa"] = advice
                new = _with_matrix(
                    base,
                    z @ base.m,
                    notes=(
                        f"online correction: rotated {theta:+.2f} deg, scale {mean_s:.4f},"
                        f" from {len(last)} acquisitions"
                    ),
                )
                new.measured = now
                new.extra = {**{k: v for k, v in base.extra.items() if k != "online"}}
                new.extra["online"] = prov
                if advice is not None:
                    # The matrix no longer follows the stored PA.
                    new.pa = advice["pa_measured"]
                self.history.clear()
                self._clear()
                self.persisted += 1
                self.last_persisted = prov
                out["persist"] = new
                if abs(theta) >= c.alarm_deg:
                    out["alarm"] = (
                        f"stored mount calibration was off by {theta:+.1f} deg (corrected"
                        f" online from {len(last)} acquisitions): run the full calibration"
                    )
                return out
            seen = ", ".join(f"{t:+.1f}" for t in thetas)
            out["alarm"] = (
                f"mount calibration estimate unstable: theta {seen} deg over the last"
                " acquisitions: run the full calibration"
            )
        n_ok = len(self.history)
        self.mark(
            f"stored mount calibration off by {e.theta:+.1f} deg (online estimate,"
            f" {e.n} moves); not persisted ({min(n_ok, c.persist_count)} of"
            f" {c.persist_count} agreeing acquisitions)",
            now,
        )
        out["suspect"] = self.suspect
        return out

    def mark(self, why: str, now: str | None = None) -> None:
        if self.suspect is None:
            self.suspect_since = now or datetime.now(UTC).isoformat(timespec="seconds")
        self.suspect = why

    def _clear(self) -> None:
        self.suspect = None
        self.suspect_since = None

    def reset(self) -> None:
        """The mount was calibrated again: forget everything."""
        self.begin()
        self.history.clear()
        self._clear()

    def status(self) -> dict[str, Any]:
        return {
            "suspect": self.suspect is not None,
            "suspect_reason": self.suspect,
            "suspect_since": self.suspect_since,
            "estimate": None if self.current is None else self.current.as_json(),
            "applied": self.applied,
            "rejected": self.rejected,
            "history": list(self.history),
            "persisted": self.persisted,
            "last_persisted": None
            if self.last_persisted is None
            else {k: v for k, v in self.last_persisted.items() if k != "estimates"},
        }


def _with_matrix(base: Calibration, m: np.ndarray, *, notes: str) -> Calibration:
    return Calibration(
        base.device,
        base.port,
        np.asarray(m, float).tolist(),
        units=base.units,
        residual=base.residual,
        measured=base.measured,
        step=base.step,
        positions=base.positions,
        plate_scale=base.plate_scale,
        pa=base.pa,
        parity=base.parity,
        notes=notes,
        extra=dict(base.extra),
    )
