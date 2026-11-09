"""The time robofast stamps things with: one injectable UTC clock.

Every path that names a night or stamps a time on something it keeps -- the
FITS writer and its nightly frame index, the spectrum sidecars and their
index, the KiwiSpec raw file names, the nightly plan record, the autofocus
results -- takes "now" from one clock, a ``Callable[[], datetime]`` returning
an aware UTC datetime. In production that is :func:`real_clock` (the host's
clock, which must be NTP-disciplined: ``docs/deploy.md``). A simulation
injects another one, so a night compressed in time has one night name
everywhere (``docs/demo.md``).

Exposure times measured by an instrument (a camera's
``LastExposureStartTime``, the spectrograph's start of exposure) still come
from the instrument; in production those agree with the host clock to well
under a second, and the night they belong to is decided by the clock here.

Clocks
------
* :func:`real_clock` -- ``datetime.now(UTC)``.
* :class:`ScaledClock` -- ``start`` plus ``scale`` times the wall-clock time
  since ``origin`` (a Unix time, so another process given the same three
  numbers keeps the same time).
* :class:`FileClock` -- a :class:`ScaledClock` read from a JSON file
  (``{"start", "scale", "origin"}``), re-read when the file changes: one
  simulation can move every process's clock at once (the simulated night's
  evening calibrations, night and morning). Simulation only.

:func:`clock_from_config` builds one from a config entry (None: the real
clock).

Night names
-----------
A night is named ``n<YYYYMMDD>`` for the local date of its *evening*: a
time before ``night_rollover_hour`` (default 12:00) local time belongs to
the previous date's night. :class:`NightRule` is the one implementation;
every component that names a night (the FITS writer, the spectrum
sidecars, the raw spectrum file names the sequencer asks KiwiSpec for, the
plan record, the request API's expected data paths, throughput) takes a
rule built by :meth:`NightRule.from_site` from the config's ``site``::

    "site": {"latitude": ..., "longitude": ..., "elevation": ...,
             "timezone": "America/Phoenix", "night_rollover_hour": 12}

Local time is ``timezone`` (an IANA name). Without one it is the site's
*standard* time, UTC plus ``round(longitude / 15)`` hours (for MINERVA,
UTC-7: the same as America/Phoenix); with neither, UTC. Never the UT date
alone: that would split a MINERVA night at 00:00 UT (17:00 MST), during
the evening calibrations.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import timezone as fixed_zone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

__all__ = [
    "NIGHT_KEYS",
    "Clock",
    "FileClock",
    "NightRule",
    "ScaledClock",
    "clock_from_config",
    "plan_with_night_rule",
    "real_clock",
]

#: A UTC clock: returns an aware datetime.
Clock = Callable[[], datetime]


def real_clock() -> datetime:
    """The host's clock, in UTC."""
    return datetime.now(UTC)


@dataclass
class ScaledClock:
    """``start`` plus ``scale`` times the wall-clock seconds since ``origin``."""

    start: datetime
    scale: float = 1.0
    #: A Unix time (``time.time()``); default: now.
    origin: float | None = None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("ScaledClock needs an aware start time")
        if not self.scale > 0:
            raise ValueError(f"scale {self.scale!r}: must be positive")
        if self.origin is None:
            self.origin = time.time()

    def __call__(self) -> datetime:
        assert self.origin is not None
        return self.start + timedelta(seconds=(time.time() - self.origin) * self.scale)

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "scale": self.scale, "origin": self.origin}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScaledClock:
        unknown = set(d) - {"start", "scale", "origin"}
        if unknown:
            raise ValueError(f"clock: unknown keys {sorted(unknown)}")
        return cls(
            datetime.fromisoformat(str(d["start"])),
            float(d.get("scale", 1.0)),
            None if d.get("origin") is None else float(d["origin"]),
        )

    def write(self, path: str | Path) -> None:
        """Write this clock to ``path`` atomically (for :class:`FileClock`)."""
        path = Path(path)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.to_dict()))
        os.replace(tmp, path)


class FileClock:
    """A :class:`ScaledClock` kept in a JSON file, re-read when the file
    changes (module doc). If the file cannot be read, the last good clock
    goes on; with none yet, the real clock (logged)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._clock: ScaledClock | None = None

    def __call__(self) -> datetime:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                self._clock = ScaledClock.from_dict(json.loads(self.path.read_text()))
                self._stamp = stamp
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if self._clock is None:
                log.warning("clock file %s unreadable (%r): using the real clock", self.path, exc)
                return real_clock()
        assert self._clock is not None
        return self._clock()


def clock_from_config(entry: Any, base: Path | None = None) -> Clock:
    """``None`` -> :func:`real_clock`; ``{"file": path}`` -> :class:`FileClock`
    (relative to ``base``); ``{"start", "scale", "origin"?}`` ->
    :class:`ScaledClock`."""
    if entry is None:
        return real_clock
    if not isinstance(entry, dict):
        raise ValueError(f"clock: expected an object, not {entry!r}")
    if "file" in entry:
        if set(entry) != {"file"}:
            raise ValueError("clock: 'file' takes no other keys")
        path = Path(entry["file"])
        if base is not None and not path.is_absolute():
            path = base / path
        return FileClock(path)
    return ScaledClock.from_dict(entry)


#: The ``site`` keys of the night convention (module doc).
NIGHT_KEYS = ("timezone", "night_rollover_hour")


@dataclass(frozen=True, eq=False)
class NightRule:
    """The night a time belongs to (module doc): the one rule every night
    name comes from."""

    #: An IANA time zone (``America/Phoenix``); None: standard time at
    #: ``longitude``, else UTC.
    timezone: str | None = None
    #: Degrees east; used only without ``timezone``.
    longitude: float | None = None
    #: Local hour (0..24) before which a time belongs to the previous night.
    rollover_hour: float = 12.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.rollover_hour < 24.0:
            raise ValueError(f"night_rollover_hour {self.rollover_hour!r}: expected 0..24")
        if self.timezone is not None:
            ZoneInfo(self.timezone)  # raises for an unknown zone

    @property
    def key(self) -> tuple[Any, ...]:
        """What decides the night names: two rules with the same key name
        every time's night alike."""
        if self.timezone is not None:
            return (self.timezone, self.rollover_hour)
        return (self.zone.utcoffset(None), self.rollover_hour)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NightRule) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        zone = self.timezone or str(self.zone)
        return f"{zone}, rollover {self.rollover_hour:g} h"

    @property
    def zone(self) -> Any:
        """The local time zone."""
        if self.timezone is not None:
            return ZoneInfo(self.timezone)
        if self.longitude is not None:
            return fixed_zone(timedelta(hours=round(self.longitude / 15.0)))
        return UTC

    def date(self, when: datetime) -> date:
        """The local date of the evening of the night ``when`` belongs to."""
        if when.tzinfo is None:
            raise ValueError("a night needs an aware datetime")
        local = when.astimezone(self.zone).replace(tzinfo=None)
        return (local - timedelta(hours=self.rollover_hour)).date()

    def name(self, when: datetime) -> str:
        """``n<YYYYMMDD>``."""
        return f"n{self.date(when):%Y%m%d}"

    @classmethod
    def from_site(cls, site: Mapping[str, Any] | None) -> NightRule:
        """The rule of a config's ``site`` (``timezone``,
        ``night_rollover_hour``, ``longitude``; module doc)."""
        site = site or {}
        tz = site.get("timezone")
        lon = site.get("longitude")
        try:
            return cls(
                None if tz is None else str(tz),
                None if lon is None else float(lon),
                float(site.get("night_rollover_hour", 12.0)),
            )
        except (TypeError, ValueError, KeyError) as exc:  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"site: {exc}") from None

    def to_site(self) -> dict[str, Any]:
        """The ``site`` keys that give this rule (module doc)."""
        out: dict[str, Any] = {"night_rollover_hour": self.rollover_hour}
        if self.timezone is not None:
            out["timezone"] = self.timezone
        return out


def geo_site(site: Mapping[str, Any]) -> dict[str, Any]:
    """A ``site`` without the night keys (for :class:`robofast.sky.Site`)."""
    return {k: v for k, v in site.items() if k not in NIGHT_KEYS}


def moved_to_site(entry: Mapping[str, Any] | None, where: str) -> None:
    """Refuse the night keys in ``entry``: they are the ``site``'s now
    (module doc), so every component names the night by one rule."""
    bad = sorted(k for k in (*NIGHT_KEYS, "rollover_hour") if k in (entry or {}))
    if bad:
        raise ValueError(
            f"{where}: {', '.join(bad)} moved to the config's 'site' (one night rule for"
            " every night name: robofast/clock.py)"
        )


def plan_with_night_rule(entry: Any, site: Any) -> Any:
    """A plan entry whose own ``site`` (the dispatch scheduler's) takes the
    enclosure ``site``'s night keys (``timezone``, ``night_rollover_hour``)
    where it gives none: one night rule for the frames, the spectra and the
    plan record (robofast/clock.py). A key given in both must agree."""
    if not (isinstance(entry, dict) and isinstance(entry.get("site"), dict)):
        return entry
    if not isinstance(site, dict):
        return entry
    own = dict(entry["site"])
    for key in NIGHT_KEYS:
        if key not in site:
            continue
        if key in own and own[key] != site[key]:
            raise ValueError(
                f"plan.site.{key} {own[key]!r} disagrees with site.{key} {site[key]!r}:"
                " one night rule names every night (robofast/clock.py)"
            )
        own[key] = site[key]
    return {**entry, "site": own}
