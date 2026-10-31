"""The observer console's service: one web page, its status feed and its actions.

See :mod:`robofast_core.console` and ``docs/console.md`` for what it shows
and what an observer can do. This module is the HTTP side: sessions,
CSRF, rate limits, the audit log and the actions themselves.

Security (the request API's posture, design.md 3.7)
----------------------------------------------------
* Loopback by default, behind a TLS reverse proxy on the same host for
  anyone else (``deploy/`` has examples); a non-loopback ``listen`` needs
  TLS here (``tls_cert``/``tls_key``) or ``--insecure-bind``.
* Tokens are the request API's (:mod:`robofast_core.api_auth`), from the
  same tokens file: ``read`` sees the page, ``operator`` acts, ``submit`` and
  ``too`` submit and cancel requests (through the request API's own code).
  ``admin`` is all of them.
* The page holds no secret. An observer signs in by pasting their token; the
  answer sets an ``HttpOnly``, ``SameSite=Strict`` session cookie (``Secure``
  behind TLS) and returns a CSRF token the page keeps in memory. Every
  state-changing call needs the cookie **and** an ``X-CSRF-Token`` header
  matching the session, a same-origin ``Origin`` (when the browser sends
  one) and a JSON body. A script may instead send ``Authorization: Token
  ...`` on each call (no cookie, so no CSRF).
* A revoked or expired token ends its sessions at their next request.
* Rate limits per token (reads, actions) and per address (failed sign-ins);
  bounded requests, a connection limit and a cap on live event streams.
* Every POST, refused or not, is one line in the audit log: who, when, from
  where, what, on what, why, and the outcome.
* The page is served with a strict Content-Security-Policy (inline script
  and style only with a per-response nonce; no third-party anything).

The console can never open an enclosure: :class:`SupervisorClient` can only
read the supervisor's status and timeline, ask it to close (revoke the
lease), clear a dead lease and clear faults -- there is no open endpoint to
call, and the client refuses any path outside that list.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import signal
import ssl
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import numpy as np

from robofast_core.api_auth import AuditLog, RateLimiter, TokenRecord, TokenStore
from robofast_core.config import load_config
from robofast_core.console.aggregate import Aggregator, ConsoleEnclosure, alarm_id
from robofast_core.console.hold import read_holds, send_command
from robofast_core.console.images import ImageSources, NoFrame
from robofast_core.console.page import render_page
from robofast_core.emergency import EmergencyFailed, close_dome, read_token
from robofast_core.frameview import SCALES, STRETCHES, render
from robofast_core.request_api import (
    MAX_LINE,
    ApiConfig,
    ApiError,
    HttpRequest,
    HttpServer,
    RequestApi,
    Response,
    _client_addr,
    _HttpError,
    check_bind,
)

log = logging.getLogger(__name__)

COOKIE = "robofast_console"
MAX_REASON = 500
#: Operator actions (``POST /console/api/<action>``), and those that must be
#: confirmed (``"confirm": true`` in the body; the page asks first).
ACTIONS = (
    "ack",
    "pause",
    "resume",
    "stop",
    "park",
    "clear_lease",
    "clear_faults",
    "emergency_close",
    "clear_spectrograph",
)
CONFIRM = ("stop", "park", "clear_lease", "clear_faults", "emergency_close", "clear_spectrograph")
#: Actions that the per-token action rate limit never blocks (closing is the
#: safe direction; an observer who has pressed other buttons must still be
#: able to close).
UNLIMITED = ("emergency_close",)
#: Body fields copied into the audit log.
AUDITED = ("enclosure", "telescope", "id", "reason", "direct")


@dataclass
class ConsoleConfig:
    listen: str = "127.0.0.1:8400"
    #: The request API's tokens file (required).
    tokens: str = ""
    #: JSON lines, one per POST; None logs them instead.
    audit_log: str | None = None
    #: Alarm acknowledgements (JSON), kept across restarts; None: in memory.
    acks: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    #: The session cookie's ``Secure`` flag; None: on when TLS ends here.
    #: Set it true behind a TLS reverse proxy.
    cookie_secure: bool | None = None
    session_ttl: float = 12 * 3600.0
    max_body: int = 65536
    header_timeout: float = 10.0
    idle_timeout: float = 60.0
    handler_timeout: float = 20.0
    max_connections: int = 64
    #: Live event streams, in all and per token.
    max_streams: int = 32
    max_streams_per_token: int = 4
    #: Per token: reads (page data, events) and actions, per minute and burst.
    rate_per_minute: float = 240.0
    rate_burst: float = 60.0
    action_per_minute: float = 20.0
    action_burst: float = 10.0
    #: Failed sign-ins allowed per client address.
    auth_fail_per_minute: float = 10.0
    auth_fail_burst: float = 5.0
    #: How often the event stream pushes (s), and the oldest status reused.
    refresh: float = 2.0
    cache: float = 1.0
    status_timeout: float = 2.0
    #: How long an enclosure stop waits for the sequencer to confirm the
    #: holds before it asks the supervisor to close (it closes regardless).
    hold_ack_timeout: float = 5.0
    #: Emergency close: how long the supervisor gets to accept ``POST
    #: /close`` before the console closes through the dome's emergency
    #: channel (docs/console.md), and the bound on that call.
    emergency_timeout: float = 2.0
    emergency_dome_timeout: float = 5.0
    #: Images (docs/console.md, "Images"): requests per token per minute and
    #: burst (thumbnails at a few Hz each, so apart from ``rate_per_minute``);
    #: the most often a guider is asked for its frame however many watch;
    #: how often a saved frame is looked for; the guider's binning target
    #: (pixels a side, on the guide computer); the largest PNG side served.
    image_per_minute: float = 900.0
    image_burst: float = 60.0
    image_min_interval: float = 0.4
    image_file_poll: float = 2.0
    guider_frame_max: int = 256
    image_max_side: int = 1024
    record_lines: int = 40
    timeline_entries: int = 60


# -- responses ------------------------------------------------------------------------


@dataclass
class RawResponse:
    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class StreamResponse:
    stream: AsyncIterator[bytes]
    headers: dict[str, str] = field(default_factory=dict)
    #: Called once the stream has ended, however it ended.
    on_close: Any = None


# -- the supervisor, restricted -----------------------------------------------------------


class NotAllowed(Exception):
    """A supervisor call outside the console's list (never sent)."""


class SupervisorClient:
    """The only way the console reaches a safety supervisor. ``ALLOWED`` is
    the whole list: read, close (revoke), clear a dead lease, clear faults.
    Nothing here can request a lease, renew one or open anything."""

    ALLOWED = (
        ("GET", "status"),
        ("GET", "timeline"),
        ("POST", "close"),
        ("POST", "faults/clear"),
        ("POST", "leases/*/clear"),
    )

    def __init__(self, url: str, http: httpx.AsyncClient, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.http = http
        self.timeout = timeout

    @classmethod
    def allowed(cls, method: str, path: str) -> bool:
        parts = [p for p in path.split("/") if p]
        if any(p in (".", "..") for p in parts):
            return False
        for m, pattern in cls.ALLOWED:
            want = pattern.split("/")
            if m == method and len(want) == len(parts):
                if all(w == "*" or w == p for w, p in zip(want, parts, strict=True)):
                    return True
        return False

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.allowed(method, path):
            raise NotAllowed(f"{method} /{path.strip('/')}")
        r = await self.http.request(
            method, f"{self.url}/{path.strip('/')}", json=body, timeout=self.timeout
        )
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code >= 400:
            raise ApiError(
                409 if r.status_code in (404, 409, 410) else 502,
                f"supervisor: {data.get('error') if isinstance(data, dict) else data}",
            )
        return data


# -- sessions and acknowledgements -----------------------------------------------------------


@dataclass
class Session:
    token_id: str
    csrf: str
    expires: float
    created: str


class Acks:
    """Alarm acknowledgements: alarm id -> who, when, why (bounded)."""

    LIMIT = 2000

    def __init__(self, path: str | Path | None) -> None:
        self.path = None if path is None else Path(path)
        self.acks: dict[str, dict[str, Any]] = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self.acks = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (OSError, ValueError) as exc:
                log.error("console: acknowledgements file %s unreadable: %r", self.path, exc)

    def add(self, alarm: str, by: str, reason: str) -> dict[str, Any]:
        entry = {"by": by, "time": datetime.now(UTC).isoformat(), "reason": reason}
        self.acks.pop(alarm, None)
        self.acks[alarm] = entry
        while len(self.acks) > self.LIMIT:
            self.acks.pop(next(iter(self.acks)))
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
                tmp.write_text(json.dumps(self.acks))
                tmp.replace(self.path)
            except OSError:
                log.exception("console: writing %s", self.path)
        return entry


# -- the application -----------------------------------------------------------------------------


class Console:
    """The endpoints (module doc), independent of the HTTP transport."""

    EMERGENCIES = 50

    def __init__(
        self,
        enclosures: dict[str, ConsoleEnclosure],
        tokens: TokenStore,
        *,
        config: ConsoleConfig | None = None,
        audit: AuditLog | None = None,
        http: httpx.AsyncClient | None = None,
        tls: bool = False,
    ) -> None:
        self.enclosures = enclosures
        self.tokens = tokens
        self.config = config or ConsoleConfig()
        self.audit = audit or AuditLog(self.config.audit_log)
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(trust_env=False)
        self.secure_cookie = (
            tls if self.config.cookie_secure is None else bool(self.config.cookie_secure)
        )
        c = self.config
        self.aggregator = Aggregator(
            enclosures,
            self.http,
            timeout=c.status_timeout,
            record_lines=c.record_lines,
            timeline_entries=c.timeline_entries,
        )
        self.supervisors = {
            n: SupervisorClient(e.supervisor_url, self.http, c.status_timeout)
            for n, e in enclosures.items()
        }
        self.images = ImageSources(
            enclosures,
            self.http,
            timeout=c.status_timeout,
            min_interval=c.image_min_interval,
            file_poll=c.image_file_poll,
            guider_frame_max=c.guider_frame_max,
        )
        self.image_limiter = RateLimiter(c.image_per_minute, c.image_burst)
        self.limiter = RateLimiter(c.rate_per_minute, c.rate_burst)
        self.action_limiter = RateLimiter(c.action_per_minute, c.action_burst)
        self.fail_limiter = RateLimiter(c.auth_fail_per_minute, c.auth_fail_burst)
        self.acks = Acks(c.acks)
        self.sessions: dict[str, Session] = {}
        self.streams: dict[str, int] = {}
        requests = {n: e.requests for n, e in enclosures.items() if e.requests is not None}
        self.requests = (
            RequestApi(requests, tokens, config=ApiConfig(), audit=self.audit, http=self.http)
            if requests
            else None
        )
        #: Emergency closes, shown as alarms (newest last, bounded).
        self.emergencies: list[dict[str, Any]] = []
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self._kick = asyncio.Event()

    async def aclose(self) -> None:
        if self.requests is not None:
            await self.requests.aclose()
        if self._own_http:
            await self.http.aclose()

    # -- status -------------------------------------------------------------------------

    async def status(self, fresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            if fresh or self._cache is None or now - self._cache[0] > self.config.cache:
                self._cache = (now, await self.aggregator.collect())
            doc = self._cache[1]
        out = json.loads(json.dumps(doc, default=str))  # a copy to annotate
        for e in self.emergencies:
            enc = out["enclosures"].get(e["enclosure"])
            if enc is not None:
                enc.setdefault("alarms", []).insert(0, dict(e))
        for enc in out["enclosures"].values():
            for a in enc.get("alarms", []):
                a["ack"] = self.acks.acks.get(a["id"])
        return out

    def changed(self) -> None:
        """Push to every event stream now (after an action)."""
        self._cache = None
        self._kick.set()
        self._kick = asyncio.Event()

    async def events(self, token_id: str) -> AsyncIterator[bytes]:
        """Server-sent events: ``status`` every ``refresh`` s and after each
        action; ends when the session or token does."""
        yield b"retry: 3000\n\n"
        while True:
            kick = self._kick
            if self._active(token_id) is None:
                yield b"event: signed-out\ndata: {}\n\n"
                return
            doc = await self.status()
            yield b"event: status\ndata: " + json.dumps(doc, default=str).encode() + b"\n\n"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(kick.wait(), self.config.refresh)

    # -- the request cycle ----------------------------------------------------------------

    async def handle(self, req: HttpRequest, addr: str) -> Response | RawResponse | StreamResponse:
        """Answer one request; never raises, never leaks a stack trace."""
        audit: dict[str, Any] | None = None
        path = unquote(urlsplit(req.target).path)
        if req.method == "POST":
            audit = {"addr": addr, "method": "POST", "path": path, "via": "console"}
        try:
            async with asyncio.timeout(self.config.handler_timeout):
                resp = await self._handle(req, path, addr, audit)
        except ApiError as exc:
            payload: dict[str, Any] = {"error": exc.message}
            if exc.errors:
                payload["errors"] = exc.errors
            resp = Response(exc.status, payload, exc.headers)
        except TimeoutError:
            resp = Response(503, {"error": "the request took too long"})
        except Exception:
            incident = secrets.token_hex(4)
            log.exception("console: incident %s (%s %s)", incident, req.method, req.target)
            resp = Response(500, {"error": "internal error", "incident": incident})
        if audit is not None:
            status = resp.status if isinstance(resp, Response | RawResponse) else 200
            audit["status"] = status
            if isinstance(resp, Response) and isinstance(resp.payload, dict):
                if status >= 400:
                    audit["error"] = resp.payload.get("error")
                elif "result" in resp.payload:
                    audit["result"] = resp.payload["result"]
            self.audit.write(**audit)
        return resp

    async def _handle(
        self, req: HttpRequest, path: str, addr: str, audit: dict[str, Any] | None
    ) -> Response | RawResponse | StreamResponse:
        path = path.rstrip("/") or "/"
        if req.method == "GET" and path in ("/", "/console"):
            nonce = secrets.token_urlsafe(16)
            return RawResponse(
                200,
                render_page(nonce).encode(),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": _csp(nonce)},
            )
        if req.method == "GET" and path == "/healthz":
            return Response(200, {"ok": True})
        if path == "/console/login" and req.method == "POST":
            return self._login(req, addr, audit)
        if not path.startswith("/console/"):
            raise ApiError(404, "not found")
        client, by_cookie, sid = self._client(req, addr)
        if audit is not None:
            audit.update(client=client.name, token_id=client.id)
        if req.method == "POST" and by_cookie:
            self._check_csrf(req, sid)
        parts = [p for p in path.split("/") if p][1:]  # after "console"
        image = parts[:2] in (["api", "image"], ["api", "pixel"], ["api", "fits"])
        ok, wait = (self.image_limiter if image else self.limiter).allow(client.id)
        if not ok:
            raise ApiError(429, "rate limit exceeded", headers={"Retry-After": str(round(wait))})
        if parts == ["logout"] and req.method == "POST":
            if sid is not None:
                self.sessions.pop(sid, None)
            return Response(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
        if parts == ["session"] and req.method == "GET":
            return Response(200, self._whoami(client, sid))
        if parts[:1] != ["api"]:
            raise ApiError(404, "not found")
        parts = parts[1:]
        if parts == ["status"] and req.method == "GET":
            self._need(client, "read", "operator")
            return Response(200, await self.status())
        if parts == ["events"] and req.method == "GET":
            self._need(client, "read", "operator")
            return self._open_stream(client)
        if parts == ["images"] and req.method == "GET":
            self._need(client, "read", "operator")
            return Response(200, {"sources": self.images.sources()})
        if image and len(parts) == 3 and req.method == "GET":
            self._need(client, "read", "operator")
            return await self._image(parts[0], parts[1], parts[2], req)
        if parts[:1] == ["v1"]:
            return await self._request_api(client, req, parts, audit)
        if len(parts) == 1 and parts[0] in ACTIONS and req.method == "POST":
            self._need(client, "operator")
            ok, wait = (
                (True, 0.0) if parts[0] in UNLIMITED else self.action_limiter.allow(client.id)
            )
            if not ok:
                raise ApiError(
                    429, "too many actions", headers={"Retry-After": str(max(1, round(wait)))}
                )
            body = _json_body(req.body)
            if audit is not None:
                audit.update(
                    action=parts[0],
                    **{
                        k: body.get(k) for k in ("enclosure", "telescope", "id", "reason", "direct")
                    },
                )
            result = await self.act(parts[0], client.name, body)
            self.changed()
            return Response(200, {"ok": True, "result": result})
        if len(parts) == 1 and parts[0] in ACTIONS:
            raise ApiError(405, f"{req.method} not allowed", headers={"Allow": "POST"})
        raise ApiError(404, "not found")

    # -- images ---------------------------------------------------------------------------

    async def _image(
        self, what: str, enclosure: str, source: str, req: HttpRequest
    ) -> Response | RawResponse:
        """``image`` (a PNG view), ``pixel`` (one value) or ``fits`` (the
        frame) of a source's latest frame (docs/console.md, "Images")."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(req.target).query).items()}
        try:
            frame = await self.images.frame(enclosure, source)
        except KeyError:
            raise ApiError(404, f"no image source {enclosure}/{source}") from None
        except NoFrame as exc:
            raise ApiError(404, str(exc)) from None
        h, w = frame.data.shape

        def number(key: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(float(query.get(key, default)))
            except ValueError:
                raise ApiError(400, f"{key}: a number") from None
            return max(lo, min(v, hi))

        if what == "pixel":
            x, y = number("x", 0, 0, w - 1), number("y", 0, 0, h - 1)
            value = float(frame.data[y, x])
            b = float(frame.meta.get("binning") or 1)
            ox, oy = frame.meta.get("origin") or [0, 0]
            return Response(
                200,
                {
                    "frame": frame.id,
                    "x": x,
                    "y": y,
                    "value": value if np.isfinite(value) else None,
                    "detector": [ox + (x + 0.5) * b - 0.5, oy + (y + 0.5) * b - 0.5],
                },
            )
        if what == "fits":
            data = await asyncio.to_thread(frame.fits_bytes)
            name = frame.path.name if frame.path is not None else f"{enclosure}-{source}.fits"
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
            return RawResponse(
                200,
                data,
                "application/fits",
                {"Content-Disposition": f'attachment; filename="{safe}"'},
            )
        scale, kind = query.get("scale", "zscale"), query.get("stretch", "linear")
        if scale not in SCALES or kind not in STRETCHES:
            raise ApiError(400, f"scale: one of {SCALES}; stretch: one of {STRETCHES}")
        max_side = number("max", 512, 16, self.config.image_max_side)
        region = None
        if all(k in query for k in ("x0", "y0", "x1", "y1")):
            region = (
                number("x0", 0, 0, w - 1),
                number("y0", 0, 0, h - 1),
                number("x1", w, 1, w),
                number("y1", h, 1, h),
            )
        key = f"{scale}|{kind}|{max_side}|{region}"
        etag = f'"{frame.id}.{hashlib.sha256(key.encode()).hexdigest()[:10]}"'
        if req.headers.get("if-none-match") == etag:
            return RawResponse(304, b"", "image/png", {"ETag": etag})
        limits = await asyncio.to_thread(frame.limits_for, scale)
        png, view = await asyncio.to_thread(
            render,
            frame.data,
            region=region,
            max_side=max_side,
            scale=scale,
            kind=kind,
            limits=limits,
        )
        meta = {**frame.meta, **view, "id": frame.id, "shape": [w, h]}
        return RawResponse(
            200,
            png,
            "image/png",
            {"ETag": etag, "X-Frame": json.dumps(meta, ensure_ascii=True, default=str)},
        )

    # -- authentication -----------------------------------------------------------------------

    def _login(self, req: HttpRequest, addr: str, audit: dict[str, Any] | None) -> Response:
        self._check_origin(req)
        body = _json_body(req.body)
        token = body.get("token")
        rec = None
        self.tokens.reload()
        if isinstance(token, str) and token.strip():
            rec = self.tokens.authenticate(token.strip())
        if rec is None or not (rec.allows("read") or rec.allows("operator")):
            ok, wait = self.fail_limiter.allow(f"addr:{addr}")
            if not ok:
                raise ApiError(
                    429, "too many failed attempts", headers={"Retry-After": str(round(wait))}
                )
            raise ApiError(401, "invalid token" if rec is None else "this token cannot read")
        if audit is not None:
            audit.update(client=rec.name, token_id=rec.id, action="login")
        self._expire_sessions()
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = Session(
            rec.id,
            secrets.token_urlsafe(32),
            time.monotonic() + self.config.session_ttl,
            datetime.now(UTC).isoformat(),
        )
        return Response(
            200,
            self._whoami(rec, sid),
            {"Set-Cookie": self._cookie(sid, int(self.config.session_ttl))},
        )

    def _cookie(self, sid: str, max_age: int) -> str:
        secure = "; Secure" if self.secure_cookie else ""
        return f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _whoami(self, client: TokenRecord, sid: str | None) -> dict[str, Any]:
        s = self.sessions.get(sid) if sid is not None else None
        return {
            "name": client.name,
            "scopes": client.scopes,
            "can": {
                "read": client.allows("read") or client.allows("operator"),
                "operate": client.allows("operator"),
                "submit": client.allows("submit"),
                "too": client.allows("too"),
            },
            "csrf": None if s is None else s.csrf,
            "enclosures": {n: e.telescopes for n, e in self.enclosures.items()},
            "requests": self.requests is not None,
        }

    def _expire_sessions(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self.sessions.items() if s.expires < now]:
            del self.sessions[sid]

    def _active(self, token_id: str) -> TokenRecord | None:
        self.tokens.reload()
        for r in self.tokens.records():
            if r.id == token_id and r.active(datetime.now(UTC)):
                return r
        return None

    def _client(self, req: HttpRequest, addr: str) -> tuple[TokenRecord, bool, str | None]:
        """(the token record, whether it came from the session cookie, the
        session id)."""
        scheme, _, token = req.headers.get("authorization", "").partition(" ")
        if scheme.lower() in ("token", "bearer") and token.strip():
            self.tokens.reload()
            rec = self.tokens.authenticate(token.strip())
            if rec is not None:
                return rec, False, None
        else:
            sid = _cookies(req.headers.get("cookie", "")).get(COOKIE)
            s = self.sessions.get(sid) if sid else None
            if s is not None and s.expires >= time.monotonic():
                rec = self._active(s.token_id)
                if rec is not None:
                    return rec, True, sid
                self.sessions.pop(sid, None)  # the token was revoked or expired
        ok, wait = self.fail_limiter.allow(f"addr:{addr}")
        if not ok:
            raise ApiError(
                429, "too many failed attempts", headers={"Retry-After": str(round(wait))}
            )
        raise ApiError(401, "sign in first")

    @staticmethod
    def _check_origin(req: HttpRequest) -> None:
        origin = req.headers.get("origin")
        if origin is None:
            return  # not a browser, or a same-origin GET-style request
        host = req.headers.get("host", "")
        if urlsplit(origin).netloc != host or not host:
            raise ApiError(403, "cross-origin request refused")

    def _check_csrf(self, req: HttpRequest, sid: str | None) -> None:
        self._check_origin(req)
        s = self.sessions.get(sid) if sid is not None else None
        sent = req.headers.get("x-csrf-token", "")
        if s is None or not hmac.compare_digest(sent.encode(), s.csrf.encode()):
            raise ApiError(403, "missing or wrong CSRF token")

    @staticmethod
    def _need(client: TokenRecord, *scopes: str) -> None:
        if not any(client.allows(s) for s in scopes):
            raise ApiError(403, f"this token lacks the {' or '.join(scopes)} scope")

    def _open_stream(self, client: TokenRecord) -> StreamResponse:
        c = self.config
        if sum(self.streams.values()) >= c.max_streams:
            raise ApiError(503, "too many event streams")
        if self.streams.get(client.id, 0) >= c.max_streams_per_token:
            raise ApiError(429, "too many event streams for this token")
        self.streams[client.id] = self.streams.get(client.id, 0) + 1

        def closed() -> None:
            self.streams[client.id] -= 1
            if self.streams[client.id] <= 0:
                del self.streams[client.id]

        return StreamResponse(self.events(client.id), on_close=closed)

    async def _request_api(
        self, client: TokenRecord, req: HttpRequest, parts: list[str], audit: dict[str, Any] | None
    ) -> Response:
        if self.requests is None:
            raise ApiError(404, "no enclosure here takes requests (no dispatch plan)")
        target = "/api/" + "/".join(parts)
        query = urlsplit(req.target).query
        if query:
            target += "?" + query
        if audit is not None:
            audit["action"] = "request_api"
        resp = await self.requests.call(client, req.method, target, req.body, audit)
        if req.method == "POST":
            self.changed()
        return resp

    # -- the actions -------------------------------------------------------------------------

    def _enclosure(self, body: dict[str, Any]) -> ConsoleEnclosure:
        name = body.get("enclosure")
        if name is None and len(self.enclosures) == 1:
            return next(iter(self.enclosures.values()))
        enc = self.enclosures.get(str(name))
        if enc is None:
            raise ApiError(400, f"enclosure: one of {sorted(self.enclosures)}")
        return enc

    async def act(self, action: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        """Carry out one operator action (module doc); returns its result."""
        reason = body.get("reason")
        if action == "emergency_close" and (reason is None or reason == ""):
            reason = "emergency close"  # no typing on a phone in the rain
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            raise ApiError(400, f"reason: say why (1 to {MAX_REASON} characters)")
        reason = reason.strip()
        if action in CONFIRM and body.get("confirm") is not True:
            raise ApiError(400, f'{action}: send "confirm": true (the page asks first)')
        if action == "ack":
            alarm = body.get("id")
            if not isinstance(alarm, str) or not 1 <= len(alarm) <= 64:
                raise ApiError(400, "id: the alarm's id")
            return {"id": alarm, **self.acks.add(alarm, operator, reason)}
        if action == "emergency_close":
            return await self._emergency_close(operator, reason, body)
        enc = self._enclosure(body)
        if action in ("pause", "resume", "stop", "park"):
            return await self._hold(enc, action, operator, reason, body.get("telescope"))
        if action == "clear_spectrograph":
            # The sequencer's own clear (docs/rv_night.md), through the
            # command box: the console never talks to the spectrograph.
            if enc.control_dir is None:
                raise ApiError(
                    409, f"{enc.name} has no operator_control: clear_spectrograph needs it"
                )
            cid = send_command(
                enc.control_dir, "clear_spectrograph", [], operator=operator, reason=reason
            )
            return {
                "enclosure": enc.name,
                "command": cid,
                "applied": await self._applied(enc, cid),
            }
        sup = self.supervisors[enc.name]
        if action == "clear_faults":
            return {
                "enclosure": enc.name,
                **await sup.call("POST", "faults/clear", {"operator": operator, "reason": reason}),
            }
        # clear_lease: the outstanding lease, by its token (which never
        # leaves this process).
        status = await sup.call("GET", "status")
        lease = status.get("lease") if isinstance(status, dict) else None
        if not isinstance(lease, dict) or lease.get("released"):
            raise ApiError(409, "no outstanding lease to clear")
        if lease.get("live"):
            raise ApiError(409, "the lease is live: its sequencer is running; stop it instead")
        token = str(lease.get("token", ""))
        reply = await sup.call(
            "POST", f"leases/{token}/clear", {"operator": operator, "reason": reason}
        )
        return {
            "enclosure": enc.name,
            "lease": reply.get("id"),
            "holder": reply.get("holder"),
            "released_now": reply.get("released_now"),
        }

    async def _hold(
        self, enc: ConsoleEnclosure, action: str, operator: str, reason: str, telescope: Any
    ) -> dict[str, Any]:
        whole = telescope is None
        if action == "park" and whole:
            # One telescope, now; the whole enclosure is "stop" (which closes).
            raise ApiError(400, "park: name one telescope (to park them all, stop the enclosure)")
        if not whole and telescope not in enc.telescopes:
            raise ApiError(400, f"telescope: one of {enc.telescopes} (or none: all of them)")
        tels = list(enc.telescopes) if whole else [str(telescope)]
        out: dict[str, Any] = {"enclosure": enc.name, "telescopes": tels, "action": action}
        if enc.control_dir is None:
            if not (action == "stop" and whole):
                raise ApiError(
                    409, f"{enc.name} has no operator_control: pause, resume, stop and park need it"
                )
        else:
            cid = send_command(enc.control_dir, action, tels, operator=operator, reason=reason)
            out["command"] = cid
            if (action == "stop" and whole) or action == "park":
                out["hold_confirmed"] = await self._applied(enc, cid)
        if action == "stop" and whole:
            # Abort and park everything: the supervisor revokes the lease (the
            # sequencer cleans up: abort, park, release) and, with no lease,
            # closes. While held, the sequencer asks for no new lease.
            reply = await self.supervisors[enc.name].call(
                "POST", "close", {"operator": operator, "reason": f"stop: {reason}"}
            )
            out["lease_revoked"] = bool(reply.get("revoked"))
        return out

    # -- emergency close ----------------------------------------------------------------

    async def _emergency_close(
        self, operator: str, reason: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Close one enclosure (``enclosure``) or every one (omitted), now
        (docs/console.md, "Emergency close")."""
        name = body.get("enclosure")
        if name is None:
            encs = list(self.enclosures.values())
        elif str(name) in self.enclosures:
            encs = [self.enclosures[str(name)]]
        else:
            raise ApiError(400, f"enclosure: one of {sorted(self.enclosures)} (or none: all)")
        direct = body.get("direct", False)
        if not isinstance(direct, bool):
            raise ApiError(400, "direct: true or false")
        results = await asyncio.gather(
            *(self._close_one(e, operator, reason, direct) for e in encs)
        )
        out = {"enclosures": {e.name: r for e, r in zip(encs, results, strict=True)}}
        if not all(r["closing"] for r in results):
            failed = [e.name for e, r in zip(encs, results, strict=True) if not r["closing"]]
            raise ApiError(502, f"emergency close NOT accepted for {failed}: {out}")
        return out

    async def _close_one(
        self, enc: ConsoleEnclosure, operator: str, reason: str, direct: bool
    ) -> dict[str, Any]:
        c = self.config
        out: dict[str, Any] = {"closing": False, "via": None}
        # 1. Hold every telescope, so the sequencer asks for no new lease and
        #    the supervisor does not reopen (as for an enclosure stop; not
        #    waited for: closing comes first).
        if enc.control_dir is not None:
            try:
                out["hold"] = send_command(
                    enc.control_dir, "stop", enc.telescopes, operator=operator, reason=reason
                )
            except OSError as exc:
                out["hold_error"] = repr(exc)
        # 2. The supervisor (parks first on a roll-off roof), within emergency_timeout.
        why = "skipped: direct close asked for"
        if not direct:
            try:
                async with asyncio.timeout(c.emergency_timeout):
                    reply = await self.supervisors[enc.name].call(
                        "POST", "close", {"operator": operator, "reason": f"EMERGENCY: {reason}"}
                    )
                out.update(closing=True, via="supervisor", lease_revoked=bool(reply.get("revoked")))
            except TimeoutError:
                why = f"no answer within {c.emergency_timeout} s"
            except (ApiError, httpx.HTTPError) as exc:
                why = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            if out["closing"]:
                self._emergency_event(enc, operator, reason, out)
                return out
        out["supervisor"] = why
        # 3. The dome's own close-only channel (does not wait for parking).
        if enc.emergency_fallback != "dome" or enc.emergency_token_file is None or not enc.dome_url:
            out["error"] = f"supervisor: {why}; no emergency channel to the dome configured"
            self._emergency_event(enc, operator, reason, out)
            return out
        try:
            token = read_token(enc.emergency_token_file)
            reply = await close_dome(
                self.http,
                enc.dome_url,
                token,
                operator=operator,
                reason=reason,
                via=f"console ({why})",
                timeout=c.emergency_dome_timeout,
            )
            out.update(closing=True, via="dome", shutter=reply.get("shutter"))
        except (OSError, ValueError, EmergencyFailed) as exc:
            out["error"] = f"supervisor: {why}; dome: {exc}"
        self._emergency_event(enc, operator, reason, out)
        return out

    def _emergency_event(
        self, enc: ConsoleEnclosure, operator: str, reason: str, out: dict[str, Any]
    ) -> None:
        """Remember an emergency close: shown as an alarm until acknowledged."""
        when = datetime.now(UTC).isoformat()
        if not out["closing"]:
            level, text = "critical", f"EMERGENCY CLOSE FAILED by {operator} at {when}"
        elif out["via"] == "dome":
            level = "critical"
            text = (
                f"EMERGENCY CLOSE sent directly to the dome by {operator} at {when} "
                "(telescopes not parked first; ClearFaults to reopen)"
            )
        else:
            level, text = "alarm", f"emergency close by {operator} at {when} (supervisor)"
        detail = f"{reason}; " + "; ".join(
            f"{k}: {out[k]}" for k in ("supervisor", "error") if out.get(k)
        )
        log.critical("console: %s: %s (%s)", enc.name, text, detail)
        self.emergencies.append(
            {
                "id": alarm_id(enc.name, "emergency", text),
                "enclosure": enc.name,
                "source": "console",
                "level": level,
                "text": text,
                "detail": detail.strip("; "),
            }
        )
        del self.emergencies[: -self.EMERGENCIES]

    async def _applied(self, enc: ConsoleEnclosure, cid: str) -> bool:
        assert enc.control_dir is not None
        deadline = time.monotonic() + self.config.hold_ack_timeout
        while time.monotonic() < deadline:
            doc = read_holds(enc.control_dir) or {}
            if any(isinstance(a, dict) and a.get("id") == cid for a in doc.get("applied", [])):
                return True
            await asyncio.sleep(0.1)
        return False


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "body is not JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def _cookies(header: str) -> dict[str, str]:
    out = {}
    for part in header.split(";"):
        k, sep, v = part.strip().partition("=")
        if sep:
            out.setdefault(k, v)
    return out


def _csp(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; manifest-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


# -- HTTP ---------------------------------------------------------------------------------------


SECURITY_HEADERS = (
    "Cache-Control: no-store",
    "X-Content-Type-Options: nosniff",
    "X-Frame-Options: DENY",
    "Referrer-Policy: no-referrer",
)


class ConsoleHttpServer(HttpServer):
    """The request API's defensive HTTP/1.1 server, plus HTML pages and
    server-sent event streams."""

    def __init__(
        self,
        app: Console,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(app, host, port, tls=tls)  # type: ignore[arg-type]
        self.console = app

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if peer else "?"
        if len(self._writers) >= self.config.max_connections:
            await self._send(writer, Response(503, {"error": "too many connections"}), False)
            writer.close()
            return
        self._writers.add(writer)
        try:
            while True:
                try:
                    async with asyncio.timeout(self.config.idle_timeout):
                        line = await reader.readline()
                except TimeoutError:
                    break
                except ValueError:
                    too_long = Response(414, {"error": "request line too long"})
                    await self._send(writer, too_long, False)
                    break
                if not line:
                    break
                try:
                    async with asyncio.timeout(self.config.header_timeout):
                        req = await self._read(line, reader, writer)
                except _HttpError as exc:
                    await self._send(writer, Response(exc.status, {"error": str(exc)}), False)
                    break
                except TimeoutError:
                    await self._send(writer, Response(408, {"error": "request timeout"}), False)
                    break
                resp = await self.console.handle(req, _client_addr(peer_ip, req.headers))
                if isinstance(resp, StreamResponse):
                    await self._stream(writer, resp)
                    break
                keep = req.keep_alive and resp.status < 500
                await self._send(writer, resp, keep)
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except Exception:
            log.exception("console connection from %s", peer_ip)
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _send(self, writer: asyncio.StreamWriter, resp: Any, keep: bool) -> None:
        if isinstance(resp, RawResponse):
            data, ctype = resp.body, resp.content_type
        else:
            data = json.dumps(resp.payload, default=str).encode()
            ctype = "application/json; charset=utf-8"
        head = [
            f"HTTP/1.1 {resp.status} {HTTPStatus(resp.status).phrase}",
            f"Content-Type: {ctype}",
            f"Content-Length: {len(data)}",
            *SECURITY_HEADERS,
            f"Connection: {'keep-alive' if keep else 'close'}",
            *(f"{k}: {v}" for k, v in resp.headers.items()),
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        await writer.drain()

    async def _stream(self, writer: asyncio.StreamWriter, resp: StreamResponse) -> None:
        head = [
            "HTTP/1.1 200 OK",
            "Content-Type: text/event-stream; charset=utf-8",
            *SECURITY_HEADERS,
            "X-Accel-Buffering: no",
            "Connection: close",
            *(f"{k}: {v}" for k, v in resp.headers.items()),
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        try:
            await writer.drain()
            async for chunk in resp.stream:
                writer.write(chunk)
                async with asyncio.timeout(self.config.idle_timeout):
                    await writer.drain()
        except (ConnectionError, TimeoutError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await resp.stream.aclose()  # type: ignore[attr-defined]
            if resp.on_close is not None:
                resp.on_close()


# -- building and running it ----------------------------------------------------------------------


def load_console_config(path: str | Path) -> tuple[ConsoleConfig, dict[str, Any], Path]:
    """(ConsoleConfig, the enclosures entry, the config's directory)."""
    path = Path(path)
    raw = load_config(path)
    raw.pop("_note", None)
    enclosures = raw.pop("enclosures", None)
    if not isinstance(enclosures, dict) or not enclosures:
        raise ValueError(f"{path}: 'enclosures' must name at least one enclosure")
    known = {f.name for f in fields(ConsoleConfig)}
    bad = set(raw) - known
    if bad:
        raise ValueError(f"{path}: unknown keys {sorted(bad)}")
    config = ConsoleConfig(**raw)
    base = path.parent
    if not config.tokens:
        raise ValueError(f"{path}: 'tokens' (the request API's tokens file) is required")
    for key in ("tokens", "audit_log", "acks", "tls_cert", "tls_key"):
        v = getattr(config, key)
        if v is not None:
            p = Path(v)
            setattr(config, key, str(p if p.is_absolute() else base / p))
    return config, enclosures, base


def build_enclosures(spec: dict[str, Any], base: Path) -> dict[str, ConsoleEnclosure]:
    return {n: ConsoleEnclosure.from_spec(n, s, base) for n, s in spec.items()}


def tls_context(config: ConsoleConfig) -> ssl.SSLContext | None:
    if config.tls_cert is None and config.tls_key is None:
        return None
    if not (config.tls_cert and config.tls_key):
        raise ValueError("tls_cert and tls_key go together")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config.tls_cert, config.tls_key)
    return ctx


async def serve(
    config: ConsoleConfig,
    enclosures: dict[str, ConsoleEnclosure],
    *,
    insecure_bind: bool = False,
) -> None:
    """Run until SIGTERM or SIGINT; prints ``{"url": ...}`` once listening."""
    tls = tls_context(config)
    host, _, port = config.listen.rpartition(":")
    host = host.strip("[]")
    check_bind(host, tls, insecure_bind)
    tokens = TokenStore(config.tokens)
    if not any(r.active(datetime.now(UTC)) for r in tokens.records()):
        log.warning("no active tokens in %s: nobody can sign in", config.tokens)
    app = Console(enclosures, tokens, config=config, tls=tls is not None)
    server = await ConsoleHttpServer(app, host, int(port), tls=tls).start()
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    print(json.dumps({"url": server.url}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await app.aclose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.console", description="The observer console."
    )
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the console")
    sp.add_argument("--config", required=True)
    sp.add_argument(
        "--insecure-bind",
        action="store_true",
        help="allow a non-loopback listen address without TLS",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config, spec, base = load_console_config(args.config)
        check_bind(
            config.listen.rpartition(":")[0].strip("[]"), tls_context(config), args.insecure_bind
        )
        enclosures = build_enclosures(spec, base)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        ap.error(f"{args.config}: {exc}")
    try:
        asyncio.run(serve(config, enclosures, insecure_bind=args.insecure_bind))
    except asyncio.CancelledError:
        pass


__all__ = [
    "MAX_LINE",
    "Console",
    "ConsoleConfig",
    "ConsoleHttpServer",
    "SupervisorClient",
    "build_enclosures",
    "load_console_config",
    "main",
    "serve",
]
