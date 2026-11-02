"""Lamp hours: how long each calibration lamp has burned, kept on disk.

ThAr and incandescent lamps age as they burn (and, less, with every
start); a lamp is replaced from its accumulated hours. The lamp server
(:mod:`robofast_core.devices.pulizzi_server`) feeds :class:`LampHours`
every status it reads from the unit, so the hours count what the unit
*reports*, not what was commanded: a lamp switched on by hand, by the
legacy software, or found on at startup is counted from when the server
first sees it on, until it sees it off.

Per lamp (by name): the lifetime totals (seconds on, starts), the same
since the last replacement (with when and by whom), every replacement's
record, and whether the lamp was on when last written.

The file (``docs/pulizzi.md``) is JSON, written atomically (a temporary
file, fsync, rename) on every on/off transition, on a replacement, and
every ``flush_interval`` seconds while a lamp burns, so a crash loses at
most that much. After a crash or a restart with a lamp still on, the file
says it was on, so the lamp is not counted as a new start; the time the
server was down is not counted (nobody saw the lamp then).

Failures never stop the server or a switch: a missing file starts every
lamp at zero (a warning); an unreadable one is moved aside
(``<file>.corrupt-<time>``), kept, and the hours start again from zero
(an error, and ``error`` says so in the status); a failed write is logged
and retried at the next flush, the hours kept in memory meanwhile.

``rated_hours`` (per lamp, optional): a warning when the current lamp
reaches :data:`LIFE_WARN` of it, an error at 100 %. Nothing is ever
refused because of hours: replacing a lamp is the operator's decision.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1
#: Fraction of ``rated_hours`` at which a lamp's life is reported as near its end.
LIFE_WARN = 0.9
_LEVELS = (None, "near", "past")


def _level(used: float | None) -> int:
    """0 (fine or no rating), 1 (near the rated life), 2 (past it)."""
    return 0 if used is None else 2 if used >= 1 else 1 if used >= LIFE_WARN else 0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _seconds(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        raise ValueError(f"not a duration: {v!r}")
    return float(v)


def _count(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"not a count: {v!r}")
    return v


def _text(v: Any) -> str | None:
    if v is not None and not isinstance(v, str):
        raise ValueError(f"not a string: {v!r}")
    return v


@dataclass
class _Lamp:
    #: Lifetime totals for this lamp position.
    seconds: float = 0.0
    cycles: int = 0
    #: The same since the last replacement (the lamp now installed).
    since_seconds: float = 0.0
    since_cycles: int = 0
    replaced_at: str | None = None
    replaced_by: str | None = None
    replacements: list[dict[str, Any]] = field(default_factory=list)
    #: On, as last seen (persisted: a restart with it on is no new start).
    on: bool = False
    #: Not persisted: the clock at the last sight of it on, and the life
    #: warning level already logged.
    last: float | None = None
    warned: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds, 1),
            "cycles": self.cycles,
            "since_replaced": {
                "seconds": round(self.since_seconds, 1),
                "cycles": self.since_cycles,
                "at": self.replaced_at,
                "by": self.replaced_by,
            },
            "replacements": self.replacements,
            "on": self.on,
        }

    @classmethod
    def from_json(cls, d: Any) -> _Lamp:
        if not isinstance(d, dict) or not isinstance(d["since_replaced"], dict):
            raise ValueError("a lamp entry is not an object")
        s = d["since_replaced"]
        reps = d.get("replacements", [])
        if not isinstance(reps, list) or not all(isinstance(r, dict) for r in reps):
            raise ValueError("replacements is not a list of objects")
        return cls(
            seconds=_seconds(d["seconds"]),
            cycles=_count(d["cycles"]),
            since_seconds=_seconds(s["seconds"]),
            since_cycles=_count(s["cycles"]),
            replaced_at=_text(s.get("at")),
            replaced_by=_text(s.get("by")),
            replacements=list(reps),
            on=d.get("on") is True,
        )


class LampHours:
    """The hours of the lamps named in ``rated`` (name -> rated hours or
    None), kept in ``path`` (None: in memory only, e.g. the simulator).
    Single-threaded: the lamp server calls it from its event loop."""

    def __init__(
        self,
        rated: dict[str, float | None],
        path: str | Path | None,
        *,
        flush_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.rated = dict(rated)
        self.path = None if path is None else Path(path)
        self.flush_interval = flush_interval
        self.clock = clock
        self.wall = wall
        self.lamps = {name: _Lamp() for name in self.rated}
        #: Entries in the file for lamps not configured now: written back as read.
        self._other: dict[str, Any] = {}
        #: Why the file could not be read (sticky) or last written.
        self.load_error: str | None = None
        self.write_error: str | None = None
        self._dirty = False
        self._written = clock()
        self._load()
        for name in self.lamps:
            self._warn_life(name)

    @property
    def error(self) -> str | None:
        return self.write_error or self.load_error

    # -- the file ------------------------------------------------------------------

    def _load(self) -> None:
        path = self.path
        if path is None:
            return
        try:
            raw = path.read_text()
        except (FileNotFoundError, NotADirectoryError):
            log.warning("lamp hours: %s not found: every lamp starts at zero hours", path)
            return
        except OSError as exc:
            # Unreadable but there: never overwrite it. Count in memory only.
            self.path = None
            self.load_error = f"{path} could not be read ({exc}); hours are not being saved"
            log.critical("LAMP HOURS: %s", self.load_error)
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != VERSION:
                raise ValueError(f"not a version-{VERSION} lamp-hours file")
            entries = data["lamps"]
            if not isinstance(entries, dict):
                raise ValueError("lamps is not an object")
            lamps = {n: _Lamp.from_json(e) for n, e in entries.items() if n in self.lamps}
        except (ValueError, TypeError, KeyError) as exc:
            aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
            try:
                os.replace(path, aside)
            except OSError as exc2:
                self.path = None
                self.load_error = (
                    f"{path} is unreadable ({exc!r}) and could not be moved aside ({exc2});"
                    " hours are not being saved"
                )
                log.critical("LAMP HOURS: %s", self.load_error)
                return
            self.load_error = (
                f"{path} was unreadable ({exc!r}); kept as {aside.name}; every lamp's hours"
                " start again from zero"
            )
            log.error("LAMP HOURS: %s", self.load_error)
            return
        self._other = {n: e for n, e in entries.items() if n not in self.lamps}
        self.lamps.update(lamps)

    def save(self) -> None:
        """Write the file now (atomically); a failure is logged and kept in
        ``write_error``, never raised."""
        self._written = self.clock()
        if self.path is None:
            self._dirty = False
            return
        data = {
            "version": VERSION,
            "updated": _iso(self.wall()),
            "lamps": {**self._other, **{n: lamp.as_json() for n, lamp in self.lamps.items()}},
        }
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
            msg = f"could not write {self.path}: {exc}"
            if msg != self.write_error:
                log.error("lamp hours: %s (kept in memory; retried)", msg)
            self.write_error = msg
            return
        if self.write_error is not None:
            log.info("lamp hours: %s written again", self.path)
        self.write_error = None
        self._dirty = False

    def close(self) -> None:
        if self._dirty:
            self.save()

    # -- counting ------------------------------------------------------------------

    def observe(self, name: str, on: bool, at: float | None = None) -> None:
        """The unit reports lamp ``name`` ``on`` (or off) at clock time ``at``."""
        now = self.clock() if at is None else at
        lamp = self.lamps[name]
        changed = False
        if on:
            if not lamp.on:
                lamp.on = changed = True
                lamp.cycles += 1
                lamp.since_cycles += 1
            elif lamp.last is not None:
                self._add(name, now - lamp.last)
            lamp.last = now if lamp.last is None else max(lamp.last, now)
        else:
            if lamp.on:
                if lamp.last is not None:
                    self._add(name, now - lamp.last)
                lamp.on, changed = False, True
            lamp.last = None
        if changed or (self._dirty and now - self._written >= self.flush_interval):
            self.save()

    def _add(self, name: str, dt: float) -> None:
        if dt <= 0:
            return
        lamp = self.lamps[name]
        lamp.seconds += dt
        lamp.since_seconds += dt
        self._dirty = True
        self._warn_life(name)

    def life_used(self, name: str) -> float | None:
        rated = self.rated.get(name)
        return None if not rated else self.lamps[name].since_seconds / 3600 / rated

    def _warn_life(self, name: str) -> None:
        used = self.life_used(name)
        level = _level(used)
        lamp = self.lamps[name]
        if level > lamp.warned and used is not None:
            log.log(
                logging.ERROR if level == 2 else logging.WARNING,
                "lamp %s has burned %.1f h since it was replaced, %.0f %% of its rated %g h%s",
                name,
                lamp.since_seconds / 3600,
                100 * used,
                self.rated[name],
                ": replace it" if level == 2 else "",
            )
        lamp.warned = level

    def replaced(self, name: str, by: str, note: str = "", hours: float = 0.0) -> dict[str, Any]:
        """Record that lamp ``name`` was replaced (by ``by``) with a lamp
        that had already burned ``hours``: the since-replacement counts
        restart, the lifetime totals go on. Returns the old lamp's record."""
        lamp = self.lamps[name]
        if lamp.on and lamp.last is not None:
            now = self.clock()
            self._add(name, now - lamp.last)
            lamp.last = max(lamp.last, now)
        at = _iso(self.wall())
        record = {
            "at": at,
            "by": by,
            "note": note,
            "hours": round(lamp.since_seconds / 3600, 4),
            "cycles": lamp.since_cycles,
            "previous": lamp.replaced_at,
        }
        lamp.replacements.append(record)
        log.warning(
            "lamp %s replaced (recorded by %s%s): the old lamp burned %.1f h in %d starts%s",
            name,
            by,
            f": {note}" if note else "",
            lamp.since_seconds / 3600,
            lamp.since_cycles,
            "" if lamp.replaced_at else " (since the hours were first tracked)",
        )
        lamp.since_seconds, lamp.since_cycles = hours * 3600.0, 0
        lamp.replaced_at, lamp.replaced_by = at, by
        lamp.warned = 0
        self._warn_life(name)
        self.save()
        return record

    def report(self, name: str) -> dict[str, Any]:
        """For ``Action("Status")``: hours to the last sight of the lamp."""
        lamp = self.lamps[name]
        used = self.life_used(name)
        return {
            "hours": round(lamp.seconds / 3600, 4),
            "cycles": lamp.cycles,
            "since_replaced": {
                "hours": round(lamp.since_seconds / 3600, 4),
                "cycles": lamp.since_cycles,
                "at": lamp.replaced_at,
                "by": lamp.replaced_by,
            },
            "rated_hours": self.rated.get(name),
            "life_used": None if used is None else round(used, 4),
            "life_warning": _LEVELS[_level(used)],
        }
