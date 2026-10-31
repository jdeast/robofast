"""The notifier's engine: alarms in, notifications out (docs/notifier.md).

Each poll the :class:`Notifier` asks its sources for the alarms active now
(each a dict with ``id``, ``enclosure``, ``source``, ``level``, ``text`` and
``detail``, as the observer console builds them) and the console's
acknowledgements, and keeps one :class:`Tracked` episode per active alarm:

* a new alarm is sent once (``raise``); while it stays active it is not sent
  again (the alarm id is the console's: a hash of enclosure, source and text);
* an alarm gone for ``clear_after`` seconds is cleared (``clear``);
* an alarm acknowledged on the console stops escalating (``ack``);
* an alarm at ``escalate_level`` or above, unacknowledged ``escalate_after``
  seconds after it was raised, is sent again as ``escalate`` (and every
  ``escalate_repeat`` seconds after that, if set);
* once a day at ``digest_at`` local time a ``digest`` lists the day's alarms
  and each sink's delivery counts.

Delivery is one :class:`SinkWorker` per sink: its own task and bounded
queue, a minimum level, the kinds it takes, a rate limit (a summary says how
many were suppressed), a timeout per attempt and retries with backoff. A
sink that fails or hangs only delays itself; the poll loop never waits on a
sink. The clock is injectable (``clock()``: seconds since the epoch).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from robofast_core.console.aggregate import alarm_id

log = logging.getLogger(__name__)

#: Severity, lowest first. ``alarm`` is the console's default level.
LEVELS = {"info": 0, "warning": 1, "alarm": 2, "critical": 3}
#: Notification kinds an alarm produces, plus the daily digest, the rate
#: limiter's summary and the operator's ``--test``.
KINDS = ("raise", "escalate", "clear", "ack", "digest", "suppressed", "test")
#: What a sink takes when its config does not say.
DEFAULT_ON = ("raise", "escalate", "digest")
#: Kinds that pass every level filter and rate limit (one a day, or a
#: summary of what the limit held back, or asked for by hand).
UNLIMITED = ("digest", "suppressed", "test")


def level_value(level: str) -> int:
    try:
        return LEVELS[level]
    except KeyError:
        raise ValueError(f"unknown level {level!r} (one of {', '.join(LEVELS)})") from None


def _level(level: Any) -> str:
    """An alarm's level, with anything unknown treated as ``alarm``."""
    return level if isinstance(level, str) and level in LEVELS else "alarm"


@dataclass
class Notification:
    kind: str
    level: str
    subject: str
    body: str
    time: float
    alarm: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "level": self.level,
            "subject": self.subject,
            "body": self.body,
            "time": datetime.fromtimestamp(self.time).astimezone().isoformat(),
            "alarm": self.alarm,
        }


class Sink(Protocol):
    """Delivers one notification or raises. ``send`` may take as long as it
    likes; its worker bounds it."""

    name: str

    async def send(self, n: Notification) -> None: ...


class AlarmSource(Protocol):
    name: str

    async def alarms(self) -> list[dict[str, Any]]: ...


class AckSource(Protocol):
    def read(self) -> dict[str, dict[str, Any]]: ...


# -- delivery -----------------------------------------------------------------------------


class SinkWorker:
    """One sink's queue, filters, rate limit and retries (module doc)."""

    def __init__(
        self,
        sink: Sink,
        *,
        min_level: str = "warning",
        on: Iterable[str] = DEFAULT_ON,
        rate_max: int = 20,
        rate_window: float = 3600.0,
        timeout: float = 60.0,
        retries: int = 3,
        retry_base: float = 5.0,
        retry_max: float = 300.0,
        queue_size: int = 100,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.sink = sink
        self.name = sink.name
        self.min_level = level_value(min_level)
        self.on = frozenset(on)
        bad = self.on - set(KINDS)
        if bad:
            raise ValueError(f"sink {self.name}: unknown kinds {sorted(bad)}")
        if rate_max < 1 or rate_window <= 0:
            raise ValueError(f"sink {self.name}: the rate limit needs max >= 1 and window > 0")
        self.rate_max, self.rate_window = int(rate_max), float(rate_window)
        self.timeout, self.retries = float(timeout), int(retries)
        self.retry_base, self.retry_max = float(retry_base), float(retry_max)
        self.clock, self.sleep = clock, sleep
        self.queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=queue_size)
        self._sent_times: deque[float] = deque()
        self._suppressed: list[Notification] = []
        self._suppressed_since: float | None = None
        self._suppressed_count = 0
        self.stats = {"sent": 0, "failed": 0, "suppressed": 0, "dropped": 0}
        self._task: asyncio.Task[None] | None = None

    def wants(self, n: Notification) -> bool:
        if n.kind in ("suppressed", "test"):
            return True
        if n.kind not in self.on:
            return False
        return n.kind in UNLIMITED or level_value(n.level) >= self.min_level

    def _room(self, now: float) -> bool:
        while self._sent_times and self._sent_times[0] <= now - self.rate_window:
            self._sent_times.popleft()
        return len(self._sent_times) < self.rate_max

    def offer(self, n: Notification) -> bool:
        """Queue ``n`` if this sink takes it and the rate limit allows;
        never blocks. True if queued."""
        if not self.wants(n):
            return False
        if n.kind not in UNLIMITED:
            now = self.clock()
            if not self._room(now):
                if not self._suppressed:
                    self._suppressed_since = now
                self._suppressed.append(n)
                del self._suppressed[:-200]  # keep the newest; the count is separate
                self.stats["suppressed"] += 1
                self._suppressed_count += 1
                return False
            self._sent_times.append(now)
        return self._put(n)

    def _put(self, n: Notification) -> bool:
        try:
            self.queue.put_nowait(n)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            log.error("notifier: %s queue full, dropped %r", self.name, n.subject)
            return False
        return True

    def tick(self) -> None:
        """Send the suppressed summary once the rate limit has room again."""
        count = self._suppressed_count
        if not count or not self._room(self.clock()):
            return
        top = max((level_value(s.level) for s in self._suppressed), default=0)
        level = next(k for k, v in LEVELS.items() if v == top)
        since = datetime.fromtimestamp(self._suppressed_since or self.clock()).astimezone()
        lines = [s.subject for s in self._suppressed[-20:]]
        body = (
            f"The rate limit ({self.rate_max} per {self.rate_window:g} s) held back {count} "
            f"notification(s) since {since:%Y-%m-%d %H:%M %Z}; the highest level was {level}.\n"
            "The most recent:\n" + "\n".join(f"  {s}" for s in lines)
        )
        self._sent_times.append(self.clock())
        self._put(
            Notification(
                "suppressed",
                level,
                f"[robofast] {count} notification(s) suppressed by the rate limit",
                body,
                self.clock(),
            )
        )
        self._suppressed.clear()
        self._suppressed_count = 0
        self._suppressed_since = None

    async def _deliver(self, n: Notification) -> None:
        for attempt in range(self.retries + 1):
            try:
                await asyncio.wait_for(self.sink.send(n), self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any sink error: log, back off, retry
                last = attempt == self.retries
                log.warning(
                    "notifier: %s failed (attempt %d of %d) for %r: %s",
                    self.name,
                    attempt + 1,
                    self.retries + 1,
                    n.subject,
                    f"{type(exc).__name__}: {exc}"[:300],
                )
                if last:
                    self.stats["failed"] += 1
                    log.error("notifier: %s gave up on %r", self.name, n.subject)
                    return
                delay = min(self.retry_max, self.retry_base * 2**attempt)
                await self.sleep(delay * (0.5 + secrets.randbelow(1000) / 1000))
            else:
                self.stats["sent"] += 1
                return

    async def run(self) -> None:
        while True:
            n = await self.queue.get()
            try:
                await self._deliver(n)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one notification end the worker
                log.exception("notifier: %s", self.name)
            finally:
                self.queue.task_done()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"notifier-sink-{self.name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


# -- alarms -------------------------------------------------------------------------------


@dataclass
class Tracked:
    """One episode of one alarm: from first seen to cleared."""

    id: str
    enclosure: str
    source: str
    level: str
    text: str
    detail: str
    first_seen: float
    last_seen: float
    acked: dict[str, Any] | None = None
    acked_at: float | None = None
    escalations: int = 0
    last_escalated: float | None = None
    cleared: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enclosure": self.enclosure,
            "source": self.source,
            "level": self.level,
            "text": self.text,
            "detail": self.detail,
            "first_seen": _iso(self.first_seen),
            "acked": self.acked,
            "escalations": self.escalations,
            "cleared": None if self.cleared is None else _iso(self.cleared),
        }


@dataclass
class Settings:
    #: Seconds between polls, and the most one poll of every source may take.
    interval: float = 10.0
    poll_timeout: float = 20.0
    #: An alarm missing this long is cleared (a flapping one is not resent).
    clear_after: float = 120.0
    #: Escalate an unacknowledged alarm at ``escalate_level`` or above this
    #: long after it was raised (None: never); then every
    #: ``escalate_repeat`` seconds (None: once).
    escalate_after: float | None = 600.0
    escalate_repeat: float | None = None
    escalate_level: str = "critical"
    #: The daily digest, ``"HH:MM"`` local time (None: none), in
    #: ``timezone`` (an IANA name; None: the host's).
    digest_at: str | None = "08:00"
    timezone: str | None = None
    #: Alarm texts (regular expressions, searched) never notified.
    ignore: list[str] = field(default_factory=list)
    #: Episodes kept for the digest.
    history: int = 1000
    #: An acknowledgement this many seconds older than the episode still
    #: counts (the console may see an alarm, and an observer acknowledge
    #: it, before the notifier's next poll).
    ack_grace: float = 60.0

    def __post_init__(self) -> None:
        level_value(self.escalate_level)
        if self.digest_at is not None:
            _hhmm(self.digest_at)
        if self.timezone is not None:
            ZoneInfo(self.timezone)
        for pattern in self.ignore:
            re.compile(pattern)


def _hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", s.strip())
    if m is None:
        raise ValueError(f"digest_at {s!r}: expected HH:MM")
    return int(m.group(1)), int(m.group(2))


def next_daily(after: float, hhmm: str, timezone: str | None = None) -> float:
    """The first ``hhmm`` local time strictly after ``after`` (epoch seconds)."""
    h, m = _hhmm(hhmm)
    tz = ZoneInfo(timezone) if timezone else None
    now = datetime.fromtimestamp(after, tz)  # naive local time if tz is None
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand.timestamp() <= after:
        cand = (cand + timedelta(days=1)).replace(hour=h, minute=m)
    return cand.timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _parse_time(s: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


class Notifier:
    """Polls the sources and feeds the sink workers (module doc)."""

    def __init__(
        self,
        sources: list[AlarmSource],
        workers: list[SinkWorker],
        *,
        acks: AckSource | None = None,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.time,
        state: str | Path | None = None,
    ) -> None:
        self.sources = sources
        self.workers = workers
        self.acks = acks
        self.settings = settings or Settings()
        self.clock = clock
        self.state_path = None if state is None else Path(state)
        self._ignore = [re.compile(p) for p in self.settings.ignore]
        self.active: dict[str, Tracked] = {}
        self.history: list[Tracked] = []
        self.last_digest: float | None = None
        self.next_digest: float | None = None
        self._load()

    # -- the loop ---------------------------------------------------------------------

    def start(self) -> None:
        for w in self.workers:
            w.start()

    async def aclose(self) -> None:
        for w in self.workers:
            await w.stop()

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait until every queue is delivered (tests, ``--test``)."""
        await asyncio.wait_for(asyncio.gather(*(w.queue.join() for w in self.workers)), timeout)

    async def run(self) -> None:
        self.start()
        try:
            while True:
                t0 = time.monotonic()
                try:
                    await self.poll_once()
                except Exception:  # the loop outlives any bug in one poll
                    log.exception("notifier: poll")
                await asyncio.sleep(max(0.5, self.settings.interval - (time.monotonic() - t0)))
        finally:
            self._save()
            await self.aclose()

    async def poll_once(self) -> None:
        alarms = await self._gather()
        acks = self._read_acks()
        now = self.clock()
        for w in self.workers:
            w.tick()  # what the rate limit held back goes before anything new
        self._update(alarms, acks, now)
        self._escalate(now)
        self._digest(now)
        self._save()

    async def _gather(self) -> list[dict[str, Any]]:
        async def one(src: AlarmSource) -> list[dict[str, Any]]:
            try:
                got = await asyncio.wait_for(src.alarms(), self.settings.poll_timeout)
                return [a for a in got if isinstance(a, dict) and a.get("id")]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("notifier: source %s: %r", src.name, exc)
                text = f"notifier: alarm source {src.name} failed"
                return [
                    {
                        "id": alarm_id("notifier", src.name, text),
                        "enclosure": "notifier",
                        "source": src.name,
                        "level": "critical",
                        "text": text,
                        "detail": f"{type(exc).__name__}: {exc}"[:300],
                    }
                ]

        lists = await asyncio.gather(*(one(s) for s in self.sources))
        return [a for lst in lists for a in lst]

    def _read_acks(self) -> dict[str, dict[str, Any]]:
        if self.acks is None:
            return {}
        try:
            return self.acks.read()
        except Exception as exc:
            log.error("notifier: reading acknowledgements: %r", exc)
            return {}

    # -- alarms -----------------------------------------------------------------------

    def _update(
        self, alarms: list[dict[str, Any]], acks: dict[str, dict[str, Any]], now: float
    ) -> None:
        seen: dict[str, dict[str, Any]] = {}
        for a in alarms:
            text = str(a.get("text", ""))
            if any(p.search(text) for p in self._ignore):
                continue
            prev = seen.get(a["id"])
            if prev is None or level_value(_level(a.get("level"))) > level_value(
                _level(prev.get("level"))
            ):
                seen[a["id"]] = a
        for aid, a in seen.items():
            t = self.active.get(aid)
            if t is None:
                t = Tracked(
                    id=aid,
                    enclosure=str(a.get("enclosure", "")),
                    source=str(a.get("source", "")),
                    level=_level(a.get("level")),
                    text=str(a.get("text", "")),
                    detail=str(a.get("detail", "")),
                    first_seen=now,
                    last_seen=now,
                )
                self.active[aid] = t
                self._remember(t)
                self._emit("raise", t, now)
            else:
                t.last_seen = now
                t.detail = str(a.get("detail", ""))
                t.level = max(t.level, _level(a.get("level")), key=level_value)
        for t in list(self.active.values()):
            ack = acks.get(t.id)
            if t.acked is None and isinstance(ack, dict):
                at = _parse_time(ack.get("time"))
                if at is None or at >= t.first_seen - self.settings.ack_grace:
                    t.acked, t.acked_at = ack, now
                    self._emit("ack", t, now)
            if t.id not in seen and now - t.last_seen >= self.settings.clear_after:
                t.cleared = now
                del self.active[t.id]
                self._emit("clear", t, now)

    def _remember(self, t: Tracked) -> None:
        self.history.append(t)
        while len(self.history) > self.settings.history:
            old = next((h for h in self.history if h.cleared is not None), self.history[0])
            self.history.remove(old)

    def _escalate(self, now: float) -> None:
        s = self.settings
        if s.escalate_after is None:
            return
        floor = level_value(s.escalate_level)
        for t in self.active.values():
            if t.acked is not None or level_value(t.level) < floor:
                continue
            if t.escalations == 0:
                due = t.first_seen + s.escalate_after
            elif s.escalate_repeat:
                due = (t.last_escalated or now) + s.escalate_repeat
            else:
                continue
            if now >= due:
                t.escalations += 1
                t.last_escalated = now
                self._emit("escalate", t, now)

    def _emit(self, kind: str, t: Tracked, now: float) -> None:
        n = self.notification(kind, t, now)
        for w in self.workers:
            w.offer(n)

    def notification(self, kind: str, t: Tracked, now: float) -> Notification:
        where = f"{t.enclosure}: " if t.enclosure else ""
        tag = {
            "raise": t.level.upper(),
            "escalate": f"{t.level.upper()}, NOT ACKNOWLEDGED",
            "clear": "cleared",
            "ack": "acknowledged",
        }[kind]
        lines = [t.text]
        if t.detail:
            lines.append(t.detail)
        lines += [
            "",
            f"enclosure: {t.enclosure}",
            f"source:    {t.source}",
            f"level:     {t.level}",
            f"raised:    {_iso(t.first_seen)}",
        ]
        if kind == "escalate":
            mins = (now - t.first_seen) / 60
            lines.append(f"unacknowledged for {mins:.0f} min (escalation {t.escalations})")
        if t.acked is not None:
            lines.append(
                f"acknowledged by {t.acked.get('by')} at {t.acked.get('time')}: "
                f"{t.acked.get('reason', '')}"
            )
        if kind == "clear" and t.cleared is not None:
            lines.append(f"cleared:   {_iso(t.cleared)}")
        lines += [f"alarm id:  {t.id}", "", "Acknowledge it on the observer console."]
        # A clear or ack carries the alarm's level, so it reaches the same sinks.
        return Notification(
            kind,
            t.level,
            f"[robofast {tag}] {where}{t.text}"[:300],
            "\n".join(lines),
            now,
            t.public(),
        )

    # -- the digest -------------------------------------------------------------------

    def _digest(self, now: float) -> None:
        s = self.settings
        if s.digest_at is None:
            return
        if self.next_digest is None:
            self.next_digest = next_daily(now, s.digest_at, s.timezone)
            return
        if now < self.next_digest:
            return
        n = self.digest(now)
        for w in self.workers:
            w.offer(n)
        for w in self.workers:
            w.stats = dict.fromkeys(w.stats, 0)
        self.last_digest = now
        self.next_digest = next_daily(now, s.digest_at, s.timezone)
        self.history = [t for t in self.history if t.cleared is None]

    def digest(self, now: float) -> Notification:
        since = self.last_digest
        eps = sorted(self.history, key=lambda t: t.first_seen)
        counts = {k: sum(t.level == k for t in eps) for k in LEVELS}
        top = max((level_value(t.level) for t in eps), default=0)
        head = (
            f"Since {_iso(since)}" if since is not None else "Since the notifier started"
        ) + f": {len(eps)} alarm(s)"
        if eps:
            head += " (" + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + ")"
        lines = [head + f"; {len(self.active)} still active.", ""]
        for t in eps[:100]:
            state = []
            if t.acked is not None:
                state.append(f"acknowledged by {t.acked.get('by')}")
            if t.escalations:
                state.append(f"escalated x{t.escalations}")
            state.append(
                "still active"
                if t.cleared is None
                else f"cleared {datetime.fromtimestamp(t.cleared).astimezone():%H:%M}"
            )
            first = datetime.fromtimestamp(t.first_seen).astimezone()
            lines.append(
                f"{first:%m-%d %H:%M} {t.level:<8} {t.enclosure}: {t.text} -- {', '.join(state)}"
            )
        if len(eps) > 100:
            lines.append(f"... and {len(eps) - 100} more")
        lines += ["", "Delivery since the last digest:"]
        for w in self.workers:
            lines.append(f"  {w.name}: " + ", ".join(f"{k} {v}" for k, v in w.stats.items()))
        lines += ["", "A missing digest means the notifier itself is not running."]
        day = datetime.fromtimestamp(now).astimezone()
        return Notification(
            "digest",
            next(k for k, v in LEVELS.items() if v == top),
            f"[robofast digest] {day:%Y-%m-%d}: {len(eps)} alarm(s), {len(self.active)} active",
            "\n".join(lines),
            now,
        )

    # -- state across restarts ----------------------------------------------------------

    def _save(self) -> None:
        if self.state_path is None:
            return
        eps = list(self.history)
        eps += [t for t in self.active.values() if all(t is not h for h in eps)]
        doc = {
            "episodes": [asdict(t) for t in eps],
            "last_digest": self.last_digest,
            "next_digest": self.next_digest,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f".{self.state_path.name}.{secrets.token_hex(4)}.tmp")
            tmp.write_text(json.dumps(doc))
            tmp.replace(self.state_path)
        except OSError:
            log.exception("notifier: writing %s", self.state_path)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            doc = json.loads(self.state_path.read_text())
            eps = [Tracked(**e) for e in doc.get("episodes", [])]
        except (OSError, ValueError, TypeError) as exc:
            log.error("notifier: state %s unreadable, starting afresh: %r", self.state_path, exc)
            return
        self.history = eps[-self.settings.history :]
        self.active = {t.id: t for t in eps if t.cleared is None}
        self.last_digest = doc.get("last_digest")
        self.next_digest = doc.get("next_digest")


__all__ = [
    "DEFAULT_ON",
    "KINDS",
    "LEVELS",
    "AckSource",
    "AlarmSource",
    "Notification",
    "Notifier",
    "Settings",
    "Sink",
    "SinkWorker",
    "Tracked",
    "level_value",
    "next_daily",
]
