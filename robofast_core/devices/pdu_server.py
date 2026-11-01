"""Alpaca Switch server for an APC switched PDU (docs/pdu.md).

One Alpaca **Switch** (device 0, ISwitchV2) whose switches are the PDU
outlets named in the config, in order (switch id 0 is the first listed).
Each is boolean (0 off, 1 on). An outlet is writable only if the config
says ``"writable": true``; everything the safety path needs -- the
enclosure controller, the weather station, the network, the supervisor's
computer -- is listed with ``"writable": false`` (or not listed at all) and
can never be switched from here: ``SetSwitch`` raises
``MethodNotImplemented`` and ``CanWrite`` is false.

``Action("PowerCycle", "<id>")`` reboots one outlet through the PDU (off,
then on after the PDU's reboot delay), only for outlets with ``"cycle":
true``, and at most once per ``min_cycle_interval`` s per outlet (else
``InvalidOperation``); every switch and cycle is logged with the Alpaca
client id. ``Action("Status", "")`` returns every configured outlet's name
and state as JSON. This is what the sequencer's recovery ladder
(``robofast_core/recovery.py``) calls.

Safety
------
The PDU address must be loopback (a simulator) unless the config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``
(like the Aqawan server). The SNMP communities are read from
``credentials`` (a JSON file readable only by its owner: ``{"read": ...,
"write": ...}``); there are no defaults and they are never logged.

Running
-------
::

    python -m robofast_core.devices.pdu_server --config pdu.json [--hardware | --simulate]

``--simulate`` starts a simulated PDU (:class:`~robofast_core.devices.apc_pdu.APCPDUSim`)
on loopback with the config's outlets (and, without a credentials file,
random communities). It prints one JSON line ``{"url": ..., "pdu": ...}``
once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import ipaddress
import json
import logging
import secrets
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.devices.apc_pdu import APCPDU, APCPDUSim, Credentials, load_credentials
from robofast_core.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    COMMON_NAMES,
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _alpaca,
    _BadRequest,
    _bool_param,
    _uint32,
)
from robofast_core.devices.snmp import SNMPError, SNMPTimeout

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
PDU_TIMEOUT = 0x502
ACTIONS = ("PowerCycle", "Status")


class HardwareNotAllowed(RuntimeError):
    pass


def check_pdu_address(host: str, *, allow_hardware: bool) -> str:
    """Resolve ``host`` once; unless ``allow_hardware`` it must be loopback."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise HardwareNotAllowed(f"cannot resolve PDU host {host!r}: {exc}") from exc
    addrs = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
    if not allow_hardware and not all(a.is_loopback for a in addrs):
        raise HardwareNotAllowed(
            f"PDU host {host!r} is not loopback: refusing to talk to real hardware without"
            " allow_hardware in the config AND --hardware"
        )
    return str(addrs[0])


@dataclass
class OutletConfig:
    outlet: int
    name: str
    #: May be switched through Alpaca (never for anything the safety path needs).
    writable: bool = False
    #: May be power-cycled (``Action("PowerCycle")``); implies writable.
    cycle: bool = False
    description: str = ""


@dataclass
class PDUConfig:
    name: str = "pdu"
    listen: str = "127.0.0.1:11120"
    pdu: dict[str, Any] = field(default_factory=lambda: {"host": "127.0.0.1", "port": 161})
    credentials: str | None = None
    allow_hardware: bool = False
    outlets: list[OutletConfig] = field(default_factory=list)
    min_cycle_interval: float = 600.0
    request_timeout: float = 10.0
    #: Alpaca discovery port (None: no discovery).
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PDUConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"pdu config: unknown keys {sorted(set(d) - known)}")
        outs = []
        for o in d.pop("outlets", []):
            o = {k: v for k, v in dict(o).items() if not k.startswith("_")}
            oc = OutletConfig(**o)
            if oc.cycle and not oc.writable:
                raise ValueError(f"outlet {oc.outlet} ({oc.name}): cycle needs writable")
            outs.append(oc)
        if len({o.outlet for o in outs}) != len(outs):
            raise ValueError("pdu config: an outlet is listed twice")
        return cls(outlets=outs, **d)


class _Handler(BaseHTTPRequestHandler):
    server: PDUServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = (json.dumps(body) if ctype == "application/json" else str(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        if self.command == "GET":
            params = {
                k.lower(): v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()
            }
        else:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 1 << 16)).decode("utf-8", "replace") if n else ""
            params = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        parts = [p for p in url.path.split("/") if p]
        lower = [p.lower() for p in parts]
        srv = self.server
        get = (lambda k: params.get(k.lower())) if self.command == "GET" else params.get
        ctid = _uint32(get("ClientTransactionID"))
        if lower[:1] == ["management"] and self.command == "GET":
            value = srv.management(lower[1:])
            if value is None:
                self._send(400, f"unknown management path {url.path}", "text/plain")
            else:
                self._send(200, _alpaca(value, ctid=ctid, stid=srv.next_stid()))
            return
        if lower[:1] == ["setup"]:
            page = "<!doctype html><title>PDU</title><p>Configured from its file."
            self._send(200, page, "text/html")
            return
        if parts != lower or len(lower) != 5 or lower[:4] != ["api", "v1", "switch", "0"]:
            self._send(400, f"unknown path {url.path}", "text/plain")
            return
        client = _uint32(get("ClientID"))
        stid = srv.next_stid()
        try:
            value = srv.member(self.command, lower[4], params, client)
        except _BadRequest as exc:
            self._send(400, str(exc), "text/plain")
            return
        except AlpacaException as exc:
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=exc.number, msg=str(exc)))
            return
        except Exception as exc:
            log.exception("switch %s %s", self.command, lower[4])
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=DRIVER_ERROR, msg=repr(exc)))
            return
        self._send(200, _alpaca(value, ctid=ctid, stid=stid))


class PDUServer(ThreadingHTTPServer):
    """The Alpaca server (module doc). SNMP runs on a private event loop."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        config: PDUConfig,
        *,
        hardware: bool = False,
        credentials: Credentials | None = None,
    ) -> None:
        allow = config.allow_hardware and hardware
        if config.allow_hardware != hardware:
            log.info("hardware use needs allow_hardware in the config AND --hardware")
        host = str(config.pdu.get("host", "127.0.0.1"))
        self.pdu_ip = check_pdu_address(host, allow_hardware=allow)
        if allow:
            log.warning("HARDWARE MODE: PDU at %s:%s", self.pdu_ip, config.pdu.get("port", 161))
        if credentials is None:
            if not config.credentials:
                raise ValueError("pdu config: credentials (a file) is required")
            credentials = load_credentials(config.credentials)
        lhost, _, lport = config.listen.rpartition(":")
        super().__init__((lhost, int(lport)), _Handler)
        self.config = config
        self.pdu = APCPDU(
            self.pdu_ip,
            int(config.pdu.get("port", 161)),
            credentials,
            timeout=float(config.pdu.get("timeout", 2.0)),
            retries=int(config.pdu.get("retries", 2)),
        )
        self._stid = 0
        self._lock = threading.Lock()
        self._clients: set[int] = set()
        self._last_cycle: dict[int, float] = {}
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._http: threading.Thread | None = None

    @property
    def address(self) -> str:
        h, p = self.server_address[:2]
        return f"{h}:{p}"

    def start(self) -> PDUServer:
        self._http = threading.Thread(target=self.serve_forever, name="pdu-http", daemon=True)
        self._http.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)

    def next_stid(self) -> int:
        with self._lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def _call(self, fn: Callable[[], Awaitable[Any]], what: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(self.config.request_timeout)
        except (concurrent.futures.TimeoutError, SNMPTimeout) as exc:
            fut.cancel()
            raise AlpacaException(PDU_TIMEOUT, f"{what}: the PDU did not answer ({exc})") from None
        except SNMPError as exc:
            raise AlpacaException(DRIVER_ERROR, f"{what}: {exc}") from None

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast PDU ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{name} switch",
                    "DeviceType": "Switch",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-pdu-{name}-switch-0",
                }
            ]
        return None

    # -- members ---------------------------------------------------------------------

    def _outlet(self, params: dict[str, str], verb: str) -> tuple[int, OutletConfig]:
        key = "id" if verb == "GET" else "Id"
        if key not in params:
            raise _BadRequest("missing parameter Id")
        try:
            i = int(params[key])
        except ValueError:
            raise _BadRequest(f"Id={params[key]!r} is not an integer") from None
        if not 0 <= i < len(self.config.outlets):
            raise AlpacaException(INVALID_VALUE, f"no switch {i}")
        return i, self.config.outlets[i]

    def _connected(self, client: int) -> bool:
        with self._lock:
            return client in self._clients

    def member(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        if member in ("connected", "connect", "disconnect") and verb == "PUT":
            on = member == "connect" or (member == "connected" and _bool_param(params, "Connected"))
            if on and not self._connected(client):
                self._call(self.pdu.describe, "Connect")
            with self._lock:
                (self._clients.add if on else self._clients.discard)(client)
            return None
        if member in COMMON_NAMES:
            if verb != "GET":
                raise _BadRequest(f"PUT {member} is not allowed")
            return {
                "connected": self._connected(client),
                "connecting": False,
                "description": f"{cfg.name}: APC switched PDU over SNMP",
                "driverinfo": "robofast_core.devices.pdu_server: Alpaca Switch for an APC PDU",
                "driverversion": DRIVER_VERSION,
                "interfaceversion": 2,
                "name": f"robofast PDU {cfg.name}",
                "supportedactions": list(ACTIONS),
            }[member]
        if member == "maxswitch" and verb == "GET":
            return len(cfg.outlets)
        if not self._connected(client) and member not in ("canwrite", "getswitchname"):
            raise AlpacaException(NOT_CONNECTED, "not connected")
        if member == "action" and verb == "PUT":
            return self._action(params.get("Action", ""), params.get("Parameters", ""), client)
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        if verb == "GET":
            i, oc = self._outlet(params, verb)
            if member == "canwrite":
                return oc.writable
            if member == "getswitchname":
                return oc.name
            if member == "getswitchdescription":
                return oc.description or f"PDU outlet {oc.outlet}"
            if member in ("getswitch", "getswitchvalue"):
                on = self._call(lambda: self.pdu.state(oc.outlet), "GetSwitch")
                return on if member == "getswitch" else (1.0 if on else 0.0)
            if member == "minswitchvalue":
                return 0.0
            if member in ("maxswitchvalue", "switchstep"):
                return 1.0
            raise _BadRequest(f"unknown member {member}")
        if member in ("setswitch", "setswitchvalue"):
            i, oc = self._outlet(params, verb)
            if not oc.writable:
                raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} ({oc.name}) is not writable")
            if member == "setswitch":
                on = _bool_param(params, "State")
            else:
                if "Value" not in params:
                    raise _BadRequest("missing parameter Value")
                try:
                    v = float(params["Value"])
                except ValueError:
                    raise _BadRequest("Value is not a number") from None
                if not 0.0 <= v <= 1.0:
                    raise AlpacaException(INVALID_VALUE, f"switch {i}: value {v} not in [0, 1]")
                on = v >= 0.5  # off-step values round to the nearest step
            log.warning("switch %d (%s) %s by client %d", i, oc.name, "ON" if on else "OFF", client)
            self._call(lambda: self.pdu.set(oc.outlet, on), "SetSwitch")
            return None
        if member == "setswitchname":
            raise AlpacaException(NOT_IMPLEMENTED, "names come from the config")
        raise _BadRequest(f"unknown member {verb} {member}")

    def _action(self, action: str, parameters: str, client: int) -> str:
        if action == "Status":
            out = []
            for i, oc in enumerate(self.config.outlets):
                on = self._call(lambda oc=oc: self.pdu.state(oc.outlet), "Status")
                out.append({"id": i, "outlet": oc.outlet, "name": oc.name, "on": on})
            return json.dumps(out)
        if action == "PowerCycle":
            try:
                i = int(parameters)
            except ValueError:
                raise AlpacaException(INVALID_VALUE, f"PowerCycle: id {parameters!r}") from None
            if not 0 <= i < len(self.config.outlets):
                raise AlpacaException(INVALID_VALUE, f"no switch {i}")
            oc = self.config.outlets[i]
            if not oc.cycle:
                raise AlpacaException(
                    INVALID_OPERATION, f"switch {i} ({oc.name}) may not be power-cycled"
                )
            now = time.monotonic()
            with self._lock:
                last = self._last_cycle.get(i)
                if last is not None and now - last < self.config.min_cycle_interval:
                    raise AlpacaException(
                        INVALID_OPERATION,
                        f"switch {i} ({oc.name}) was cycled {now - last:.0f} s ago"
                        f" (at most once per {self.config.min_cycle_interval:.0f} s)",
                    )
                self._last_cycle[i] = now
            log.warning("switch %d (%s) POWER-CYCLED by client %d", i, oc.name, client)
            self._call(lambda: self.pdu.reboot(oc.outlet), "PowerCycle")
            return json.dumps({"id": i, "outlet": oc.outlet, "name": oc.name, "cycled": True})
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"no action {action!r}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--hardware", action="store_true", help="allow a non-loopback PDU")
    mode.add_argument("--simulate", action="store_true", help="run against a simulated PDU")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = PDUConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    creds = None
    sim_loop = None
    if args.simulate:
        creds = (
            load_credentials(config.credentials)
            if config.credentials
            else Credentials(secrets.token_hex(8), secrets.token_hex(8))
        )
        n = max([o.outlet for o in config.outlets] or [8])
        names = [f"Outlet {k}" for k in range(1, n + 1)]
        for o in config.outlets:
            names[o.outlet - 1] = o.name
        sim_loop = asyncio.new_event_loop()
        threading.Thread(target=sim_loop.run_forever, daemon=True).start()
        sim = asyncio.run_coroutine_threadsafe(APCPDUSim(creds, names).start(), sim_loop).result(5)
        config.pdu = {**config.pdu, "host": "127.0.0.1", "port": sim.port}
        config.allow_hardware = False
    try:
        server = PDUServer(config, hardware=args.hardware, credentials=creds).start()
    except HardwareNotAllowed as exc:
        log.error("%s", exc)
        sys.exit(2)
    print(
        json.dumps(
            {"url": f"http://{server.address}", "pdu": f"{server.pdu_ip}:{config.pdu['port']}"}
        ),
        flush=True,
    )
    stop = threading.Event()
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()
    if sim_loop is not None:
        sim_loop.call_soon_threadsafe(sim_loop.stop)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
