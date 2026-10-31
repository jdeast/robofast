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
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Clock", "FileClock", "ScaledClock", "clock_from_config", "real_clock"]

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
