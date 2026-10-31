"""A fake MINERVA weather-page server for tests (and for trying the bridge).

Serves the two SAO pages the MINERVA bridge reads, in their real formats,
stamped with the current time (or a configured age):

* ``/weather/weather.cur_cond`` -- the ridge station (UTC timestamp line,
  then ``key=value``), no auth;
* ``/temps/sky_temps_now`` -- the cloud sensors (local MST timestamp, then
  one value per sensor), HTTP basic auth with the given user and password.

The fields are mutable attributes; tests change them between polls. The
defaults are the real pages captured 2026-10-30 20:16 MST, but with
good-weather values (``GOOD_RIDGE``, ``GOOD_CLOUD``).
"""

from __future__ import annotations

import base64
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: The ridge page as captured 2026-10-30 ~20:16 MST.
RIDGE_FIXTURE = """2026, 10, 31, 03, 16, 02, 012
outsideTemp=8.7
windSpeed=7.21
windGustSpeed=20.38
windDirectionDegrees=226
barometer=22.68
outsideHumidity=100.0
wxt510Rain=137.91
totalRain=2240.33
outsideDewPt=8.7
"""
#: The cloud page as captured at the same time (5 fields: MEarth, Aurora, MINERVA).
CLOUD_FIXTURE = "Oct-30-2026 20:16:01 -3.9 -3.9 -3.9\n"

GOOD_RIDGE: dict[str, Any] = {
    "outsideTemp": 8.7,
    "windSpeed": 7.21,
    "windGustSpeed": 20.38,
    "windDirectionDegrees": 226,
    "barometer": 22.68,
    "outsideHumidity": 30.0,
    "wxt510Rain": 137.91,
    "totalRain": 2240.33,
    "outsideDewPt": -8.0,
}
#: MEarth, Aurora, MINERVA (the 5-field layout), all clear.
GOOD_CLOUD: list[Any] = [-45.0, -40.0, -30.0]

MST = timezone(timedelta(hours=-7))


def ridge_page(values: dict[str, Any], when: datetime) -> str:
    stamp = when.astimezone(UTC).strftime("%Y, %m, %d, %H, %M, %S, ") + (
        f"{when.microsecond // 1000:03d}"
    )
    return stamp + "\n" + "".join(f"{k}={v}\n" for k, v in values.items())


def cloud_page(values: list[Any], when: datetime) -> str:
    stamp = when.astimezone(MST).strftime("%b-%d-%Y %H:%M:%S")
    return " ".join([stamp, *(str(v) for v in values)]) + "\n"


class FeedState:
    def __init__(self, user: str = "user", password: str = "secret") -> None:
        self.ridge: dict[str, Any] = dict(GOOD_RIDGE)
        self.cloud: list[Any] = list(GOOD_CLOUD)
        #: Seconds subtracted from "now" in each page's timestamp.
        self.ridge_age = 0.0
        self.cloud_age = 0.0
        #: HTTP status to answer with (200 = normal).
        self.status = 200
        #: Seconds to stall before answering.
        self.delay = 0.0
        self.user, self.password = user, password
        self.requests: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    server: FeedServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, text: str, headers: dict[str, str] | None = None) -> None:
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        feed = self.server.feed
        feed.requests.append(self.path)
        if feed.delay:
            time.sleep(feed.delay)
        if feed.status != 200:
            self._send(feed.status, "injected fault")
            return
        now = datetime.now(UTC)
        if self.path == "/weather/weather.cur_cond":
            self._send(200, ridge_page(feed.ridge, now - timedelta(seconds=feed.ridge_age)))
        elif self.path == "/temps/sky_temps_now":
            token = base64.b64encode(f"{feed.user}:{feed.password}".encode()).decode()
            if self.headers.get("Authorization") != f"Basic {token}":
                self._send(401, "auth required", {"WWW-Authenticate": 'Basic realm="sky"'})
                return
            self._send(200, cloud_page(feed.cloud, now - timedelta(seconds=feed.cloud_age)))
        else:
            self._send(404, "not found")


class FeedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, feed: FeedState | None = None, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _Handler)
        self.feed = feed or FeedState()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FeedServer:
        self._thread = threading.Thread(target=self.serve_forever, name="feed-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.feed.delay = 0.0
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
