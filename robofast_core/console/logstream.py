"""The console's log: one merged, ordered stream of what happens (docs/console.md, "Log").

Built from the documents the console already gathers (console/aggregate.py),
each time it gathers them:

* the supervisor's timeline (``GET /timeline``) and the sequencer's (the
  latest entries of its status snapshot): every command's outcome and every
  event, each once (by time, source, what);
* changes the console sees between two reads: an alarm raised or cleared
  (the guiders', the spectrograph's, the supervisor's, the weather's, every
  source's), the weather verdict, a telescope's state, a guider's state, the
  spectrograph's job and phase, the operator holds;
* the console's own actions (``note``).

Each entry: ``{"id", "time", "enclosure", "source", "level", "text"}``;
``level`` is ``critical``, ``alarm``, ``warning`` or ``info``. Ids increase;
a client asks for entries after the last id it has. Bounded (``size``).
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from typing import Any

LEVELS = ("info", "warning", "alarm", "critical")
_BAD_OUTCOMES = ("error", "timeout", "rejected", "not_authorized", "cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _compact(detail: Any, limit: int = 160) -> str:
    if not detail:
        return ""
    if isinstance(detail, dict):
        text = " ".join(
            f"{k}={json.dumps(v, default=str) if not isinstance(v, str) else v}"
            for k, v in detail.items()
            if k not in ("level", "t")
        )
    else:
        text = str(detail)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LogStream:
    def __init__(self, size: int = 3000) -> None:
        self.entries: deque[dict[str, Any]] = deque(maxlen=size)
        self.next_id = 1
        self._seen: deque[str] = deque(maxlen=4 * size)
        self._seen_set: set[str] = set()
        #: enclosure -> the last state read (for changes).
        self._last: dict[str, dict[str, Any]] = {}

    # -- adding --------------------------------------------------------------------

    def add(
        self, enclosure: str, source: str, level: str, text: str, time: str | None = None
    ) -> dict[str, Any]:
        e = {
            "id": self.next_id,
            "time": time or _now(),
            "enclosure": enclosure,
            "source": source,
            "level": level if level in LEVELS else "info",
            "text": text,
        }
        self.next_id += 1
        self.entries.append(e)
        return e

    def note(self, enclosure: str, text: str, level: str = "info") -> None:
        """The console's own doing (an operator action)."""
        self.add(enclosure, "console", level, text)

    def _once(self, key: str) -> bool:
        if key in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        return True

    def _timeline(self, enclosure: str, source: str, items: list[Any]) -> None:
        rows = [x for x in items if isinstance(x, dict) and isinstance(x.get("time"), str)]
        rows.sort(key=lambda x: x["time"])
        for x in rows:
            if x.get("kind") == "command" and x.get("outcome") == "sent":
                continue
            key = "|".join(
                map(
                    str,
                    (
                        enclosure,
                        source,
                        x["time"],
                        x.get("source"),
                        x.get("what"),
                        x.get("outcome"),
                    ),
                )
            )
            if not self._once(key):
                continue
            detail = x.get("detail") if isinstance(x.get("detail"), dict) else {}
            if x.get("kind") == "command":
                bad = x.get("outcome") in _BAD_OUTCOMES
                text = f"{x.get('source')} → {x.get('device')} {x.get('what')}: {x.get('outcome')}"
                if bad and detail.get("error"):
                    text += f" ({detail['error']})"
                level = "warning" if bad else "info"
            else:
                text = f"{x.get('what')} {_compact(detail)}".strip()
                level = str(detail.get("level") or "info")
                if level not in LEVELS:
                    level = "alarm" if level in ("error",) else "info"
                if x.get("what") in ("alarm",):
                    level = "alarm"
            self.add(enclosure, source, level, text, x["time"])

    # -- from the console's status document -----------------------------------------------

    def ingest(self, doc: dict[str, Any]) -> None:
        for name, enc in (doc.get("enclosures") or {}).items():
            if not isinstance(enc, dict) or enc.get("error"):
                continue
            self._timeline(name, "supervisor", enc.get("timeline") or [])
            self._timeline(name, "sequencer", enc.get("_sequencer_timeline") or [])
            state = self._state(enc)
            last = self._last.get(name)
            self._last[name] = state
            if last is None:
                continue  # the first read is the baseline
            self._changes(name, last, state)

    @staticmethod
    def _state(enc: dict[str, Any]) -> dict[str, Any]:
        w = enc.get("weather") or {}
        b = w.get("bridge") or {}
        spec = enc.get("spectrograph") or {}
        job = spec.get("job") or {}
        n = enc.get("enclosure") or {}
        return {
            "alarms": {
                a["id"]: (a.get("source"), a.get("level"), a.get("text"))
                for a in enc.get("alarms") or []
                if isinstance(a, dict) and "id" in a
            },
            "weather": (w.get("safe"), w.get("reason")),
            "sources": {k: v.get("state") for k, v in (b.get("sources") or {}).items()},
            "shutter": n.get("shutter"),
            "telescopes": {
                t: (x.get("state"), (x.get("guider") or {}).get("state"))
                for t, x in (enc.get("telescopes") or {}).items()
                if isinstance(x, dict)
            },
            "job": (job.get("id"), job.get("state"), job.get("phase")) if job else None,
            "holds": {
                t: (h.get("action"), h.get("operator"), h.get("reason"))
                for t, h in (n.get("holds") or {}).items()
                if isinstance(h, dict)
            },
        }

    def _changes(self, name: str, a: dict[str, Any], b: dict[str, Any]) -> None:
        add = self.add
        for aid, (src, level, text) in b["alarms"].items():
            if aid not in a["alarms"]:
                add(name, str(src or "alarm"), str(level or "alarm"), f"ALARM: {text}")
        for aid, (src, _level, text) in a["alarms"].items():
            if aid not in b["alarms"]:
                add(name, str(src or "alarm"), "info", f"cleared: {text}")
        if a["weather"] != b["weather"]:
            safe, reason = b["weather"]
            verdict = "safe" if safe else "UNSAFE" if safe is False else "unknown"
            add(name, "weather", "info" if safe else "warning", f"weather {verdict}: {reason}")
        for src, st in b["sources"].items():
            if a["sources"].get(src) not in (None, st):
                add(name, "weather", "info", f"{src}: {a['sources'][src]} → {st}")
        if a["shutter"] != b["shutter"] and b["shutter"] is not None:
            add(name, "enclosure", "info", f"shutter {a['shutter']} → {b['shutter']}")
        for t, (st, g) in b["telescopes"].items():
            st0, g0 = a["telescopes"].get(t, (None, None))
            if st0 is not None and st0 != st:
                add(name, t, "alarm" if st == "gave_up" else "info", f"{t}: {st0} → {st}")
            if g0 is not None and g0 != g and g is not None:
                level = "alarm" if g == "failed" else "warning" if g == "lost" else "info"
                add(name, f"{t}-guider", level, f"{t} guider: {g0} → {g}")
        if b["job"] != a["job"] and b["job"] is not None:
            jid, st, phase = b["job"]
            add(name, "spectrograph", "info", f"job #{jid} {st}" + (f": {phase}" if phase else ""))
        for t, h in b["holds"].items():
            if a["holds"].get(t) != h:
                add(name, t, "warning", f"{t} held: {h[0]} by {h[1]} ({h[2]})")
        for t in a["holds"]:
            if t not in b["holds"]:
                add(name, t, "info", f"{t}: hold lifted")

    # -- reading ---------------------------------------------------------------------

    def since(self, after: int, limit: int = 500) -> list[dict[str, Any]]:
        out = [e for e in self.entries if e["id"] > after]
        return out[-limit:]
