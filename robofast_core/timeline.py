"""A thread-safe, append-only record of commands and events.

Every device command (whether it was sent, rejected, timed out or failed) and
every safety/sequencer event lands here with a ``time.monotonic()`` timestamp.
Tests assert against it ("nothing from the observing task reached the
telescope after the lease was revoked"); in production it is the audit trail.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    """One record. ``kind`` is ``"command"`` or ``"event"``."""

    t: float
    kind: str
    source: str  # grant holder for commands; component name for events
    device: str | None
    what: str  # member name for commands; event name for events
    outcome: str  # commands: sent/ok/rejected/timeout/error/cancelled
    detail: dict[str, Any] = field(default_factory=dict)


class Timeline:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self.t0 = time.monotonic()

    def _add(self, entry: Entry) -> Entry:
        with self._lock:
            self._entries.append(entry)
        return entry

    def command(
        self,
        source: str,
        device: str,
        member: str,
        outcome: str,
        t: float | None = None,
        **detail: Any,
    ) -> Entry:
        return self._add(
            Entry(
                t if t is not None else time.monotonic(),
                "command",
                source,
                device,
                member,
                outcome,
                detail,
            )
        )

    def event(self, source: str, what: str, **detail: Any) -> Entry:
        entry = self._add(Entry(time.monotonic(), "event", source, None, what, "", detail))
        log.info("%s: %s %s", source, what, detail or "")
        return entry

    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._entries)

    def commands(
        self,
        device: str | None = None,
        source_prefix: str | None = None,
        outcome: str | None = None,
        since: float | None = None,
    ) -> list[Entry]:
        out = []
        for e in self.entries():
            if e.kind != "command":
                continue
            if device is not None and e.device != device:
                continue
            if source_prefix is not None and not e.source.startswith(source_prefix):
                continue
            if outcome is not None and e.outcome != outcome:
                continue
            if since is not None and e.t < since:
                continue
            out.append(e)
        return out

    def events(self, what: str | None = None, source: str | None = None) -> list[Entry]:
        return [
            e
            for e in self.entries()
            if e.kind == "event"
            and (what is None or e.what == what)
            and (source is None or e.source == source)
        ]

    def first_event(self, what: str, after: float = float("-inf")) -> Entry | None:
        for e in self.events(what):
            if e.t >= after:
                return e
        return None

    def dump(self) -> str:
        """Human-readable listing, relative to construction time."""
        lines = []
        for e in self.entries():
            dev = f" {e.device}" if e.device else ""
            extra = f" {e.detail}" if e.detail else ""
            lines.append(
                f"{e.t - self.t0:9.3f} {e.kind:7s} {e.source}{dev} {e.what} {e.outcome}{extra}"
            )
        return "\n".join(lines)
