"""Observing requests in the LCO OCS request language (design.md 3.7).

::

    RequestGroup            who asked, how important (ipp_value), observation_type
      Request               one schedulable unit: windows, location, cadence
        Configuration       one target with one instrument_type, with a status
          InstrumentConfig  exposure_time, exposure_count, optical_elements

The JSON uses the field names, nesting and units of the Observatory Control
System's observation portal (``observatorycontrolsystem/observation-portal``),
so a client written for an OCS portal -- TOM Toolkit's ``OCSFacility`` above
all (docs/tom_facility.md) -- can submit to robofast unchanged. What the OCS
has no field for lives in the ``extra_params`` of the level it belongs to
(the OCS's own extension point); docs/request_language.md lists every
field, its unit, and what robofast does with it.

The dispatch scheduler (:mod:`robofast_core.dispatch`) picks a *Request* and
executes its configurations in order, one :class:`InstrumentConfig` per
:class:`~robofast_core.plan.Observation`; a request's configurations taken
together are one *visit*.

Status is per configuration (:class:`Status`; robofast's own, reported as
``status``):

* ``PENDING`` -- not started, or reset (a standing request after a visit);
* ``ATTEMPTED`` -- started at least once but not completed; still schedulable;
* ``COMPLETED`` -- done (a one-shot request's configuration);
* ``FAILED`` -- failed ``max_attempts`` times (device errors, refused slews;
  an interruption by weather or a ToO does not count);
* ``EXPIRED`` -- its windows (or a ToO's deadline) passed before it completed;
* ``CANCELED`` -- withdrawn.

Requests and groups report the OCS ``state`` (:func:`ocs_state`):
``PENDING``, ``COMPLETED``, ``WINDOW_EXPIRED``, ``FAILURE_LIMIT_REACHED`` or
``CANCELED``.

A request with ``extra_params.cadence`` is *standing*: after each completed
visit its configurations go back to ``PENDING``, and the scheduler enforces
``min_interval`` between visits and ``max_per_night``. Without one it is a
one-shot request. (The OCS's own ``cadence`` -- start, end, period, jitter --
is a recipe for many one-shot requests, expanded by the request API's
``/api/requestgroups/cadence/``, not part of a stored request.)

A group whose ``observation_type`` is ``RAPID_RESPONSE`` or ``TIME_CRITICAL``
is a target of opportunity (design.md 3.7.1): it ranks above every normal
request, has a deadline (the latest start) and an interrupt policy
(:class:`TooSpec`, from ``extra_params.too`` or the type's defaults).

Everything round-trips through JSON (:meth:`RequestGroup.from_dict`,
:meth:`RequestGroup.to_dict`); times are ISO 8601 (one without a time zone
is UTC, as in the OCS). Validation errors are :class:`ValueError` naming the
offending entry.

**The previous robofast JSON** (``ra_hours``, ``exptime``, ``instrument``,
``priority``, ``too``, ...) still loads for one release: :func:`upgrade_legacy`
rewrites it into this shape (every ``from_dict`` calls it), and
``to_dict`` writes the new shape, so state files and request files migrate
the first time they are written.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

log = logging.getLogger(__name__)

__all__ = [
    "INTERRUPT_POLICIES",
    "OBSERVATION_TYPES",
    "TOO_DEFAULTS",
    "Cadence",
    "Configuration",
    "Constraints",
    "InstrumentConfig",
    "Request",
    "RequestGroup",
    "Status",
    "Target",
    "TooSpec",
    "Window",
    "from_legacy_csv",
    "load_request_groups",
    "ocs_state",
    "upgrade_legacy",
]


class Status(StrEnum):
    PENDING = "PENDING"
    ATTEMPTED = "ATTEMPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"

    @property
    def schedulable(self) -> bool:
        return self in (Status.PENDING, Status.ATTEMPTED)

    @property
    def terminal(self) -> bool:
        return not self.schedulable


def ocs_state(statuses: list[Status]) -> str:
    """The OCS ``state`` of a request (or group) whose configurations have
    ``statuses``: ``PENDING`` while any can still be scheduled, ``COMPLETED``
    when all completed, else the first of ``CANCELED``,
    ``FAILURE_LIMIT_REACHED`` and ``WINDOW_EXPIRED`` that applies."""
    if not statuses or any(s.schedulable for s in statuses):
        return "PENDING"
    if all(s is Status.COMPLETED for s in statuses):
        return "COMPLETED"
    for status, state in (
        (Status.CANCELED, "CANCELED"),
        (Status.FAILED, "FAILURE_LIMIT_REACHED"),
        (Status.EXPIRED, "WINDOW_EXPIRED"),
    ):
        if status in statuses:
            return state
    return "COMPLETED"  # pragma: no cover


#: ToO interrupt policies, weakest first (design.md 3.7.1):
#: "next" -- after the current observation; "now-after-readout" -- at the
#: next exposure boundary; "now" -- abort the current exposure.
INTERRUPT_POLICIES = ("next", "now-after-readout", "now")

#: The OCS observation types robofast takes, and for the two ToO types their
#: default interrupt policy and priority class (``extra_params.too``
#: overrides them). ``DIRECT`` and ``REAL_TIME`` are not supported.
OBSERVATION_TYPES = ("NORMAL", "RAPID_RESPONSE", "TIME_CRITICAL")
TOO_DEFAULTS: dict[str, tuple[str, int]] = {
    "RAPID_RESPONSE": ("now-after-readout", 2),
    "TIME_CRITICAL": ("next", 1),
}


def _time(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        t = value
    else:
        try:
            t = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{what}: not an ISO 8601 time: {value!r}") from exc
    # The OCS reads a time without a zone as UTC.
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _number(value: Any, what: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what}: expected a number, got {value!r}")
    x = float(value)
    if not (lo <= x <= hi):  # also rejects NaN
        raise ValueError(f"{what}: {value!r} is outside [{lo:g}, {hi:g}]")
    return x


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what}: expected a positive integer, got {value!r}")
    return value


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what}: expected true or false")
    return value


def _check_keys(d: Any, allowed: set[str], required: set[str], what: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ValueError(f"{what}: expected an object, got {type(d).__name__}")
    unknown = set(d) - allowed - {"_note"}
    if unknown:
        raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
    missing = required - set(d)
    if missing:
        raise ValueError(f"{what}: missing {sorted(missing)}")
    return d


def _extra(d: dict[str, Any], allowed: set[str], what: str) -> dict[str, Any]:
    """``d["extra_params"]``, checked against robofast's keys for that level."""
    return _check_keys(d.get("extra_params") or {}, allowed, set(), f"{what}.extra_params")


def _unsupported(d: dict[str, Any], keys: dict[str, tuple[Any, ...]], what: str) -> None:
    """OCS fields robofast accepts only at their 'off' values."""
    for k, ok in keys.items():
        if k in d and d[k] not in ok:
            raise ValueError(f"{what}.{k}: {d[k]!r} is not supported by robofast")


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


@dataclass
class Target:
    """A sidereal (OCS ``ICRS``) target: ``ra`` and ``dec`` in degrees at
    ``epoch``.

    The astrometry after ``dec`` is optional. Scheduling ignores it; a
    spectrum's barycentric correction (:mod:`robofast_core.spectra`) uses it:
    ``proper_motion_ra`` (mu_alpha* = mu_alpha cos(dec)) and
    ``proper_motion_dec`` in mas/yr, ``epoch`` (Julian year of the
    coordinates; default 2000.0), ``parallax`` in mas and, a robofast
    extension, ``extra_params.radial_velocity`` (systemic) in km/s. Python
    keeps the short names (``pm_ra``, ``pm_dec``, ``rv``) that
    :mod:`robofast_core.spectra` reads, and ``ra_hours``/``dec_deg`` for the
    scheduler.
    """

    name: str
    ra: float
    dec: float
    pm_ra: float | None = None
    pm_dec: float | None = None
    epoch: float | None = None
    parallax: float | None = None
    rv: float | None = None

    #: Python name: (JSON name, range).
    _ASTROMETRY: ClassVar[dict[str, tuple[str, tuple[float, float]]]] = {
        "pm_ra": ("proper_motion_ra", (-1e5, 1e5)),
        "pm_dec": ("proper_motion_dec", (-1e5, 1e5)),
        "epoch": ("epoch", (1800.0, 2200.0)),
        "parallax": ("parallax", (0.0, 1e4)),
    }
    _RV_RANGE: ClassVar[tuple[float, float]] = (-3e5, 3e5)

    @property
    def ra_hours(self) -> float:
        return self.ra / 15.0

    @property
    def dec_deg(self) -> float:
        return self.dec

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Target:
        keys = {"type", "name", "ra", "dec", "extra_params"}
        keys |= {j for j, _ in cls._ASTROMETRY.values()}
        _check_keys(d, keys, {"name", "ra", "dec"}, what)
        ttype = d.get("type") or "ICRS"
        if ttype != "ICRS":
            raise ValueError(f"{what}.type: {ttype!r}; robofast takes only ICRS (sidereal) targets")
        extra = _extra(d, {"radial_velocity"}, what)
        kw = {
            py: _number(d[js], f"{what}.{js}", *rng)
            for py, (js, rng) in cls._ASTROMETRY.items()
            if d.get(js) is not None
        }
        if extra.get("radial_velocity") is not None:
            kw["rv"] = _number(
                extra["radial_velocity"], f"{what}.extra_params.radial_velocity", *cls._RV_RANGE
            )
        ra = _number(d["ra"], f"{what}.ra", 0.0, 360.0)
        return cls(
            str(d["name"]),
            0.0 if ra == 360.0 else ra,
            _number(d["dec"], f"{what}.dec", -90.0, 90.0),
            **kw,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "ICRS", "name": self.name, "ra": self.ra, "dec": self.dec}
        for py, (js, _) in self._ASTROMETRY.items():
            if getattr(self, py) is not None:
                d[js] = getattr(self, py)
        if self.rv is not None:
            d["extra_params"] = {"radial_velocity": self.rv}
        return d

    def astrometry(self) -> dict[str, float]:
        """The optional astrometry that is set, by its Python name (for
        :mod:`robofast_core.spectra`)."""
        names = [*self._ASTROMETRY, "rv"]
        return {k: getattr(self, k) for k in names if getattr(self, k) is not None}

    def internal(self) -> dict[str, Any]:
        """The target as the sequencer and :mod:`robofast_core.spectra` read
        it: ``name``, ``ra_hours``, ``dec_deg`` and the astrometry."""
        return {
            "name": self.name,
            "ra_hours": self.ra_hours,
            "dec_deg": self.dec_deg,
            **self.astrometry(),
        }


@dataclass
class Constraints:
    """Per-configuration limits. They can only *tighten* the telescope's and
    site's (the stricter value wins), never loosen them.

    OCS fields: ``max_airmass``, ``min_lunar_distance`` (deg; Python
    ``min_moon_sep``), ``max_lunar_phase`` (illuminated fraction, 0 new to 1
    full). ``max_seeing`` and ``min_transparency`` are not supported. The
    rest are robofast's, in ``extra_params``: ``min_alt``, ``max_alt``,
    ``max_sun_alt`` (deg) and ``min_ha``, ``max_ha`` (hours; negative: east
    of the meridian).
    """

    min_alt: float | None = None
    max_alt: float | None = None
    max_airmass: float | None = None
    min_moon_sep: float | None = None
    max_sun_alt: float | None = None
    min_ha: float | None = None
    max_ha: float | None = None
    max_lunar_phase: float | None = None

    #: Python name: (JSON name, range).
    _OCS: ClassVar[dict[str, tuple[str, tuple[float, float]]]] = {
        "max_airmass": ("max_airmass", (1.0, 100.0)),
        "min_moon_sep": ("min_lunar_distance", (0.0, 180.0)),
        "max_lunar_phase": ("max_lunar_phase", (0.0, 1.0)),
    }
    _EXTRA: ClassVar[dict[str, tuple[float, float]]] = {
        "min_alt": (-90.0, 90.0),
        "max_alt": (-90.0, 90.0),
        "max_sun_alt": (-90.0, 90.0),
        "min_ha": (-12.0, 12.0),
        "max_ha": (-12.0, 12.0),
    }

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Constraints:
        keys = {js for js, _ in cls._OCS.values()}
        keys |= {"max_seeing", "min_transparency", "extra_params"}
        d = _check_keys(d or {}, keys, set(), what)
        _unsupported(d, {"max_seeing": (None,), "min_transparency": (None,)}, what)
        extra = _extra(d, set(cls._EXTRA), what)
        kw = {
            py: _number(d[js], f"{what}.{js}", *rng)
            for py, (js, rng) in cls._OCS.items()
            if d.get(js) is not None
        }
        kw.update(
            {
                k: _number(v, f"{what}.extra_params.{k}", *cls._EXTRA[k])
                for k, v in extra.items()
                if v is not None
            }
        )
        c = cls(**kw)
        if c.min_alt is not None and c.max_alt is not None and c.min_alt >= c.max_alt:
            raise ValueError(f"{what}: min_alt >= max_alt")
        if c.min_ha is not None and c.max_ha is not None and c.min_ha >= c.max_ha:
            raise ValueError(f"{what}: min_ha >= max_ha")
        return c

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            js: getattr(self, py)
            for py, (js, _) in self._OCS.items()
            if getattr(self, py) is not None
        }
        extra = {k: getattr(self, k) for k in self._EXTRA if getattr(self, k) is not None}
        if extra:
            d["extra_params"] = extra
        return d


@dataclass
class InstrumentConfig:
    """OCS ``exposure_time`` (s; Python ``exptime``), ``exposure_count``
    (``count``), ``optical_elements.filter`` (``filter``) and readout
    ``mode``; robofast's ``extra_params``: ``port``, ``iodine``,
    ``target_counts`` and ``light``."""

    exptime: float
    count: int = 1
    #: Filter name, selected on the telescope's filter wheel by the
    #: sequencer; a telescope whose limits list ``filters`` without it is
    #: not eligible.
    filter: str | None = None
    #: False for darks and biases (shutter closed, telescope not moved); set
    #: by a DARK or BIAS configuration, or per instrument config with
    #: ``extra_params.light``.
    light: bool = True
    #: M3 port, selected by the sequencer through the telescope's port
    #: action; only telescopes whose limits list it in ``ports`` are eligible.
    port: str | None = None
    #: SPECTRUM only: the iodine cell in the beam (None: the spectrograph
    #: service's default), and the exposure-meter counts that end the
    #: exposure (``exptime`` is then the maximum; None: timed).
    iodine: bool | None = None
    target_counts: float | None = None
    #: The OCS readout mode (kept, not acted on).
    mode: str = ""

    EXTRA: ClassVar[set[str]] = {"port", "iodine", "target_counts", "light"}

    @classmethod
    def from_dict(cls, d: Any, what: str) -> InstrumentConfig:
        keys = {"exposure_time", "exposure_count", "optical_elements", "mode"}
        keys |= {"rotator_mode", "rois", "extra_params"}
        _check_keys(d, keys, {"exposure_time"}, what)
        _unsupported(d, {"rotator_mode": (None, ""), "rois": (None, [])}, what)
        extra = _extra(d, cls.EXTRA, what)
        oe = _check_keys(
            d.get("optical_elements") or {}, {"filter"}, set(), f"{what}.optical_elements"
        )
        iodine = extra.get("iodine")
        if iodine is not None:
            _flag(iodine, f"{what}.extra_params.iodine")
        counts = extra.get("target_counts")
        mode = d.get("mode")
        return cls(
            _number(d["exposure_time"], f"{what}.exposure_time", 0.0, 86400.0),
            _count(d.get("exposure_count", 1), f"{what}.exposure_count"),
            _text(oe.get("filter")),
            _flag(extra.get("light", True), f"{what}.extra_params.light"),
            _text(extra.get("port")),
            iodine,
            None
            if counts is None
            else _number(counts, f"{what}.extra_params.target_counts", 1.0, 1e15),
            "" if mode is None else str(mode),
        )

    def to_dict(self, *, light: bool = True) -> dict[str, Any]:
        """``light``: the configuration type's default (False for DARK and
        BIAS); ``extra_params.light`` is written only where it differs."""
        d: dict[str, Any] = {"exposure_time": self.exptime, "exposure_count": self.count}
        d["optical_elements"] = {} if self.filter is None else {"filter": self.filter}
        if self.mode:
            d["mode"] = self.mode
        extra: dict[str, Any] = {}
        if self.light != light:
            extra["light"] = self.light
        if self.port is not None:
            extra["port"] = self.port
        if self.iodine is not None:
            extra["iodine"] = self.iodine
        if self.target_counts is not None:
            extra["target_counts"] = self.target_counts
        if extra:
            d["extra_params"] = extra
        return d


@dataclass
class Configuration:
    id: str
    target: Target
    #: OCS ``instrument_type``; the telescope must list it among its
    #: instruments.
    instrument: str
    instrument_configs: list[InstrumentConfig]
    constraints: Constraints = field(default_factory=Constraints)
    status: Status = Status.PENDING
    #: Failed attempts so far (interruptions do not count).
    failures: int = 0
    #: ``extra_params.allow_partial``: if interrupted (or failed) part-way,
    #: keep the frames taken and take only the rest; otherwise the
    #: configuration is re-taken whole.
    allow_partial: bool = False
    #: ``guiding_config.mode`` ``ON``: guide on the target while exposing
    #: (light frames; docs/guider.md).
    guide: bool = False
    #: ``EXPOSE`` (take the instrument configs' frames), ``DARK`` and
    #: ``BIAS`` (the same, shutter closed), ``AUTO_FOCUS`` (focus the
    #: telescope on the target, with the instrument config's filter and
    #: port; its exposure_time, if positive, overrides the autofocus exposure
    #: time), as in the LCO OCS; or ``SPECTRUM``: fiber spectra
    #: (``exposure_count`` per instrument config) with the enclosure's
    #: spectrograph, fed by every telescope of the request that is free when
    #: it starts (docs/rv_night.md). A SPECTRUM is always guided, its
    #: instrument_type is ``spectrograph`` and its instrument configs are
    #: light, with optional ``iodine`` and ``target_counts``; ``filter`` and
    #: ``port`` are not allowed (the fiber has its own port, selected by the
    #: guider config).
    type: str = "EXPOSE"

    TYPES: ClassVar[tuple[str, ...]] = ("EXPOSE", "DARK", "BIAS", "AUTO_FOCUS", "SPECTRUM")
    GUIDE_MODES: ClassVar[tuple[str, ...]] = ("", "ON", "OFF")

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Configuration:
        keys = {"id", "type", "instrument_type", "target", "instrument_configs", "constraints"}
        keys |= {"acquisition_config", "guiding_config", "repeat_duration", "extra_params"}
        keys |= {"status", "failures"}
        _check_keys(d, keys, {"id", "target", "instrument_configs"}, what)
        _unsupported(d, {"repeat_duration": (None,)}, what)
        extra = _extra(d, {"allow_partial"}, what)
        allow_partial = _flag(
            extra.get("allow_partial", False), f"{what}.extra_params.allow_partial"
        )
        guiding = _check_keys(
            d.get("guiding_config") or {},
            {"mode", "optional", "exposure_time", "optical_elements", "extra_params"},
            set(),
            f"{what}.guiding_config",
        )
        gmode = guiding.get("mode") or ""
        if gmode not in cls.GUIDE_MODES:
            raise ValueError(
                f"{what}.guiding_config.mode: {gmode!r}; expected one of {cls.GUIDE_MODES[1:]}"
            )
        guide = gmode == "ON"
        # Acquisition is the telescope's (its pointing model and the guider's
        # acquisition); the OCS block is accepted and not used.
        _check_keys(
            d.get("acquisition_config") or {},
            {"mode", "exposure_time", "extra_params"},
            set(),
            f"{what}.acquisition_config",
        )
        ics = d["instrument_configs"]
        if not isinstance(ics, list) or not ics:
            raise ValueError(f"{what}.instrument_configs: expected a non-empty list")
        try:
            status = Status(d.get("status", "PENDING"))
        except ValueError as exc:
            raise ValueError(f"{what}.status: {d.get('status')!r}") from exc
        ctype = d.get("type") or "EXPOSE"
        if ctype not in cls.TYPES:
            raise ValueError(f"{what}.type: {ctype!r}; expected one of {cls.TYPES}")
        parsed = [
            InstrumentConfig.from_dict(ic, f"{what}.instrument_configs[{i}]")
            for i, ic in enumerate(ics)
        ]
        for i, ic in enumerate(parsed):
            if ctype in ("DARK", "BIAS"):
                if ic.light and "light" in (ics[i].get("extra_params") or {}):
                    raise ValueError(f"{what}.instrument_configs[{i}]: a {ctype} is not light")
                ic.light = False
            elif ctype != "EXPOSE" and not ic.light:
                raise ValueError(f"{what}.instrument_configs[{i}]: a {ctype} frame is light")
        if ctype == "AUTO_FOCUS" and len(parsed) != 1:
            raise ValueError(f"{what}: an AUTO_FOCUS needs exactly one light instrument config")
        default = "spectrograph" if ctype == "SPECTRUM" else "imager"
        instrument = str(d.get("instrument_type") or default)
        if ctype == "SPECTRUM":
            if instrument != "spectrograph":
                raise ValueError(f"{what}: a SPECTRUM's instrument_type is 'spectrograph'")
            if gmode == "OFF":
                raise ValueError(f"{what}: a SPECTRUM is always guided")
            guide = True
            for i, ic in enumerate(parsed):
                if ic.filter is not None or ic.port is not None:
                    raise ValueError(
                        f"{what}.instrument_configs[{i}]: a SPECTRUM frame has no filter or port"
                    )
        else:
            for i, ic in enumerate(parsed):
                if ic.iodine is not None or ic.target_counts is not None:
                    raise ValueError(
                        f"{what}.instrument_configs[{i}]: iodine and target_counts"
                        " are for SPECTRUM configurations"
                    )
        return cls(
            str(d["id"]),
            Target.from_dict(d["target"], f"{what}.target"),
            instrument,
            parsed,
            Constraints.from_dict(d.get("constraints"), f"{what}.constraints"),
            status,
            int(d.get("failures", 0)),
            allow_partial,
            guide,
            ctype,
        )

    def to_dict(self) -> dict[str, Any]:
        light = self.type not in ("DARK", "BIAS")
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "instrument_type": self.instrument,
            "target": self.target.to_dict(),
            "instrument_configs": [ic.to_dict(light=light) for ic in self.instrument_configs],
            "constraints": self.constraints.to_dict(),
            "guiding_config": {"mode": "ON" if self.guide or self.type == "SPECTRUM" else "OFF"},
            "status": str(self.status),
        }
        if self.failures:
            d["failures"] = self.failures
        if self.allow_partial:
            d["extra_params"] = {"allow_partial": True}
        return d

    @property
    def light(self) -> bool:
        return any(ic.light for ic in self.instrument_configs)


@dataclass
class Window:
    start: datetime
    end: datetime

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Window:
        _check_keys(d, {"start", "end"}, {"start", "end"}, what)
        w = cls(_time(d["start"], f"{what}.start"), _time(d["end"], f"{what}.end"))
        if w.end <= w.start:
            raise ValueError(f"{what}: end is not after start")
        return w

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass
class Cadence:
    """A standing request (``extra_params.cadence``): visit at most
    ``max_per_night`` times a night, at least ``min_interval`` seconds apart
    (start to start)."""

    min_interval: float = 0.0
    max_per_night: int | None = None

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Cadence:
        _check_keys(d, {"min_interval", "max_per_night"}, set(), what)
        n = d.get("max_per_night")
        if n is not None:
            _count(n, f"{what}.max_per_night")
        return cls(_number(d.get("min_interval", 0.0), f"{what}.min_interval", 0.0), n)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"min_interval": self.min_interval}
        if self.max_per_night is not None:
            d["max_per_night"] = self.max_per_night
        return d


@dataclass
class Request:
    id: str
    configurations: list[Configuration]
    #: When it may be observed (a visit must fit inside one); empty: any time.
    windows: list[Window] = field(default_factory=list)
    #: Telescopes allowed to take it (``location.telescope``, or several in
    #: ``extra_params.telescopes``: a SPECTRUM's fiber feeds); None: any
    #: with the instrument.
    telescopes: list[str] | None = None
    cadence: Cadence | None = None
    #: ``extra_params.max_attempts``: a configuration fails for good after
    #: this many failed attempts.
    max_attempts: int = 3
    #: The rest of the OCS ``location`` (``telescope_class``, ``site``,
    #: ``enclosure``): kept; the request API picks the enclosure by it.
    location: dict[str, str] = field(default_factory=dict)
    observation_note: str = ""
    #: OCS ``optimization_type`` (TIME or AIRMASS) and
    #: ``acceptability_threshold``: kept, not used by the dispatch scheduler.
    optimization_type: str = "TIME"
    acceptability_threshold: float | None = None
    #: Set by the group (not part of the request's JSON).
    group: RequestGroup | None = field(default=None, repr=False, compare=False)

    LOCATION: ClassVar[set[str]] = {"telescope_class", "site", "enclosure", "telescope"}

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Request:
        keys = {"id", "configurations", "windows", "location", "observation_note"}
        keys |= {"optimization_type", "acceptability_threshold", "configuration_repeats"}
        keys |= {"extra_params", "state", "cadence"}
        _check_keys(d, keys, {"id", "configurations"}, what)
        if d.get("cadence") is not None:
            raise ValueError(
                f"{what}.cadence: an OCS cadence (start, end, period, jitter) is expanded by "
                "POST /api/requestgroups/cadence/ before submission; robofast's standing "
                "cadence is extra_params.cadence (min_interval, max_per_night)"
            )
        extra = _extra(d, {"cadence", "max_attempts", "telescopes"}, what)
        loc = _check_keys(d.get("location") or {}, cls.LOCATION, set(), f"{what}.location")
        location = {k: str(v) for k, v in loc.items() if k != "telescope" and v not in (None, "")}
        configs = d["configurations"]
        if not isinstance(configs, list) or not configs:
            raise ValueError(f"{what}.configurations: expected a non-empty list")
        repeats = d.get("configuration_repeats")
        repeats = 1 if repeats is None else _count(repeats, f"{what}.configuration_repeats")
        parsed = [
            Configuration.from_dict(c, f"{what}.configurations[{i}]") for i, c in enumerate(configs)
        ]
        # OCS configuration_repeats: the configurations, in order, n times
        # over (nodding); stored expanded, the copies' ids suffixed .2, .3...
        for k in range(2, repeats + 1):
            for c in parsed[: len(configs)]:
                again = copy.deepcopy(c)
                again.id = f"{c.id}.{k}"
                parsed.append(again)
        tels = extra.get("telescopes")
        if tels is not None and (
            not isinstance(tels, list) or not tels or not all(isinstance(t, str) for t in tels)
        ):
            raise ValueError(f"{what}.extra_params.telescopes: expected a non-empty list of names")
        if loc.get("telescope") not in (None, ""):
            if tels is not None:
                raise ValueError(
                    f"{what}: give location.telescope or extra_params.telescopes, not both"
                )
            tels = [str(loc["telescope"])]
        opt = d.get("optimization_type") or "TIME"
        if opt not in ("TIME", "AIRMASS"):
            raise ValueError(f"{what}.optimization_type: {opt!r}; expected TIME or AIRMASS")
        thr = d.get("acceptability_threshold")
        note = d.get("observation_note") or ""
        return cls(
            str(d["id"]),
            parsed,
            [
                Window.from_dict(w, f"{what}.windows[{i}]")
                for i, w in enumerate(d.get("windows") or [])
            ],
            list(tels) if tels is not None else None,
            None
            if extra.get("cadence") is None
            else Cadence.from_dict(extra["cadence"], f"{what}.extra_params.cadence"),
            _count(extra.get("max_attempts", 3), f"{what}.extra_params.max_attempts"),
            location,
            str(note),
            opt,
            None if thr is None else _number(thr, f"{what}.acceptability_threshold", 0.0, 100.0),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "configurations": [c.to_dict() for c in self.configurations],
            "windows": [w.to_dict() for w in self.windows],
        }
        location = dict(self.location)
        extra: dict[str, Any] = {}
        if self.telescopes is not None and len(self.telescopes) == 1:
            location["telescope"] = self.telescopes[0]
        elif self.telescopes is not None:
            extra["telescopes"] = list(self.telescopes)
        if location:
            d["location"] = location
        if self.observation_note:
            d["observation_note"] = self.observation_note
        if self.optimization_type != "TIME":
            d["optimization_type"] = self.optimization_type
        if self.acceptability_threshold is not None:
            d["acceptability_threshold"] = self.acceptability_threshold
        if self.cadence is not None:
            extra["cadence"] = self.cadence.to_dict()
        if self.max_attempts != 3:
            extra["max_attempts"] = self.max_attempts
        if extra:
            d["extra_params"] = extra
        return d

    @property
    def state(self) -> str:
        """The OCS state (:func:`ocs_state`)."""
        return ocs_state([c.status for c in self.configurations])

    @property
    def standing(self) -> bool:
        return self.cadence is not None

    @property
    def done(self) -> bool:
        """Nothing left to schedule: every configuration is terminal."""
        return all(c.status.terminal for c in self.configurations)

    def fits(self, start: datetime, end: datetime) -> bool:
        """Whether ``[start, end]`` lies inside one window (always, if none)."""
        return not self.windows or any(w.start <= start and end <= w.end for w in self.windows)

    def window_end(self, when: datetime) -> datetime | None:
        """End of the window containing ``when`` (None: no windows, or none
        contains it)."""
        for w in self.windows:
            if w.start <= when < w.end:
                return w.end
        return None

    def windows_over(self, when: datetime) -> bool:
        return bool(self.windows) and all(w.end <= when for w in self.windows)


@dataclass
class TooSpec:
    """What makes a request group a target of opportunity (design.md
    3.7.1): its ``observation_type`` (RAPID_RESPONSE or TIME_CRITICAL) and
    ``extra_params.too``, whose fields default from the type
    (:data:`TOO_DEFAULTS`) and the windows."""

    #: Latest start time; after it, the ToO is EXPIRED if it never started.
    #: Default: the end of the group's last window.
    deadline: datetime
    #: Interrupt policy, one of :data:`INTERRUPT_POLICIES`.
    policy: str = "next"
    #: Ranks above normal requests (class 0); a ToO interrupts only an
    #: observation of a lower class.
    priority_class: int = 1

    @classmethod
    def from_dict(
        cls, d: Any, what: str, *, observation_type: str, last_window: datetime | None
    ) -> TooSpec:
        d = _check_keys(d or {}, {"deadline", "policy", "priority_class"}, set(), what)
        policy0, class0 = TOO_DEFAULTS[observation_type]
        policy = d.get("policy", policy0)
        if policy not in INTERRUPT_POLICIES:
            raise ValueError(f"{what}.policy: {policy!r}; expected one of {INTERRUPT_POLICIES}")
        pc = d.get("priority_class", class0)
        if isinstance(pc, bool) or not isinstance(pc, int) or pc < 1:
            raise ValueError(f"{what}.priority_class: expected an integer >= 1, got {pc!r}")
        if d.get("deadline") is not None:
            deadline = _time(d["deadline"], f"{what}.deadline")
        elif last_window is not None:
            deadline = last_window
        else:
            raise ValueError(f"{what}.deadline: required when no request has windows")
        return cls(deadline, policy, pc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadline": self.deadline.isoformat(),
            "policy": self.policy,
            "priority_class": self.priority_class,
        }


@dataclass
class RequestGroup:
    id: str
    requests: list[Request]
    name: str = ""
    proposal: str = ""
    #: OCS ``ipp_value``: the dispatch score's base (design.md 3.7; any
    #: positive scale).
    priority: float = 1.0
    too: TooSpec | None = None
    #: OCS ``created``: when the request API (or the scheduler) took it.
    submitted: datetime | None = None
    #: The request-API client that submitted it (its token's name); empty
    #: for groups from a request file or the drop box.
    submitter: str = ""
    #: OCS ``observation_type``: NORMAL, or RAPID_RESPONSE / TIME_CRITICAL
    #: for a ToO. Empty: derived from ``too``.
    observation_type: str = ""
    #: OCS ``operator``: SINGLE (one request) or MANY. Empty: derived.
    operator: str = ""

    def __post_init__(self) -> None:
        for r in self.requests:
            r.group = self

    @property
    def ipp_value(self) -> float:
        return self.priority

    @classmethod
    def from_dict(cls, d: Any, what: str | None = None) -> RequestGroup:
        what = what or f"request group {d.get('id') if isinstance(d, dict) else d!r}"
        d = upgrade_legacy(d, what)
        keys = {"id", "name", "proposal", "ipp_value", "operator", "observation_type"}
        keys |= {"requests", "extra_params", "submitter", "created", "state"}
        _check_keys(d, keys, {"id", "requests"}, what)
        extra = _extra(d, {"too"}, what)
        reqs = d["requests"]
        if not isinstance(reqs, list) or not reqs:
            raise ValueError(f"{what}.requests: expected a non-empty list")
        operator = d.get("operator") or ("SINGLE" if len(reqs) == 1 else "MANY")
        if operator not in ("SINGLE", "MANY"):
            raise ValueError(f"{what}.operator: {operator!r}; expected SINGLE or MANY")
        if operator == "SINGLE" and len(reqs) != 1:
            raise ValueError(f"{what}.operator: SINGLE takes exactly one request")
        otype = d.get("observation_type") or "NORMAL"
        if otype not in OBSERVATION_TYPES:
            raise ValueError(
                f"{what}.observation_type: {otype!r}; robofast takes {OBSERVATION_TYPES}"
            )
        requests = [Request.from_dict(r, f"{what}.requests[{i}]") for i, r in enumerate(reqs)]
        too = None
        if otype in TOO_DEFAULTS:
            ends = [w.end for r in requests for w in r.windows]
            too = TooSpec.from_dict(
                extra.get("too"),
                f"{what}.extra_params.too",
                observation_type=otype,
                last_window=max(ends) if ends else None,
            )
        elif extra.get("too") is not None:
            raise ValueError(
                f"{what}.extra_params.too: only for observation_type RAPID_RESPONSE or "
                "TIME_CRITICAL"
            )
        group = cls(
            str(d["id"]),
            requests,
            str(d.get("name") or ""),
            str(d.get("proposal") or ""),
            _number(d.get("ipp_value", 1.0), f"{what}.ipp_value", 0.0),
            too,
            None if d.get("created") is None else _time(d["created"], f"{what}.created"),
            str(d.get("submitter") or ""),
            otype,
            operator,
        )
        group.validate(what)
        return group

    def validate(self, what: str) -> None:
        seen: set[str] = set()
        for r in self.requests:
            if r.id in seen:
                raise ValueError(f"{what}: duplicate request id {r.id!r}")
            seen.add(r.id)
            for c in r.configurations:
                if c.id in seen:
                    raise ValueError(f"{what}: duplicate configuration id {c.id!r}")
                seen.add(c.id)
            if self.too is not None and r.cadence is not None:
                raise ValueError(f"{what}: a ToO request cannot have a cadence")

    @property
    def ocs_observation_type(self) -> str:
        """``observation_type`` as written: NORMAL without ``too``; with one,
        the given ToO type, else the one its policy suggests."""
        if self.too is None:
            return "NORMAL"
        if self.observation_type in TOO_DEFAULTS:
            return self.observation_type
        return "TIME_CRITICAL" if self.too.policy == "next" else "RAPID_RESPONSE"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "proposal": self.proposal,
            "ipp_value": self.priority,
            "operator": self.operator or ("SINGLE" if len(self.requests) == 1 else "MANY"),
            "observation_type": self.ocs_observation_type,
            "state": self.state,
        }
        if self.too is not None:
            d["extra_params"] = {"too": self.too.to_dict()}
        if self.submitted is not None:
            d["created"] = self.submitted.isoformat()
        if self.submitter:
            d["submitter"] = self.submitter
        d["requests"] = [r.to_dict() for r in self.requests]
        return d

    @property
    def state(self) -> str:
        """The OCS state of the group (:func:`ocs_state` over every
        configuration)."""
        return ocs_state([c.status for c in self.configurations()])

    @property
    def priority_class(self) -> int:
        return 0 if self.too is None else self.too.priority_class

    def configurations(self) -> list[Configuration]:
        return [c for r in self.requests for c in r.configurations]


# -- the previous robofast JSON -------------------------------------------------------------

_LEGACY_GROUP = {"priority", "too", "submitted"}
_LEGACY_REQUEST = {"telescopes", "max_attempts"}
_LEGACY_CONFIG = {"instrument", "allow_partial", "guide"}
_LEGACY_TARGET = {"ra_hours", "dec_deg", "pm_ra", "pm_dec", "rv"}
_LEGACY_IC = {"exptime", "count", "filter", "port", "light", "iodine", "target_counts"}
_LEGACY_CONSTRAINTS = {"min_alt", "max_alt", "min_moon_sep", "max_sun_alt", "min_ha", "max_ha"}
_warned = False


def _move(d: dict[str, Any], old: str, new: str) -> None:
    if old in d:
        d[new] = d.pop(old)


def _sub(d: dict[str, Any], key: str) -> dict[str, Any]:
    sub = d.get(key)
    if not isinstance(sub, dict):
        sub = d[key] = {}
    return sub


def _robofast_cadence(c: Any) -> bool:
    """A request ``cadence`` in robofast's previous shape (not the OCS's)."""
    return isinstance(c, dict) and {"start", "end", "period", "jitter"}.isdisjoint(c)


def upgrade_legacy(d: Any, what: str = "request group") -> Any:
    """Rewrite a request group in robofast's previous JSON (``priority``,
    ``too``, ``telescopes``, ``cadence``, ``instrument``, ``ra_hours``,
    ``exptime``, ...) into the OCS shape; a group already in it is returned
    as is. Only the old keys are moved, so a mixture also loads. Kept for one
    release (docs/request_language.md); logs a deprecation warning once."""
    global _warned
    if not isinstance(d, dict) or not _is_legacy(d):
        return d
    if not _warned:
        log.warning(
            "%s: robofast's previous request JSON is deprecated; it is converted to the OCS "
            "shape (docs/request_language.md) and will stop loading in the next release",
            what,
        )
        _warned = True
    d = copy.deepcopy(d)
    _move(d, "priority", "ipp_value")
    _move(d, "submitted", "created")
    too = d.pop("too", None)
    if too is not None:
        _sub(d, "extra_params")["too"] = too
        if not d.get("observation_type"):
            policy = too.get("policy", "next") if isinstance(too, dict) else "next"
            d["observation_type"] = "TIME_CRITICAL" if policy == "next" else "RAPID_RESPONSE"
    for r in d.get("requests") or []:
        if not isinstance(r, dict):
            continue
        if "telescopes" in r:
            tels = r.pop("telescopes")
            if tels is not None:
                _sub(r, "extra_params")["telescopes"] = tels
        if _robofast_cadence(r.get("cadence")):
            _sub(r, "extra_params")["cadence"] = r.pop("cadence")
        if "max_attempts" in r:
            _sub(r, "extra_params")["max_attempts"] = r.pop("max_attempts")
        for c in r.get("configurations") or []:
            if isinstance(c, dict):
                _upgrade_configuration(c)
    return d


def _upgrade_configuration(c: dict[str, Any]) -> None:
    _move(c, "instrument", "instrument_type")
    if "allow_partial" in c:
        _sub(c, "extra_params")["allow_partial"] = c.pop("allow_partial")
    if "guide" in c:
        guide = c.pop("guide")
        if not isinstance(guide, bool):
            raise ValueError(f"configuration {c.get('id')!r}.guide: expected true or false")
        _sub(c, "guiding_config")["mode"] = "ON" if guide else "OFF"
    t = c.get("target")
    if isinstance(t, dict):
        if "ra_hours" in t:
            ra = t.pop("ra_hours")
            number = isinstance(ra, int | float) and not isinstance(ra, bool)
            t["ra"] = ra * 15.0 if number else ra
        _move(t, "dec_deg", "dec")
        _move(t, "pm_ra", "proper_motion_ra")
        _move(t, "pm_dec", "proper_motion_dec")
        if "rv" in t:
            _sub(t, "extra_params")["radial_velocity"] = t.pop("rv")
    for ic in c.get("instrument_configs") or []:
        if not isinstance(ic, dict):
            continue
        _move(ic, "exptime", "exposure_time")
        _move(ic, "count", "exposure_count")
        if "filter" in ic:
            _sub(ic, "optical_elements")["filter"] = ic.pop("filter")
        for k in ("port", "light", "iodine", "target_counts"):
            if k in ic:
                _sub(ic, "extra_params")[k] = ic.pop(k)
    k = c.get("constraints")
    if isinstance(k, dict):
        _move(k, "min_moon_sep", "min_lunar_distance")
        for key in ("min_alt", "max_alt", "max_sun_alt", "min_ha", "max_ha"):
            if key in k:
                _sub(k, "extra_params")[key] = k.pop(key)


def _is_legacy(d: dict[str, Any]) -> bool:
    if _LEGACY_GROUP & set(d):
        return True
    for r in d.get("requests") or []:
        if not isinstance(r, dict):
            continue
        if _LEGACY_REQUEST & set(r) or _robofast_cadence(r.get("cadence")):
            return True
        for c in r.get("configurations") or []:
            if not isinstance(c, dict):
                continue
            if _LEGACY_CONFIG & set(c):
                return True
            if isinstance(c.get("target"), dict) and _LEGACY_TARGET & set(c["target"]):
                return True
            k = c.get("constraints")
            if isinstance(k, dict) and _LEGACY_CONSTRAINTS & set(k):
                return True
            for ic in c.get("instrument_configs") or []:
                if isinstance(ic, dict) and _LEGACY_IC & set(ic):
                    return True
    return False


def load_request_groups(source: str | Path | list[Any]) -> list[RequestGroup]:
    """Request groups from a JSON file (a list of groups, or ``{"groups":
    [...]}``) or an already-parsed list. Ids must be unique across groups."""
    data = json.loads(Path(source).read_text()) if not isinstance(source, list) else source
    if isinstance(data, dict):
        data = data.get("groups")
    if not isinstance(data, list):
        raise ValueError("expected a list of request groups (or {'groups': [...]})")
    groups = [RequestGroup.from_dict(g, f"groups[{i}]") for i, g in enumerate(data)]
    seen: set[str] = set()
    for g in groups:
        ids = [g.id, *(r.id for r in g.requests), *(c.id for c in g.configurations())]
        for i in ids:
            if i in seen:
                raise ValueError(f"duplicate id {i!r} (group {g.id!r})")
            seen.add(i)
    return groups


def from_legacy_csv(path: str | Path, *, instrument: str = "spectrograph") -> list[RequestGroup]:
    """Convert a legacy MINERVA target spreadsheet export (``targets.csv``,
    as read by minerva-control's ``targetlist.mkdict``) to request groups.

    Columns used: ``name``, ``ra`` (hours), ``dec`` (deg), ``exptime`` (s),
    ``priority``, ``active`` (1 to include), and optionally ``seplimit``
    (s between visits; legacy default 3 h), ``maxobs`` (visits per night;
    legacy default 2) and ``num`` (exposures; default 1). A header's
    parenthesised unit, e.g. ``ra (hours)``, is ignored, as the legacy reader
    did. Each active row becomes a standing request with that cadence
    (``extra_params.cadence``) and ``ipp_value`` the legacy priority; with
    the default instrument its configuration is a ``SPECTRUM`` (fed by every
    free telescope, as legacy's ``doSpectra`` with its ``tele_list``).
    """
    groups = []
    with Path(path).open(newline="") as f:
        reader = csv.DictReader(f)
        for n, raw in enumerate(reader, start=2):
            row = {k.split("(")[0].strip(): (v or "").strip() for k, v in raw.items() if k}
            what = f"{path}:{n}"
            try:
                if row.get("active", "1") not in ("1", "1.0", "True", "true"):
                    continue
                name = row["name"]
                seplimit = float(row["seplimit"]) if row.get("seplimit") else 3 * 3600.0
                maxobs = int(float(row["maxobs"])) if row.get("maxobs") else 2
                count = int(float(row["num"])) if row.get("num") else 1
                key = name.replace(" ", "_")
                spectrum = instrument == "spectrograph"
                config = {
                    "id": f"{key}/c",
                    "type": "SPECTRUM" if spectrum else "EXPOSE",
                    "instrument_type": instrument,
                    "target": {
                        "type": "ICRS",
                        "name": name,
                        "ra": float(row["ra"]) * 15.0,
                        "dec": float(row["dec"]),
                    },
                    "instrument_configs": [
                        {"exposure_time": float(row["exptime"]), "exposure_count": count}
                    ],
                }
                cadence = {"min_interval": seplimit, "max_per_night": maxobs}
                groups.append(
                    RequestGroup.from_dict(
                        {
                            "id": key,
                            "name": name,
                            "proposal": "legacy",
                            "ipp_value": float(row["priority"]),
                            "operator": "SINGLE",
                            "observation_type": "NORMAL",
                            "requests": [
                                {
                                    "id": f"{key}/r",
                                    "extra_params": {"cadence": cadence},
                                    "configurations": [config],
                                }
                            ],
                        },
                        what,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{what}: {exc}") from exc
    return groups
