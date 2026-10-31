"""A small Alpaca SafetyMonitor + ObservingConditions server.

The site weather bridge (:mod:`robofast_core.weather_bridge`) evaluates the
site's real feeds and calls :meth:`WeatherState.publish`; tests and the
standalone server call :meth:`WeatherState.update`. Alpaca clients read
``IsSafe``, the ObservingConditions members and ``TimeSinceLastUpdate``. The
server is itself fail-closed: ``IsSafe`` is False if no update has arrived
within ``stale_after`` seconds (set it to ``None`` to disable that, which
tests do to exercise the supervisor's own staleness check).

ObservingConditions: a member the publisher does not provide is
``NotImplemented``; one it provides but cannot currently measure (stale or
missing data) answers a DriverException (0x500), never an old value.
``TimeSinceLastUpdate("")`` is the time since the last publish (the bridge's
liveness); ``TimeSinceLastUpdate("Humidity")`` etc. is the age of that
sensor's own data. ``SensorDescription`` names each sensor's source.

The reasons behind the verdict are served as JSON by ``GET /weather/status``
and by the SafetyMonitor's ``Action("WeatherStatus")``.

For tests it adds fault injection (:meth:`WeatherState.set_mode`):
``"ok"``, ``"error"`` (HTTP 500 on every device request) and ``"hang"``
(requests block until the mode changes or the server stops).

Only the members robofast uses, plus the common ones Alpaca clients and
ConformU expect, are implemented. Run standalone with::

    python -m robofast_core.weather_server --port 11111

and drive it with ``PUT /control/safe`` (form ``value=true|false``),
``PUT /control/mode`` (``value=ok|error|hang``) and ``PUT /control/feed``
(``value=on|off``; "off" freezes the data so it goes stale).
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

log = logging.getLogger(__name__)

NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
ACTION_NOT_IMPLEMENTED = 0x40C
DRIVER_ERROR = 0x500


class _AlpacaFault(Exception):
    def __init__(self, number: int, message: str) -> None:
        super().__init__(message)
        self.number = number


class WeatherState:
    def __init__(self, safe: bool = False, stale_after: float | None = 60.0) -> None:
        self._lock = threading.Lock()
        self._mode_changed = threading.Condition(self._lock)
        self.safe = safe
        self.stale_after = stale_after
        self.last_update = time.monotonic()
        self.mode = "ok"
        self.connected = False
        self.conditions: dict[str, float | None] = {
            "cloudcover": 0.0,
            "humidity": 30.0,
            "rainrate": 0.0,
            "temperature": 10.0,
            "windspeed": 2.0,
        }
        #: member -> wall-clock (Unix) time its data was measured.
        self.sensor_times: dict[str, float] = {}
        #: member -> SensorDescription.
        self.descriptions: dict[str, str] = {}
        #: The publisher's latest status (reasons, readings), served as JSON.
        self.status: dict[str, Any] = {"note": "no weather bridge attached"}
        self._feed_stop: threading.Event | None = None
        self._feed_thread: threading.Thread | None = None

    # -- data -------------------------------------------------------------

    def update(self, safe: bool | None = None, **conditions: float) -> None:
        """A new weather report arrived."""
        with self._lock:
            if safe is not None:
                self.safe = bool(safe)
            self.conditions.update(conditions)
            self.last_update = time.monotonic()

    def publish(
        self,
        *,
        safe: bool,
        conditions: dict[str, float | None],
        sensor_times: dict[str, float] | None = None,
        descriptions: dict[str, str] | None = None,
        status: dict[str, Any] | None = None,
    ) -> None:
        """Replace everything with a new evaluation (the weather bridge's path).

        ``conditions`` keys are lower-case ObservingConditions members; a
        ``None`` value means "provided, but no valid current reading".
        """
        with self._lock:
            self.safe = bool(safe)
            self.conditions = dict(conditions)
            self.sensor_times = dict(sensor_times or {})
            self.descriptions = dict(descriptions or {})
            if status is not None:
                self.status = status
            self.last_update = time.monotonic()

    def sensor_age(self, sensor: str) -> float:
        """TimeSinceLastUpdate for one sensor (lower-case member name)."""
        with self._lock:
            if sensor not in self.conditions:
                raise NotImplementedError
            if self.conditions[sensor] is None or sensor not in self.sensor_times:
                raise _AlpacaFault(DRIVER_ERROR, f"{sensor}: no current reading")
            return max(0.0, time.time() - self.sensor_times[sensor])

    def status_json(self) -> str:
        with self._lock:
            return json.dumps(self.status)

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self.last_update

    def is_safe(self) -> bool:
        with self._lock:
            fresh = (
                self.stale_after is None or time.monotonic() - self.last_update <= self.stale_after
            )
            return self.safe and fresh

    # -- feed (stands in for the scraper) -----------------------------------

    def start_feed(self, interval: float = 0.2) -> None:
        """Re-stamp the current conditions every ``interval`` s."""
        self.stop_feed()
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(interval):
                self.update()

        self._feed_stop = stop
        self._feed_thread = threading.Thread(target=run, name="weather-feed", daemon=True)
        self._feed_thread.start()
        self.update()

    def stop_feed(self) -> None:
        if self._feed_stop is not None:
            self._feed_stop.set()
            assert self._feed_thread is not None
            self._feed_thread.join(timeout=5)
            self._feed_stop = self._feed_thread = None

    # -- faults -----------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in ("ok", "error", "hang"):
            raise ValueError(mode)
        with self._mode_changed:
            self.mode = mode
            self._mode_changed.notify_all()

    def wait_while_hung(self, limit: float) -> None:
        with self._mode_changed:
            self._mode_changed.wait_for(lambda: self.mode != "hang", timeout=limit)


def _alpaca(value: Any = None, *, ctid: int = 0, stid: int = 0, err: int = 0, msg: str = ""):
    body: dict[str, Any] = {
        "ClientTransactionID": ctid,
        "ServerTransactionID": stid,
        "ErrorNumber": err,
        "ErrorMessage": msg,
    }
    if value is not None:
        body["Value"] = value
    return body


class _Handler(BaseHTTPRequestHandler):
    server: WeatherServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _params(self) -> dict[str, str]:
        url = urlparse(self.path)
        params = {k.lower(): v[0] for k, v in parse_qs(url.query).items()}
        if self.command == "PUT":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            params.update({k.lower(): v[0] for k, v in parse_qs(body).items()})
        return params

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
        params = self._params()
        parts = [p for p in urlparse(self.path).path.lower().split("/") if p]
        state = self.server.state
        try:
            ctid = int(params.get("clienttransactionid", 0))
        except ValueError:
            ctid = 0
        stid = self.server.next_stid()

        if parts[:1] == ["control"] and self.command == "PUT" and len(parts) == 2:
            self._control(parts[1], params.get("value", ""))
            return
        if parts == ["weather", "status"] and self.command == "GET":
            self._send(200, json.loads(state.status_json()))
            return
        if parts[:1] == ["management"]:
            self._send(200, _alpaca(self._management(parts), ctid=ctid, stid=stid))
            return
        if len(parts) != 5 or parts[:2] != ["api", "v1"] or parts[3] != "0":
            self._send(400, {"error": f"unknown path {self.path}"})
            return
        device, member = parts[2], parts[4]
        if device not in ("safetymonitor", "observingconditions"):
            self._send(400, {"error": f"unknown device {device}"})
            return

        if state.mode == "hang":
            state.wait_while_hung(self.server.hang_limit)
        if state.mode == "error":
            self._send(500, {"error": "injected fault"})
            return

        try:
            value = self._member(device, member, params)
        except NotImplementedError:
            self._send(
                200,
                _alpaca(ctid=ctid, stid=stid, err=NOT_IMPLEMENTED, msg=f"{member} not implemented"),
            )
            return
        except ValueError as exc:
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=INVALID_VALUE, msg=str(exc)))
            return
        except _AlpacaFault as exc:
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=exc.number, msg=str(exc)))
            return
        self._send(200, _alpaca(value, ctid=ctid, stid=stid))

    def _control(self, what: str, value: str) -> None:
        state = self.server.state
        if what == "safe":
            state.update(safe=value.lower() == "true")
        elif what == "mode":
            state.set_mode(value)
        elif what == "feed":
            state.start_feed() if value == "on" else state.stop_feed()
        else:
            self._send(400, {"error": f"unknown control {what}"})
            return
        self._send(200, {"ok": True})

    def _management(self, parts: list[str]) -> Any:
        tail = parts[1:]
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": "robofast weather (prototype)",
                "Manufacturer": "robofast",
                "ManufacturerVersion": "0.0.1",
                "Location": "test",
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": "robofast weather safety",
                    "DeviceType": "SafetyMonitor",
                    "DeviceNumber": 0,
                    "UniqueID": "robofast-weather-safetymonitor-0",
                },
                {
                    "DeviceName": "robofast weather conditions",
                    "DeviceType": "ObservingConditions",
                    "DeviceNumber": 0,
                    "UniqueID": "robofast-weather-observingconditions-0",
                },
            ]
        return None

    def _member(self, device: str, member: str, params: dict[str, str]) -> Any:
        state = self.server.state
        common = {
            "name": f"robofast weather {device}",
            "description": "robofast site-weather bridge (prototype)",
            "driverinfo": "robofast_core.weather_server",
            "driverversion": "0.0.1",
            "interfaceversion": 3 if device == "safetymonitor" else 2,
            "supportedactions": ["WeatherStatus"],
            "connecting": False,
        }
        if self.command == "PUT":
            if member == "connected":
                state.connected = params.get("connected", "").lower() == "true"
                return None
            if member in ("connect", "disconnect"):
                state.connected = member == "connect"
                return None
            if member == "action":
                if params.get("action", "").lower() != "weatherstatus":
                    raise _AlpacaFault(
                        ACTION_NOT_IMPLEMENTED, f"action {params.get('action')!r} not supported"
                    )
                return state.status_json()
            if device == "observingconditions" and member == "refresh":
                return None
            if device == "observingconditions" and member == "averageperiod":
                if float(params.get("averageperiod", "nan")) != 0.0:
                    raise ValueError("only AveragePeriod 0 (instantaneous) is supported")
                return None
            raise NotImplementedError
        if member == "connected":
            return state.connected
        if member in common:
            return common[member]
        if device == "safetymonitor":
            if member == "issafe":
                return state.is_safe()
            if member == "devicestate":
                return [{"Name": "IsSafe", "Value": state.is_safe()}]
            raise NotImplementedError
        # observingconditions
        if member == "timesincelastupdate":
            sensor = params.get("sensorname", "").lower()
            return state.age() if not sensor else state.sensor_age(sensor)
        if member == "sensordescription":
            sensor = params.get("sensorname", "").lower()
            if sensor not in state.conditions:
                raise NotImplementedError
            return state.descriptions.get(sensor, sensor)
        if member == "averageperiod":
            return 0.0
        if member in state.conditions:
            value = state.conditions[member]
            if value is None:
                raise _AlpacaFault(DRIVER_ERROR, f"{member}: no current reading")
            return value
        raise NotImplementedError


class WeatherServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        state: WeatherState,
        host: str = "127.0.0.1",
        port: int = 0,
        hang_limit: float = 120.0,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.state = state
        self.hang_limit = hang_limit
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients that time out and hang up (exactly what the fault tests do)
        # are routine here, not tracebacks.
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):
            log.debug("client %s went away", client_address)
            return
        super().handle_error(request, client_address)

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid += 1
            return self._stid

    def start(self) -> WeatherServer:
        self._thread = threading.Thread(target=self.serve_forever, name="weather-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.state.set_mode("ok")  # release hung handlers
        self.state.stop_feed()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--stale-after", type=float, default=60.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    state = WeatherState(safe=False, stale_after=args.stale_after)
    server = WeatherServer(state, args.host, args.port)
    print(f"weather server on http://{server.address}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
