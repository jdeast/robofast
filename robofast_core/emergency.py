"""The emergency close channel: close an enclosure when its supervisor cannot.

The safety supervisor is the only thing that commands an enclosure
(design.md 3.3): its privileged dome grant is the one way to open or close.
The observer console asks it to close (``POST /close``). If the supervisor
is down or does not answer within ``emergency_timeout`` (2 s), the console
has one more way, and it can only close: this module.

* **Dome side** (:class:`EmergencyGate`, used by
  :mod:`robofast_core.devices.aqawan_server`): one extra endpoint,
  ``PUT /robofast/v1/emergency_close`` with ``Authorization: Bearer
  <token>`` and a JSON body ``{"operator", "reason", "via"}``. It does one
  thing, the enclosure's own close (``CloseShutter``); there is no parameter
  and no other path that could open, so the single owner still holds for
  opening (the supervisor's grant). The token's SHA-256 is in the dome
  server's config (``emergency_close.token_sha256``), never the token.
* **Latched.** An emergency close is latched as an enclosure fault: the
  dome's fault channel (``Action("Faults")``) reports it, so the supervisor,
  once it is back, alarms and will not open; and the dome itself refuses
  ``OpenShutter`` until an operator clears it with ``ClearFaults`` (through
  the supervisor, which the dome allows only once both shutters are
  closed). Every emergency close and every clear is one JSON line in the
  dome's ``emergency_close.log``, which the server reads back on start, so
  a restart does not forget the latch.
* **Console side** (:func:`close_dome`): the client, with the token read
  from a file only the console's user can read.

Making a token::

    python -m robofast_core.emergency token

prints ``{"token": ..., "sha256": ...}``: the token goes in the console's
token file, the hash in the dome server's config.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from robofast_core.cancel import isolated

log = logging.getLogger(__name__)

#: The one endpoint (on the dome server's own port).
PATH = "/robofast/v1/emergency_close"
MAX_TEXT = 500


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """(token, its SHA-256 hex)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def read_token(path: str | Path) -> str:
    """The token in ``path`` (one line); refuses a file others can read."""
    p = Path(path)
    if os.name == "posix" and p.stat().st_mode & 0o077:
        raise ValueError(f"{p}: the emergency token file must be readable by its owner only")
    token = p.read_text().strip()
    if not token:
        raise ValueError(f"{p}: empty")
    return token


@dataclass
class EmergencyConfig:
    """The dome server's ``emergency_close`` entry."""

    #: SHA-256 (hex) of each token allowed to close (usually one: the console's).
    token_sha256: list[str] = field(default_factory=list)
    #: JSON lines: every emergency close and every clear of the latch.
    log: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EmergencyConfig | None:
        if d is None:
            return None
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        unknown = set(d) - {"token_sha256", "log"}
        if unknown:
            raise ValueError(f"emergency_close: unknown keys {sorted(unknown)}")
        hashes = d.get("token_sha256") or []
        if isinstance(hashes, str):
            hashes = [hashes]
        hashes = [str(h).lower() for h in hashes]
        if not hashes or any(len(h) != 64 or set(h) - set("0123456789abcdef") for h in hashes):
            raise ValueError("emergency_close.token_sha256: one or more SHA-256 hex digests")
        return cls(hashes, d.get("log"))


class EmergencyGate:
    """The dome server's side: who may close, the latch, the log."""

    def __init__(self, config: EmergencyConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        #: The emergency close that latched (None: not latched).
        self.latched: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        if self.config.log is None:
            return
        try:
            lines = Path(self.config.log).read_text().splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.error("emergency log %s unreadable: %r; assuming latched", self.config.log, exc)
            self.latched = {"time": None, "operator": "?", "reason": f"log unreadable: {exc}"}
            return
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "emergency_close" and e.get("ok"):
                self.latched = e
            elif e.get("event") == "latch_cleared":
                self.latched = None
        if self.latched is not None:
            log.warning("emergency close still latched (from %s)", self.latched.get("time"))

    def authorized(self, header: str | None) -> bool:
        scheme, _, token = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        got = hash_token(token)
        ok = False
        for h in self.config.token_sha256:
            ok |= hmac.compare_digest(got, h)
        return ok

    def _write(self, entry: dict[str, Any]) -> None:
        if self.config.log is None:
            return
        try:
            path = Path(self.config.log)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            log.exception("writing the emergency close log %s", self.config.log)

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = {"time": datetime.now(UTC).isoformat(), "event": "emergency_close", **entry}
        with self._lock:
            if entry.get("ok"):
                self.latched = entry
            self._write(entry)
        log.critical("EMERGENCY CLOSE %s", json.dumps(entry, default=str))
        return entry

    def clear(self, note: str) -> bool:
        """Clear the latch (with ClearFaults); True if it was latched."""
        with self._lock:
            was = self.latched is not None
            if was:
                self.latched = None
                self._write(
                    {"time": datetime.now(UTC).isoformat(), "event": "latch_cleared", "note": note}
                )
        if was:
            log.warning("emergency close latch cleared (%s)", note)
        return was

    def describe(self) -> str | None:
        e = self.latched
        if e is None:
            return None
        return (
            f"emergency close by {e.get('operator', '?')} at {e.get('time')}"
            f" ({e.get('via', '?')}): {e.get('reason', '')}"
        )


def parse_body(body: bytes) -> dict[str, str]:
    """``{"operator", "reason", "via"}`` from a request body, bounded."""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        k: str(data.get(k) or default)[:MAX_TEXT]
        for k, default in (("operator", "unknown"), ("reason", "emergency close"), ("via", "?"))
    }


async def close_dome(
    http: httpx.AsyncClient,
    dome_url: str,
    token: str,
    *,
    operator: str,
    reason: str,
    via: str = "console",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Ask the dome server to close, on the emergency channel. Raises
    :class:`EmergencyFailed` with the dome's answer if it did not accept."""
    try:
        r = await isolated(
            http.put(
                dome_url.rstrip("/") + PATH,
                json={"operator": operator, "reason": reason, "via": via},
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        )
    except httpx.HTTPError as exc:
        raise EmergencyFailed(f"dome unreachable: {type(exc).__name__}: {exc}") from exc
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:200]}
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else data
        raise EmergencyFailed(f"dome refused ({r.status_code}): {err}")
    return data


class EmergencyFailed(Exception):
    """The emergency close was not accepted (unreachable, refused, failed)."""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m robofast_core.emergency", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token", help="make a token; print it and its SHA-256")
    hp = sub.add_parser("hash", help="print the SHA-256 of the token in a file")
    hp.add_argument("file")
    args = ap.parse_args(argv)
    if args.cmd == "token":
        token, digest = new_token()
        print(json.dumps({"token": token, "sha256": digest}))
    else:
        print(hash_token(Path(args.file).read_text()))


if __name__ == "__main__":
    main()
