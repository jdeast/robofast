"""Serve fake devices over Alpaca HTTP, so another *process* can drive them.

The tests that kill the supervisor or the sequencer need the devices to
outlive the process under test. So the fakes live in the test process behind
this server, the process under test reaches them through the real
:class:`~robofast.alpaca_client.AlpacaDevice`, and the test keeps
reading ground truth straight from the fake objects.

``GET .../{member}`` reads the attribute, ``PUT`` sets it; a member in
:data:`~robofast.alpaca_client.METHODS` is called with its parameters
in that order. Names match case-insensitively. The fakes' faults apply:
``hang()`` holds the request (up to ``hang_limit``), ``fail()`` answers
HTTP 500, and a :class:`FakeDeviceError` is an Alpaca driver error (0x500).
A camera's ``ImageArray`` (an :class:`~robofast.imagebytes.ImageArray`)
goes out as ImageBytes when the client accepts it, else as Alpaca JSON.

The Alpaca management API (``/management/apiversions``,
``/management/v1/description``, ``/management/v1/configureddevices``)
lists the fakes as a real Alpaca server lists its devices; with
``discovery_port`` it also answers Alpaca UDP discovery, so
:mod:`robofast.discover` finds it as it would a device plugged in.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast import hwgate
from robofast import imagebytes as ib
from robofast.alpaca_client import METHODS
from robofast.devices.aqawan_server import _Discovery
from robofast.testing.fakes import FakeDevice, FakeDeviceError
from robofast.weather_server import NOT_IMPLEMENTED, _alpaca

log = logging.getLogger(__name__)


_TYPES = {
    t.lower(): t
    for t in (
        "Camera",
        "CoverCalibrator",
        "Dome",
        "FilterWheel",
        "Focuser",
        "ObservingConditions",
        "Rotator",
        "SafetyMonitor",
        "Switch",
        "Telescope",
    )
}


def _decode(text: str) -> Any:
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


class _Handler(BaseHTTPRequestHandler):
    server: FakeAlpacaServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
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
        params = {k.lower(): v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if self.command == "PUT":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            form = parse_qs(body, keep_blank_values=True)
            params.update({k.lower(): v[0] for k, v in form.items()})
        parts = [p for p in url.path.lower().split("/") if p]
        ctid = int(params.get("clienttransactionid", 0) or 0)
        if parts[:1] == ["management"] and self.command == "GET":
            value = self.server.management(parts[1:])
            if value is None:
                self._send(400, {"error": f"unknown path {self.path}"})
            else:
                self._send(200, _alpaca(value, ctid=ctid))
            return
        device = self.server.devices.get("/".join(parts[2:4])) if len(parts) == 5 else None
        if parts[:2] != ["api", "v1"] or device is None:
            self._send(400, {"error": f"unknown path {self.path}"})
            return
        deadline = time.monotonic() + self.server.hang_limit
        while device._hung.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        if device._failure is not None:
            self._send(500, {"error": device._failure})
            return
        names = {n.lower(): n for n in dir(device) if not n.startswith("_")}
        name = names.get(parts[4])
        if name is None:
            self._send(200, _alpaca(ctid=ctid, err=NOT_IMPLEMENTED, msg=f"{parts[4]}"))
            return
        try:
            if name in METHODS:
                args = [_decode(params.get(p.lower(), "")) for p in METHODS[name][1]]
                value = getattr(device, name)(*args)
            elif self.command == "PUT":
                setattr(device, name, _decode(params.get(name.lower(), "")))
                value = None
            else:
                value = getattr(device, name)
        except FakeDeviceError as exc:
            self._send(200, _alpaca(ctid=ctid, err=0x500, msg=str(exc)))
            return
        if isinstance(value, ib.ImageArray):
            self._send_image(value, ctid)
            return
        self._send(200, _alpaca(value, ctid=ctid))

    def _send_image(self, image: ib.ImageArray, ctid: int) -> None:
        if ib.MEDIA_TYPE in (self.headers.get("Accept") or ""):
            data = ib.encode_imagebytes(image.data, element_type=image.element_type, ctid=ctid)
            self.send_response(200)
            self.send_header("Content-Type", ib.MEDIA_TYPE)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        body = _alpaca(image.data.tolist(), ctid=ctid)
        body.update(Type=image.element_type, Rank=image.data.ndim)
        self._send(200, body)


class FakeAlpacaServer(ThreadingHTTPServer):
    """``devices`` maps a device type (``"dome"``, ...) to its fake, as device
    0, or ``"type/N"`` (``"telescope/1"``) to device N."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        devices: dict[str, FakeDevice],
        host: str = "127.0.0.1",
        port: int = 0,
        hang_limit: float = 30.0,
        discovery_port: int | None = None,
    ) -> None:
        super().__init__((host, port), _Handler)
        #: A robofast simulator: the hardware gate accepts it (hwgate).
        self.registration = hwgate.SimulatorRegistration(self.server_address[1])
        self.devices = {k.lower() if "/" in k else f"{k.lower()}/0": d for k, d in devices.items()}
        self.hang_limit = hang_limit
        self._thread: threading.Thread | None = None
        #: Alpaca UDP discovery on ``discovery_port`` (0: any free port), on
        #: the server's own host; None: off.
        self.discovery: _Discovery | None = None
        if discovery_port is not None:
            self.discovery = _Discovery(host, discovery_port, self.server_address[1])

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):  # a killed client
            return
        super().handle_error(request, client_address)

    def management(self, tail: list[str]) -> Any:
        """The management API's value for ``/management/<tail>``; None: unknown."""
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": "robofast fakes",
                "Manufacturer": "robofast",
                "ManufacturerVersion": "test",
                "Location": "loopback",
            }
        if tail == ["v1", "configureddevices"]:
            entries = []
            for key, device in self.devices.items():
                kind, _, number = key.partition("/")
                entries.append(
                    {
                        "DeviceName": f"{type(device).__name__} {number}",
                        "DeviceType": _TYPES.get(kind, kind),
                        "DeviceNumber": int(number),
                        "UniqueID": f"robofast-fake-{self.server_address[1]}-{kind}-{number}",
                    }
                )
            return entries
        return None

    def start(self) -> FakeAlpacaServer:
        if self.discovery is not None:
            self.discovery.start()
        self._thread = threading.Thread(target=self.serve_forever, name="fake-alpaca", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        for d in self.devices.values():
            d.unhang()
        self.shutdown()
        self.server_close()
        if self.discovery is not None:
            self.discovery.stop()
        self.registration.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
