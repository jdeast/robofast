"""The guide log: every guide frame of one telescope, at the guide rate.

With ``guide_log`` in the guider service's config, the guide loop keeps a
row for every guide frame (:class:`~robofast.guider.loop.GuideLogRow`)
and :class:`GuideLog` appends them to a per-telescope, per-night CSV
file every ``interval`` seconds::

    "guide_log": {"dir": "/data/minerva/t1/guidelog", "interval": 5.0,
                  "site": {"timezone": "America/Phoenix"}}

    <dir>/<scope>.<night>.guide.csv     e.g. t1.n20261121.guide.csv

One line per guide frame (valid or not)::

    t_start,t_end,valid,star_x,star_y,setpoint_x,setpoint_y,fiber_x,fiber_y,
    offset_arcsec,flux,fwhm,fiber_counts

Times are Unix seconds (UTC) of the exposure's start and end; positions
are guide-camera pixels; ``setpoint`` is where the loop holds the star
(the fiber, or the offset star's setpoint with offset guiding) and
``fiber`` is the fiber; ``offset_arcsec`` is this frame's distance of the
star from the setpoint (with the guide config's ``plate_scale``; else
empty); ``flux`` is the star's background-subtracted counts in the
centroid aperture (the FAU photometry: a per-telescope flux record for
flux-weighted exposure midpoints) and ``fiber_counts`` the counts inside
the fiber's footprint (``fiber_flux``; else empty).

Why (2016-2020 MINERVA RVs): the legacy guide loop stalled for minutes at
exposure starts, T2 held stars 7-11" from the fiber for months, and the
legacy log reported 0.3" when T2 was 1" off: nothing recorded where the
star actually was, or how much light each telescope sent. The night is
named by ``site``'s night rule (``timezone``, ``night_rollover_hour``,
``longitude``: :class:`robofast.clock.NightRule`, the rule every robofast
night name comes from; without ``site``, UTC, as the other services).

The rows are written by a worker thread, so a slow disk never delays the
guide loop; while a write fails the rows are kept (bounded by
``max_pending``) and written with the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robofast.clock import NightRule

log = logging.getLogger(__name__)

COLUMNS = (
    "t_start",
    "t_end",
    "valid",
    "star_x",
    "star_y",
    "setpoint_x",
    "setpoint_y",
    "fiber_x",
    "fiber_y",
    "offset_arcsec",
    "flux",
    "fwhm",
    "fiber_counts",
)
HEADER = ",".join(COLUMNS) + "\n"


@dataclass
class GuideLogConfig:
    dir: str
    interval: float = 5.0
    #: The night rule's keys (module doc); None: UTC.
    site: dict[str, Any] | None = None
    #: Rows kept while the file cannot be written.
    max_pending: int = 200_000

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuideLogConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"guide_log: unknown keys {sorted(unknown)}")
        if not d.get("dir"):
            raise ValueError("guide_log needs dir")
        c = cls(**d)
        if c.interval <= 0 or c.max_pending <= 0:
            raise ValueError("guide_log: interval and max_pending must be positive")
        NightRule.from_site(c.site)  # raises on a bad rule
        return c


def _num(v: float, digits: int) -> str:
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) and math.isfinite(v) else ""


class GuideLog:
    """Drains a :class:`~robofast.guider.loop.Guider`'s ``log_frames``
    into the per-night CSV file (module doc)."""

    def __init__(self, guider: Any, config: GuideLogConfig) -> None:
        self.guider = guider
        self.config = config
        self.scope = guider.config.scope
        self.rule = NightRule.from_site(config.site)
        self.pending: list[tuple[str, str]] = []  # (night, line)
        self.written = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_file: str | None = None
        self._task: asyncio.Task | None = None

    def _drain(self) -> None:
        q = self.guider.log_frames
        if not q:
            return
        offset = time.time() - self.guider.clock.now()
        ps = self.guider.config.plate_scale
        while q:
            r = q.popleft()
            t0, t1 = r.t_start + offset, r.t_end + offset
            off = (
                math.hypot(r.star_x - r.setpoint_x, r.star_y - r.setpoint_y) * ps
                if ps
                else math.nan
            )
            line = ",".join(
                (
                    _num(t0, 4),
                    _num(t1, 4),
                    "1" if r.valid else "0",
                    _num(r.star_x, 3),
                    _num(r.star_y, 3),
                    _num(r.setpoint_x, 3),
                    _num(r.setpoint_y, 3),
                    _num(r.fiber_x, 3),
                    _num(r.fiber_y, 3),
                    _num(off, 3),
                    _num(r.flux, 1),
                    _num(r.fwhm, 3),
                    _num(r.fiber_counts, 1),
                )
            )
            self.pending.append((self.rule.name(datetime.fromtimestamp(t1, UTC)), line + "\n"))
        del self.pending[: -self.config.max_pending]

    def path(self, night: str) -> Path:
        return Path(self.config.dir) / f"{self.scope}.{night}.guide.csv"

    def _write(self, rows: list[tuple[str, str]]) -> str:
        """Append ``rows`` (in a worker thread); the last file written."""
        by_night: dict[str, list[str]] = {}
        for night, line in rows:
            by_night.setdefault(night, []).append(line)
        last = ""
        for night, lines in by_night.items():
            p = self.path(night)
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists() or p.stat().st_size == 0
            with p.open("a") as f:
                if new:
                    f.write(HEADER)
                f.writelines(lines)
            last = str(p)
        return last

    async def flush(self) -> bool:
        """Write what is pending now; True if written (or nothing to write)."""
        self._drain()
        if not self.pending:
            return True
        rows = list(self.pending)
        try:
            self.last_file = await asyncio.to_thread(self._write, rows)
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("guide log %s: %s", self.config.dir, self.last_error)
            return False
        del self.pending[: len(rows)]
        self.written += len(rows)
        self.last_error = None
        return True

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.interval)
            await self.flush()

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        with contextlib.suppress(Exception):
            async with asyncio.timeout(10):
                await self.flush()  # the last rows

    def status(self) -> dict[str, Any]:
        return {
            "dir": self.config.dir,
            "written": self.written,
            "pending": len(self.pending) + len(self.guider.log_frames),
            "failures": self.failures,
            "last_error": self.last_error,
            "file": self.last_file,
        }
