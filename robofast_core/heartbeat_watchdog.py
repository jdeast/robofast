"""The enclosure heartbeat: the device contract, and a watchdog for simulators.

The contract
------------
The enclosure's Alpaca **Dome** device implements ``Action("Heartbeat", "")``
(and lists it in ``SupportedActions``). Each call restarts a countdown of
``heartbeat_timeout`` seconds that the *device* keeps. If the countdown runs
out, the device closes the shutter on its own and refuses ``OpenShutter``
until the pings resume. The safety supervisor sends the pings (every
``SafetyConfig.heartbeat_interval``) only while it is healthy, so a crashed,
hung or stuck supervisor leaves an enclosure that closes itself.

Why a Dome action rather than a separate (Switch-like) device: the Aqawan's
watchdog lives in the same controller that drives its shutters, and the
enclosure's Alpaca server talks to that controller anyway. A ping answered by
the dome's own server shows that the path to whatever would close is alive; a
separate device would be a second server whose health says nothing about the
dome's. ``Action()`` is part of every Alpaca device, so no new protocol is
needed. (The supervisor holds the heartbeat as its own device owner, so it
*can* point at a different server: in the tests below, it does.)

The watchdog
------------
OmniSim's dome has no heartbeat, so :class:`HeartbeatWatchdog` adds one from
outside: a tiny Alpaca server that answers ``Action("Heartbeat")`` on
``/api/v1/dome/0`` and, while the pings are overdue, commands
``CloseShutter`` on the target dome every ``check_interval``. (It cannot
refuse ``OpenShutter``, which goes to the simulator directly; re-closing
while overdue is the nearest it gets.) Run it as its own process::

    python -m robofast_core.heartbeat_watchdog --dome 127.0.0.1:32323 --timeout 5

and point the supervisor's heartbeat device at the address it prints. It is
a stand-in for the controller in localhost tests, not a safety device.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from robofast_core.weather_server import NOT_IMPLEMENTED, _alpaca

log = logging.getLogger(__name__)

CLOSED, CLOSING = 1, 3  # ASCOM ShutterState


class _Handler(BaseHTTPRequestHandler):
    server: HeartbeatWatchdog
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: Any) -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        params = {k.lower(): v[0] for k, v in parse_qs(url.query).items()}
        if self.command == "PUT":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            params.update({k.lower(): v[0] for k, v in parse_qs(body).items()})
        parts = [p for p in url.path.lower().split("/") if p]
        if parts[:4] != ["api", "v1", "dome", "0"] or len(parts) != 5:
            self._send(400, "unknown path")
            return
        member = parts[4]
        ctid = int(params.get("clienttransactionid", 0) or 0)
        value: Any = None
        if member == "action" and self.command == "PUT":
            if params.get("action", "").lower() != "heartbeat":
                self._send(200, _alpaca(ctid=ctid, err=0x40C, msg="only Heartbeat"))
                return
            self.server.ping()
            value = "ok"
        elif member == "supportedactions":
            value = ["Heartbeat"]
        elif member == "connected":
            value = True if self.command == "GET" else None
        elif member in ("name", "description", "driverinfo"):
            value = "robofast heartbeat watchdog (simulator stand-in)"
        else:
            self._send(200, _alpaca(ctid=ctid, err=NOT_IMPLEMENTED, msg=f"{member}"))
            return
        self._send(200, _alpaca(value, ctid=ctid))


class HeartbeatWatchdog(ThreadingHTTPServer):
    """Close ``dome_address``'s dome whenever pings are ``timeout`` s overdue.

    The countdown starts when the watchdog starts. ``trips`` counts the
    times it went overdue; ``closes_sent`` the ``CloseShutter`` commands.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        dome_address: str,
        timeout: float,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        dome_number: int = 0,
        check_interval: float = 0.1,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.timeout = timeout
        self.check_interval = check_interval
        self.pinged_at = time.monotonic()
        self.trips = 0
        self.closes_sent = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._dome = httpx.Client(
            base_url=f"http://{dome_address}/api/v1/dome/{dome_number}/",
            trust_env=False,
            timeout=max(1.0, check_interval),
        )

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def ping(self) -> None:
        with self._lock:
            self.pinged_at = time.monotonic()

    def overdue(self) -> bool:
        with self._lock:
            return time.monotonic() - self.pinged_at > self.timeout

    def _dome_call(self, verb: str, member: str, **params: str) -> Any:
        r = self._dome.request(verb, member.lower(), **({"data": params} if params else {}))
        r.raise_for_status()
        body = r.json()
        if body.get("ErrorNumber"):
            raise RuntimeError(f"{member}: {body.get('ErrorMessage')}")
        return body.get("Value")

    def _close(self) -> None:
        if not self._dome_call("GET", "Connected"):
            self._dome_call("PUT", "Connected", Connected="True")
        if int(self._dome_call("GET", "ShutterStatus")) not in (CLOSED, CLOSING):
            self._dome_call("PUT", "CloseShutter")
            self.closes_sent += 1

    def _watch(self) -> None:
        tripped = False
        while not self._stop.wait(self.check_interval):
            if not self.overdue():
                tripped = False
                continue
            if not tripped:
                tripped = True
                self.trips += 1
                log.warning("heartbeat overdue (> %s s): closing the dome", self.timeout)
            try:
                self._close()
            except Exception as exc:  # keep trying every check_interval
                log.error("watchdog close failed: %r", exc)

    def start(self) -> HeartbeatWatchdog:
        for target, name in ((self.serve_forever, "watchdog-http"), (self._watch, "watchdog")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._workers.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.shutdown()
        self.server_close()
        for t in self._workers:
            t.join(timeout=5)
        self._dome.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dome", required=True, help="host:port of the Alpaca dome to close")
    ap.add_argument("--timeout", type=float, required=True, help="heartbeat timeout (s)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    wd = HeartbeatWatchdog(args.dome, args.timeout, args.host, args.port).start()
    print(f"heartbeat watchdog on http://{wd.address}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        wd.stop()


if __name__ == "__main__":
    main()
