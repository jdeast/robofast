"""Where the notifier's alarms come from (docs/notifier.md).

The notifier raises nothing of its own invention: it reuses the observer
console's :class:`~robofast_core.console.aggregate.Aggregator`, so it sees
exactly the alarms the console shows, with the same ids -- the supervisor's
(``GET /status``: its alarms, faults, quarantined or orphaned leases), the
sequencer's status snapshot (stale, quarantined, needs an operator, a
telescope that gave up or lost frames), each guider's and the
spectrograph's ``GET /status``, the weather bridge, and any of them not
answering. It reads the console's acknowledgements file for acks, and can
check that the console itself answers (``GET /healthz``). It needs neither
the console process nor anything new in the services.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from robofast_core.cancel import isolated
from robofast_core.console.aggregate import Aggregator, alarm_id

log = logging.getLogger(__name__)


class AggregatorSource:
    """Every enclosure's alarms, as the console gathers them."""

    name = "services"

    def __init__(self, aggregator: Aggregator) -> None:
        self.aggregator = aggregator

    async def alarms(self) -> list[dict[str, Any]]:
        doc = await self.aggregator.collect()
        out: list[dict[str, Any]] = []
        for name, enc in (doc.get("enclosures") or {}).items():
            if not isinstance(enc, dict):
                continue
            if enc.get("error"):
                text = f"could not gather {name}'s status"
                out.append(
                    {
                        "id": alarm_id(name, "notifier", text),
                        "enclosure": name,
                        "source": "notifier",
                        "level": "critical",
                        "text": text,
                        "detail": str(enc["error"]),
                    }
                )
            out.extend(a for a in enc.get("alarms") or [] if isinstance(a, dict))
        return out


class ConsoleHealth:
    """An alarm while the observer console does not answer ``/healthz``
    (observers would then neither see nor acknowledge anything)."""

    name = "console"

    def __init__(self, url: str, http: httpx.AsyncClient, timeout: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.http = http
        self.timeout = timeout

    async def alarms(self) -> list[dict[str, Any]]:
        try:
            r = await isolated(self.http.get(f"{self.url}/healthz", timeout=self.timeout))
            r.raise_for_status()
            return []
        except httpx.HTTPError as exc:
            text = "observer console unreachable"
            return [
                {
                    "id": alarm_id("console", "console", text),
                    "enclosure": "",
                    "source": "console",
                    "level": "warning",
                    "text": text,
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                }
            ]


class AcksFile:
    """The console's acknowledgements (``acks`` in its config): alarm id ->
    ``{"by", "time", "reason"}``. Re-read when it changes; a file being
    replaced or unreadable keeps the last good copy."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._sig: tuple[int, int] | None = None
        self._acks: dict[str, dict[str, Any]] = {}
        self._warned = False

    def read(self) -> dict[str, dict[str, Any]]:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if sig != self._sig:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self._acks = {k: v for k, v in data.items() if isinstance(v, dict)}
                self._sig = sig
            self._warned = False
        except FileNotFoundError:
            pass  # nothing acknowledged yet
        except (OSError, ValueError) as exc:
            if not self._warned:
                log.error("notifier: acknowledgements %s unreadable: %r", self.path, exc)
                self._warned = True
        return self._acks


__all__ = ["AcksFile", "AggregatorSource", "ConsoleHealth"]
