"""Authentication, rate limiting and the audit log for the request API.

Tokens
------
Each client gets its own API token, created by an operator with::

    python -m robofast_core.request_api tokens add --name my-tom --scope read --scope submit

which prints the token **once**. A token looks like ``rft_<id>_<secret>``:
``id`` (12 hex digits) names the record, ``secret`` is 32 random bytes
(base64url). Only a salted hash of the secret is kept (HMAC-SHA256 keyed
with a per-token random salt; a slow KDF buys nothing for 256 random bits),
in a JSON tokens file that lives outside any repository (its path is in the
API's config; the CLI refuses a path inside a git work tree) and is written
with mode 0600. There are no default tokens: with an empty file nothing is
authorized.

A token's ``name`` is the client's identity: it is recorded as the
``submitter`` of what it submits, and a client sees and cancels only its
own request groups (``admin`` sees and cancels all). Several tokens may
share a name (rotation). Revoking marks the record (kept for the audit
trail); the server re-reads the file when it changes, so a revocation takes
effect at the next request.

Scopes: ``read`` (status, list and get request groups, data pointers),
``submit`` (submit, validate, cancel normal request groups), ``too`` (submit,
validate, cancel targets of opportunity), ``admin`` (everything, any
client's groups).

Checking a token costs the same whether its id exists or not, and the
secret's hash is compared with :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = ("read", "submit", "too", "admin")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
_TOKEN_RE = re.compile(r"rft_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
#: Hashed when the token's id is unknown, so that path costs the same.
_DUMMY_SALT = "00" * 16
_DUMMY_HASH = "00" * 32


def _digest(salt_hex: str, secret: str) -> str:
    return hmac.new(bytes.fromhex(salt_hex), secret.encode(), hashlib.sha256).hexdigest()


@dataclass
class TokenRecord:
    id: str
    name: str
    scopes: list[str]
    salt: str
    hash: str
    created: str
    expires: str | None = None
    revoked: str | None = None

    def allows(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes

    def active(self, now: datetime) -> bool:
        if self.revoked is not None:
            return False
        return self.expires is None or now < datetime.fromisoformat(self.expires)

    def public(self) -> dict[str, Any]:
        """The record without its salt and hash."""
        return {k: v for k, v in asdict(self).items() if k not in ("salt", "hash")}


def inside_git_worktree(path: str | Path) -> Path | None:
    """The git work tree ``path`` would be in, if any."""
    p = Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


class TokenStore:
    """The tokens file (module doc). Thread-safe enough for one asyncio
    server plus the CLI (writes are atomic renames)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._records: dict[str, TokenRecord] = {}
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self.reload()

    # -- the file ---------------------------------------------------------------

    def reload(self) -> None:
        """Re-read the file if it changed (cheap: one stat). A missing file is
        an empty store; an unreadable one raises :class:`ValueError`."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._records, self._stamp = {}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        if st.st_mode & 0o077:
            log.warning("tokens file %s is readable by others (chmod 600 it)", self.path)
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("not a version-1 tokens file")
            records = {}
            for d in data.get("tokens", []):
                rec = TokenRecord(**d)
                bytes.fromhex(rec.salt), bytes.fromhex(rec.hash)  # ValueError if not hex
                if not isinstance(rec.scopes, list) or not set(rec.scopes) <= set(SCOPES):
                    raise ValueError(f"token {rec.id}: unknown scopes {rec.scopes}")
                records[rec.id] = rec
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"tokens file {self.path}: {exc}") from exc
        self._records, self._stamp = records, stamp

    def _save(self) -> None:
        data = {"version": 1, "tokens": [asdict(r) for r in self._records.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
        self._stamp = None
        self.reload()

    # -- management (the CLI) --------------------------------------------------

    def add(
        self, name: str, scopes: list[str], *, expires_days: float | None = None
    ) -> tuple[TokenRecord, str]:
        """Create a token; returns its record and the token (shown once)."""
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"name {name!r}: use letters, digits and ._@- (at most 64)")
        bad = set(scopes) - set(SCOPES)
        if bad or not scopes:
            raise ValueError(f"scopes {sorted(bad) or scopes}: expected some of {SCOPES}")
        with self._lock:
            self.reload()
            tid = secrets.token_hex(6)
            while tid in self._records:
                tid = secrets.token_hex(6)
            secret = secrets.token_urlsafe(32)
            salt = secrets.token_hex(16)
            now = datetime.now(UTC)
            rec = TokenRecord(
                tid,
                name,
                sorted(set(scopes), key=SCOPES.index),
                salt,
                _digest(salt, secret),
                now.isoformat(),
                None if expires_days is None else (now + timedelta(days=expires_days)).isoformat(),
            )
            self._records[tid] = rec
            self._save()
        return rec, f"rft_{tid}_{secret}"

    def revoke(self, *, name: str | None = None, token_id: str | None = None) -> list[str]:
        """Revoke by id, or every active token of a name; returns the ids."""
        with self._lock:
            self.reload()
            now = datetime.now(UTC).isoformat()
            hit = [
                r
                for r in self._records.values()
                if r.revoked is None
                and (
                    (token_id is not None and r.id == token_id)
                    or (name is not None and r.name == name)
                )
            ]
            for r in hit:
                r.revoked = now
            if hit:
                self._save()
        return [r.id for r in hit]

    def records(self) -> list[TokenRecord]:
        self.reload()
        return list(self._records.values())

    # -- checking ------------------------------------------------------------------

    def authenticate(self, token: str, now: datetime | None = None) -> TokenRecord | None:
        """The active record for ``token``, or None. Constant-time in the
        secret; the same work whether or not the id exists."""
        now = now or datetime.now(UTC)
        m = _TOKEN_RE.fullmatch(token)
        tid, secret = (m.group(1), m.group(2)) if m else ("", "x" * 43)
        rec = self._records.get(tid)
        salt, expected = (rec.salt, rec.hash) if rec else (_DUMMY_SALT, _DUMMY_HASH)
        ok = hmac.compare_digest(_digest(salt, secret), expected)
        if not ok or rec is None or m is None or not rec.active(now):
            return None
        return rec


@dataclass
class _Bucket:
    tokens: float
    t: float


@dataclass
class RateLimiter:
    """A token bucket per key: ``burst`` requests at once, refilled at
    ``per_minute``."""

    per_minute: float = 60.0
    burst: float = 20.0
    clock: Any = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """(allowed, seconds to wait before retrying)."""
        now = self.clock()
        b = self._buckets.get(key)
        if b is None:
            b = self._buckets[key] = _Bucket(self.burst, now)
        rate = self.per_minute / 60.0
        b.tokens = min(self.burst, b.tokens + (now - b.t) * rate)
        b.t = now
        if b.tokens >= cost:
            b.tokens -= cost
            return True, 0.0
        return False, (cost - b.tokens) / rate if rate > 0 else 3600.0


class AuditLog:
    """Append-only JSON lines, one per mutating call (never the token)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = None if path is None else Path(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **entry: Any) -> None:
        line = json.dumps({"time": datetime.now(UTC).isoformat(), **entry}, default=str)
        if self.path is None:
            log.info("audit: %s", line)
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
        except OSError:
            log.exception("audit log %s not writable; entry: %s", self.path, line)
