"""Observing requests, modelled on the LCO OCS (design.md 3.7).

::

    RequestGroup            who asked, how important, ToO or not
      Request               one schedulable unit: windows, telescopes, cadence
        Configuration       one target with one instrument, with a status
          InstrumentConfig  exposure time, count, filter, M3 port

The dispatch scheduler (:mod:`robofast_core.dispatch`) picks a *Request* and
executes its configurations in order, one :class:`InstrumentConfig` per
:class:`~robofast_core.plan.Observation`; a request's configurations taken
together are one *visit*.

Status is per configuration (:class:`Status`):

* ``PENDING`` -- not started, or reset (a standing request after a visit);
* ``ATTEMPTED`` -- started at least once but not completed; still schedulable;
* ``COMPLETED`` -- done (a one-shot request's configuration);
* ``FAILED`` -- failed ``max_attempts`` times (device errors, refused slews;
  an interruption by weather or a ToO does not count);
* ``EXPIRED`` -- its windows (or a ToO's deadline) passed before it completed;
* ``CANCELED`` -- withdrawn.

A request with a ``cadence`` is *standing*: after each completed visit its
configurations go back to ``PENDING``, and the scheduler enforces
``min_interval`` between visits and ``max_per_night``. Without one it is a
one-shot request.

A request group with a ``too`` entry is a target of opportunity (design.md
3.7.1): it ranks above every normal request, has a deadline (the latest
start) and an interrupt policy.

Everything round-trips through JSON (:meth:`RequestGroup.from_dict`,
:meth:`RequestGroup.to_dict`); times are ISO 8601 with a time zone.
Validation errors are :class:`ValueError` naming the offending entry.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "INTERRUPT_POLICIES",
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


#: ToO interrupt policies, weakest first (design.md 3.7.1):
#: "next" -- after the current observation; "now-after-readout" -- at the
#: next exposure boundary; "now" -- abort the current exposure.
INTERRUPT_POLICIES = ("next", "now-after-readout", "now")


def _time(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        t = value
    else:
        try:
            t = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{what}: not an ISO 8601 time: {value!r}") from exc
    if t.tzinfo is None:
        raise ValueError(f"{what}: {value!r} has no time zone (add Z or +00:00)")
    return t


def _number(value: Any, what: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what}: expected a number, got {value!r}")
    x = float(value)
    if not (lo <= x <= hi):  # also rejects NaN
        raise ValueError(f"{what}: {value!r} is outside [{lo:g}, {hi:g}]")
    return x


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


@dataclass
class Target:
    """A sidereal target, ICRS (J2000) coordinates. Proper motion is ignored
    (it does not matter for scheduling)."""

    name: str
    ra_hours: float
    dec_deg: float

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Target:
        _check_keys(d, {"name", "ra_hours", "dec_deg"}, {"name", "ra_hours", "dec_deg"}, what)
        return cls(
            str(d["name"]),
            _number(d["ra_hours"], f"{what}.ra_hours", 0.0, 24.0),
            _number(d["dec_deg"], f"{what}.dec_deg", -90.0, 90.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ra_hours": self.ra_hours, "dec_deg": self.dec_deg}


@dataclass
class Constraints:
    """Per-configuration limits. They can only *tighten* the telescope's and
    site's (the stricter value wins), never loosen them."""

    min_alt: float | None = None
    max_alt: float | None = None
    max_airmass: float | None = None
    min_moon_sep: float | None = None
    max_sun_alt: float | None = None
    #: Hour angle limits, hours (negative: east of the meridian).
    min_ha: float | None = None
    max_ha: float | None = None

    _RANGES: ClassVar[dict[str, tuple[float, float]]] = {
        "min_alt": (-90.0, 90.0),
        "max_alt": (-90.0, 90.0),
        "max_airmass": (1.0, 100.0),
        "min_moon_sep": (0.0, 180.0),
        "max_sun_alt": (-90.0, 90.0),
        "min_ha": (-12.0, 12.0),
        "max_ha": (-12.0, 12.0),
    }

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Constraints:
        d = _check_keys(d or {}, set(cls._RANGES), set(), what)
        kw = {k: _number(v, f"{what}.{k}", *cls._RANGES[k]) for k, v in d.items() if v is not None}
        c = cls(**kw)
        if c.min_alt is not None and c.max_alt is not None and c.min_alt >= c.max_alt:
            raise ValueError(f"{what}: min_alt >= max_alt")
        if c.min_ha is not None and c.max_ha is not None and c.min_ha >= c.max_ha:
            raise ValueError(f"{what}: min_ha >= max_ha")
        return c

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._RANGES if getattr(self, k) is not None}


@dataclass
class InstrumentConfig:
    exptime: float
    count: int = 1
    #: Filter name, selected on the telescope's filter wheel by the
    #: sequencer; a telescope whose limits list ``filters`` without it is
    #: not eligible.
    filter: str | None = None
    #: False for darks and biases (shutter closed, telescope not moved).
    light: bool = True
    #: M3 port, selected by the sequencer through the telescope's port
    #: action; only telescopes whose limits list it in ``ports`` are eligible.
    port: str | None = None

    @classmethod
    def from_dict(cls, d: Any, what: str) -> InstrumentConfig:
        _check_keys(d, {"exptime", "count", "filter", "light", "port"}, {"exptime"}, what)
        count = d.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{what}.count: expected a positive integer, got {count!r}")
        return cls(
            _number(d["exptime"], f"{what}.exptime", 0.0, 86400.0),
            count,
            None if d.get("filter") is None else str(d["filter"]),
            bool(d.get("light", True)),
            None if d.get("port") is None else str(d["port"]),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"exptime": self.exptime, "count": self.count}
        if self.filter is not None:
            d["filter"] = self.filter
        if not self.light:
            d["light"] = False
        if self.port is not None:
            d["port"] = self.port
        return d


@dataclass
class Configuration:
    id: str
    target: Target
    #: Instrument type; the telescope must list it among its instruments.
    instrument: str
    instrument_configs: list[InstrumentConfig]
    constraints: Constraints = field(default_factory=Constraints)
    status: Status = Status.PENDING
    #: Failed attempts so far (interruptions do not count).
    failures: int = 0
    #: If interrupted (or failed) part-way, keep the frames taken and take
    #: only the rest; otherwise the configuration is re-taken whole.
    allow_partial: bool = False
    #: Guide on the target while exposing (light frames; docs/guider.md).
    guide: bool = False
    #: ``EXPOSE`` (take the instrument configs' frames) or ``AUTO_FOCUS``
    #: (focus the telescope on the target, with the instrument config's
    #: filter and port; its exptime, if positive, overrides the autofocus
    #: exposure time). As in the LCO OCS.
    type: str = "EXPOSE"

    TYPES: ClassVar[tuple[str, ...]] = ("EXPOSE", "AUTO_FOCUS")

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Configuration:
        keys = {"id", "target", "instrument", "instrument_configs", "constraints", "status"}
        keys |= {"failures", "allow_partial", "guide", "type"}
        _check_keys(d, keys, {"id", "target", "instrument_configs"}, what)
        allow_partial = d.get("allow_partial", False)
        if not isinstance(allow_partial, bool):
            raise ValueError(f"{what}.allow_partial: expected true or false")
        guide = d.get("guide", False)
        if not isinstance(guide, bool):
            raise ValueError(f"{what}.guide: expected true or false")
        ics = d["instrument_configs"]
        if not isinstance(ics, list) or not ics:
            raise ValueError(f"{what}.instrument_configs: expected a non-empty list")
        try:
            status = Status(d.get("status", "PENDING"))
        except ValueError as exc:
            raise ValueError(f"{what}.status: {d.get('status')!r}") from exc
        ctype = d.get("type", "EXPOSE")
        if ctype not in cls.TYPES:
            raise ValueError(f"{what}.type: {ctype!r}; expected one of {cls.TYPES}")
        if ctype == "AUTO_FOCUS" and (
            len(ics) != 1 or not isinstance(ics[0], dict) or ics[0].get("light", True) is False
        ):
            raise ValueError(f"{what}: an AUTO_FOCUS needs exactly one light instrument config")
        return cls(
            str(d["id"]),
            Target.from_dict(d["target"], f"{what}.target"),
            str(d.get("instrument", "imager")),
            [
                InstrumentConfig.from_dict(ic, f"{what}.instrument_configs[{i}]")
                for i, ic in enumerate(ics)
            ],
            Constraints.from_dict(d.get("constraints"), f"{what}.constraints"),
            status,
            int(d.get("failures", 0)),
            allow_partial,
            guide,
            ctype,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "target": self.target.to_dict(),
            "instrument": self.instrument,
            "instrument_configs": [ic.to_dict() for ic in self.instrument_configs],
            "status": str(self.status),
        }
        if self.constraints.to_dict():
            d["constraints"] = self.constraints.to_dict()
        if self.failures:
            d["failures"] = self.failures
        if self.allow_partial:
            d["allow_partial"] = True
        if self.guide:
            d["guide"] = True
        if self.type != "EXPOSE":
            d["type"] = self.type
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
    """A standing request: visit at most ``max_per_night`` times a night, at
    least ``min_interval`` seconds apart (start to start)."""

    min_interval: float = 0.0
    max_per_night: int | None = None

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Cadence:
        _check_keys(d, {"min_interval", "max_per_night"}, set(), what)
        n = d.get("max_per_night")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            raise ValueError(f"{what}.max_per_night: expected a positive integer, got {n!r}")
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
    #: Telescopes allowed to take it; None: any with the instrument.
    telescopes: list[str] | None = None
    cadence: Cadence | None = None
    #: A configuration fails for good after this many failed attempts.
    max_attempts: int = 3
    #: Set by the group (not part of the request's JSON).
    group: RequestGroup | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: Any, what: str) -> Request:
        keys = {"id", "configurations", "windows", "telescopes", "cadence", "max_attempts"}
        _check_keys(d, keys, {"id", "configurations"}, what)
        configs = d["configurations"]
        if not isinstance(configs, list) or not configs:
            raise ValueError(f"{what}.configurations: expected a non-empty list")
        tels = d.get("telescopes")
        if tels is not None and (
            not isinstance(tels, list) or not tels or not all(isinstance(t, str) for t in tels)
        ):
            raise ValueError(f"{what}.telescopes: expected a non-empty list of names")
        max_attempts = d.get("max_attempts", 3)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"{what}.max_attempts: expected a positive integer")
        return cls(
            str(d["id"]),
            [
                Configuration.from_dict(c, f"{what}.configurations[{i}]")
                for i, c in enumerate(configs)
            ],
            [
                Window.from_dict(w, f"{what}.windows[{i}]")
                for i, w in enumerate(d.get("windows", []))
            ],
            list(tels) if tels is not None else None,
            None
            if d.get("cadence") is None
            else Cadence.from_dict(d["cadence"], f"{what}.cadence"),
            max_attempts,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "configurations": [c.to_dict() for c in self.configurations],
        }
        if self.windows:
            d["windows"] = [w.to_dict() for w in self.windows]
        if self.telescopes is not None:
            d["telescopes"] = list(self.telescopes)
        if self.cadence is not None:
            d["cadence"] = self.cadence.to_dict()
        if self.max_attempts != 3:
            d["max_attempts"] = self.max_attempts
        return d

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
    """What makes a request group a target of opportunity (design.md 3.7.1)."""

    #: Latest start time; after it, the ToO is EXPIRED if it never started.
    deadline: datetime
    #: Interrupt policy, one of :data:`INTERRUPT_POLICIES`.
    policy: str = "next"
    #: Ranks above normal requests (class 0); a ToO interrupts only an
    #: observation of a lower class.
    priority_class: int = 1

    @classmethod
    def from_dict(cls, d: Any, what: str) -> TooSpec:
        _check_keys(d, {"deadline", "policy", "priority_class"}, {"deadline"}, what)
        policy = d.get("policy", "next")
        if policy not in INTERRUPT_POLICIES:
            raise ValueError(f"{what}.policy: {policy!r}; expected one of {INTERRUPT_POLICIES}")
        pc = d.get("priority_class", 1)
        if isinstance(pc, bool) or not isinstance(pc, int) or pc < 1:
            raise ValueError(f"{what}.priority_class: expected an integer >= 1, got {pc!r}")
        return cls(_time(d["deadline"], f"{what}.deadline"), policy, pc)

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
    #: The dispatch score's base (design.md 3.7; any positive scale).
    priority: float = 1.0
    too: TooSpec | None = None
    submitted: datetime | None = None
    #: The request-API client that submitted it (its token's name); empty
    #: for groups from a request file or the drop box.
    submitter: str = ""

    def __post_init__(self) -> None:
        for r in self.requests:
            r.group = self

    @classmethod
    def from_dict(cls, d: Any, what: str | None = None) -> RequestGroup:
        what = what or f"request group {d.get('id') if isinstance(d, dict) else d!r}"
        keys = {"id", "name", "proposal", "priority", "too", "requests", "submitted", "submitter"}
        _check_keys(d, keys, {"id", "requests"}, what)
        reqs = d["requests"]
        if not isinstance(reqs, list) or not reqs:
            raise ValueError(f"{what}.requests: expected a non-empty list")
        group = cls(
            str(d["id"]),
            [Request.from_dict(r, f"{what}.requests[{i}]") for i, r in enumerate(reqs)],
            str(d.get("name", "")),
            str(d.get("proposal", "")),
            _number(d.get("priority", 1.0), f"{what}.priority", 0.0),
            None if d.get("too") is None else TooSpec.from_dict(d["too"], f"{what}.too"),
            None if d.get("submitted") is None else _time(d["submitted"], f"{what}.submitted"),
            str(d.get("submitter", "")),
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id}
        if self.name:
            d["name"] = self.name
        if self.proposal:
            d["proposal"] = self.proposal
        d["priority"] = self.priority
        if self.too is not None:
            d["too"] = self.too.to_dict()
        if self.submitted is not None:
            d["submitted"] = self.submitted.isoformat()
        if self.submitter:
            d["submitter"] = self.submitter
        d["requests"] = [r.to_dict() for r in self.requests]
        return d

    @property
    def priority_class(self) -> int:
        return 0 if self.too is None else self.too.priority_class

    def configurations(self) -> list[Configuration]:
        return [c for r in self.requests for c in r.configurations]


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
    did. Each active row becomes a standing request with that cadence.
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
                groups.append(
                    RequestGroup.from_dict(
                        {
                            "id": key,
                            "name": name,
                            "proposal": "legacy",
                            "priority": float(row["priority"]),
                            "requests": [
                                {
                                    "id": f"{key}/r",
                                    "cadence": {"min_interval": seplimit, "max_per_night": maxobs},
                                    "configurations": [
                                        {
                                            "id": f"{key}/c",
                                            "instrument": instrument,
                                            "target": {
                                                "name": name,
                                                "ra_hours": float(row["ra"]),
                                                "dec_deg": float(row["dec"]),
                                            },
                                            "instrument_configs": [
                                                {"exptime": float(row["exptime"]), "count": count}
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                        what,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{what}: {exc}") from exc
    return groups
