"""The nightly plan record: the scheduler's decisions as JSON lines.

The dispatch scheduler (:mod:`robofast_core.dispatch`) writes every answer
it gives and every report it gets to a :class:`PlanRecord`: one file per
night, ``<dir>/<name>-<YYYY-MM-DD>.jsonl``, where the date is the local date
of the *evening* (a time before ``night_rollover_hour`` local time belongs
to the previous date's night). Local time is the ``timezone`` given (an IANA
name, e.g. ``America/Phoenix``), else the site's mean solar time.

Each line is one JSON object with ``t`` (ISO 8601, UTC) and ``kind``:

* ``snapshot`` -- the scheduler as it is (requests with their status, state,
  limits, settings); the first line each process writes to a night's file;
* ``decision`` -- a new visit: ``telescope``, ``request``, ``config``,
  ``observation``, ``score`` and its ``terms``, ``runners_up`` (observable
  candidates ranked below it, with scores) and ``rejected`` (the top
  rejected candidates, by priority, with the constraints they failed);
* ``continue`` -- the next observation of a visit in progress;
* ``abandon`` -- a visit dropped mid-way (``why``);
* ``idle`` -- nothing observable (with ``rejected``); ``night_over``;
* ``too`` and ``submit`` (the request group received), ``cancel``,
  ``expired``;
* ``preempt`` -- a telescope asked to stop for a ToO (``policy``,
  ``accepted``);
* ``outcome`` -- how an observation ended (``outcome``, ``frames``).

Inspect a night with ``python -m robofast_core.plan_record FILE`` (one line
per entry), and re-run its decisions with ``--replay``: :func:`replay`
rebuilds the scheduler from each snapshot and feeds it the recorded calls at
the recorded times, then compares its choices with the recorded ones.

Writing never raises: an error is logged and the scheduler carries on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

__all__ = ["PlanRecord", "ReplayResult", "night_of", "read_record", "replay"]

#: Entry kinds that end one call to ``next_observation``.
ANSWERS = ("decision", "continue", "idle", "night_over", "join")


def night_of(
    when: datetime,
    *,
    rollover_hour: float = 12.0,
    timezone: str | None = None,
    longitude: float | None = None,
) -> date:
    """The night ``when`` belongs to: the local date of its evening."""
    if when.tzinfo is None:
        raise ValueError("night_of needs an aware datetime")
    if timezone is not None:
        local = when.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    else:
        local = when.astimezone(UTC).replace(tzinfo=None)
        if longitude is not None:
            local += timedelta(hours=longitude / 15.0)  # mean solar time
    return (local - timedelta(hours=rollover_hour)).date()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):  # a numpy scalar
        return value.item()
    return str(value)


def _clean(value: Any) -> Any:
    """Non-finite floats as strings (JSON has none)."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


class PlanRecord:
    """Appends entries to one JSON-lines file per night (module doc)."""

    def __init__(
        self,
        directory: str | Path,
        *,
        name: str = "plan",
        night_rollover_hour: float = 12.0,
        timezone: str | None = None,
        longitude: float | None = None,
    ) -> None:
        if not 0.0 <= night_rollover_hour < 24.0:
            raise ValueError(f"night_rollover_hour {night_rollover_hour!r}: expected 0..24")
        if timezone is not None:
            ZoneInfo(timezone)  # raises for an unknown zone
        self.directory = Path(directory)
        self.name = name
        self.rollover_hour = float(night_rollover_hour)
        self.timezone = timezone
        self.longitude = longitude
        #: Files this process has written a snapshot to.
        self._snapshotted: set[Path] = set()

    def night(self, when: datetime) -> date:
        return night_of(
            when,
            rollover_hour=self.rollover_hour,
            timezone=self.timezone,
            longitude=self.longitude,
        )

    def path(self, when: datetime) -> Path:
        return self.directory / f"{self.name}-{self.night(when).isoformat()}.jsonl"

    def write(
        self,
        when: datetime,
        kind: str,
        fields: dict[str, Any],
        snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Append one entry (after a snapshot, if this is the first entry this
        process writes to that night's file). Never raises."""
        try:
            path = self.path(when)
            t = when.astimezone(UTC).isoformat()
            lines = []
            if snapshot is not None and path not in self._snapshotted:
                lines.append({"t": t, "kind": "snapshot", **snapshot()})
            lines.append({"t": t, "kind": kind, **fields})
            text = "".join(
                json.dumps(_clean(e), default=_json_default, separators=(",", ":")) + "\n"
                for e in lines
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(text)
            if snapshot is not None:
                self._snapshotted.add(path)
        except Exception:
            log.exception("plan record: could not write a %s entry", kind)


def read_record(source: str | Path | Iterable[str]) -> list[dict[str, Any]]:
    """The entries of a record file (or of its lines); a line that is not
    JSON (a torn last line after a crash) is skipped with a warning."""
    lines = Path(source).read_text().splitlines() if isinstance(source, str | Path) else source
    entries = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            log.warning("plan record line %d is not JSON; skipped", n)
    return entries


# -- replay -----------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2000, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


class _Memory:
    """A record that keeps entries in a list (the replay's own)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def write(self, when, kind, fields, snapshot=None) -> None:
        self.entries.append({"t": when.isoformat(), "kind": kind, **fields})


@dataclass
class ReplayResult:
    #: Answers compared (decision, continue, idle, night_over) and preemptions.
    compared: int = 0
    #: (time, telescope, recorded, replayed), each "kind request/config".
    mismatches: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.compared > 0 and not self.mismatches


def _key(e: dict[str, Any] | None) -> str:
    if e is None:
        return "nothing"
    if e["kind"] == "preempt":
        return f"preempt {e['telescope']} {e['policy']}"
    return f"{e['kind']} {e.get('request') or ''}/{e.get('config') or ''}".rstrip("/ ")


async def replay(entries: list[dict[str, Any]]) -> ReplayResult:
    """Re-run a night: rebuild the scheduler from each snapshot, replay the
    recorded calls (answers, reports, ToOs, cancellations, preemption
    checks) at the recorded times, and compare its choices with the
    recorded ones. Preemption requests are answered as recorded."""
    from robofast_core.dispatch import DispatchScheduler  # imports astropy

    result = ReplayResult()
    clock = _Clock()
    sched: DispatchScheduler | None = None
    memory = _Memory()
    handed: dict[str, Any] = {}
    preempts = [i for i, e in enumerate(entries) if e["kind"] == "preempt"]
    used: set[int] = set()

    def answer_preempt(telescope: str, policy: str, reason: str) -> bool:
        for i in preempts:
            if i not in used:
                used.add(i)
                rec = entries[i]
                if (rec["telescope"], rec["policy"]) != (telescope, policy):
                    result.mismatches.append(
                        (rec["t"], telescope, _key(rec), f"preempt {telescope} {policy}")
                    )
                return bool(rec["accepted"])
        result.mismatches.append((clock.t.isoformat(), telescope, "nothing", "preempt"))
        return False

    for i, e in enumerate(entries):
        kind = e["kind"]
        if kind == "snapshot":
            memory = _Memory()
            sched = DispatchScheduler.from_snapshot(e, clock=clock, record=memory)  # type: ignore[arg-type]
            sched.set_preempt_callback(answer_preempt)
            handed = {}
            continue
        if sched is None:
            continue
        clock.t = datetime.fromisoformat(e["t"])
        if kind in ANSWERS:
            tel = e["telescope"]
            start = len(memory.entries)
            obs = await sched.next_observation(tel)
            handed[tel] = obs
            mine = [m for m in memory.entries[start:] if m["kind"] in ANSWERS]
            got = mine[-1] if mine else None
            result.compared += 1
            if _key(got) != _key(e):
                result.mismatches.append((e["t"], tel, _key(e), _key(got)))
        elif kind in ("outcome", "member_outcome"):
            obs = handed.get(e["telescope"])
            if obs is None:
                result.mismatches.append((e["t"], e["telescope"], _key(e), "no observation"))
                continue
            sched.report(e["telescope"], obs, e["outcome"], int(e["frames"]))
        elif kind == "too":
            if e["group"]["id"] in sched.groups:  # already in this file's snapshot
                sched.check_toos()
            else:
                await sched.submit_too(e["group"])
        elif kind == "submit":
            if e["group"]["id"] not in sched.groups:  # else in this file's snapshot
                await sched.submit(e["group"])
        elif kind == "cancel":
            sched.cancel(e["group"])
        elif kind == "held":
            sched.set_held(e["telescopes"])
        elif kind == "spectrograph":
            sched.set_spectrograph_available(bool(e["available"]), e.get("reason") or "")
        elif kind == "preempt":
            result.compared += 1
            if i not in used:
                sched.check_toos()  # a background check made this one
                if i not in used:
                    result.mismatches.append((e["t"], e["telescope"], _key(e), "nothing"))
    return result


def _show(e: dict[str, Any]) -> str:
    kind = e["kind"]
    head = f"{e['t'][:19]} {kind:<10}"
    if kind == "snapshot":
        return f"{head} {len(e.get('groups', []))} request groups"
    if kind == "decision":
        rej = "; ".join(f"{r['request']}: {', '.join(r['reasons'])}" for r in e["rejected"])
        return (
            f"{head} {e['telescope']} {e['request']} score {e['score']:.3f}"
            f" ({len(e['runners_up'])} runners-up){' | rejected ' + rej if rej else ''}"
        )
    rest = {k: v for k, v in e.items() if k not in ("t", "kind")}
    return f"{head} {json.dumps(rest, default=str)[:160]}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Show or replay a nightly plan record.")
    ap.add_argument("file", help="a <name>-<date>.jsonl record")
    ap.add_argument("--replay", action="store_true", help="re-run the decisions and compare")
    args = ap.parse_args(argv)
    entries = read_record(args.file)
    if not args.replay:
        for e in entries:
            print(_show(e))
        return
    result = asyncio.run(replay(entries))
    for t, tel, rec, got in result.mismatches:
        print(f"{t} {tel}: recorded {rec}, replayed {got}")
    print(f"{result.compared} compared, {len(result.mismatches)} mismatches")
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
