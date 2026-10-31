"""A fake site Redis server for tests (and for trying the bridge).

Speaks just enough RESP2 for :mod:`robofast_core.resp`: ``SELECT``, ``GET``,
``MGET`` and ``PING``. Every command received is recorded (``commands``), so
tests can assert the bridge never sends anything else; any other command is
answered with an error and recorded too.

``REDIS_DB7`` is MINERVA's site-weather database as captured from
``starlord.sao.arizona.edu`` on 2026-10-31 02:27 MST; ``REDIS_DB8`` stands
for the stale older station in db 8 (its ``time`` was 2024-06-21; the other
values here are copies, not a capture). With a ``clock``, the server
restamps the timestamp keys of ``live_db`` with ``clock() - age`` on every
read, in MST (UTC-7, no DST), as the real server's writer would.
"""

from __future__ import annotations

import socketserver
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

MST = timezone(timedelta(hours=-7))

#: db 7 as captured 2026-10-31 02:27 MST.
REDIS_DB7: dict[str, str] = {
    "a_sky": "-14.8",
    "aurora": "-14.8",
    "boltwood": "-0.0",
    "boltwood_time": "2026-10-31 02:27:24",
    "dew": "8.7",
    "hail": "1.8",
    "hail_duration": "320",
    "hail_intensity": "0.0",
    "humidity": "100.0",
    "m_bolt": "-1.3",
    "mbolt_time": "2026-10-31 02:27:24",
    "mjd": "61344.393993",
    "pressure": "22.69",
    "rain": "2240.33",
    "rain_current": "137.91",
    "rain_duration": "52250",
    "rain_intensity": "0.0",
    "sky_limit": "-35.0",
    "temperature": "8.7",
    "time": "2026-10-31 02:27:20",
    "wind": "11.3",
    "wind_direction": "251",
    "wind_gust": "38.4",
}
#: db 8: an old station, last written 2024-06-21 (values other than the times are copies).
REDIS_DB8: dict[str, str] = {
    **REDIS_DB7,
    "time": "2024-06-21 11:02:13",
    "boltwood_time": "2024-06-21 11:02:13",
    "mbolt_time": "2024-06-21 11:02:13",
}
#: Good observing weather in db 7's format (wind in the server's units, km/h).
GOOD_REDIS: dict[str, str] = {
    **REDIS_DB7,
    "humidity": "30.0",
    "dew": "-8.0",
    "m_bolt": "-45.0",
    "aurora": "-40.0",
    "a_sky": "-40.0",
    "boltwood": "-30.0",
}
TIMESTAMP_KEYS = ("time", "boltwood_time", "mbolt_time")
_MJD_UNIX = 40587.0


class RedisFeedState:
    def __init__(
        self,
        dbs: dict[int, dict[str, str]] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        live_db: int = 7,
    ) -> None:
        self.dbs = dbs if dbs is not None else {7: dict(GOOD_REDIS), 8: dict(REDIS_DB8)}
        self.clock = clock
        self.live_db = live_db
        #: Seconds subtracted from clock() for each timestamp key of live_db.
        self.ages: dict[str, float] = dict.fromkeys(TIMESTAMP_KEYS, 0.0)
        #: Seconds to stall before answering each command.
        self.delay = 0.0
        #: Close the connection without answering.
        self.drop = False
        #: Every command received, as a list of str.
        self.commands: list[list[str]] = []
        self.lock = threading.Lock()

    def snapshot(self, db: int) -> dict[str, str]:
        data = dict(self.dbs.get(db, {}))
        if self.clock is not None and db == self.live_db:
            now = self.clock()
            for key, age in self.ages.items():
                if key in data:
                    stamp = now - timedelta(seconds=age)
                    data[key] = stamp.astimezone(MST).strftime("%Y-%m-%d %H:%M:%S")
            if "mjd" in data:
                data["mjd"] = f"{now.timestamp() / 86400.0 + _MJD_UNIX:.6f}"
        return data


def _bulk(value: str | None) -> bytes:
    if value is None:
        return b"$-1\r\n"
    raw = value.encode()
    return b"$%d\r\n%s\r\n" % (len(raw), raw)


class _Handler(socketserver.StreamRequestHandler):
    server: RedisFeed

    def _command(self) -> list[str] | None:
        line = self.rfile.readline()
        if not line:
            return None
        if not line.startswith(b"*"):
            return line.decode(errors="replace").split()  # inline command
        args = []
        for _ in range(int(line[1:])):
            n = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(n + 2)[:-2].decode(errors="replace"))
        return args

    def handle(self) -> None:
        feed = self.server.feed
        db = 0
        while True:
            try:
                args = self._command()
            except (ValueError, OSError):
                return
            if not args:
                return
            with feed.lock:
                feed.commands.append(args)
            if feed.delay:
                time.sleep(feed.delay)
            if feed.drop:
                return
            cmd = args[0].upper()
            if cmd == "PING":
                out = b"+PONG\r\n"
            elif cmd == "SELECT" and len(args) == 2 and args[1].isdigit():
                db = int(args[1])
                out = b"+OK\r\n"
            elif cmd == "GET" and len(args) == 2:
                out = _bulk(feed.snapshot(db).get(args[1]))
            elif cmd == "MGET" and len(args) > 1:
                data = feed.snapshot(db)
                out = b"*%d\r\n" % (len(args) - 1) + b"".join(_bulk(data.get(k)) for k in args[1:])
            else:
                out = b"-ERR unknown command or wrong arguments\r\n"
            try:
                self.wfile.write(out)
                self.wfile.flush()
            except OSError:
                return


class RedisFeed(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, feed: RedisFeedState | None = None, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        super().__init__((host, port), _Handler)
        self.feed = feed or RedisFeedState()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"redis://{host}:{port}"

    def start(self) -> RedisFeed:
        self._thread = threading.Thread(target=self.serve_forever, name="feed-redis", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.feed.delay = 0.0
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> RedisFeed:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
