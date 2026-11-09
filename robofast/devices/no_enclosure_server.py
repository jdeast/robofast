"""Alpaca devices for a telescope with no enclosure (docs/no_enclosure.md).

A classroom or backyard telescope on a tripod has no dome and no weather
station, but the safety supervisor (:mod:`robofast.safety`) always runs an
enclosure and a safety monitor. This server models "no enclosure" as
devices, so the supervisor keeps its one path:

* **Dome 0, the sky.** ``OpenShutter`` and ``CloseShutter`` succeed at once
  and ``ShutterStatus`` follows the last command (``shutterOpen`` /
  ``shutterClosed``; closed at start). Nothing moves: what the supervisor
  does around a close -- ``park_before_close`` parks every telescope, the
  park-before-close interlock holds a routine close until all report
  ``AtPark`` -- is what makes a revoked lease safe here.
* **SafetyMonitor 0, the operator's duty.** ``IsSafe`` is true only while
  an operator is on duty at the supervisor (``GET /status``, its
  ``duty.on_duty``: the observer console's ``duty`` action), read every
  ``poll_interval``; false when nobody is, and false when the supervisor
  has not answered for ``stale_after`` seconds. With no weather sensor,
  the operator on duty is the weather: going off duty makes it unsafe, and
  the supervisor revokes the lease, parks the telescopes and "closes".
  Duty expiry (``duty_ttl``, ``duty_grace``) is the supervisor's own
  attended-only check (docs/console.md, "Operator duty").
  ``Action("Status")`` answers the reason as JSON.

Nothing here touches hardware, so there is no hardware gate.

Running
-------
::

    python -m robofast.devices.no_enclosure_server --config sky.json

with a JSON config::

    {"name": "classroom", "listen": "127.0.0.1:11130",
     "supervisor": "http://127.0.0.1:8100"}

(optional: ``poll_interval`` 2 s, ``stale_after`` 10 s, ``request_timeout``
2 s, ``discovery_port``). The supervisor's config then names this server
for both ``devices.dome`` and ``devices.safetymonitor``. It prints one JSON
line, ``{"url": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Any

import httpx

from robofast.config import load_device_config, require_keys
from robofast.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    CAN,
    CLOSED,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    NOT_IMPLEMENTED_GET,
    NOT_IMPLEMENTED_PUT,
    OPEN,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
)
from robofast.devices.tcsgo_server import COMMON_NAMES, _Handler

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
#: The safety monitor's actions; the dome has none.
ACTIONS = ("Status",)

#: A duty source answers (an operator is on duty, why); it may raise.
DutySource = Callable[[], tuple[bool, str]]


@dataclass
class NoEnclosureConfig:
    name: str
    listen: str
    #: The safety supervisor's URL (its ``listen``), read for the duty.
    supervisor: str
    poll_interval: float = 2.0
    #: IsSafe is false once the supervisor has not answered for this long.
    stale_after: float = 10.0
    request_timeout: float = 2.0
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NoEnclosureConfig:
        require_keys(d, ["name", "listen", "supervisor"])
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown key(s) {', '.join(sorted(unknown))}")
        cfg = cls(**d)
        if not cfg.poll_interval > 0 or not cfg.stale_after > cfg.poll_interval:
            raise ValueError("poll_interval must be > 0 and stale_after > poll_interval")
        return cfg


def supervisor_duty(url: str, timeout: float) -> DutySource:
    """The supervisor's duty (``GET /status``) as a :data:`DutySource`."""
    client = httpx.Client(trust_env=False, timeout=timeout)

    def read() -> tuple[bool, str]:
        r = client.get(f"{url.rstrip('/')}/status")
        r.raise_for_status()
        duty = r.json().get("duty") or {}
        who = duty.get("on_duty")
        if who:
            return True, f"operator {who} on duty"
        return False, "no operator on duty"

    return read


class OperatorDuty:
    """The safety monitor's verdict: a :data:`DutySource` polled in a
    thread; safe only on a fresh answer that an operator is on duty."""

    def __init__(self, source: DutySource, poll_interval: float, stale_after: float) -> None:
        self.source = source
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._on_duty = False
        self._why = "the supervisor not read yet"
        self._read_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="duty-poll", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def poll(self) -> None:
        try:
            on_duty, why = self.source()
        except Exception as exc:
            with self._lock:
                self._why = f"supervisor not answering: {exc!r}"
            log.warning("duty read failed: %r", exc)
            return
        with self._lock:
            if on_duty != self._on_duty:
                log.warning("%s: %s", "safe" if on_duty else "unsafe", why)
            self._on_duty, self._why, self._read_at = bool(on_duty), why, time.monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.poll_interval)

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._read_at is None else time.monotonic() - self._read_at
            if age is None or age > self.stale_after:
                why = self._why if age is None else f"no answer for {age:.0f} s ({self._why})"
                return {"safe": False, "reason": why, "read_ago": age}
            return {"safe": self._on_duty, "reason": self._why, "read_ago": age}

    @property
    def safe(self) -> bool:
        return bool(self.status()["safe"])


class NoEnclosureServer(ThreadingHTTPServer):
    """The Alpaca server: the sky (Dome 0) and the operator's duty
    (SafetyMonitor 0). ``duty``: the duty source (default: the
    supervisor's ``GET /status``)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: NoEnclosureConfig, *, duty: DutySource | None = None) -> None:
        host, _, port = config.listen.rpartition(":")
        super().__init__((host, int(port)), _Handler)  # type: ignore[arg-type]
        self.config = config
        self.devices = ("dome", "safetymonitor")
        self.duty = OperatorDuty(
            duty or supervisor_duty(config.supervisor, config.request_timeout),
            config.poll_interval,
            config.stale_after,
        )
        self._shutter = CLOSED
        self._shutter_lock = threading.Lock()
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self._stopped = False

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    @property
    def shutter(self) -> int:
        with self._shutter_lock:
            return self._shutter

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    # -- management ----------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast no enclosure ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{name} sky (no enclosure)",
                    "DeviceType": "Dome",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-no-enclosure-{name}-dome-0",
                },
                {
                    "DeviceName": f"{name} operator duty",
                    "DeviceType": "SafetyMonitor",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-no-enclosure-{name}-safetymonitor-0",
                },
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>No enclosure</title>"
            f"</head><body><h1>robofast no enclosure: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members -------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        with self._clients_lock:
            if value:
                self._clients.add((device, client))
            else:
                self._clients.discard((device, client))

    def _common(self, device: str, member: str, client: int) -> Any:
        name = self.config.name
        if device == "dome":
            desc = f"The sky over {name}: no enclosure (opens and closes at once)"
            info = "robofast.devices.no_enclosure_server: the sky as an Alpaca Dome"
            version, actions = 3, []
            dev_name = f"robofast sky {name}"
        else:
            desc = f"Operator duty at {name}: safe only while an operator is on duty"
            info = "robofast.devices.no_enclosure_server: operator duty as a SafetyMonitor"
            version, actions = 3, list(ACTIONS)
            dev_name = f"robofast operator duty {name}"
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": desc,
            "driverinfo": info,
            "driverversion": DRIVER_VERSION,
            "interfaceversion": version,
            "name": dev_name,
            "supportedactions": actions,
        }[member]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        if verb == "PUT" and member in ("connected", "connect", "disconnect"):
            on = member == "connect" or (member == "connected" and _bool_param(params, "Connected"))
            self._set_connected(device, client, on)
            return None
        if member in COMMON_NAMES:
            if verb != "GET" or member in ("connect", "disconnect"):
                raise _BadRequest(f"{verb} {member} is not allowed")
            return self._common(device, member, client)
        if member == "action" and verb == "GET":
            raise _BadRequest("action is a PUT method")
        if device == "dome":
            return self._dome(verb, member, params, client)
        return self._safety_monitor(verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    def _dome(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        if verb == "GET" and member in CAN:
            return CAN[member]
        self._need("dome", member, client)
        if verb == "GET":
            if member == "shutterstatus":
                return self.shutter
            if member in ("athome", "atpark", "slaved", "slewing"):
                return False
            if member == "devicestate":
                return [
                    {"Name": "AtHome", "Value": False},
                    {"Name": "AtPark", "Value": False},
                    {"Name": "ShutterStatus", "Value": self.shutter},
                    {"Name": "Slewing", "Value": False},
                    {"Name": "TimeStamp", "Value": _now_stamp()},
                ]
            if member in NOT_IMPLEMENTED_GET:
                raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
            raise _BadRequest(f"{verb} {member}: no such member")
        if member in ("openshutter", "closeshutter"):
            state = OPEN if member == "openshutter" else CLOSED
            with self._shutter_lock:
                changed, self._shutter = self._shutter != state, state
            if changed:
                log.info("the sky %s (ClientID %d)", "open" if state == OPEN else "closed", client)
            return None
        if member == "abortslew":
            return None
        if member == "slaved":
            if _bool_param(params, "Slaved"):
                raise AlpacaException(NOT_IMPLEMENTED, "Slaved: the sky cannot slave")
            return None
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "the sky has no actions")
        if member in NOT_IMPLEMENTED_PUT:
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        raise _BadRequest(f"{verb} {member}: no such member")

    def _safety_monitor(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        self._need("safetymonitor", member, client)
        if verb == "GET":
            if member == "issafe":
                return self.duty.safe
            if member == "devicestate":
                return [
                    {"Name": "IsSafe", "Value": self.duty.safe},
                    {"Name": "TimeStamp", "Value": _now_stamp()},
                ]
            raise _BadRequest(f"{verb} {member}: no such member")
        if member == "action":
            if params.get("Action", "").lower() == "status":
                return json.dumps(self.duty.status())
            raise AlpacaException(
                ACTION_NOT_IMPLEMENTED, f"action {params.get('Action')!r} is not supported"
            )
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        raise _BadRequest(f"{verb} {member}: no such member")

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> NoEnclosureServer:
        self.duty.start()
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="no-enclosure-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        self.duty.stop()


def _now_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_device_config(args.config, NoEnclosureConfig.from_dict)
    if args.listen:
        config.listen = args.listen
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server = NoEnclosureServer(config).start()
    print(json.dumps({"url": f"http://{server.address}"}), flush=True)
    stop.wait()
    server.stop()


def main(argv: list[str] | None = None) -> None:
    """The console script (and ``python -m``)."""
    with contextlib.suppress(KeyboardInterrupt):
        _main(argv)


if __name__ == "__main__":
    main()
