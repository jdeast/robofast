"""The request API: the one internet-facing surface (design.md 3.7).

A small authenticated REST API over HTTP/1.1 and JSON for submitting,
validating, following and canceling observing requests
(:mod:`robofast_core.request_model`), submitting targets of opportunity, and
reading the enclosures' status. The safety supervisor's API and Alpaca stay
on the control network; this process only reads their status and talks to
the dispatch scheduler through files (below), so a bug here cannot move
hardware or take down a night.

Running it
----------
::

    python -m robofast_core.request_api serve --config request_api.json
    python -m robofast_core.request_api tokens add --config request_api.json \\
        --name my-tom --scope read --scope submit         # prints the token once
    python -m robofast_core.request_api tokens list   --config request_api.json
    python -m robofast_core.request_api tokens revoke --config request_api.json --name my-tom

The config (``sites/minerva/request_api.example.json``)::

    {
      "listen": "127.0.0.1:8300",
      "tokens": "/etc/robofast/api-tokens.json",      # outside any repository
      "audit_log": "/var/log/robofast/request-api-audit.jsonl",
      "enclosures": {
        "aqawan1": {"config": "aqawan1.json", "plan": "dispatch.example.json"}
      },
      ...ApiConfig fields (rate limits, size limits, timeouts, TLS, data paths)
    }

Each enclosure names its enclosure config (the file its supervisor and
sequencer read) and, if the sequencer runs with ``--plan``, that plan file;
the plan must be a dispatch plan with ``state`` and ``too_dropbox`` set.

**Transport.** It must sit behind a TLS reverse proxy (``deploy/`` has nginx
and Caddy examples) and listens on loopback. It refuses to listen on any
other address unless it terminates TLS itself (``tls_cert``/``tls_key``) or
``--insecure-bind`` is given. Requests are limited in size (``max_body``,
header and URL length, header count), in time (``header_timeout``,
``idle_timeout``, ``handler_timeout``) and in number (``max_connections``,
per-token rate limits); a chunked body is refused (send Content-Length).
Errors are JSON ``{"error": ...}``; an unexpected one is logged with an
incident id and answered 500 with only that id, never a stack trace.

**Authentication** (:mod:`robofast_core.api_auth`): ``Authorization: Token
<token>`` (or ``Bearer``), per-client tokens hashed at rest, scopes
``read``/``submit``/``too``/``admin``, per-token rate limits, and an audit
log line for every POST (including refused ones).

Endpoints
---------
All under ``/api/v1`` (a trailing slash is ignored):

* ``GET /`` -- who am I: the token's name, scopes and the enclosures.
* ``POST /requestgroups`` (``submit``) -- submit a request group (the
  :class:`~robofast_core.request_model.RequestGroup` JSON, without ``too``).
  Ids may be omitted (they are generated); ``status``, ``submitted`` and
  ``submitter`` are set here. 202 with the ids; the group is ``QUEUED``
  until the scheduler takes it in. ``?enclosure=NAME`` picks the enclosure
  when the request's ``telescopes`` do not.
* ``POST /requestgroups/validate`` (``submit`` or ``too``) -- the same checks
  without submitting, plus a dry run over the coming night on a coarse grid:
  which telescopes could start each request, when, and the commonest reasons
  why not.
* ``GET /requestgroups`` (``read``) -- this client's groups (``admin``: all),
  ``?state=`` and ``?limit=`` filter.
* ``GET /requestgroups/{id}`` (``read``) -- one group, with each
  configuration's status.
* ``POST /requestgroups/{id}/cancel`` (``submit`` for a normal group,
  ``too`` for a ToO; own groups, or ``admin``).
* ``POST /toos`` (``too``) -- submit a target of opportunity (a group with
  a ``too`` entry).
* ``GET /status`` (``read``) -- each enclosure: weather verdict and reason,
  shutter, lease, faults (from the supervisor), and each telescope's state
  and current observation (from the sequencer's status snapshot).
* ``GET /requestgroups/{id}/data`` (``read``) -- data pointers: each
  completed observation of each configuration (telescope, time, frames) and
  its ``files``: the frames as the sequencer wrote them (relative to its
  data root; ``docs/data.md``), or, for a record without them, the paths
  expected from ``data_path_template`` if that is set. No files are served.

``GET /healthz`` (no token) answers ``{"ok": true}`` for the proxy.

Intake and status
-----------------
The dispatch scheduler, running in the sequencer process, is the source of
truth. A submission or cancellation is a file renamed into the enclosure's
drop box (``<time>-submit-<id>.json``, ``<time>-cancel-<id>.json``); the
scheduler takes the files in, in order, every ``too_poll`` seconds (10 by
default) and records the result in its state file, which this process reads
(never writes). So a submission made while the sequencer is down waits in the
drop box and is taken in when it starts. A group's ``intake`` is ``QUEUED``
(in the drop box), ``ACCEPTED`` (in the scheduler's state; ``state`` and the
configurations' ``status`` come from there), ``REJECTED`` (the scheduler
refused it: ``error`` says why) or ``CANCELED`` (canceled while still
queued). Canceling a queued group takes its file back out of the drop box;
canceling an accepted one asks the scheduler, which cancels every
configuration not yet finished: an observation in progress finishes, then
its visit ends.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from robofast_core.api_auth import (
    SCOPES,
    AuditLog,
    RateLimiter,
    TokenRecord,
    TokenStore,
    inside_git_worktree,
)
from robofast_core.config import load_config, telescopes
from robofast_core.dispatch import DispatchScheduler, dispatch_from_config
from robofast_core.request_model import RequestGroup, Status

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
GROUP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")
SUB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}")
RESERVED_IDS = {"validate"}
_DROP_RE = re.compile(r"(\d{20})-(submit|cancel)-(.+)\.json")
#: Size limits on a submitted group (the body limit bounds the rest).
MAX_REQUESTS = 20
MAX_CONFIGURATIONS = 20
MAX_INSTRUMENT_CONFIGS = 50
MAX_WINDOWS = 50
MAX_TEXT = 200
#: HTTP limits.
MAX_LINE = 8192
MAX_HEADERS = 64


@dataclass
class ApiConfig:
    listen: str = "127.0.0.1:8300"
    #: The tokens file (required; keep it outside any repository).
    tokens: str = ""
    #: JSON lines, one per POST; None logs them instead.
    audit_log: str | None = None
    max_body: int = 65536
    #: Per token: sustained requests per minute, and the burst allowed.
    rate_per_minute: float = 60.0
    rate_burst: float = 20.0
    #: A validation (dry run) costs this many requests.
    validate_cost: float = 5.0
    #: Failed authentications allowed per client address (per minute, burst).
    auth_fail_per_minute: float = 30.0
    auth_fail_burst: float = 10.0
    header_timeout: float = 10.0
    idle_timeout: float = 30.0
    handler_timeout: float = 30.0
    max_connections: int = 64
    #: The dry run's grid step and horizon (s), and its computation budget.
    validate_step: float = 900.0
    validate_horizon: float = 86400.0
    validate_budget: float = 20.0
    #: Timeout for reading the supervisor's status.
    status_timeout: float = 2.0
    #: Terminate TLS here (normally the reverse proxy does).
    tls_cert: str | None = None
    tls_key: str | None = None
    #: Expected path of each frame, e.g.
    #: "/data/{night}/{telescope}/{configuration}.{instrument_config}.{frame:04d}.fits";
    #: fields: enclosure, night (YYYYMMDD, local), telescope, group, request,
    #: configuration, instrument_config, frame. None: no paths.
    data_path_template: str | None = None


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        errors: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status, self.message = status, message
        self.errors = errors or []
        self.headers = headers or {}


@dataclass
class Response:
    status: int
    payload: Any
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class HttpRequest:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes = b""

    @property
    def keep_alive(self) -> bool:
        conn = self.headers.get("connection", "").lower()
        if self.version == "HTTP/1.0":
            return conn == "keep-alive"
        return conn != "close"


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat()


def _group_state(statuses: list[Status]) -> str:
    if any(s.schedulable for s in statuses):
        return "PENDING"
    kinds = set(statuses)
    return str(kinds.pop()) if len(kinds) == 1 else "DONE"


# -- one enclosure, as seen from here ---------------------------------------------------


class Enclosure:
    """An enclosure's dispatch scheduler, seen through its files: the
    request file and state file (read) and the drop box (written)."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        plan: dict[str, Any],
        *,
        config_base: Path,
        plan_base: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self.telescopes = list(telescopes(config))
        plan = dict(plan)
        plan.pop("_note", None)
        if plan.pop("type", None) != "dispatch":
            raise ValueError(f"enclosure {name}: the plan is not a dispatch plan")
        for key in ("state", "too_dropbox"):
            if key not in plan:
                raise ValueError(f"enclosure {name}: the dispatch plan needs {key!r}")
        self.state_path = self._resolve(plan.pop("state"), plan_base)
        self.dropbox = self._resolve(plan.pop("too_dropbox"), plan_base)
        self.plan, self.plan_base = plan, plan_base
        self.clock = clock or (lambda: datetime.now(UTC))
        self.supervisor_url = (
            config.get("supervisor_url") or f"http://{config.get('listen', '127.0.0.1:8100')}"
        )
        sf = config.get("status_file")
        self.status_file = None if sf is None else self._resolve(sf, config_base)
        #: For dry runs: the same limits and settings, no requests.
        empty = {k: v for k, v in plan.items() if k not in ("requests", "legacy_csv")}
        self.planner = dispatch_from_config(
            empty, self.telescopes, base=plan_base, clock=self.clock
        )
        self.state_error: str | None = None
        self._view: DispatchScheduler | None = None
        self._stamp: tuple[Any, ...] | None = None
        self._last_ns = 0
        self.view()  # a broken request file is a startup error

    @staticmethod
    def _resolve(p: str, base: Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else base / path

    @staticmethod
    def _stamp_of(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def view(self) -> DispatchScheduler:
        """The scheduler as of its state file (rebuilt when a file changes)."""
        files = [self.state_path]
        for key in ("requests", "legacy_csv"):
            if isinstance(self.plan.get(key), str):
                files.append(self._resolve(self.plan[key], self.plan_base))
        stamp = tuple(self._stamp_of(f) for f in files)
        if self._view is not None and stamp == self._stamp:
            return self._view
        try:
            view = dispatch_from_config(
                self.plan, self.telescopes, base=self.plan_base, clock=self.clock
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if self._view is None:
                raise
            log.error("%s: request file unreadable (%r); keeping the last view", self.name, exc)
            return self._view
        self.state_error = None
        if stamp[0] is not None:
            try:
                view.restore_state(json.loads(self.state_path.read_text()))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.state_error = f"scheduler state unreadable: {exc!r}"
                log.error("%s: %s", self.name, self.state_error)
        self._view, self._stamp = view, stamp
        return view

    # -- the drop box --------------------------------------------------------------

    def drop(self, kind: str, gid: str, payload: dict[str, Any]) -> Path:
        """Write ``payload`` into the drop box atomically; names sort in
        submission order."""
        self._last_ns = max(self._last_ns + 1, time.time_ns())
        name = f"{self._last_ns:020d}-{kind}-{gid}.json"
        self.dropbox.mkdir(parents=True, exist_ok=True)
        tmp = self.dropbox / f".{name}.tmp"
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, self.dropbox / name)
        return self.dropbox / name

    def queued(self) -> list[tuple[Path, Any]]:
        out = []
        for path in sorted(self.dropbox.glob("*.json")):
            try:
                out.append((path, json.loads(path.read_text())))
            except (OSError, ValueError):
                continue  # taken in meanwhile, or not ours
        return out

    def filed(self, sub: str) -> dict[str, Path]:
        """Our submissions in ``accepted/``, ``rejected/`` or ``canceled/``,
        by group id (the latest wins)."""
        out: dict[str, Path] = {}
        d = self.dropbox / sub
        if d.is_dir():
            for path in sorted(d.glob("*.json")):
                m = _DROP_RE.fullmatch(path.name)
                if m and m.group(2) == "submit":
                    out[m.group(3)] = path
        return out

    def cancel_queued(self, path: Path) -> bool:
        """Take a queued submission back out of the drop box; False if the
        scheduler took it first."""
        dest = self.dropbox / "canceled"
        dest.mkdir(exist_ok=True)
        try:
            os.replace(path, dest / path.name)
        except FileNotFoundError:
            return False
        return True

    # -- what we know about each group -------------------------------------------

    def entries(self) -> dict[str, dict[str, Any]]:
        """Every group we can report on, by id: the scheduler's, then the
        queued, canceled-while-queued and rejected submissions."""
        view = self.view()
        queued = self.queued()
        cancel_pending = {
            str(d["cancel"]) for _, d in queued if isinstance(d, dict) and "cancel" in d
        }
        out: dict[str, dict[str, Any]] = {}
        for gid, g in view.groups.items():
            d = g.to_dict()
            d.update(
                enclosure=self.name,
                intake="ACCEPTED",
                state=_group_state([c.status for c in g.configurations()]),
                cancel_pending=gid in cancel_pending,
            )
            out[gid] = d
        for path, d in queued:
            if isinstance(d, dict) and "cancel" not in d and str(d.get("id")) not in out:
                out[str(d.get("id"))] = self._filed_entry(d, "QUEUED", _path=path)
        for gid, path in self.filed("canceled").items():
            if gid not in out:
                out[gid] = self._filed_entry(self._read(path), "CANCELED")
        for gid, path in self.filed("rejected").items():
            if gid not in out:
                err = path.with_name(path.name + ".error")
                text = err.read_text().strip() if err.exists() else "rejected"
                out[gid] = self._filed_entry(self._read(path), "REJECTED", error=text)
        return out

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _filed_entry(self, d: dict[str, Any], intake: str, **extra: Any) -> dict[str, Any]:
        d = dict(d)
        status = "CANCELED" if intake == "CANCELED" else "PENDING"
        for r in d.get("requests", []) if isinstance(d.get("requests"), list) else []:
            for c in r.get("configurations", []) if isinstance(r, dict) else []:
                if isinstance(c, dict):
                    c["status"] = status
        d.update(enclosure=self.name, intake=intake, state=intake, **extra)
        return d


# -- the application ---------------------------------------------------------------------


class RequestApi:
    """The endpoints (module doc), independent of the HTTP transport."""

    def __init__(
        self,
        enclosures: dict[str, Enclosure],
        tokens: TokenStore,
        *,
        config: ApiConfig | None = None,
        audit: AuditLog | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.enclosures = enclosures
        self.tokens = tokens
        self.config = config or ApiConfig()
        self.audit = audit or AuditLog(self.config.audit_log)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.limiter = RateLimiter(self.config.rate_per_minute, self.config.rate_burst)
        self.fail_limiter = RateLimiter(
            self.config.auth_fail_per_minute, self.config.auth_fail_burst
        )
        self._http = http
        self._own_http = http is None
        #: Dry runs are CPU-bound (astropy): one at a time, off the event loop.
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="dry-run")
        if self.config.data_path_template is not None:
            self._data_path(self.config.data_path_template, _SAMPLE_FIELDS)  # fail at startup

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- the request cycle ------------------------------------------------------------

    async def handle(self, req: HttpRequest, addr: str) -> Response:
        """Answer one request; never raises, never leaks a stack trace."""
        audit: dict[str, Any] | None = None
        if req.method == "POST":
            audit = {"addr": addr, "method": req.method, "path": urlsplit(req.target).path}
        try:
            async with asyncio.timeout(self.config.handler_timeout):
                resp = await self._handle(req, addr, audit)
        except ApiError as exc:
            payload: dict[str, Any] = {"error": exc.message}
            if exc.errors:
                payload["errors"] = exc.errors
            resp = Response(exc.status, payload, exc.headers)
        except TimeoutError:
            resp = Response(503, {"error": "the request took too long"})
        except Exception:
            incident = secrets.token_hex(4)
            log.exception("request API: incident %s (%s %s)", incident, req.method, req.target)
            resp = Response(500, {"error": "internal error", "incident": incident})
        if audit is not None:
            audit["status"] = resp.status
            if resp.status >= 400 and isinstance(resp.payload, dict):
                audit["error"] = resp.payload.get("error")
                if resp.payload.get("errors"):
                    audit["errors"] = resp.payload["errors"][:10]
            self.audit.write(**audit)
        return resp

    async def _handle(self, req: HttpRequest, addr: str, audit: dict[str, Any] | None) -> Response:
        url = urlsplit(req.target)
        path = unquote(url.path)
        if path.rstrip("/") == "/healthz" and req.method == "GET":
            return Response(200, {"ok": True})
        if not path.startswith(PREFIX + "/") and path != PREFIX:
            raise ApiError(404, "not found")
        client = self._authenticate(req, addr)
        if audit is not None:
            audit.update(client=client.name, token_id=client.id)
        parts = [p for p in path[len(PREFIX) :].split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        cost = self.config.validate_cost if parts[-1:] == ["validate"] else 1.0
        ok, wait = self.limiter.allow(client.id, cost)
        if not ok:
            raise ApiError(
                429, "rate limit exceeded", headers={"Retry-After": str(max(1, round(wait)))}
            )
        route, gid = self._route(req.method, parts)
        if gid is not None and audit is not None:
            audit["group"] = gid
        if route == "whoami":
            return Response(
                200,
                {
                    "name": client.name,
                    "scopes": client.scopes,
                    "enclosures": {n: e.telescopes for n, e in self.enclosures.items()},
                },
            )
        if route == "submit":
            self._need(client, "submit")
            return self._submit(client, req.body, query, too=False, audit=audit)
        if route == "too":
            self._need(client, "too")
            return self._submit(client, req.body, query, too=True, audit=audit)
        if route == "validate":
            self._need(client, "submit", "too")
            return await self._validate(client, req.body, query)
        if route == "list":
            self._need(client, "read")
            return self._list(client, query)
        if route == "get":
            self._need(client, "read")
            entry = self._find(client, gid)[1]
            return Response(200, {k: v for k, v in entry.items() if not k.startswith("_")})
        if route == "data":
            self._need(client, "read")
            return Response(200, self._data(client, gid))
        if route == "cancel":
            self._need(client, "submit", "too")
            return self._cancel(client, gid)
        if route == "status":
            self._need(client, "read")
            return Response(200, await self._status())
        raise ApiError(404, "not found")  # pragma: no cover

    _ROUTES: ClassVar[dict[tuple[str, ...], dict[str, str]]] = {
        (): {"GET": "whoami"},
        ("requestgroups",): {"GET": "list", "POST": "submit"},
        ("requestgroups", "validate"): {"POST": "validate"},
        ("requestgroups", "*"): {"GET": "get"},
        ("requestgroups", "*", "cancel"): {"POST": "cancel"},
        ("requestgroups", "*", "data"): {"GET": "data"},
        ("toos",): {"POST": "too"},
        ("status",): {"GET": "status"},
    }

    def _route(self, method: str, parts: list[str]) -> tuple[str, str | None]:
        gid = None
        key = tuple(parts)
        if key not in self._ROUTES and len(parts) >= 2 and parts[0] == "requestgroups":
            gid = parts[1]
            key = ("requestgroups", "*", *parts[2:])
        methods = self._ROUTES.get(key)
        if methods is None:
            raise ApiError(404, "not found")
        if method not in methods:
            raise ApiError(405, f"{method} not allowed", headers={"Allow": ", ".join(methods)})
        return methods[method], gid

    def _authenticate(self, req: HttpRequest, addr: str) -> TokenRecord:
        scheme, _, token = req.headers.get("authorization", "").partition(" ")
        rec = None
        self.tokens.reload()
        if scheme.lower() in ("token", "bearer") and token.strip():
            rec = self.tokens.authenticate(token.strip())
        if rec is not None:
            return rec
        ok, wait = self.fail_limiter.allow(f"addr:{addr}")
        if not ok:
            raise ApiError(
                429, "too many failed attempts", headers={"Retry-After": str(max(1, round(wait)))}
            )
        what = "invalid token" if token.strip() else "missing token"
        raise ApiError(401, what, headers={"WWW-Authenticate": 'Token realm="robofast"'})

    @staticmethod
    def _need(client: TokenRecord, *scopes: str) -> None:
        if not any(client.allows(s) for s in scopes):
            raise ApiError(403, f"this token lacks the {' or '.join(scopes)} scope")

    # -- submissions ---------------------------------------------------------------------

    def _parse(
        self, client: TokenRecord, body: bytes, query: dict[str, str], too: bool | None
    ) -> tuple[RequestGroup, Enclosure, list[str]]:
        """Parse and check a submitted group: (group, enclosure, problems
        found against the enclosure). Raises ApiError(400) if it does not
        parse at all."""
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"body is not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object (a request group)")
        errors = _check_input(data)
        if errors:
            raise ApiError(400, "invalid request group", errors)
        if too is True and data.get("too") is None:
            raise ApiError(400, "invalid request group", ["too: required for a ToO (/toos)"])
        if too is False and data.get("too") is not None:
            raise ApiError(
                400, "invalid request group", ["too: submit targets of opportunity to /toos"]
            )
        _fill_ids(data)
        data["submitter"] = client.name
        data.pop("submitted", None)
        try:
            group = RequestGroup.from_dict(data, "request group")
        except ValueError as exc:
            raise ApiError(400, "invalid request group", [str(exc)]) from None
        enclosure = self._enclosure_for(group, query)
        return group, enclosure, self._problems(group, enclosure)

    def _enclosure_for(self, group: RequestGroup, query: dict[str, str]) -> Enclosure:
        if "enclosure" in query:
            enc = self.enclosures.get(query["enclosure"])
            if enc is None:
                raise ApiError(400, f"unknown enclosure {query['enclosure']!r}")
            return enc
        named = {t for r in group.requests for t in (r.telescopes or [])}
        if named:
            owners = [e for e in self.enclosures.values() if named & set(e.telescopes)]
            if len(owners) == 1:
                return owners[0]
            if len(owners) > 1:
                raise ApiError(400, f"telescopes {sorted(named)} span several enclosures")
            raise ApiError(400, "invalid request group", [f"unknown telescopes {sorted(named)}"])
        if len(self.enclosures) == 1:
            return next(iter(self.enclosures.values()))
        raise ApiError(400, f"name an enclosure (?enclosure=...): one of {sorted(self.enclosures)}")

    def _problems(self, group: RequestGroup, enc: Enclosure) -> list[str]:
        """What would stop this group from ever running on ``enc``."""
        now = self.clock()
        out = []
        known: set[str] = set()
        for e in self.enclosures.values():
            for gid, d in e.entries().items():
                if d["intake"] == "REJECTED":
                    continue
                known.add(gid)
                for r in d.get("requests", []):
                    known.add(str(r.get("id")))
                    known.update(str(c.get("id")) for c in r.get("configurations", []))
        ids = [group.id, *(r.id for r in group.requests), *(c.id for c in group.configurations())]
        out += [f"id {i!r} is already in use" for i in ids if i in known]
        for i, r in enumerate(group.requests):
            what = f"requests[{i}] ({r.id})"
            tels = r.telescopes or enc.telescopes
            unknown = sorted(set(tels) - set(enc.telescopes))
            if unknown:
                out.append(f"{what}.telescopes: {unknown} not in {enc.name} {enc.telescopes}")
                continue
            for j, c in enumerate(r.configurations):
                if not any(c.instrument in enc.planner.limits[t].instruments for t in tels):
                    have = sorted({x for t in tels for x in enc.planner.limits[t].instruments})
                    out.append(
                        f"{what}.configurations[{j}].instrument: no {c.instrument!r} on "
                        f"{tels} (they have {have})"
                    )
            if r.windows and all(w.end <= now for w in r.windows):
                out.append(f"{what}.windows: every window has already ended")
        if group.too is not None and group.too.deadline <= now:
            out.append(f"too.deadline: {group.too.deadline.isoformat()} has passed")
        return out

    def _submit(
        self,
        client: TokenRecord,
        body: bytes,
        query: dict[str, str],
        *,
        too: bool,
        audit: dict[str, Any] | None,
    ) -> Response:
        group, enc, problems = self._parse(client, body, query, too)
        if audit is not None:
            audit["group"] = group.id
        if problems:
            raise ApiError(400, "request group not accepted", problems)
        group.submitted = self.clock()
        enc.drop("submit", group.id, group.to_dict())
        log.info("%s: %s submitted %s", enc.name, client.name, group.id)
        return Response(
            202,
            {
                "id": group.id,
                "enclosure": enc.name,
                "intake": "QUEUED",
                "too": group.too is not None,
                "requests": [
                    {"id": r.id, "configurations": [c.id for c in r.configurations]}
                    for r in group.requests
                ],
                "url": f"{PREFIX}/requestgroups/{group.id}",
            },
        )

    async def _validate(self, client: TokenRecord, body: bytes, query: dict[str, str]) -> Response:
        group, enc, problems = self._parse(client, body, query, None)
        out: dict[str, Any] = {
            "valid": not problems,
            "errors": problems,
            "id": group.id,
            "enclosure": enc.name,
        }
        if not problems:
            loop = asyncio.get_running_loop()
            deadline = time.monotonic() + self.config.validate_budget
            out["dry_run"] = await loop.run_in_executor(
                self._pool, dry_run, enc, group, self.clock(), self.config, deadline
            )
        return Response(200, out)

    # -- reading -----------------------------------------------------------------------------

    def _visible(self, client: TokenRecord, entry: dict[str, Any]) -> bool:
        return client.allows("admin") or entry.get("submitter") == client.name

    def _find(self, client: TokenRecord, gid: str | None) -> tuple[Enclosure, dict[str, Any]]:
        for enc in self.enclosures.values():
            entry = enc.entries().get(str(gid))
            if entry is not None and self._visible(client, entry):
                return enc, entry
        raise ApiError(404, f"no request group {gid!r}")

    def _list(self, client: TokenRecord, query: dict[str, str]) -> Response:
        try:
            limit = max(1, min(1000, int(query.get("limit", 200))))
        except ValueError:
            raise ApiError(400, "limit: expected an integer") from None
        want = query.get("state")
        rows = []
        for enc in self.enclosures.values():
            for gid, d in enc.entries().items():
                if not self._visible(client, d):
                    continue
                if want is not None and want not in (d["state"], d["intake"]):
                    continue
                rows.append(
                    {
                        "id": gid,
                        "enclosure": enc.name,
                        "intake": d["intake"],
                        "state": d["state"],
                        "submitter": d.get("submitter", ""),
                        "submitted": d.get("submitted"),
                        "too": d.get("too") is not None,
                    }
                )
        rows.sort(key=lambda r: (r["submitted"] or "", r["id"]), reverse=True)
        return Response(200, {"count": len(rows), "results": rows[:limit]})

    def _data(self, client: TokenRecord, gid: str | None) -> dict[str, Any]:
        enc, entry = self._find(client, gid)
        view = enc.view()
        tmpl = self.config.data_path_template
        configs = []
        for r in entry.get("requests", []):
            for c in r.get("configurations", []):
                obs = view.observed.get(c["id"], []) if entry["intake"] == "ACCEPTED" else []
                records = []
                for o in obs:
                    rec = dict(o)
                    # Recorded paths (the frames as written) win over the
                    # template's expected ones.
                    if "files" not in rec and tmpl is not None:
                        t = datetime.fromisoformat(o["time"])
                        local = view.site.night_start(t) + timedelta(
                            hours=view.site.longitude / 15.0
                        )
                        rec["files"] = [
                            self._data_path(
                                tmpl,
                                {
                                    "enclosure": enc.name,
                                    "night": local.strftime("%Y%m%d"),
                                    "telescope": o["telescope"],
                                    "group": entry["id"],
                                    "request": r["id"],
                                    "configuration": c["id"],
                                    "instrument_config": o.get("instrument_config", 0),
                                    "frame": i,
                                },
                            )
                            for i in range(int(o.get("frames", 0)))
                        ]
                    records.append(rec)
                configs.append(
                    {
                        "id": c["id"],
                        "request": r["id"],
                        "status": c.get("status"),
                        "observations": records,
                    }
                )
        return {
            "id": entry["id"],
            "enclosure": enc.name,
            "configurations": configs,
            "paths_configured": tmpl is not None
            or any("files" in o for c in configs for o in c["observations"]),
        }

    @staticmethod
    def _data_path(template: str, values: dict[str, Any]) -> str:
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"data_path_template {template!r}: {exc!r}") from exc

    # -- cancel ----------------------------------------------------------------------------

    def _cancel(self, client: TokenRecord, gid: str | None) -> Response:
        enc, entry = self._find(client, gid)
        is_too = entry.get("too") is not None
        self._need(client, "too" if is_too else "submit")
        gid = entry["id"]
        if entry["intake"] == "QUEUED":
            if enc.cancel_queued(entry["_path"]):
                log.info("%s: %s canceled queued %s", enc.name, client.name, gid)
                return Response(200, {"id": gid, "intake": "CANCELED", "state": "CANCELED"})
            entry = {**entry, "intake": "ACCEPTED", "state": "PENDING"}  # just taken in
        if entry["intake"] != "ACCEPTED" or entry["state"] != "PENDING":
            return Response(
                200,
                {
                    "id": gid,
                    "intake": entry["intake"],
                    "state": entry["state"],
                    "canceled_now": False,
                    "detail": "nothing left to cancel",
                },
            )
        enc.drop("cancel", gid, {"cancel": gid, "by": client.name})
        log.info("%s: %s asked to cancel %s", enc.name, client.name, gid)
        return Response(
            202,
            {
                "id": gid,
                "intake": "ACCEPTED",
                "state": "PENDING",
                "cancel_pending": True,
                "detail": "the scheduler cancels it at its next drop-box check; an "
                "observation in progress finishes first",
            },
        )

    # -- status ------------------------------------------------------------------------------

    async def _status(self) -> dict[str, Any]:
        results = await asyncio.gather(
            *(self._enclosure_status(e) for e in self.enclosures.values())
        )
        return {
            "time": datetime.now(UTC).isoformat(),
            "enclosures": {
                e.name: r for e, r in zip(self.enclosures.values(), results, strict=True)
            },
        }

    async def _supervisor(self, enc: Enclosure) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.status_timeout)
        try:
            r = await self._http.get(
                f"{enc.supervisor_url}/status", timeout=self.config.status_timeout
            )
            r.raise_for_status()
            s = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s: supervisor status: %r", enc.name, exc)
            return {"reachable": False}
        lease = s.get("lease") if isinstance(s.get("lease"), dict) else None
        return {
            "reachable": True,
            # The weather verdict (the supervisor's, from the safety monitor).
            "safe": s.get("safe"),
            "reason": s.get("reason"),
            "open_allowed": s.get("open_allowed"),
            "shutter": s.get("shutter"),
            "fault": s.get("fault"),
            "alarms": len(s.get("alarms") or []),
            # Never the lease token: it is a capability on the control network.
            "lease": None
            if lease is None
            else {
                k: lease.get(k)
                for k in ("holder", "live", "opened", "revoked", "reason", "quarantined")
            },
        }

    def _sequencer(self, enc: Enclosure) -> dict[str, Any]:
        if enc.status_file is None:
            return {"running": None, "detail": "no status_file in the enclosure config"}
        try:
            snap = json.loads(enc.status_file.read_text())
            age = (datetime.now(UTC) - datetime.fromisoformat(snap["time"])).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            return {"running": False, "detail": "no status snapshot"}
        fresh = age <= float(snap.get("max_age", 15))
        return {
            "running": fresh,
            "age": round(age, 1),
            "quarantined": snap.get("quarantined"),
            "needs_operator": snap.get("needs_operator"),
            "telescopes": snap.get("telescopes", {}) if fresh else {},
        }

    async def _enclosure_status(self, enc: Enclosure) -> dict[str, Any]:
        sup = await self._supervisor(enc)
        seq = self._sequencer(enc)
        tels = {}
        for t in enc.telescopes:
            s = seq.get("telescopes", {}).get(t)
            tels[t] = (
                {"state": "unknown", "current": None}
                if not isinstance(s, dict)
                else {
                    "state": s.get("state"),
                    "exposing": s.get("exposing"),
                    "current": s.get("current"),
                }
            )
        seq.pop("telescopes", None)
        view = enc.view()
        out = {
            "supervisor": sup,
            "sequencer": seq,
            "telescopes": tels,
            "queue": {
                "pending": sum(
                    1
                    for g in view.groups.values()
                    if any(c.status.schedulable for c in g.configurations())
                ),
                "intake_waiting": sum(1 for _ in enc.dropbox.glob("*.json")),
            },
        }
        if enc.state_error:
            out["scheduler_error"] = "scheduler state unreadable"
        return out


_SAMPLE_FIELDS = {
    "enclosure": "e",
    "night": "20260101",
    "telescope": "t1",
    "group": "g",
    "request": "r",
    "configuration": "c",
    "instrument_config": 0,
    "frame": 0,
}


def _check_input(d: dict[str, Any]) -> list[str]:
    """What the model's own validation does not cover for untrusted input:
    string types, id syntax, sizes, and fields only the scheduler sets."""
    errs: list[str] = []

    def text(v: Any, what: str, limit: int = MAX_TEXT) -> None:
        if v is not None and (not isinstance(v, str) or len(v) > limit):
            errs.append(f"{what}: expected a string of at most {limit} characters")

    def ident(v: Any, what: str, pattern: re.Pattern[str]) -> None:
        if v is None:
            return
        if not isinstance(v, str) or not pattern.fullmatch(v):
            errs.append(
                f"{what}: {v!r} is not a valid id (letters, digits and ._:+-"
                f"{'/' if pattern is SUB_ID_RE else ''}, starting with a letter or digit)"
            )

    ident(d.get("id"), "id", GROUP_ID_RE)
    if d.get("id") in RESERVED_IDS:
        errs.append(f"id: {d['id']!r} is reserved")
    for k in ("name", "proposal"):
        text(d.get(k), k)
    reqs = d.get("requests")
    if not isinstance(reqs, list):
        return errs  # the model says so
    if len(reqs) > MAX_REQUESTS:
        errs.append(f"requests: at most {MAX_REQUESTS}")
    for i, r in enumerate(reqs[:MAX_REQUESTS]):
        if not isinstance(r, dict):
            continue
        rw = f"requests[{i}]"
        ident(r.get("id"), f"{rw}.id", SUB_ID_RE)
        if isinstance(r.get("windows"), list) and len(r["windows"]) > MAX_WINDOWS:
            errs.append(f"{rw}.windows: at most {MAX_WINDOWS}")
        tels = r.get("telescopes")
        if isinstance(tels, list):
            for j, t in enumerate(tels):
                text(t, f"{rw}.telescopes[{j}]", 64)
        configs = r.get("configurations")
        if not isinstance(configs, list):
            continue
        if len(configs) > MAX_CONFIGURATIONS:
            errs.append(f"{rw}.configurations: at most {MAX_CONFIGURATIONS}")
        for j, c in enumerate(configs[:MAX_CONFIGURATIONS]):
            if not isinstance(c, dict):
                continue
            cw = f"{rw}.configurations[{j}]"
            ident(c.get("id"), f"{cw}.id", SUB_ID_RE)
            for k in ("status", "failures"):
                if k in c:
                    errs.append(f"{cw}.{k}: set by the scheduler, not by clients")
            text(c.get("instrument"), f"{cw}.instrument", 64)
            if isinstance(c.get("target"), dict):
                text(c["target"].get("name"), f"{cw}.target.name")
            ics = c.get("instrument_configs")
            if isinstance(ics, list):
                if len(ics) > MAX_INSTRUMENT_CONFIGS:
                    errs.append(f"{cw}.instrument_configs: at most {MAX_INSTRUMENT_CONFIGS}")
                for k, ic in enumerate(ics[:MAX_INSTRUMENT_CONFIGS]):
                    if isinstance(ic, dict):
                        text(ic.get("filter"), f"{cw}.instrument_configs[{k}].filter", 64)
    return errs


def _fill_ids(d: dict[str, Any]) -> None:
    """Generate the ids a client left out."""
    gid = d.get("id") or f"rg-{secrets.token_hex(6)}"
    d["id"] = gid
    for i, r in enumerate(d.get("requests", [])):
        if isinstance(r, dict):
            rid = r.get("id") or f"{gid}-r{i}"
            r["id"] = rid
            for j, c in enumerate(r.get("configurations", []) or []):
                if isinstance(c, dict) and not c.get("id"):
                    c["id"] = f"{rid}-c{j}"


# -- the dry run ---------------------------------------------------------------------------


def _sun_altitudes(enc: Enclosure, times: list[datetime]) -> list[float]:
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time

    site = enc.planner.site
    t = Time(times)
    sun = get_body("sun", t, site.location).transform_to(AltAz(obstime=t, location=site.location))
    return [float(a) for a in sun.alt.deg]


def dry_run(
    enc: Enclosure,
    group: RequestGroup,
    now: datetime,
    config: ApiConfig,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Where and when each request of ``group`` could start during the
    coming night (the dark hours within ``validate_horizon`` of ``now``),
    on a grid of ``validate_step``, by the scheduler's own hard constraints.
    Stops early (``truncated``) at ``deadline`` (``time.monotonic()``)."""
    from robofast_core import sky

    step = timedelta(seconds=config.validate_step)
    n = int(config.validate_horizon // config.validate_step) + 1
    times = [now + i * step for i in range(n)]
    sun = _sun_altitudes(enc, times)
    dark = [a <= enc.planner.config.max_sun_alt for a in sun]
    if True in dark:
        first = dark.index(True)
        last = dark.index(False, first) if False in dark[first:] else n
        night = times[first:last]
    else:
        night = []
    ok: dict[tuple[str, str], list[datetime]] = collections.defaultdict(list)
    why: dict[tuple[str, str], collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    truncated = False
    for t in night:
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
        sky_t = sky.sky_now(enc.planner.site, t)
        for tel in enc.telescopes:
            for cand in enc.planner.evaluate(tel, t, sky_t, group.requests):
                key = (cand.request.id, tel)
                if cand.ok:
                    ok[key].append(t)
                else:
                    # Drop the numbers so similar reasons count together.
                    why[key].update(re.sub(r"-?\d+(\.\d+)?", "#", x) for x in cand.reasons)
    requests = {}
    for r in group.requests:
        per_tel = {}
        for tel in enc.telescopes:
            starts = ok.get((r.id, tel), [])
            windows: list[dict[str, str]] = []
            for t in starts:
                if windows and t - datetime.fromisoformat(windows[-1]["end"]) <= step:
                    windows[-1]["end"] = t.isoformat()
                else:
                    windows.append({"start": t.isoformat(), "end": t.isoformat()})
            per_tel[tel] = {
                "windows": windows,
                "reasons": [f"{k} ({v}x)" for k, v in why[(r.id, tel)].most_common(3)],
            }
        requests[r.id] = {
            "observable": any(v["windows"] for v in per_tel.values()),
            "telescopes": per_tel,
        }
    return {
        "night": None if not night else {"start": _iso(night[0]), "end": _iso(night[-1])},
        "step": config.validate_step,
        "truncated": truncated,
        "requests": requests,
    }


# -- HTTP/1.1 ------------------------------------------------------------------------------


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class HttpServer:
    """Plain asyncio HTTP/1.1 for :class:`RequestApi`, defensive: bounded
    line, header and body sizes, timeouts, connection limit; no chunked
    bodies."""

    def __init__(
        self,
        app: RequestApi,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.app = app
        self.host, self.port = host, port
        self.tls = tls
        self.config = app.config
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"

    async def start(self) -> HttpServer:
        self._server = await asyncio.start_server(
            self._connection, self.host, self.port, ssl=self.tls, limit=MAX_LINE
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            await self._server.wait_closed()

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
                except ValueError:  # longer than the stream limit
                    await self._send(
                        writer, Response(414, {"error": "request line too long"}), False
                    )
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
                resp = await self.app.handle(req, _client_addr(peer_ip, req.headers))
                keep = req.keep_alive and resp.status < 500
                await self._send(writer, resp, keep)
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except Exception:
            log.exception("request API connection from %s", peer_ip)
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _read(
        self, line: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> HttpRequest:
        if not line.endswith(b"\n"):
            raise _HttpError(414, "request line too long")
        parts = line.decode("latin-1").rstrip("\r\n").split(" ")
        if len(parts) != 3 or not parts[0].isalpha() or not parts[1].startswith("/"):
            raise _HttpError(400, "malformed request line")
        method, target, version = parts
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise _HttpError(505, "HTTP/1.0 or HTTP/1.1 only")
        headers: dict[str, str] = {}
        size = 0
        while True:
            try:
                h = await reader.readline()
            except ValueError:  # longer than the stream limit
                raise _HttpError(431, "header line too long") from None
            if h in (b"\r\n", b"\n"):
                break
            if not h.endswith(b"\n"):
                raise _HttpError(400, "incomplete headers")
            size += len(h)
            if len(headers) >= MAX_HEADERS or size > 4 * MAX_LINE:
                raise _HttpError(431, "too many headers")
            if h[:1] in (b" ", b"\t"):
                raise _HttpError(400, "folded header lines are not accepted")
            k, sep, v = h.decode("latin-1").partition(":")
            k = k.strip().lower()
            if not sep or not k or " " in k:
                raise _HttpError(400, "malformed header")
            if k in headers and k in ("content-length", "authorization", "host"):
                raise _HttpError(400, f"duplicate {k} header")
            headers[k] = v.strip()
        if "transfer-encoding" in headers:
            raise _HttpError(411, "chunked bodies are not accepted; send Content-Length")
        cl = headers.get("content-length", "0") or "0"
        if not cl.isdigit():
            raise _HttpError(400, "bad Content-Length")
        n = int(cl)
        if n > self.config.max_body:
            raise _HttpError(413, f"body larger than {self.config.max_body} bytes")
        if n and method == "POST":
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise _HttpError(415, "send Content-Type: application/json")
        if n and headers.get("expect", "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
        body = await reader.readexactly(n) if n else b""
        return HttpRequest(method, target, version, headers, body)

    async def _send(self, writer: asyncio.StreamWriter, resp: Response, keep: bool) -> None:
        data = json.dumps(resp.payload, default=str).encode()
        head = [
            f"HTTP/1.1 {resp.status} {HTTPStatus(resp.status).phrase}",
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(data)}",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            f"Connection: {'keep-alive' if keep else 'close'}",
            *(f"{k}: {v}" for k, v in resp.headers.items()),
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        await writer.drain()


def _client_addr(peer: str, headers: dict[str, str]) -> str:
    """The client's address: the proxy's ``X-Real-IP`` when the connection
    comes from loopback (the proxy), else the peer."""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    real = headers.get("x-real-ip", "").strip()
    return real[:64] if loopback and real else peer


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# -- building it from a config file ----------------------------------------------------------


def load_api_config(path: str | Path) -> tuple[ApiConfig, dict[str, Any], Path]:
    """(ApiConfig, the enclosures entry, the config's directory)."""
    path = Path(path)
    raw = load_config(path)
    raw.pop("_note", None)
    enclosures = raw.pop("enclosures", None)
    if not isinstance(enclosures, dict) or not enclosures:
        raise ValueError(f"{path}: 'enclosures' must name at least one enclosure")
    known = {f.name for f in fields(ApiConfig)}
    bad = set(raw) - known
    if bad:
        raise ValueError(f"{path}: unknown keys {sorted(bad)}")
    config = ApiConfig(**raw)
    base = path.parent
    if not config.tokens:
        raise ValueError(f"{path}: 'tokens' (the tokens file) is required")
    config.tokens = str(Enclosure._resolve(config.tokens, base))
    if config.audit_log is not None:
        config.audit_log = str(Enclosure._resolve(config.audit_log, base))
    return config, enclosures, base


def build_enclosures(
    spec: dict[str, Any], base: Path, clock: Callable[[], datetime] | None = None
) -> dict[str, Enclosure]:
    out = {}
    for name, entry in spec.items():
        entry = {"config": entry} if isinstance(entry, str) else dict(entry)
        cpath = Enclosure._resolve(entry["config"], base)
        config = load_config(cpath)
        if "plan" in entry:
            ppath = Enclosure._resolve(entry["plan"], base)
            plan, plan_base = load_config(ppath), ppath.parent
        else:
            plan, plan_base = config.get("plan", {}), cpath.parent
        out[name] = Enclosure(
            name, config, plan, config_base=cpath.parent, plan_base=plan_base, clock=clock
        )
    return out


def tls_context(config: ApiConfig) -> ssl.SSLContext | None:
    if config.tls_cert is None and config.tls_key is None:
        return None
    if not (config.tls_cert and config.tls_key):
        raise ValueError("tls_cert and tls_key go together")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config.tls_cert, config.tls_key)
    return ctx


def check_bind(host: str, tls: ssl.SSLContext | None, insecure: bool) -> None:
    """Refuse a non-loopback address without TLS (unless ``insecure``)."""
    if not is_loopback(host) and tls is None and not insecure:
        raise ValueError(
            f"refusing to listen on {host!r} without TLS: run it behind a TLS reverse proxy "
            "on 127.0.0.1, set tls_cert/tls_key, or pass --insecure-bind"
        )


async def serve(
    config: ApiConfig,
    enclosures: dict[str, Enclosure],
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
        log.warning("no active tokens in %s: every request will be refused", config.tokens)
    app = RequestApi(enclosures, tokens, config=config)
    server = await HttpServer(app, host, int(port), tls=tls).start()
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


# -- the command line ---------------------------------------------------------------------------


def _tokens_path(args: argparse.Namespace) -> Path:
    if args.tokens:
        return Path(args.tokens)
    if args.config:
        return Path(load_api_config(args.config)[0].tokens)
    raise SystemExit("give --tokens FILE or --config API_CONFIG")


def _audit(args: argparse.Namespace) -> AuditLog | None:
    if not args.config:
        return None
    return AuditLog(load_api_config(args.config)[0].audit_log)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.request_api", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the API")
    sp.add_argument("--config", required=True)
    sp.add_argument(
        "--insecure-bind",
        action="store_true",
        help="allow a non-loopback listen address without TLS",
    )
    tp = sub.add_parser("tokens", help="manage API tokens")
    tsub = tp.add_subparsers(dest="action", required=True)
    for name in ("add", "revoke", "list"):
        p = tsub.add_parser(name)
        p.add_argument("--config", help="the API config (for its tokens file and audit log)")
        p.add_argument("--tokens", help="the tokens file (overrides --config)")
        if name == "add":
            p.add_argument("--name", required=True, help="the client's name")
            p.add_argument(
                "--scope",
                action="append",
                required=True,
                help=f"repeat or comma-separate: {', '.join(SCOPES)}",
            )
            p.add_argument("--expires-days", type=float)
            p.add_argument(
                "--force", action="store_true", help="allow a tokens file inside a git work tree"
            )
        if name == "revoke":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--name")
            g.add_argument("--id")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.cmd == "serve":
        try:
            config, spec, base = load_api_config(args.config)
            enclosures = build_enclosures(spec, base)
            check_bind(
                config.listen.rpartition(":")[0].strip("[]"),
                tls_context(config),
                args.insecure_bind,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            ap.error(f"{args.config}: {exc}")
        try:
            asyncio.run(serve(config, enclosures, insecure_bind=args.insecure_bind))
        except asyncio.CancelledError:
            pass
        return
    try:
        path = _tokens_path(args)
        store = TokenStore(path)
        audit = _audit(args)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if args.action == "add":
        repo = inside_git_worktree(path.parent if not path.exists() else path)
        if repo is not None and not args.force:
            ap.error(f"{path} is inside the git work tree {repo}; keep tokens out of repositories")
        scopes = [s.strip() for x in args.scope for s in x.split(",") if s.strip()]
        try:
            rec, token = store.add(args.name, scopes, expires_days=args.expires_days)
        except ValueError as exc:
            ap.error(str(exc))
        if audit is not None:
            audit.write(
                client="cli", action="token_add", token_id=rec.id, name=rec.name, scopes=rec.scopes
            )
        print(
            f"token for {rec.name} (id {rec.id}, scopes {','.join(rec.scopes)}):", file=sys.stderr
        )
        print("store it now; it is not kept and cannot be shown again:", file=sys.stderr)
        print(token)
    elif args.action == "revoke":
        ids = store.revoke(name=args.name, token_id=args.id)
        if audit is not None:
            audit.write(client="cli", action="token_revoke", token_ids=ids)
        print(json.dumps({"revoked": ids}))
        if not ids:
            sys.exit(1)
    else:
        for r in store.records():
            print(json.dumps(r.public()))


if __name__ == "__main__":
    main()
