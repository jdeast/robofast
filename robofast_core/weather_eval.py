"""Weather limits: per-source staleness, hysteresis, rain, and the combined verdict.

Each evaluation judges every source afresh against ``now`` (never against a
time frozen at startup):

* **Staleness** is the reading's own measurement time vs ``now``. A stale,
  missing or failed source is *unknown*; its old values are not checked
  (and not served to Alpaca clients).
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
* **Rain** is detected from the *increase* of a cumulative counter within a
  window (a decrease is a counter reset: history restarts there), or from a
  source that flags rain itself. Rain holds the verdict unsafe for
  ``rain_hold`` seconds after the last detection.

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

    def __post_init__(self) -> None:
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
        self.last_rain: datetime | None = None
        self.last_rain_reason = ""
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

        rain_until = None
        if self.last_rain is not None:
            rain_until = self.last_rain + timedelta(seconds=self.rain_hold)
            if now < rain_until:
                reasons.append(
                    f"rain: {self.last_rain_reason} at {self.last_rain:%Y-%m-%dT%H:%M:%SZ};"
                    f" holding until {rain_until:%H:%M:%SZ}"
                )
            else:
                rain_until = None

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
            now, state, "open" if use_open else "close", reasons, statuses, rain_until
        )

    # -- one source -------------------------------------------------------------

    def _rain(self, when: datetime, why: str) -> None:
        if self.last_rain is None or when >= self.last_rain:
            self.last_rain, self.last_rain_reason = when, why

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
        if reading.rain:
            self._rain(reading.time, f"{spec.name} reports rain")
        grouped = {f for fields in spec.groups.values() for f in fields}

        def check(name: str, value: float | None, limits: FieldLimits) -> None:
            if value is None:
                unknown.append(f"{name} missing")
                return
            bad = limits.violation(value, use_open)
            if bad:
                unsafe.append(f"{name}={bad}")

        for name, limits in spec.limits.items():
            if name not in grouped:
                check(name, values.get(name), limits)
        for group, fields in spec.groups.items():
            decider = next((f for f in fields if values.get(f) is not None), None)
            if decider is None:
                values[group] = None
                unknown.append(f"{group}: no sensor available ({', '.join(fields)})")
                continue
            values[group] = values[decider]
            common["notes"].append(f"{group} from {decider}")
            check(f"{group}({decider})", values[decider], spec.limits[decider])
        for name in spec.rain:
            counter = self._counters[(spec.name, name)]
            value = values.get(name)
            if value is None:
                unknown.append(f"rain counter {name} missing")
                values[f"{name}_rate"] = None
                continue
            note = counter.add(reading.time, value)
            if note:
                common["notes"].append(f"{name}: {note}")
            values[f"{name}_rate"] = counter.rate()
            if counter.raining():
                self._rain(
                    reading.time,
                    f"{spec.name}.{name} +{counter.increase():g} in {counter.rule.window:g} s",
                )
        state = UNSAFE if unsafe else UNKNOWN if unknown else SAFE
        return status(state, unsafe + unknown, fresh=True, **common)
