"""Serve fake devices over Alpaca HTTP, so another *process* can drive them.

The tests that kill the supervisor or the sequencer need the devices to
outlive the process under test. So the fakes live in the test process behind
this server, the process under test reaches them through the real
:class:`~robofast_core.alpaca_client.AlpacaDevice`, and the test keeps
reading ground truth straight from the fake objects.

``GET .../{member}`` reads the attribute, ``PUT`` sets it; a member in
:data:`~robofast_core.alpaca_client.METHODS` is called with its parameters
in that order. Names match case-insensitively. The fakes' faults apply:
``hang()`` holds the request (up to ``hang_limit``), ``fail()`` answers
HTTP 500, and a :class:`FakeDeviceError` is an Alpaca driver error (0x500).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.alpaca_client import METHODS
from robofast_core.testing.fakes import FakeDevice, FakeDeviceError
from robofast_core.weather_server import NOT_IMPLEMENTED, _alpaca

log = logging.getLogger(__name__)


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
        device = self.server.devices.get("/".join(parts[2:4])) if len(parts) == 5 else None
        if parts[:2] != ["api", "v1"] or device is None:
            self._send(400, {"error": f"unknown path {self.path}"})
            return
        ctid = int(params.get("clienttransactionid", 0) or 0)
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
        self._send(200, _alpaca(value, ctid=ctid))


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
    ) -> None:
        super().__init__((host, port), _Handler)
        self.devices = {k.lower() if "/" in k else f"{k.lower()}/0": d for k, d in devices.items()}
        self.hang_limit = hang_limit
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):  # a killed client
            return
        super().handle_error(request, client_address)

    def start(self) -> FakeAlpacaServer:
        self._thread = threading.Thread(target=self.serve_forever, name="fake-alpaca", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        for d in self.devices.values():
            d.unhang()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
