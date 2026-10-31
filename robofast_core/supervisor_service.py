"""The safety supervisor as its own process, with a small HTTP/JSON API.

Running the supervisor
----------------------
::

    python -m robofast_core.supervisor_service --config safety.json

with a config like (device addresses are Alpaca ``host:port``, optionally
``host:port/N`` for device number N; ``safety`` holds
:class:`~robofast_core.safety.SafetyConfig` fields)::

    {
      "name": "safety",
      "listen": "127.0.0.1:8100",
      "devices": {
        "dome": "127.0.0.1:32323",
        "safetymonitor": "127.0.0.1:11111",
        "observingconditions": "127.0.0.1:11111",
        "heartbeat": "127.0.0.1:11112"
      },
      "safety": {"poll_interval": 5, "reopen_hold": 1200, "lease_ttl": 60,
                 "heartbeat_interval": 2}
    }

``observingconditions``, ``telescope`` (needed for ``park_before_close``; when
present, no lease is granted unless it reports ``AtPark``) and ``heartbeat``
are optional; with ``heartbeat_interval`` set and no
``heartbeat`` entry, the pings go to the dome itself (the normal case; see
:mod:`robofast_core.heartbeat_watchdog`). The process prints one JSON line,
``{"url": ..., "epoch": ...}``, on stdout once it is listening, logs to
stderr, and on SIGTERM or SIGINT revokes the lease and closes before it
exits. Run it under systemd (``Restart=always``; ``deploy/`` has an example
unit); it starts closed. ``timeline_size`` (default 10000) bounds the
in-memory record of commands and events.

Sequencers use it through :class:`robofast_core.lease_client.RemoteSupervisor`.

The API
-------
All bodies are JSON. A lease is named by a token ``{epoch}.{id}``; the epoch
is random per process start, so after a restart every old token is unknown
(404) and its holder must stop.

* ``GET /status`` -- the supervisor's state (read-only).
* ``POST /leases`` ``{"holder", "wait", "request"}`` -- wait up to ``wait``
  seconds for a lease; answers ``{"granted": true, "lease": {...}}`` or
  ``{"granted": false}`` (ask again). Repeating a ``request`` id returns the
  lease already granted to it, so a lost answer does not strand a lease.
* ``GET /leases/{token}?wait=S`` -- the lease's state; waits up to ``S`` s
  for it to open (if not yet open) or to be revoked or released.
* ``POST /leases/{token}/renew`` -- 200 with the state, or 410 if revoked.
* ``POST /leases/{token}/release``.
* ``POST /leases/{token}/quarantine`` -- the holder keeps the lease on
  purpose (its task ignored cancellation); only ``clear`` releases it.
* ``POST /leases/{token}/recover`` ``{"holder"}`` -- a restarted sequencer
  releases its crashed predecessor's lease, after cleaning up (see
  :mod:`robofast_core.safety`). 409 if the lease is live, quarantined,
  another holder's, or the enclosure is not confirmed closed yet.
* ``POST /leases/{token}/clear`` ``{"operator", "reason"}`` -- the same, for
  an operator who has made the telescope safe; logged.

``recover`` and ``clear`` are idempotent: they answer 200 with
``"released_now"`` true or false. A lease's state is ``{"token", "id",
"holder", "ttl", "live", "opened", "revoked", "reason", "released",
"quarantined"}``; ``GET /status`` shows the latest one. There is no authentication: like Alpaca,
this stays on the isolated control network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import signal
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.alpaca import alpaca_owner
from robofast_core.safety import (
    ENCLOSURE_MEMBERS,
    OpenLease,
    RecoveryRefused,
    SafetyConfig,
    SafetySupervisor,
)
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: The longest a single long-poll may wait (s).
MAX_WAIT = 30.0
#: Released leases kept for lookups (older tokens answer 404).
KEEP_LEASES = 100


def lease_state(lease: OpenLease, token: str) -> dict[str, Any]:
    return {"token": token, **lease.state()}


class _Error(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class SupervisorServer:
    """Serve one :class:`SafetySupervisor` over HTTP/JSON (plain asyncio)."""

    def __init__(self, supervisor: SafetySupervisor, host: str = "127.0.0.1", port: int = 0):
        self.supervisor = supervisor
        self.host, self.port = host, port
        self.epoch = secrets.token_hex(6)
        self._leases: dict[int, tuple[OpenLease, str | None]] = {}
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> SupervisorServer:
        self._server = await asyncio.start_server(self._connection, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            await self._server.wait_closed()

    # -- HTTP/1.1, just enough of it --------------------------------------------

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                method, target, _ = line.decode("latin-1").split(" ", 2)
                headers = {}
                while (h := await reader.readline()) not in (b"\r\n", b"\n", b""):
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length") or 0)
                body = await reader.readexactly(n) if n else b""
                status, payload = await self._respond(method, target, body)
                data = json.dumps(payload).encode()
                head = (
                    f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n"
                )
                writer.write(head.encode() + data)
                await writer.drain()
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _respond(self, method: str, target: str, body: bytes) -> tuple[int, Any]:
        url = urlparse(target)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            args = json.loads(body) if body else {}
            if method == "GET" and parts == ["status"]:
                return 200, self.status()
            if method == "POST" and parts == ["leases"]:
                return 200, await self._request(args)
            if parts[:1] == ["leases"] and len(parts) in (2, 3):
                lease, token = self._lookup(parts[1])
                action = parts[2] if len(parts) == 3 else None
                if method == "GET" and action is None:
                    await self._wait_change(lease, float(query.get("wait", 0)))
                elif method == "POST" and action == "renew":
                    if not lease.live:
                        return 410, lease_state(lease, token)
                    lease.renew()
                elif method == "POST" and action == "release":
                    lease.release()
                    self.supervisor.timeline.event(
                        self.supervisor.name, "lease_released", lease=lease.id
                    )
                elif method == "POST" and action == "quarantine":
                    lease.quarantine()
                    self.supervisor.timeline.event(
                        self.supervisor.name, "lease_quarantined", lease=lease.id
                    )
                elif method == "POST" and action == "recover":
                    now = await self.supervisor.recover(lease.id, str(args["holder"]))
                    return 200, {**lease_state(lease, token), "released_now": now}
                elif method == "POST" and action == "clear":
                    operator = str(args["operator"]).strip()
                    if not operator:
                        raise _Error(400, "clear needs an operator name")
                    now = await self.supervisor.clear(lease.id, operator, str(args["reason"]))
                    return 200, {**lease_state(lease, token), "released_now": now}
                else:
                    raise _Error(404, f"no {method} {url.path}")
                return 200, lease_state(lease, token)
            raise _Error(404, f"no {method} {url.path}")
        except _Error as exc:
            return exc.status, {"error": str(exc)}
        except RecoveryRefused as exc:
            return 409, {"error": str(exc)}
        except (ValueError, TypeError, KeyError) as exc:
            return 400, {"error": f"bad request: {exc!r}"}

    # -- leases -------------------------------------------------------------------

    def _token(self, lease: OpenLease) -> str:
        return f"{self.epoch}.{lease.id}"

    def _lookup(self, token: str) -> tuple[OpenLease, str]:
        epoch, _, lease_id = token.partition(".")
        entry = self._leases.get(int(lease_id)) if lease_id.isdigit() else None
        if epoch != self.epoch or entry is None:
            raise _Error(404, f"unknown lease {token} (supervisor restarted?)")
        return entry[0], token

    async def _request(self, args: dict[str, Any]) -> dict[str, Any]:
        holder, request = str(args["holder"]), args.get("request")
        for lease, req in self._leases.values():
            if request is not None and req == request and not lease.released.is_set():
                return {"granted": True, "lease": lease_state(lease, self._token(lease))}
        wait = min(float(args.get("wait", 0)), MAX_WAIT)
        try:
            lease = await asyncio.wait_for(self.supervisor.request_open(holder), wait)
        except TimeoutError:
            return {"granted": False}
        self._leases[lease.id] = (lease, request)
        for old in sorted(self._leases)[:-KEEP_LEASES]:
            if self._leases[old][0].released.is_set():
                del self._leases[old]
        return {"granted": True, "lease": lease_state(lease, self._token(lease))}

    async def _wait_change(self, lease: OpenLease, wait: float) -> None:
        events = [lease.revoked, lease.released] + ([] if lease.opened.is_set() else [lease.opened])
        if wait <= 0 or any(e.is_set() for e in events[:2]):
            return
        waiters = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(
                waiters, timeout=min(wait, MAX_WAIT), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for w in waiters:
                w.cancel()

    def status(self) -> dict[str, Any]:
        lease = self.supervisor.lease
        return {
            **self.supervisor.status(),
            "epoch": self.epoch,
            "lease": None if lease is None else lease_state(lease, self._token(lease)),
        }


# -- the process ------------------------------------------------------------------


def _device(spec: str) -> tuple[str, int]:
    address, _, number = spec.partition("/")
    return address, int(number or 0)


def build(config: dict[str, Any], timeline: Timeline) -> SafetySupervisor:
    """A supervisor wired to the Alpaca devices named in ``config``."""
    devices = config["devices"]
    scfg = SafetyConfig(**config.get("safety", {}))
    timeout = scfg.dome_timeout

    def owner(name: str, kind: str, required: bool = False, **kw: Any):
        if name not in devices and not required:
            return None
        address, number = _device(devices[name])
        return alpaca_owner(
            name, kind, address, timeline, number=number, default_timeout=timeout, **kw
        )

    heartbeat = None
    if scfg.heartbeat_interval is not None:
        address, number = _device(devices.get("heartbeat", devices["dome"]))
        heartbeat = alpaca_owner("heartbeat", "dome", address, timeline, number=number)
    return SafetySupervisor(
        config.get("name", "safety"),
        dome=owner("dome", "dome", True, privileged_members=ENCLOSURE_MEMBERS),
        safety_monitor=owner("safetymonitor", "safetymonitor", True),
        observing_conditions=owner("observingconditions", "observingconditions"),
        telescope=owner("telescope", "telescope"),
        heartbeat=heartbeat,
        timeline=timeline,
        config=scfg,
    )


async def serve(config: dict[str, Any]) -> None:
    supervisor = build(config, Timeline(maxlen=config.get("timeline_size", 10000)))
    host, _, port = config.get("listen", "127.0.0.1:8100").rpartition(":")
    server = await SupervisorServer(supervisor, host, int(port)).start()
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    print(json.dumps({"url": server.url, "epoch": server.epoch}), flush=True)
    try:
        await supervisor.run()  # closes (bounded) when cancelled
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = json.load(f)
    try:
        asyncio.run(serve(config))
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
