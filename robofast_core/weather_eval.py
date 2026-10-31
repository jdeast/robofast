"""Weather limits: per-source staleness, hysteresis, rain, and the combined verdict.

Each evaluation judges every source afresh against ``now`` (never against a
time frozen at startup):

* **Staleness** is the reading's own measurement time vs ``now``. A stale,
  missing or failed source is *unknown*; its old values are not checked
  (and not served to Alpaca clients). A field with its own measurement time
  (``Reading.field_times``) goes stale on its own: it becomes missing, and a
  limited field that is missing makes the source unknown.
* **Limits** are ``[low, high]`` (inclusive; ``null`` = open-ended) per
  field, with a stricter ``open`` pair and a looser ``close`` pair.
  **Hysteresis** uses the bridge's own previous verdict: while it last said
  "safe" the close limits apply; otherwise (including at startup) the open
  limits do. The supervisor only opens while the bridge says safe, so
  "bridge safe" is the fail-safe stand-in for "enclosure open" and needs no
  extra input (see ``docs/weather_bridge.md``).
* A **group** of fields (e.g. several cloud sensors) is decided by the first
  one in its list with a valid value, as the legacy code did; none valid is
  unknown.
* A **dynamic limit** takes one side of a field's limits from another field
  of the same reading (MINERVA: the FLWO observers' ``sky_limit``), with
  offsets for the open and close limits and a sanity range; outside the
  range, or missing, the static limits apply.
* **Rain** is detected from the *increase* of a cumulative counter within a
  window (a decrease is a counter reset: history restarts there), from a
  rate field above a threshold (``rain_rates``), or from a source that
  flags rain itself. Rain holds the verdict unsafe for ``rain_hold`` seconds
  after the last detection -- if the source *enforces* rain. A source with
  ``enforce_rain`` false (shadow) is evaluated and held the same way, but
  only reported: "would close: rain ..." in the reasons and ``would_close``.

The combiner: unsafe if any source is unsafe (advisory ones included) or rain
is being held; unknown -- which is unsafe -- if a required source is unknown
or every fetched (non-live) source is; safe otherwise.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from robofast_core.weather_sources import Reading

SAFE, UNSAFE, UNKNOWN = "safe", "unsafe", "unknown"

Limit = tuple[float | None, float | None]


def _limit(value: Any, what: str) -> Limit:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{what}: a limit is [low, high] (null for open-ended)")
    lo, hi = (None if v is None else float(v) for v in value)
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"{what}: low {lo} > high {hi}")
    return lo, hi


@dataclass(frozen=True)
class FieldLimits:
    open: Limit
    close: Limit

    @classmethod
    def parse(cls, spec: Mapping[str, Any], what: str) -> FieldLimits:
        extra = set(spec) - {"open", "close"}
        if extra or "open" not in spec:
            raise ValueError(f"{what}: needs 'open' (and optionally 'close'), got {sorted(spec)}")
        open_ = _limit(spec["open"], f"{what}.open")
        close = _limit(spec.get("close", spec["open"]), f"{what}.close")
        # Open limits must be at least as strict as close limits, or the
        # enclosure could open on conditions that would close it at once.
        if open_[0] is None and close[0] is not None:
            raise ValueError(f"{what}: open low limit looser than close")
        if open_[1] is None and close[1] is not None:
            raise ValueError(f"{what}: open high limit looser than close")
        if open_[0] is not None and close[0] is not None and open_[0] < close[0]:
            raise ValueError(f"{what}: open low limit looser than close")
        if open_[1] is not None and close[1] is not None and open_[1] > close[1]:
            raise ValueError(f"{what}: open high limit looser than close")
        return cls(open_, close)

    def violation(self, value: float, use_open: bool) -> str | None:
        lo, hi = self.open if use_open else self.close
        which = "open" if use_open else "close"
        if lo is not None and value < lo:
            return f"{value:g} < {lo:g} ({which} limit)"
        if hi is not None and value > hi:
            return f"{value:g} > {hi:g} ({which} limit)"
        return None


@dataclass(frozen=True)
class RainRule:
    #: Seconds of counter history to look back over.
    window: float
    #: Counter increase within the window that means "raining" (counter units).
    threshold: float

    @classmethod
    def parse(cls, spec: Mapping[str, Any], what: str) -> RainRule:
        extra = set(spec) - {"window", "threshold"}
        if extra:
            raise ValueError(f"{what}: unknown keys {sorted(extra)}")
        rule = cls(float(spec["window"]), float(spec["threshold"]))
        if rule.window <= 0 or rule.threshold <= 0:
            raise ValueError(f"{what}: window and threshold must be > 0")
        return rule


class RainCounter:
    """Rain from the increase of a cumulative counter over a window of data time."""

    def __init__(self, rule: RainRule) -> None:
        self.rule = rule
        self.history: deque[tuple[float, float]] = deque()

    def add(self, when: datetime, value: float) -> str | None:
        """Record a sample; return a note if the counter was reset."""
        t = when.timestamp()
        note = None
        if self.history:
            last_t, last_v = self.history[-1]
            if t <= last_t:
                return None  # the same reading again (or older): nothing new
            if value < last_v:
                note = f"counter reset ({last_v:g} -> {value:g})"
                self.history.clear()
        self.history.append((t, value))
        while self.history and self.history[0][0] < t - self.rule.window:
            self.history.popleft()
        return note

    def increase(self) -> float:
        if not self.history:
            return 0.0
        return self.history[-1][1] - self.history[0][1]

    def rate(self) -> float | None:
        """Counter units per hour over the window, or None without two samples."""
        if len(self.history) < 2:
            return None
        (t0, v0), (t1, v1) = self.history[0], self.history[-1]
        return (v1 - v0) / (t1 - t0) * 3600.0

    def raining(self) -> bool:
        return self.increase() >= self.rule.threshold


@dataclass(frozen=True)
class DynamicLimit:
    """One side of a field's limits, taken from another field's value.

    ``value + open_offset`` is the open limit and ``value + close_offset``
    the close limit, on ``side`` ("high" or "low"). A value outside
    ``range`` (garbage, or a setting nobody should trust) or missing falls
    back to the static limits.
    """

    field: str
    side: str = "high"
    open_offset: float = 0.0
    close_offset: float = 0.0
    range: Limit = (None, None)

    @classmethod
    def parse(cls, spec: Mapping[str, Any], what: str) -> DynamicLimit | None:
        """None if ``enabled`` is false (the default: dynamic limits are opt-in)."""
        spec = dict(spec)
        enabled = bool(spec.pop("enabled", False))
        extra = set(spec) - {"field", "side", "open_offset", "close_offset", "range"}
        if extra or "field" not in spec:
            raise ValueError(f"{what}: needs 'field'; unknown keys {sorted(extra)}")
        dyn = cls(
            field=str(spec["field"]),
            side=str(spec.get("side", "high")),
            open_offset=float(spec.get("open_offset", 0.0)),
            close_offset=float(spec.get("close_offset", 0.0)),
            range=_limit(spec.get("range", [None, None]), f"{what}.range"),
        )
        if dyn.side not in ("high", "low"):
            raise ValueError(f"{what}: side must be 'high' or 'low'")
        # The open limit must be at least as strict as the close limit.
        if (dyn.side == "high" and dyn.open_offset > dyn.close_offset) or (
            dyn.side == "low" and dyn.open_offset < dyn.close_offset
        ):
            raise ValueError(f"{what}: open_offset looser than close_offset")
        return dyn if enabled else None

    def apply(self, static: FieldLimits, value: float | None) -> tuple[FieldLimits, str]:
        """(the limits to use, a note saying which)."""
        lo, hi = self.range
        if value is None or (lo is not None and value < lo) or (hi is not None and value > hi):
            shown = "missing" if value is None else f"{value:g} outside {list(self.range)}"
            return static, f"{self.field} {shown}: static limits"
        o, c = value + self.open_offset, value + self.close_offset
        if self.side == "high":
            limits = FieldLimits((static.open[0], o), (static.close[0], c))
        else:
            limits = FieldLimits((o, static.open[1]), (c, static.close[1]))
        return limits, f"{self.field}={value:g}: limits open {o:g}, close {c:g}"


@dataclass
class SourceSpec:
    """What the evaluator needs to know about one source."""

    name: str
    required: bool = True
    stale_after: float = 300.0
    live: bool = False
    limits: dict[str, FieldLimits] = field(default_factory=dict)
    #: group name -> fields in priority order; the first with a value decides.
    groups: dict[str, list[str]] = field(default_factory=dict)
    rain: dict[str, RainRule] = field(default_factory=dict)
    #: field -> threshold: rain while the field's value exceeds it (e.g. mm/h).
    rain_rates: dict[str, float] = field(default_factory=dict)
    #: False: this source's rain is evaluated and reported, never made unsafe.
    enforce_rain: bool = True
    #: field -> a limit side taken from another field (see DynamicLimit).
    dynamic_limits: dict[str, DynamicLimit] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unlimited = [f for f in self.dynamic_limits if f not in self.limits]
        if unlimited:
            raise ValueError(f"source {self.name}: dynamic limits without static ones: {unlimited}")
        grouped = [f for fields in self.groups.values() for f in fields]
        if len(grouped) != len(set(grouped)):
            raise ValueError(f"source {self.name}: a field is in more than one group")
        missing = [f for f in grouped if f not in self.limits]
        if missing:
            raise ValueError(f"source {self.name}: grouped fields without limits: {missing}")
        clash = set(self.groups) & (set(self.limits) | set(self.rain))
        if clash:
            raise ValueError(f"source {self.name}: group names clash with fields: {clash}")


@dataclass
class SourceStatus:
    name: str
    required: bool
    state: str
    reasons: list[str]
    #: Seconds since the data was measured (None: no data).
    age: float | None
    #: Measurement time of the data, if any.
    time: datetime | None
    #: Parsed values plus derived ones (group deciders, rain rates).
    values: dict[str, float | None]
    #: True when the data is fresh enough to be served and checked.
    fresh: bool
    notes: list[str] = field(default_factory=list)
    #: Fields measured at their own time (else ``time`` applies).
    field_times: dict[str, datetime] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "required": self.required,
            "reasons": self.reasons,
            "age": None if self.age is None else round(self.age, 1),
            "time": None if self.time is None else self.time.isoformat(),
            "fresh": self.fresh,
            "values": self.values,
            "notes": self.notes,
            "field_times": {k: v.isoformat() for k, v in self.field_times.items()},
        }


@dataclass
class Evaluation:
    time: datetime
    state: str
    #: Which limits applied: "open" (the bridge was not safe) or "close".
    limits: str
    reasons: list[str]
    sources: dict[str, SourceStatus]
    rain_until: datetime | None = None
    #: What would make it unsafe if it were enforced (shadow rain); also in ``reasons``.
    would_close: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return self.state == SAFE

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(),
            "safe": self.safe,
            "state": self.state,
            "limits": self.limits,
            "reasons": self.reasons,
            "rain_until": None if self.rain_until is None else self.rain_until.isoformat(),
            "would_close": self.would_close,
            "sources": {k: v.as_dict() for k, v in self.sources.items()},
        }

    def line(self) -> str:
        """One log line: verdict, reasons, then each source's values."""
        head = f"{self.state.upper()} limits={self.limits}"
        why = "; ".join(self.reasons) if self.reasons else "-"
        parts = []
        for s in self.sources.values():
            age = "-" if s.age is None else f"{s.age:.0f}s"
            vals = " ".join(
                f"{k}={'NA' if v is None else f'{v:.6g}'}" for k, v in sorted(s.values.items())
            )
            parts.append(f"{s.name}[{s.state},age={age}]: {vals}".rstrip())
        return f"{head} reasons=[{why}] | " + " | ".join(parts)


class Evaluator:
    """Judge readings against limits; keeps the hysteresis and rain state."""

    def __init__(
        self,
        sources: list[SourceSpec],
        *,
        rain_hold: float = 1800.0,
        max_clock_skew: float = 120.0,
    ) -> None:
        self.specs = {s.name: s for s in sources}
        if len(self.specs) != len(sources):
            raise ValueError("duplicate source names")
        self.rain_hold = rain_hold
        self.max_clock_skew = max_clock_skew
        #: The previous verdict was "safe": use the close limits.
        self.was_safe = False
        #: enforced -> (time of the last rain detection, why); False = shadow.
        self.rain: dict[bool, tuple[datetime, str]] = {}
        self._counters = {
            (s.name, f): RainCounter(rule) for s in sources for f, rule in s.rain.items()
        }

    def evaluate(self, now: datetime, readings: Mapping[str, Reading | str | None]) -> Evaluation:
        """``readings[name]`` is the latest Reading, an error message, or None."""
        use_open = not self.was_safe
        statuses = {
            name: self._source(spec, readings.get(name), now, use_open)
            for name, spec in self.specs.items()
        }
        reasons: list[str] = []
        for s in statuses.values():
            tag = "" if s.required else " (advisory)"
            reasons += [f"{s.name}{tag}: {r}" for r in s.reasons]

        rain_until = self._rain_held(True, now)
        if rain_until is not None:
            reasons.append(self._rain_reason(True, rain_until))
        would_close = []
        shadow_until = self._rain_held(False, now)
        if shadow_until is not None:
            would_close.append(
                "would close: " + self._rain_reason(False, shadow_until) + " (rain not enforced)"
            )
            reasons += would_close

        fetched = [s for s in statuses.values() if not self.specs[s.name].live]
        if any(s.state == UNSAFE for s in statuses.values()) or rain_until is not None:
            state = UNSAFE
        elif not fetched or any(s.required and s.state == UNKNOWN for s in statuses.values()):
            state = UNKNOWN
        elif all(s.state == UNKNOWN for s in fetched):
            state = UNKNOWN
            reasons.append("every weather source is unknown")
        else:
            state = SAFE
        if not statuses:
            reasons.append("no sources configured")
        self.was_safe = state == SAFE
        return Evaluation(
            now, state, "open" if use_open else "close", reasons, statuses, rain_until, would_close
        )

    @property
    def last_rain(self) -> datetime | None:
        """The last enforced rain detection (data time)."""
        return self.rain[True][0] if True in self.rain else None

    def _rain_held(self, enforced: bool, now: datetime) -> datetime | None:
        if enforced not in self.rain:
            return None
        until = self.rain[enforced][0] + timedelta(seconds=self.rain_hold)
        return until if now < until else None

    def _rain_reason(self, enforced: bool, until: datetime) -> str:
        when, why = self.rain[enforced]
        return f"rain: {why} at {when:%Y-%m-%dT%H:%M:%SZ}; holding until {until:%H:%M:%SZ}"

    # -- one source -------------------------------------------------------------

    def _rain(self, spec: SourceSpec, when: datetime, why: str) -> None:
        last = self.rain.get(spec.enforce_rain)
        if last is None or when >= last[0]:
            self.rain[spec.enforce_rain] = (when, why)

    def _source(
        self, spec: SourceSpec, reading: Reading | str | None, now: datetime, use_open: bool
    ) -> SourceStatus:
        def status(state: str, reasons: list[str], **kw: Any) -> SourceStatus:
            kw.setdefault("age", None)
            kw.setdefault("time", None)
            kw.setdefault("values", {})
            kw.setdefault("fresh", False)
            return SourceStatus(spec.name, spec.required, state, reasons, **kw)

        if reading is None:
            return status(UNKNOWN, ["no reading yet"])
        if isinstance(reading, str):
            return status(UNKNOWN, [f"fetch failed: {reading}"])
        age = (now - reading.time).total_seconds()
        values: dict[str, float | None] = dict(reading.values)
        common = dict(age=age, time=reading.time, values=values, notes=list(reading.notes))
        if age > spec.stale_after:
            return status(
                UNKNOWN, [f"stale: data {age:.0f} s old (limit {spec.stale_after:g} s)"], **common
            )
        if age < -self.max_clock_skew:
            return status(UNKNOWN, [f"timestamp {-age:.0f} s in the future"], **common)

        unsafe: list[str] = []
        unknown: list[str] = []
        # Fields with their own measurement time go stale on their own.
        field_times = dict(reading.field_times)
        stale: dict[str, str] = {}
        for name, when in field_times.items():
            f_age = (now - when).total_seconds()
            bound = reading.field_stale_after.get(name, spec.stale_after)
            if f_age > bound:
                stale[name] = f"stale: data {f_age:.0f} s old (limit {bound:g} s)"
            elif f_age < -self.max_clock_skew:
                stale[name] = f"timestamp {-f_age:.0f} s in the future"
            else:
                continue
            values[name] = None
            common["notes"].append(f"{name} {stale[name]}")
        if reading.rain:
            self._rain(spec, reading.time, f"{spec.name} reports rain")
        grouped = {f for fields in spec.groups.values() for f in fields}

        def limits_for(name: str) -> FieldLimits:
            limits = spec.limits[name]
            dyn = spec.dynamic_limits.get(name)
            if dyn is None:
                return limits
            limits, note = dyn.apply(limits, values.get(dyn.field))
            common["notes"].append(f"{name}: {note}")
            return limits

        def check(label: str, name: str) -> None:
            value = values.get(name)
            if value is None:
                unknown.append(f"{label} {stale.get(name, 'missing')}")
                return
            bad = limits_for(name).violation(value, use_open)
            if bad:
                unsafe.append(f"{label}={bad}")

        for name in spec.limits:
            if name not in grouped:
                check(name, name)
        for group, fields in spec.groups.items():
            decider = next((f for f in fields if values.get(f) is not None), None)
            if decider is None:
                values[group] = None
                unknown.append(f"{group}: no sensor available ({', '.join(fields)})")
                continue
            values[group] = values[decider]
            if decider in field_times:
                field_times[group] = field_times[decider]
            common["notes"].append(f"{group} from {decider}")
            check(f"{group}({decider})", decider)
        # Missing rain data makes an enforcing source unknown; a shadow
        # source's rain fields are only reported, so their absence is a note.
        rain_gaps = unknown if spec.enforce_rain else common["notes"]
        for name, threshold in spec.rain_rates.items():
            value = values.get(name)
            if value is None:
                rain_gaps.append(f"rain rate {name} {stale.get(name, 'missing')}")
            elif value > threshold:
                self._rain(
                    spec,
                    field_times.get(name, reading.time),
                    f"{spec.name}.{name}={value:g} > {threshold:g}",
                )
        for name in spec.rain:
            counter = self._counters[(spec.name, name)]
            value = values.get(name)
            if value is None:
                rain_gaps.append(f"rain counter {name} {stale.get(name, 'missing')}")
                values[f"{name}_rate"] = None
                continue
            when = field_times.get(name, reading.time)
            note = counter.add(when, value)
            if note:
                common["notes"].append(f"{name}: {note}")
            values[f"{name}_rate"] = counter.rate()
            if counter.raining():
                self._rain(
                    spec,
                    when,
                    f"{spec.name}.{name} +{counter.increase():g} in {counter.rule.window:g} s",
                )
        state = UNSAFE if unsafe else UNKNOWN if unknown else SAFE
        return status(state, unsafe + unknown, fresh=True, field_times=field_times, **common)
