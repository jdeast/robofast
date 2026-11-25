"""Self-correcting mount calibration (docs/guider.md, "Self-correcting mount
calibration").

Fine acquisition drives the star onto the fiber pixel through the mount's
calibration ``M`` (:mod:`robofast.guider.calibration`: ``d_pixels = M @
d_mount``). When ``M`` is rotated from the truth, a north correction bleeds
into east and the star spirals in slowly, walks away, or never gets under
the capture radius (legacy MINERVA, Dec 2025 - Apr 2026: 57% of spectra hit
the 750 s acquisition timeout). On MINERVA a rotated matrix almost always
means the **rotator lost its position** (it drops out, or PWI is killed);
its physical angle changes only on site visits, and homing it puts it
right -- but the mechanism survives only ~100 homes, so homes must be rare.

So the mount matrix in use is two parts:

* the **base** matrix (:class:`~robofast.guider.calibration.Calibration`,
  from a full calibration): the truth for a homed rotator. Only a *scale*
  correction is ever saved into it;
* the **rotator offset** (:class:`RotatorOffset`, per device and port, in
  :class:`RotatorOffsetStore`): the detector rotation ``theta`` since the
  last rotator home, applied on top, ``R(theta) @ base``. It survives
  restarts and nights (a lost position stays lost until a home) and is
  cleared by a rotator home or a full calibration.

Every mount move the acquisition makes is a measurement: the move ``u``
actually sent (arcsec east, north) and the star's displacement ``d``
(pixels) between the frame before and the frame after it. With ``p = M @
u`` the predicted displacement (``M`` the matrix in use), :func:`estimate`
fits ``d = Z p`` with ``Z`` a rotation by ``theta`` and a scale ``s`` (the
complex least squares ``z = sum(conj(p) d) / sum(|p|^2)``), and also the
mirrored model ``d = W conj(p)``: with two or more moves in clearly
different directions, a mirror that fits much better than any rotation is a
parity flip.

Pairs are kept only when well measured (:class:`SelfCalConfig`): the
predicted move at least ``min_move`` pixels, the star found in both frames
and not saturated, the move not clipped, and ``|d| / |p|`` within
``max_scale_error`` of 1 (else it was probably another star).

:class:`SelfCal`:

* during an acquisition (:meth:`SelfCal.begin`, :meth:`SelfCal.add`):
  :meth:`SelfCal.corrected` gives ``Z @ M`` once ``|theta| >=
  correct_deg`` or ``|s - 1| >= scale_tolerance``, for the rest of this
  acquisition (any angle, a 180 deg sign error included);
  :meth:`SelfCal.parity_flipped` says when the moves are mirrored (never
  corrected);
* after it (:meth:`SelfCal.finish`): ``persist_count`` converged
  acquisitions in a row that need a correction and agree (theta within
  ``persist_tolerance_deg``, scale within 5%) -- ``persist_after_loss``
  after a reported rotator drop-out (:meth:`SelfCal.rotator_lost`) --
  update the rotator offset (never to more than ``max_save_deg``: that is
  alarmed instead) and, for a scale error, the base matrix. One outlier
  never saves anything;
* :attr:`SelfCal.suspect`: why the mount calibration should be measured
  again or the rotator homed (None: no reason). A rotator offset of
  ``alarm_deg`` or more keeps it set, even saved, until a rotator home or a
  full calibration.

What ``theta`` means (:func:`rotator_advice`): the image PA (FITS / PA
calibration convention, robofast.rotcal: ``pa = offset + sense *
mechanical``) is off by ``-theta``, so the rotator's reported mechanical
angle is off by ``-sense * theta`` -- a lost position. Only an offset that
comes back after a rotator home points at the PA calibration's ``offset``.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast.guider.calibration import Calibration

HOME_ADVICE = "home the rotator when convenient (the rotator survives ~100 homes; use sparingly)"


@dataclass
class SelfCalConfig:
    """The guide config's ``mount_selfcal`` entry (``false``: off)."""

    #: Smallest predicted move (pixels) that counts as a measurement.
    min_move: float = 15.0
    #: |d| / |p| must lie within [1/(1+e), 1+e] (else: another star?).
    max_scale_error: float = 1.0
    #: Correct the rotation during an acquisition from |theta| (deg) on.
    correct_deg: float = 5.0
    #: Correct the scale from |s - 1| on (legacy MINERVA: 0.81).
    scale_tolerance: float = 0.05
    #: A rotator offset from here alarms ``rotator_position_lost``.
    alarm_deg: float = 20.0
    #: Never save a rotator offset beyond this (deg): alarm instead.
    max_save_deg: float = 90.0
    #: Save after this many converged acquisitions in a row agree...
    persist_count: int = 3
    #: ... or this many after a reported rotator drop-out / PWI restart...
    persist_after_loss: int = 2
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
        if (
            not (0 < c.correct_deg <= c.alarm_deg <= c.max_save_deg)
            or c.persist_count < 2
            or c.persist_after_loss < 1
            or c.min_move <= 0
            or c.scale_tolerance <= 0
        ):
            raise ValueError(
                "mount_selfcal: 0 < correct_deg <= alarm_deg <= max_save_deg,"
                " persist_count >= 2, persist_after_loss >= 1, positive min_move"
                " and scale_tolerance"
            )
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


def rotation(theta_deg: float, scale: float = 1.0) -> np.ndarray:
    """``s R(theta)`` (theta from +x towards +y on the detector)."""
    t = math.radians(theta_deg)
    c, s = scale * math.cos(t), scale * math.sin(t)
    return np.array([[c, -s], [s, c]])


def rotator_advice(theta: float, sense: int | None = None) -> dict[str, Any]:
    """What a detector rotation ``theta`` (deg) means (module doc): the
    image PA (FITS convention) is off by ``-theta``; with the rotation
    ``sense`` known, the rotator's reported mechanical angle by ``-sense *
    theta`` (a lost position)."""
    out: dict[str, Any] = {"pa_error_deg": round(-theta, 3)}
    if sense in (1, -1):
        out["rotator_angle_error_deg"] = round(-sense * theta, 3)
    return out


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RotatorOffset:
    """The rotator offset since the last home (module doc): the matrix in
    use is ``rotation(theta) @ base``."""

    device: str
    port: str
    #: Detector rotation (deg) on top of the base matrix.
    theta: float = 0.0
    #: When it was first measured since the last home, last updated, from
    #: how many moves and acquisitions (the last update), and the event that
    #: probably lost the position ("" if none was seen).
    since: str = ""
    updated: str = ""
    pairs: int = 0
    acquisitions: int = 0
    cause: str = ""
    #: Rotator homes reported (POST /rotator {"event": "homed"}), the last,
    #: and the offset just before it (to tell a lost position, which a home
    #: clears, from a wrong PA calibration, which comes back).
    homes: int = 0
    last_home: str = ""
    theta_before_home: float | None = None
    #: Past updates, newest last.
    history: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, base: Calibration) -> Calibration:
        if self.theta == 0.0:
            return base
        return _with_matrix(
            base,
            rotation(self.theta) @ base.m,
            notes=f"{base.notes} + rotator offset {self.theta:+.2f} deg since {self.since}".strip(),
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RotatorOffset:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class RotatorOffsetStore:
    """One JSON file per device and port, beside the calibrations:
    ``<device>__<port>.rotator.json`` (atomic writes)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, device: str, port: str) -> Path:
        name = f"{_SAFE.sub('_', device)}__{_SAFE.sub('_', port or 'default')}.rotator.json"
        return self.root / name

    def load(self, device: str, port: str) -> RotatorOffset | None:
        p = self.path(device, port)
        if not p.exists():
            return None
        with p.open() as f:
            return RotatorOffset.from_dict(json.load(f))

    def save(self, off: RotatorOffset) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(off.device, off.port)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".rot-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(off.as_json(), f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return p


@dataclass
class SelfCal:
    """One port's online estimate (module doc)."""

    config: SelfCalConfig = field(default_factory=SelfCalConfig)
    pairs: list[Pair] = field(default_factory=list)
    rejected: int = 0
    #: This acquisition's estimate (relative to the matrix in use), and
    #: whether it corrected the matrix.
    current: Estimate | None = None
    applied: bool = False
    #: Converged acquisitions' estimates that needed a correction, newest last.
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=10))
    suspect: str | None = None
    suspect_since: str | None = None
    #: A reported rotator drop-out / PWI restart not yet re-measured.
    lost: str | None = None
    saved: int = 0

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
        """Offer one move (``m`` the matrix in use); returns why it was
        rejected (None: kept)."""
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

    def _off(self, e: Estimate) -> tuple[bool, bool]:
        c = self.config
        return abs(e.theta) >= c.correct_deg, abs(e.scale - 1.0) >= c.scale_tolerance

    def corrected(self, inuse: Calibration) -> Calibration | None:
        """``Z @ M`` for the rest of this acquisition (None: keep ``M``)."""
        e = self.current
        if e is None or not any(self._off(e)):
            return None
        return _with_matrix(inuse, e.z @ inuse.m, notes="corrected during this acquisition")

    # -- across acquisitions --------------------------------------------------

    def finish(
        self,
        base: Calibration,
        offset: RotatorOffset,
        converged: bool,
        *,
        sense: int | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Close one acquisition. Returns what to do: ``offset`` (a new
        :class:`RotatorOffset` to save), ``base`` (a scale-corrected base
        :class:`Calibration` to save), ``events`` and ``alarms`` (lists of
        (kind, text, fields))."""
        c = self.config
        e = self.current
        now = now or _now()
        out: dict[str, Any] = {"events": [], "alarms": []}
        if not converged or e is None:
            return out
        rot_off, scale_off = self._off(e)
        if not (rot_off or scale_off):
            # The matrix in use is right (within the thresholds).
            self.history.clear()
            self.lost = None
            self._settle(offset)
            return out
        self.history.append({**e.as_json(), "time": now})
        need = c.persist_after_loss if self.lost else c.persist_count
        last = list(self.history)[-need:]
        if len(last) < need:
            self.mark(
                f"mount calibration in use off by {e.theta:+.1f} deg, scale {e.scale:.3f}"
                f" ({e.n} moves); not saved yet ({len(last)} of {need} agreeing acquisitions)",
                now,
            )
            return out
        thetas = [h["theta_deg"] for h in last]
        ref = thetas[-1]
        spread = max(abs(_wrap(t - ref)) for t in thetas)
        scales = [h["scale"] for h in last]
        mean_s = float(np.mean(scales))
        if spread > c.persist_tolerance_deg or max(scales) - min(scales) > 0.05 * mean_s:
            seen = ", ".join(f"{t:+.1f}" for t in thetas)
            text = (
                f"rotator position probably lost (theta {seen} deg over the last acquisitions,"
                f" unstable; since {self.lost or offset.cause or 'unknown'}): {HOME_ADVICE}"
            )
            out["alarms"].append(("rotator_position_lost", text, {"thetas_deg": thetas}))
            self.mark(text, now)
            return out
        theta = ref + float(np.mean([_wrap(t - ref) for t in thetas]))
        pairs = int(sum(h["pairs"] for h in last))
        cause = self.lost or offset.cause
        self.history.clear()
        self.lost = None
        new_off: RotatorOffset | None = None
        too_far = False
        if abs(theta) >= c.correct_deg:
            total = _wrap(offset.theta + theta)
            if abs(total) > c.max_save_deg:
                too_far = True
                text = (
                    f"mount calibration rotated {total:+.1f} deg from the base matrix"
                    f" ({len(last)} acquisitions agree): over {c.max_save_deg:g} deg, not saved"
                    " automatically (a sign or port error?): run the full calibration"
                )
                out["alarms"].append(("mount_cal_suspect", text, {"theta_deg": round(total, 3)}))
                self.mark(text, now)
            else:
                new_off = RotatorOffset(**offset.as_json())
                new_off.theta = round(total, 4)
                new_off.since = offset.since if offset.theta != 0.0 else now
                new_off.updated = now
                new_off.pairs = pairs
                new_off.acquisitions = len(last)
                new_off.cause = cause or ""
                prov = {
                    "time": now,
                    "theta_deg": round(total, 3),
                    "change_deg": round(theta, 3),
                    "pairs": pairs,
                    "acquisitions": len(last),
                    "cause": cause or "",
                    "estimates": last,
                }
                new_off.history = [*offset.history, prov][-20:]
                fields_ = {
                    "theta_deg": round(total, 3),
                    "change_deg": round(theta, 3),
                    "pairs": pairs,
                    "acquisitions": len(last),
                    "cause": cause or "unknown",
                    **rotator_advice(total, sense),
                }
                text = (
                    f"rotator offset {total:+.2f} deg since the last rotator home"
                    f" ({theta:+.2f} deg from {len(last)} agreeing acquisitions, {pairs} moves;"
                    f" since {cause or 'unknown'})"
                )
                back = offset.theta_before_home
                if (
                    offset.theta == 0.0
                    and back is not None
                    and abs(_wrap(total - back)) <= c.persist_tolerance_deg
                ):
                    # The same offset survived a rotator home: not a lost
                    # position but the PA calibration (robofast.rotcal).
                    fields_["pa_calibration_offset_change_deg"] = round(-total, 3)
                    text += (
                        f"; it survived a rotator home: the PA calibration's offset looks"
                        f" {-total:+.1f} deg off -- fix the PA calibration"
                    )
                out["events"].append(("rotator_offset_saved", text, fields_))
                out["offset"] = new_off
                if abs(total) >= c.alarm_deg:
                    text = (
                        f"rotator position probably lost (theta = {total:+.1f} deg, since"
                        f" {cause or 'unknown'}): {HOME_ADVICE}"
                    )
                    out["alarms"].append(("rotator_position_lost", text, fields_))
        if abs(mean_s - 1.0) >= c.scale_tolerance:
            prov = {
                "source": "online",
                "scale": round(mean_s, 4),
                "pairs": pairs,
                "acquisitions": len(last),
                "estimates": last,
                "time": now,
                "previous_matrix": base.matrix,
                "previous_measured": base.measured,
            }
            new = _with_matrix(
                base,
                mean_s * base.m,
                notes=f"online scale correction x{mean_s:.4f} from {len(last)} acquisitions",
            )
            new.measured = now
            new.extra["online"] = prov
            out["base"] = new
            out["events"].append(
                (
                    "mount_scale_saved",
                    f"mount calibration scale x{mean_s:.4f} saved to the base matrix"
                    f" ({len(last)} agreeing acquisitions, {pairs} moves)",
                    {k: v for k, v in prov.items() if k not in ("estimates", "previous_matrix")},
                )
            )
        self.saved += 1
        if not too_far:
            self._settle(new_off or offset)
        return out

    def _settle(self, offset: RotatorOffset) -> None:
        """Clear the suspect flag unless the rotator offset is large (kept
        until a rotator home or a full calibration)."""
        if abs(offset.theta) >= self.config.alarm_deg:
            self.mark(
                f"rotator position probably lost (theta = {offset.theta:+.1f} deg, since"
                f" {offset.cause or 'unknown'}): {HOME_ADVICE}"
            )
        else:
            self._clear()

    def rotator_lost(self, why: str, now: str | None = None) -> None:
        """A rotator drop-out, reconnect or PWI restart was seen: its
        position may be lost; the next acquisitions re-measure it."""
        self.lost = why
        self.history.clear()
        self.mark(f"rotator position possibly lost ({why}): re-measuring", now)

    def rotator_homed(self, offset: RotatorOffset, now: str | None = None) -> RotatorOffset:
        """The rotator was homed: the offset is cleared (and counted)."""
        now = now or _now()
        new = RotatorOffset(**offset.as_json())
        new.theta_before_home = offset.theta if offset.theta != 0.0 else offset.theta_before_home
        new.theta = 0.0
        new.since = new.updated = ""
        new.pairs = new.acquisitions = 0
        new.cause = ""
        new.homes = offset.homes + 1
        new.last_home = now
        new.history = [*offset.history, {"time": now, "event": "homed"}][-20:]
        self.reset()
        return new

    def mark(self, why: str, now: str | None = None) -> None:
        if self.suspect is None:
            self.suspect_since = now or _now()
        self.suspect = why

    def _clear(self) -> None:
        self.suspect = None
        self.suspect_since = None

    def reset(self) -> None:
        """A new calibration or a rotator home: forget the estimate."""
        self.begin()
        self.history.clear()
        self.lost = None
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
            "rotator_lost": self.lost,
            "saved": self.saved,
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
