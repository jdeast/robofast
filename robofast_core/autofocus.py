"""Autofocus: V-curve (default), donut, and sweep methods (docs/autofocus.md).

Far from focus a star's half-flux diameter (HFD) grows *linearly* with the
focuser offset, at a slope fixed by the optics: the geometric blur is a
disk (an annulus, with a central obstruction) of diameter ``|dz| / N`` for
a focal-plane defocus ``dz`` and focal ratio ``N``, and its HFD is
``k = sqrt((1 + eps^2) / 2)`` times that (``eps``: the obstruction's
fractional diameter). Near focus the seeing dominates and the curve
rounds off into a hyperbola. So the focus can be *computed* from star
sizes measured on one known side of it, instead of searched for:

* **V-curve** (``method="vcurve"``, the default): a one-time calibration
  sweep per telescope/port/filter (:meth:`Autofocus.calibrate`) measures
  the two arms' slopes and where each arm's line crosses zero relative to
  the best focus, and stores them (:class:`FocusStore`). Each run then
  takes **two exposures** on the calibrated side, ``far`` and ``near``
  offsets from the predicted focus (both far enough, a blur of 6x the
  worst seeing's FWHM, that the seeing hardly changes the HFD of the
  donut, and farther than the prediction can be wrong, so the side is
  known), and computes ``focus = x - side * HFD / slope`` from each.
  The secant slope between the two points must match the calibrated slope
  (``slope_tolerance``); if it is reversed the stars were on the *other*
  side (the prediction was off by more than the offsets), and the run is
  repeated once from the other arm's estimate; any other mismatch (clouds,
  the wrong star, saturation, a blend) falls back to a full sweep.
* **Donut** (``method="donut"``): one exposure far enough out that the
  defocused star is an annulus much larger than the seeing; its outer
  diameter ``D`` (half-plateau edge, :mod:`robofast_core.starmeasure`)
  gives ``|dz| = D N`` directly, and the side gives the sign. One exposure;
  needs no calibration (the calibration refines the scale if present).
* **Sweep** (``method="sweep"``, also the fallback): ``sweep_points``
  positions through focus, a robust weighted fit (hyperbola, or the
  legacy parabola with its bugs fixed), and the **fit's** focus is used.

Every focuser move ends travelling *from the configured side* (toward
``-side``), overshooting by ``backlash`` steps first when it would
otherwise arrive from the other side, so mechanical backlash never shifts
the result. Each focuser move is bounded (``move_timeout``) and checked
(``position_tolerance``); device errors propagate (the sequencer treats
them as the telescope's error), while a measurement that cannot give a
focus (no stars, a failed fit) ends in an :class:`AutofocusResult` with
``success=False`` and the focuser back at its starting position.

**Temperature model** (:class:`FocusModel`): the starting position is
predicted from registry values (focuser or ambient temperature, ...):
``focus = zero_point + sum(c_i (v_i - v_i0))``. Each successful run
updates only the zero point; the model never replaces a measured focus.

The runner talks to the focuser and the camera only through
:class:`~robofast_core.owner.Grant` objects (so a sequencer's fence covers
it) and takes its exposures through an *exposer* (``exptime -> image``),
by default straight from the camera grant; the sequencer passes one that
also saves the frames.

Credits: the legacy MINERVA autofocus (``newauto.py``) and the TRES guider's
(J. Eastman, J. Kansky) were the starting points; the V-curve/HFD
approach follows FocusMax (Weber & Brady).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast_core.focusfit import (
    FitError,
    fit_hyperbola,
    fit_line,
    fit_parabola,
    two_point,
)
from robofast_core.imagebytes import ImageArray, fits_order
from robofast_core.owner import DeviceError, Grant
from robofast_core.starmeasure import FieldMeasurement, MeasureConfig, measure

log = logging.getLogger(__name__)

__all__ = [
    "METHODS",
    "Autofocus",
    "AutofocusConfig",
    "AutofocusFailed",
    "AutofocusResult",
    "AutofocusSetup",
    "Calibration",
    "FocusModel",
    "FocusOptics",
    "FocusPoint",
    "FocusStore",
    "FocuserError",
    "camera_exposer",
    "publish_result",
]

METHODS = ("vcurve", "donut", "sweep")
#: HFD of a seeing-limited star over its FWHM (Moffat, beta ~ 4.8).
HFD_PER_FWHM = 1.2


class AutofocusFailed(Exception):
    """The measurements could not give a focus (no stars, a failed fit)."""


class FocuserError(DeviceError):
    """The focuser did not reach its target (timeout, wrong position)."""


class _Inconsistent(Exception):
    """Two-point data disagree with the calibration (fall back)."""


class _WrongSide(_Inconsistent):
    def __init__(self, msg: str, estimate: float) -> None:
        super().__init__(msg)
        self.estimate = estimate


# -- configuration ---------------------------------------------------------------


@dataclass
class FocusOptics:
    """The geometry that turns focuser steps into star sizes."""

    #: Focal ratio of the beam at the detector.
    f_ratio: float
    #: Unbinned pixel size, microns.
    pixel_size: float
    #: arcsec per unbinned pixel.
    pixel_scale: float
    #: Central obstruction, fraction of the pupil diameter (0: none).
    obstruction: float = 0.0
    #: Microns of focuser travel per step (the focuser's ``StepSize``).
    um_per_step: float = 1.0
    #: Focal-plane motion per unit of focuser travel: 1 when the focuser
    #: moves the instrument (CDK700 Nasmyth focusers); larger when it moves
    #: the secondary (about m^2 + 1 for secondary magnification m). Only
    #: its magnitude matters: the side of focus is always in focuser steps.
    focal_plane_factor: float = 1.0

    @property
    def focal_um_per_step(self) -> float:
        return abs(self.um_per_step * self.focal_plane_factor)

    @property
    def hfd_per_diameter(self) -> float:
        """HFD of a uniformly lit annulus over its outer diameter."""
        return math.sqrt((1 + self.obstruction**2) / 2)

    @property
    def donut_slope(self) -> float:
        """Geometric blur (donut outer) diameter, unbinned px per step."""
        return self.focal_um_per_step / (self.f_ratio * self.pixel_size)

    @property
    def hfd_slope(self) -> float:
        """Theoretical V-curve arm slope, unbinned px of HFD per step."""
        return self.hfd_per_diameter * self.donut_slope

    def seeing_hfd(self, seeing: float) -> float:
        """HFD (unbinned px) of a seeing-limited star of FWHM ``seeing`` arcsec."""
        return HFD_PER_FWHM * seeing / self.pixel_scale


@dataclass
class AutofocusConfig:
    """One telescope's (port's, filter's) autofocus settings (docs/autofocus.md)."""

    optics: FocusOptics
    method: str = "vcurve"
    #: The side of focus used and approached from, in focuser steps: +1
    #: (positions above focus) or -1.
    side: int = 1
    exptime: float = 5.0
    #: Camera binning and subframe (x0, y0, nx, ny, unbinned) for focus
    #: frames; None: leave the camera as it is. Restored afterwards.
    binning: int | None = None
    roi: tuple[int, int, int, int] | None = None
    #: Worst seeing to plan the offsets for (FWHM, arcsec).
    max_seeing: float = 4.0
    #: How far the starting prediction can be off (steps); offsets are at
    #: least this, so the side of focus is certain.
    max_start_error: float = 0.0
    #: The near point's geometric blur (donut) diameter over the worst
    #: seeing's FWHM: far enough out that the seeing barely moves the HFD.
    blur_ratio: float = 6.0
    #: Far offset over near offset.
    far_ratio: float = 1.5
    #: How a two-point run turns HFDs into a focus: "linear" (each point
    #: ``x - side * HFD / slope``, averaged: the seeing hardly changes the
    #: HFD of a donut much larger than it) or "hyperbola" (solve the arm's
    #: hyperbola through both points: :func:`~robofast_core.focusfit.two_point`).
    vcurve_model: str = "linear"
    #: Explicit offsets from the predicted focus (steps, > 0); None: derived.
    near_offset: float | None = None
    far_offset: float | None = None
    #: Allowed |secant / calibrated slope - 1| for a two-point run.
    slope_tolerance: float = 0.2
    #: Fall back to a sweep when the two points are inconsistent.
    fallback: bool = True
    #: Donut: offset (None: far_offset), and the least donut diameter over
    #: the worst seeing's FWHM for the edge to be seeing-independent.
    donut_offset: float | None = None
    donut_min_ratio: float = 5.0
    #: Sweep: points, half width (None: far_offset), fit ("hyperbola" or
    #: "parabola"), and how many times to re-centre a sweep whose minimum
    #: is at its edge.
    sweep_points: int = 9
    sweep_half_width: float | None = None
    sweep_fit: str = "hyperbola"
    sweep_recentre: int = 1
    #: Calibration sweep: points and half width (None: 1.2 x far_offset).
    calibration_points: int = 17
    calibration_half_width: float | None = None
    #: Calibration arm fits use the points whose geometric blur is at least
    #: this many times the calibration's seeing FWHM.
    arm_min_ratio: float = 5.0
    #: Overshoot (steps) past the target on ``side`` before the final
    #: approach, when the move would otherwise end travelling toward ``side``.
    backlash: float = 0.0
    #: Focuser limits (None: 0 and the focuser's MaxStep).
    min_position: float | None = None
    max_position: float | None = None
    #: A move ends within this many steps of its target, or it failed.
    position_tolerance: float = 2.0
    move_timeout: float = 120.0
    poll_interval: float = 0.25
    #: Exposure: ImageReady within exptime + this.
    readout_margin: float = 60.0
    download_timeout: float = 120.0
    #: Fewest measured stars per exposure.
    min_stars: int = 3
    #: Star measurement (:class:`~robofast_core.starmeasure.MeasureConfig` fields).
    measure: dict[str, Any] = field(default_factory=dict)
    #: Take one more exposure at the final focus and report its FWHM/HFD.
    verify: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AutofocusConfig:
        d = dict(d)
        d.pop("_note", None)
        known = {f.name for f in fields(cls)}
        if set(d) - known:
            raise ValueError(f"autofocus: unknown keys {sorted(set(d) - known)}")
        if "optics" not in d:
            raise ValueError("autofocus: optics is required")
        optics = d.pop("optics")
        if not isinstance(optics, FocusOptics):
            okeys = {f.name for f in fields(FocusOptics)}
            if set(optics) - okeys:
                raise ValueError(f"autofocus.optics: unknown keys {sorted(set(optics) - okeys)}")
            optics = FocusOptics(**optics)
        if d.get("roi") is not None:
            d["roi"] = tuple(int(v) for v in d["roi"])
        cfg = cls(optics=optics, **d)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"autofocus.method {self.method!r}: expected one of {METHODS}")
        if self.side not in (1, -1):
            raise ValueError("autofocus.side must be +1 or -1")
        if self.vcurve_model not in ("linear", "hyperbola"):
            raise ValueError("autofocus.vcurve_model must be 'linear' or 'hyperbola'")
        if self.sweep_fit not in ("hyperbola", "parabola"):
            raise ValueError("autofocus.sweep_fit must be 'hyperbola' or 'parabola'")
        if self.sweep_points < 5 or self.calibration_points < 7:
            raise ValueError("autofocus: sweep_points >= 5 and calibration_points >= 7")
        o = self.optics
        if o.f_ratio <= 0 or o.pixel_size <= 0 or o.pixel_scale <= 0:
            raise ValueError("autofocus.optics: f_ratio, pixel_size, pixel_scale must be > 0")
        if not 0 <= o.obstruction < 1:
            raise ValueError("autofocus.optics.obstruction must be in [0, 1)")
        for name in ("near_offset", "far_offset", "donut_offset", "sweep_half_width"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"autofocus.{name} must be > 0")
        MeasureConfig(**self.measure)  # fail at startup


# -- calibration, model, store -----------------------------------------------------------


@dataclass
class Calibration:
    """A telescope/port/filter's measured V-curve (from :meth:`Autofocus.calibrate`).

    Slopes are magnitudes in unbinned px of HFD per step; ``offset_*`` is
    where that arm's line crosses zero, minus the best focus (steps); the
    donut terms likewise for the donut diameter.
    """

    focus: float
    slope_plus: float
    slope_minus: float
    offset_plus: float = 0.0
    offset_minus: float = 0.0
    donut_plus: float | None = None
    donut_minus: float | None = None
    donut_offset_plus: float = 0.0
    donut_offset_minus: float = 0.0
    seeing_hfd: float | None = None
    time: str = ""
    points: int = 0

    def slope(self, side: int) -> float:
        return self.slope_plus if side > 0 else self.slope_minus

    def offset(self, side: int) -> float:
        return self.offset_plus if side > 0 else self.offset_minus

    def donut(self, side: int) -> tuple[float | None, float]:
        if side > 0:
            return self.donut_plus, self.donut_offset_plus
        return self.donut_minus, self.donut_offset_minus

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Calibration:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def term_value(name: str, value: Any) -> float | None:
    """A focus-model term's value: the registry value, or for a ``sin:``
    term the sine of it in degrees (legacy ``guessFocus``'s gravity term,
    ``sin(alt)``). None when missing or not finite."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return math.sin(math.radians(v)) if name.startswith("sin:") else v


def term_source(name: str) -> str:
    """The registry name a term reads (``sin:{tel}.tel.alt`` -> ``{tel}.tel.alt``)."""
    return name.split(":", 1)[1] if name.startswith("sin:") else name


#: Values recorded with every autofocus result whether the model uses them
#: or not, so a later fit (robofast_core/focusmodel.py) can add their terms.
FOCUS_CONTEXT = ("{tel}.focuser.temperature", "sin:{tel}.tel.alt")


@dataclass
class FocusModel:
    """``focus = zero_point + sum(coeff * (value - reference))`` (module doc).

    A term named ``sin:<value>`` uses the sine (degrees) of the value: the
    legacy gravity term ``C2 (sin(alt) - G0)`` is
    ``{"sin:{tel}.tel.alt": C2}`` with reference ``G0``.

    ``terms`` maps registry value names (``{tel}`` is replaced by the
    telescope name, e.g. ``"{tel}.focuser.temperature"``) to coefficients
    in steps per unit; ``reference`` gives each term's reference value.
    A value that is missing or stale counts as its reference (the term
    contributes nothing), and is reported.
    """

    terms: dict[str, float] = field(default_factory=dict)
    reference: dict[str, float] = field(default_factory=dict)
    zero_point: float | None = None
    updated: str | None = None

    def correction(self, values: dict[str, float | None]) -> tuple[float, list[str]]:
        total, missing = 0.0, []
        for name, c in self.terms.items():
            v = term_value(name, values.get(name))
            ref = self.reference.get(name, 0.0)
            if v is None:
                missing.append(name)
                continue
            total += c * (v - ref)
        return total, missing

    def predict(self, values: dict[str, float | None]) -> float | None:
        if self.zero_point is None:
            return None
        return self.zero_point + self.correction(values)[0]

    def update(
        self, measured: float, values: dict[str, float | None], when: datetime | None = None
    ) -> None:
        """Re-anchor the zero point on a measured focus (never the reverse)."""
        self.zero_point = measured - self.correction(values)[0]
        self.updated = (when or datetime.now(UTC)).isoformat()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FocusModel:
        d = dict(d or {})
        d.pop("_note", None)
        known = {f.name for f in fields(cls)}
        if set(d) - known:
            raise ValueError(f"focus model: unknown keys {sorted(set(d) - known)}")
        return cls(
            {str(k): float(v) for k, v in (d.get("terms") or {}).items()},
            {str(k): float(v) for k, v in (d.get("reference") or {}).items()},
            None if d.get("zero_point") is None else float(d["zero_point"]),
            d.get("updated"),
        )


class FocusStore:
    """Calibrations, model zero points and recent results, in one JSON file
    (written atomically). Keys are ``telescope/port/filter`` (``-`` for
    none). Without a path it lives in memory."""

    def __init__(self, path: str | Path | None = None, history: int = 200) -> None:
        self.path = Path(path) if path is not None else None
        self.history_size = history
        self.data: dict[str, Any] = {"calibrations": {}, "models": {}, "last": {}, "history": []}
        if self.path is not None and self.path.exists():
            loaded = json.loads(self.path.read_text())
            for k in self.data:
                self.data[k] = loaded.get(k, self.data[k])

    @staticmethod
    def key(telescope: str, port: str | None = None, filter: str | None = None) -> str:
        return f"{telescope}/{port or '-'}/{filter or '-'}"

    def calibration(self, key: str) -> Calibration | None:
        d = self.data["calibrations"].get(key)
        return None if d is None else Calibration.from_dict(d)

    def set_calibration(self, key: str, cal: Calibration) -> None:
        self.data["calibrations"][key] = asdict(cal)
        self.save()

    def model_zero_point(self, key: str) -> tuple[float | None, str | None]:
        d = self.data["models"].get(key) or {}
        return d.get("zero_point"), d.get("updated")

    def set_model_zero_point(self, key: str, model: FocusModel) -> None:
        self.data["models"][key] = {"zero_point": model.zero_point, "updated": model.updated}
        self.save()

    def last_focus(self, key: str) -> float | None:
        d = self.data["last"].get(key)
        return None if d is None else d.get("focus")

    def record(self, key: str, result: AutofocusResult) -> None:
        if result.success and result.focus is not None:
            self.data["last"][key] = {"focus": result.focus, "time": result.time}
        self.data["history"].append({"key": key, **result.summary()})
        del self.data["history"][: -self.history_size]
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=float))
        os.replace(tmp, self.path)


@dataclass
class AutofocusSetup:
    """A telescope's autofocus, as the sequencer uses it: the base config,
    overrides by port and filter, the temperature model and the store.

    From the ``autofocus`` entry of a telescope in the enclosure config::

        {"optics": {...}, "method": "vcurve", ...,      # AutofocusConfig
         "overrides": {"-/V": {"exptime": 10}, "fau/-": {...}},
         "model": {"terms": {"{tel}.focuser.temperature": -52.9},
                   "reference": {"{tel}.focuser.temperature": 8.95}},
         "store": "focus_t1.json"}

    Overrides are keyed ``port/filter`` (``-`` for any) and applied from
    least to most specific: ``-/filter``, ``port/-``, ``port/filter``.
    """

    base: dict[str, Any]
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: FocusModel = field(default_factory=FocusModel)
    store: FocusStore = field(default_factory=FocusStore)

    @classmethod
    def from_dict(cls, d: dict[str, Any], base_path: Path | None = None) -> AutofocusSetup:
        d = dict(d)
        d.pop("_note", None)
        model = FocusModel.from_dict(d.pop("model", None))
        overrides = d.pop("overrides", None) or {}
        store_path = d.pop("store", None)
        if store_path is not None:
            store_path = Path(store_path)
            if base_path is not None and not store_path.is_absolute():
                store_path = base_path / store_path
        setup = cls(d, dict(overrides), model, FocusStore(store_path))
        setup.config_for(None, None)  # fail at startup
        for k in overrides:
            if k.count("/") != 1:
                raise ValueError(f"autofocus.overrides: key {k!r} is not 'port/filter'")
            port, filt = k.split("/")
            setup.config_for(None if port == "-" else port, None if filt == "-" else filt)
        return setup

    def config_for(
        self, port: str | None, filter: str | None, extra: dict[str, Any] | None = None
    ) -> AutofocusConfig:
        merged = copy.deepcopy(self.base)
        for k in (f"-/{filter or '-'}", f"{port or '-'}/-", f"{port or '-'}/{filter or '-'}"):
            if k != "-/-" and k in self.overrides:
                over = copy.deepcopy(self.overrides[k])
                if "optics" in over:
                    merged["optics"] = {**merged.get("optics", {}), **over.pop("optics")}
                merged.update(over)
        merged.update(extra or {})
        return AutofocusConfig.from_dict(merged)


# -- results ---------------------------------------------------------------------------


@dataclass
class FocusPoint:
    """One focus exposure: where, and what was measured (HFD and donut in
    unbinned pixels; FWHM in arcsec)."""

    position: float
    hfd: float
    hfd_err: float
    fwhm: float
    donut: float
    stars: int
    rejected: dict[str, int] = field(default_factory=dict)


@dataclass
class AutofocusResult:
    telescope: str
    method: str
    success: bool = False
    #: The measured best focus (steps); None on failure.
    focus: float | None = None
    focus_err: float | None = None
    #: Where the run started, and the model's prediction then (if any).
    start: float | None = None
    predicted: float | None = None
    #: The method that produced ``focus`` (a fallback changes it).
    used: str = ""
    #: HFD at focus (unbinned px) and the seeing (FWHM, arcsec) from the fit.
    hfd_at_focus: float | None = None
    seeing: float | None = None
    points: list[FocusPoint] = field(default_factory=list)
    exposures: int = 0
    fallbacks: list[str] = field(default_factory=list)
    message: str = ""
    time: str = ""
    duration: float = 0.0
    model_values: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d["points"] = [
            {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(p).items()}
            for p in self.points
        ]
        return d


def publish_result(registry: Any, telescope: str, result: AutofocusResult) -> None:
    """The last autofocus, as ``{tel}.autofocus.*`` registry values (FITS
    keywords AF*): they describe the last run, so they never go stale."""
    vals: list[tuple[str, Any, str, str, str]] = [
        ("time", result.time, "AFTIME", "", "Last autofocus (UTC)"),
        ("method", result.used or result.method, "AFMETHOD", "", "Last autofocus method"),
        ("success", result.success, "AFOK", "", "Last autofocus succeeded"),
        ("nexp", result.exposures, "AFNEXP", "", "Last autofocus exposures"),
    ]
    if result.focus is not None:
        vals.append(("focus", round(result.focus, 1), "AFFOCUS", "step", "Last autofocus focus"))
    if result.focus_err is not None and math.isfinite(result.focus_err):
        vals.append(("focus_err", round(result.focus_err, 1), "AFFOCERR", "step", "Its error"))
    if result.seeing is not None and math.isfinite(result.seeing):
        vals.append(("seeing", round(result.seeing, 2), "AFSEEING", "arcsec", "Autofocus FWHM"))
    for name, value, kw, unit, comment in vals:
        registry.publish(
            f"{telescope}.autofocus.{name}",
            value,
            max_age=None,
            unit=unit,
            keyword=kw,
            comment=comment,
        )


# -- exposures -------------------------------------------------------------------------

Exposer = Callable[[float], Awaitable[np.ndarray]]


def camera_exposer(
    cam: Grant,
    *,
    readout_margin: float = 60.0,
    poll: float = 0.25,
    download_timeout: float = 120.0,
) -> Exposer:
    """Take a light exposure on ``cam`` and return it in FITS order."""

    async def expose(exptime: float) -> np.ndarray:
        await cam.call("StartExposure", exptime, True)
        async with asyncio.timeout(exptime + readout_margin):
            while not await cam.get("ImageReady"):
                await asyncio.sleep(poll)
        image = await cam.get("ImageArray", timeout=download_timeout)
        if isinstance(image, ImageArray):
            return fits_order(image.data)
        return np.asarray(image)

    return expose


# -- the runner ------------------------------------------------------------------------


class Autofocus:
    """One autofocus run's machinery for one telescope (module doc).

    ``focuser`` and ``camera`` are grants; ``exposer`` defaults to
    :func:`camera_exposer` on ``camera``. ``store`` keeps calibrations and
    the model's zero point under ``key``; ``model`` predicts the start from
    ``values()`` (registry value names -> values). ``event`` is called as
    ``event(what, **detail)`` for the timeline.
    """

    def __init__(
        self,
        telescope: str,
        config: AutofocusConfig,
        focuser: Grant,
        camera: Grant | None = None,
        *,
        exposer: Exposer | None = None,
        store: FocusStore | None = None,
        key: str | None = None,
        model: FocusModel | None = None,
        values: Callable[[], dict[str, float | None]] | None = None,
        event: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if exposer is None and camera is None:
            raise ValueError("give a camera grant or an exposer")
        self.telescope = telescope
        self.config = config
        self.focuser = focuser
        self.camera = camera
        self.exposer = exposer or camera_exposer(
            camera,  # type: ignore[arg-type]
            readout_margin=config.readout_margin,
            poll=config.poll_interval,
            download_timeout=config.download_timeout,
        )
        self.store = store or FocusStore()
        self.key = key or FocusStore.key(telescope)
        # A copy: the zero point is per key (port, filter).
        self.model = copy.deepcopy(model) if model is not None else FocusModel()
        zp, updated = self.store.model_zero_point(self.key)
        if zp is not None:
            self.model.zero_point, self.model.updated = zp, updated
        self._values = values or (lambda: {})
        self._event = event or (lambda what, **kw: None)
        self._limits: tuple[float, float] | None = None
        #: Result and calibration times (the sequencer's clock, robofast_core.clock).
        self.clock = clock or (lambda: datetime.now(UTC))
        self._approached: float | None = None  # last position reached from `side`
        self._result: AutofocusResult | None = None
        self._binning = 1

    # -- public -------------------------------------------------------------------

    @property
    def calibration(self) -> Calibration | None:
        return self.store.calibration(self.key)

    def offsets(self) -> tuple[float, float]:
        """(near, far) offsets from the predicted focus, steps: the near
        point's geometric blur is ``blur_ratio`` times the worst seeing's
        FWHM, plus ``max_start_error`` (module doc)."""
        cfg = self.config
        near = cfg.near_offset
        if near is None:
            fwhm_px = cfg.max_seeing / cfg.optics.pixel_scale
            near = cfg.blur_ratio * fwhm_px / cfg.optics.donut_slope + cfg.max_start_error
        far = cfg.far_offset if cfg.far_offset is not None else near * cfg.far_ratio
        if far <= near:
            raise ValueError("autofocus: far_offset must exceed near_offset")
        return near, far

    async def run(
        self, method: str | None = None, *, start: float | None = None
    ) -> AutofocusResult:
        """Focus: ``method`` (default: the config's). Returns the result,
        with the focuser at the new focus (or back at the start, on failure)."""
        method = method or self.config.method
        if method not in METHODS:
            raise ValueError(f"autofocus method {method!r}")
        return await self._run(method, start, calibrate=False)

    async def calibrate(self, *, start: float | None = None) -> AutofocusResult:
        """The calibration sweep (module doc); stores the calibration."""
        return await self._run("sweep", start, calibrate=True)

    # -- the run -------------------------------------------------------------------

    async def _run(self, method: str, start: float | None, calibrate: bool) -> AutofocusResult:
        cfg = self.config
        t0 = time.monotonic()
        values = dict(self._values())
        res = AutofocusResult(
            self.telescope,
            "calibrate" if calibrate else method,
            time=self.clock().isoformat(timespec="seconds"),
            model_values=values,
        )
        self._result = res
        res.predicted = self.model.predict(values)
        if start is None:
            start = res.predicted
        if start is None:
            start = self.store.last_focus(self.key)
        if start is None:
            start = float(await self.focuser.get("Position"))
        res.start = float(start)
        self._event("autofocus_start", telescope=self.telescope, method=res.method, start=start)
        restore = await self._configure_camera()
        try:
            try:
                if calibrate:
                    focus, err = await self._calibrate(start)
                    res.used = "calibrate"
                elif method == "vcurve":
                    focus, err = await self._vcurve_with_fallback(start)
                elif method == "donut":
                    focus, err = await self._donut_with_fallback(start)
                else:
                    focus, err = await self._sweep(start)
                    res.used = res.used or "sweep"
            except AutofocusFailed as exc:
                res.success = False
                res.message = str(exc)
                log.warning("%s: autofocus failed: %s", self.telescope, exc)
                lo, hi = await self._bounds()
                if lo <= start <= hi:
                    await self.move(start)  # back where it started
            else:
                res.success = True
                res.focus, res.focus_err = float(focus), float(err)
                await self.move(focus)
                if cfg.verify:
                    p = await self._point(focus, move=False)
                    res.details["verify"] = asdict(p)
                    if math.isfinite(p.fwhm):
                        res.seeing = p.fwhm
                # Only the model's zero point follows the measurement.
                self.model.update(res.focus, values, self.clock())
                self.store.set_model_zero_point(self.key, self.model)
        finally:
            await restore()
            res.duration = time.monotonic() - t0
            self.store.record(self.key, res)
            self._event(
                "autofocus_done",
                telescope=self.telescope,
                success=res.success,
                method=res.used or res.method,
                focus=res.focus,
                focus_err=res.focus_err,
                exposures=res.exposures,
                fallbacks=res.fallbacks,
                message=res.message,
            )
        return res

    # -- methods -------------------------------------------------------------------

    def _slope(self, side: int) -> float:
        cal = self.calibration
        if cal is not None and cal.slope(side) > 0:
            return cal.slope(side)
        return self.config.optics.hfd_slope

    def _arm_offset(self, side: int) -> float:
        cal = self.calibration
        return cal.offset(side) if cal is not None else 0.0

    async def _vcurve_with_fallback(self, start: float) -> tuple[float, float]:
        res = self._result
        assert res is not None
        try:
            try:
                focus, err = await self._vcurve(start)
            except _WrongSide as exc:
                res.fallbacks.append(f"wrong side: {exc}")
                self._event("autofocus_wrong_side", telescope=self.telescope, estimate=exc.estimate)
                focus, err = await self._vcurve(exc.estimate)
            res.used = "vcurve"
            return focus, err
        except _Inconsistent as exc:
            res.fallbacks.append(str(exc))
            if not self.config.fallback:
                raise AutofocusFailed(f"V-curve: {exc}") from exc
            self._event("autofocus_fallback", telescope=self.telescope, reason=str(exc))
            res.used = "sweep"
            return await self._sweep(start)

    async def _vcurve(self, start: float) -> tuple[float, float]:
        cfg = self.config
        s = cfg.side
        m = self._slope(s)
        near, far = self.offsets()
        p_far = await self._point(start + s * far)
        p_near = await self._point(start + s * near)
        for p in (p_far, p_near):
            if p.stars < cfg.min_stars:
                raise _Inconsistent(f"{p.stars} stars at {p.position:.0f}")
        res = self._result
        assert res is not None
        pts = (
            p_far.position,
            p_far.hfd,
            p_far.hfd_err,
            p_near.position,
            p_near.hfd,
            p_near.hfd_err,
        )
        tp = two_point(*pts, m, s)
        res.details["slope"] = m
        res.details["secant_ratio"] = tp.slope_ratio
        tol = cfg.slope_tolerance
        if abs(tp.slope_ratio + 1) <= tol:
            # Reversed: the stars shrank moving out, so we are on the other
            # side of focus. Estimate from the other arm, then redo.
            m2 = self._slope(-s)
            est = (p_far.position + s * p_far.hfd / m2 + p_near.position + s * p_near.hfd / m2) / 2
            raise _WrongSide(f"secant/slope = {tp.slope_ratio:.2f}", est - self._arm_offset(-s))
        if abs(tp.slope_ratio - 1) > tol:
            raise _Inconsistent(f"secant/slope = {tp.slope_ratio:.2f} (tolerance {tol})")
        if cfg.vcurve_model == "hyperbola":
            a_max = 1.5 * cfg.optics.seeing_hfd(cfg.max_seeing)
            if tp.a2 > a_max**2:
                raise _Inconsistent(f"seeing term {math.sqrt(tp.a2):.1f} px > {a_max:.1f} px")
            if tp.a2 > 0:
                res.hfd_at_focus = math.sqrt(tp.a2)
                res.seeing = res.hfd_at_focus / HFD_PER_FWHM * cfg.optics.pixel_scale
            zero, err = tp.zero, tp.zero_err
        else:
            # Each point on its own: focus = x - side * HFD / slope; averaged.
            zs = [p.position - s * p.hfd / m for p in (p_far, p_near)]
            es = [p.hfd_err / m for p in (p_far, p_near)]
            zero = (zs[0] + zs[1]) / 2
            err = math.hypot(*es) / 2
            res.details["zeros"] = zs
        if (p_near.position - zero) * s <= 0:
            raise _Inconsistent("the near point is not on the arm")
        return zero - self._arm_offset(s), err

    async def _donut_with_fallback(self, start: float) -> tuple[float, float]:
        res = self._result
        assert res is not None
        try:
            focus, err = await self._donut(start)
            res.used = "donut"
            return focus, err
        except _Inconsistent as exc:
            res.fallbacks.append(str(exc))
            if not self.config.fallback:
                raise AutofocusFailed(f"donut: {exc}") from exc
            self._event("autofocus_fallback", telescope=self.telescope, reason=str(exc))
            res.used = "sweep"
            return await self._sweep(start)

    async def _donut(self, start: float) -> tuple[float, float]:
        cfg = self.config
        s = cfg.side
        cal = self.calibration
        scale, offset = cal.donut(s) if cal is not None else (None, 0.0)
        scale = scale or cfg.optics.donut_slope
        dist = cfg.donut_offset
        if dist is None:
            # Big enough for a seeing-independent edge, and past the start error.
            fwhm = cfg.max_seeing / cfg.optics.pixel_scale
            dist = max(cfg.donut_min_ratio * fwhm / scale + cfg.max_start_error, self.offsets()[1])
        x = start + s * dist
        p = await self._point(x)
        if p.stars < cfg.min_stars:
            raise _Inconsistent(f"{p.stars} stars at {x:.0f}")
        if not math.isfinite(p.donut):
            raise _Inconsistent("no donut edge measured")
        min_d = cfg.donut_min_ratio * cfg.max_seeing / cfg.optics.pixel_scale
        if p.donut < min_d:
            raise _Inconsistent(f"donut {p.donut:.1f} px < {min_d:.1f} px")
        delta = p.donut / scale
        res = self._result
        assert res is not None
        res.details["donut"] = {"diameter": p.donut, "scale": scale, "delta": delta}
        # The donut's zero crossing sits `offset` from focus (calibration).
        focus = p.position - s * delta - offset
        donut_err = self._donut_err(p)
        return focus, donut_err / scale

    def _donut_err(self, p: FocusPoint) -> float:
        # The edge scatters about as the HFD does: use the HFD's relative error.
        rel = p.hfd_err / p.hfd if p.hfd > 0 and math.isfinite(p.hfd_err) else 0.02
        return p.donut * rel

    def _sweep_positions(self, centre: float, n: int, half: float) -> list[float]:
        s = self.config.side
        # From the configured side toward the other: every move travels -side.
        return [centre + s * half * (1 - 2 * i / (n - 1)) for i in range(n)]

    async def _sweep(self, start: float) -> tuple[float, float]:
        cfg = self.config
        half = cfg.sweep_half_width or self.offsets()[1]
        centre = start
        res = self._result
        assert res is not None
        last_exc: Exception | None = None
        for attempt in range(cfg.sweep_recentre + 1):
            pts = [
                await self._point(x) for x in self._sweep_positions(centre, cfg.sweep_points, half)
            ]
            good = [p for p in pts if p.stars >= cfg.min_stars and math.isfinite(p.hfd)]
            if len(good) < 5:
                raise AutofocusFailed(f"sweep: {len(good)} usable points of {len(pts)}")
            x = np.array([p.position for p in good])
            h = np.array([p.hfd for p in good])
            e = np.array([p.hfd_err for p in good])
            try:
                if cfg.sweep_fit == "parabola":
                    fit = fit_parabola(x, h, e)
                    res.details["sweep"] = {"fit": "parabola", "coeffs": list(fit.coeffs)}
                    return fit.x0, fit.x0_err
                hfit = fit_hyperbola(x, h, e)
            except FitError as exc:
                last_exc = exc
                # Re-centre on the smallest star if the minimum was at an edge.
                best = float(x[int(np.argmin(h))])
                if attempt < cfg.sweep_recentre and best in (x.min(), x.max()):
                    res.fallbacks.append(f"sweep re-centred on {best:.0f}: {exc}")
                    centre = best
                    continue
                raise AutofocusFailed(f"sweep fit: {exc}") from exc
            res.hfd_at_focus = hfit.a
            res.seeing = hfit.a / HFD_PER_FWHM * cfg.optics.pixel_scale
            res.details["sweep"] = {
                "fit": "hyperbola",
                "a": hfit.a,
                "m_plus": hfit.m_plus,
                "m_minus": hfit.m_minus,
                "chi2": hfit.chi2,
                "used": len(hfit.used),
            }
            return hfit.x0, hfit.x0_err
        raise AutofocusFailed(f"sweep: {last_exc}")

    async def _calibrate(self, start: float) -> tuple[float, float]:
        cfg = self.config
        half = cfg.calibration_half_width or 1.2 * self.offsets()[1]
        pts = [
            await self._point(x) for x in self._sweep_positions(start, cfg.calibration_points, half)
        ]
        good = [p for p in pts if p.stars >= cfg.min_stars and math.isfinite(p.hfd)]
        if len(good) < 7:
            raise AutofocusFailed(f"calibration: {len(good)} usable points of {len(pts)}")
        x = np.array([p.position for p in good])
        h = np.array([p.hfd for p in good])
        e = np.array([p.hfd_err for p in good])
        d = np.array([p.donut for p in good])
        try:
            hfit = fit_hyperbola(x, h, e)
        except FitError as exc:
            raise AutofocusFailed(f"calibration fit: {exc}") from exc
        x0 = hfit.x0
        # Points far enough out that the blur dominates the seeing.
        fwhm_cal = hfit.a / HFD_PER_FWHM
        far = np.abs(x - x0) * cfg.optics.donut_slope >= cfg.arm_min_ratio * fwhm_cal
        arms: dict[int, Any] = {}
        donuts: dict[int, Any] = {}
        min_d = cfg.donut_min_ratio * fwhm_cal
        for side in (1, -1):
            sel = far & ((x - x0) * side > 0)
            if sel.sum() < 3:
                raise AutofocusFailed(
                    f"calibration: {int(sel.sum())} points on the {'+' if side > 0 else '-'} arm"
                )
            try:
                line = fit_line(x[sel], h[sel], e[sel])
            except FitError as exc:
                raise AutofocusFailed(f"calibration arm: {exc}") from exc
            if line.slope * side <= 0:
                raise AutofocusFailed("calibration: the arms do not form a V")
            arms[side] = line
            dsel = sel & np.isfinite(d) & (d >= min_d)
            if dsel.sum() >= 2:
                try:
                    donuts[side] = fit_line(x[dsel], d[dsel], d[dsel] * e[dsel] / h[dsel])
                except FitError:
                    pass
        cal = Calibration(
            focus=float(x0),
            slope_plus=float(arms[1].slope),
            slope_minus=float(-arms[-1].slope),
            offset_plus=float(arms[1].zero - x0),
            offset_minus=float(arms[-1].zero - x0),
            seeing_hfd=float(hfit.a),
            time=self.clock().isoformat(timespec="seconds"),
            points=len(good),
        )
        if 1 in donuts and donuts[1].slope > 0:
            cal.donut_plus = float(donuts[1].slope)
            cal.donut_offset_plus = float(donuts[1].zero - x0)
        if -1 in donuts and donuts[-1].slope < 0:
            cal.donut_minus = float(-donuts[-1].slope)
            cal.donut_offset_minus = float(donuts[-1].zero - x0)
        self.store.set_calibration(self.key, cal)
        res = self._result
        assert res is not None
        res.hfd_at_focus = hfit.a
        res.seeing = hfit.a / HFD_PER_FWHM * cfg.optics.pixel_scale
        res.details["calibration"] = asdict(cal)
        res.details["theory_slope"] = cfg.optics.hfd_slope
        return x0, hfit.x0_err

    # -- one exposure ----------------------------------------------------------------

    async def _point(self, position: float, *, move: bool = True) -> FocusPoint:
        cfg = self.config
        if move:
            await self.move(position)
        image = await self.exposer(cfg.exptime)
        res = self._result
        assert res is not None
        res.exposures += 1
        b = self._binning
        mcfg = MeasureConfig(**cfg.measure)
        expect = self._expected_hfd(position) / b  # binned px
        if "smooth" not in cfg.measure:
            # A quarter of the expected size, so a donut is one detection.
            mcfg.smooth = max(1.0, expect / 4.0)
        if "background_box" not in cfg.measure:
            # Background tiles several donuts wide, so no donut fills one.
            mcfg.background_box = max(64, round(6 * expect))
        field_m: FieldMeasurement = await asyncio.to_thread(measure, image, mcfg)
        fwhm = field_m.fwhm * b * cfg.optics.pixel_scale
        p = FocusPoint(
            position=float(position),
            hfd=field_m.hfd * b,
            hfd_err=field_m.hfd_err * b,
            fwhm=fwhm,
            donut=field_m.donut * b,
            stars=field_m.n,
            rejected=field_m.rejected,
        )
        res.points.append(p)
        self._event(
            "autofocus_point",
            telescope=self.telescope,
            position=round(p.position, 1),
            hfd=round(p.hfd, 3),
            stars=p.stars,
        )
        return p

    def _expected_hfd(self, position: float) -> float:
        res = self._result
        centre = res.start if res is not None and res.start is not None else position
        geo = self._slope(self.config.side) * abs(position - centre)
        seeing = self.config.optics.seeing_hfd(min(self.config.max_seeing, 2.0))
        return math.hypot(geo, seeing)

    # -- the focuser -------------------------------------------------------------------

    async def _bounds(self) -> tuple[float, float]:
        if self._limits is None:
            cfg = self.config
            lo = cfg.min_position if cfg.min_position is not None else 0.0
            hi = cfg.max_position
            if hi is None:
                try:
                    hi = float(await self.focuser.get("MaxStep"))
                except DeviceError:
                    hi = math.inf
            self._limits = (lo, hi)
        return self._limits

    async def move(self, target: float) -> None:
        """Move to ``target``, arriving from the configured side (module doc)."""
        cfg = self.config
        s = cfg.side
        lo, hi = await self._bounds()
        target = round(target)
        if not lo <= target <= hi:
            raise AutofocusFailed(f"focus position {target} outside [{lo:g}, {hi:g}]")
        cur = float(await self.focuser.get("Position"))
        if abs(cur - target) <= cfg.position_tolerance and (
            cfg.backlash <= 0 or self._approached == target
        ):
            return  # already here (and, with backlash, arrived from the side)
        if cfg.backlash > 0 and (cur - target) * s <= 0:
            # It would arrive travelling toward `side`: overshoot first.
            over = min(max(target + s * cfg.backlash, lo), hi)
            if over != target:
                await self._move_raw(over)
        await self._move_raw(target)
        self._approached = target

    async def _move_raw(self, target: float) -> None:
        cfg = self.config
        target = round(target)
        await self.focuser.call("Move", int(target))
        try:
            async with asyncio.timeout(cfg.move_timeout):
                while await self.focuser.get("IsMoving"):
                    await asyncio.sleep(cfg.poll_interval)
        except TimeoutError:
            try:
                await self.focuser.call("Halt")
            except Exception as exc:  # Halt is optional (TCSGO has none)
                log.warning("%s: focuser Halt after timeout: %r", self.telescope, exc)
            raise FocuserError(
                f"{self.telescope}: focuser still moving after {cfg.move_timeout} s (to {target})"
            ) from None
        pos = float(await self.focuser.get("Position"))
        if abs(pos - target) > cfg.position_tolerance:
            raise FocuserError(f"{self.telescope}: focuser at {pos:g}, commanded {target}")
        self._approached = None

    # -- the camera --------------------------------------------------------------------

    async def _configure_camera(self) -> Callable[[], Awaitable[None]]:
        """Apply ``binning``/``roi``; return a coroutine function restoring them."""
        cfg = self.config
        self._binning = 1
        cam = self.camera
        if cam is None or (cfg.binning is None and cfg.roi is None):
            if cam is not None:
                try:
                    self._binning = int(await cam.get("BinX"))
                except Exception:
                    self._binning = 1

            async def nothing() -> None:
                return None

            return nothing
        members = ("BinX", "BinY", "StartX", "StartY", "NumX", "NumY")
        saved = {m: await cam.get(m) for m in members}
        b = int(cfg.binning or saved["BinX"])
        if cfg.roi is not None:
            x0, y0, nx, ny = cfg.roi
        else:
            x0 = int(saved["StartX"]) * int(saved["BinX"])
            y0 = int(saved["StartY"]) * int(saved["BinY"])
            nx = int(saved["NumX"]) * int(saved["BinX"])
            ny = int(saved["NumY"]) * int(saved["BinY"])
        new = {
            "BinX": b,
            "BinY": b,
            "StartX": x0 // b,
            "StartY": y0 // b,
            "NumX": nx // b,
            "NumY": ny // b,
        }
        for m in members:
            await cam.set(m, new[m])
        self._binning = b

        async def restore() -> None:
            for m in members:
                try:
                    await cam.set(m, saved[m])
                except Exception as exc:
                    log.warning("%s: restoring camera %s: %r", self.telescope, m, exc)

        return restore
