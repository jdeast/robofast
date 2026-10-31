"""Optional: mirror guide telemetry to Redis in tres-guider's format.

Off by default. For a future TRES integration, the guider can publish each
summary on a Redis channel (default ``tracking_data``) as the JSON object
jdeast/tres-guider's ``tres.py`` publishes (J. Eastman, J. Kansky)::

    {"timestamp": "YYYY-mm-ddTHH:MM:SS.ffffff", "x_mispointing", "y_mispointing",
     "north_mispointing", "east_mispointing", "dx", "dy", "north", "east",
     "counts", "fwhm", "platescale", "roi_x1", "roi_x2", "roi_y1", "roi_y2"}

``x/y_mispointing`` are the star's pixel, ``dx``/``dy`` the last correction
(pixels), ``north``/``east`` the same in arcseconds (from the plate scale
and PA when configured; tres-guider assumed PA 0), and ``*_mispointing`` the
star's offset from the target in arcseconds.

**Read-only toward external systems**: this only ever ``PUBLISH``es. It
never subscribes, never reads keys and never acts on anything it receives,
so nothing on Redis can command the guider. ``redis`` is an optional
import; the client may be any object with ``publish(channel, message)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.guider.calibration import sky_matrix

log = logging.getLogger(__name__)


class TrackingPublisher:
    def __init__(
        self,
        client: Any,
        *,
        channel: str = "tracking_data",
        plate_scale: float | None = None,
        pa: float = 0.0,
        parity: int = 1,
    ) -> None:
        self.client = client
        self.channel = channel
        self.plate_scale = plate_scale
        self.pa = pa
        self.parity = parity
        self.published = 0

    @classmethod
    def connect(cls, host: str, port: int = 6379, **kw: Any) -> TrackingPublisher:
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("the redis package is not installed") from exc
        return cls(redis.Redis(host=host, port=port, socket_timeout=0.5), **kw)

    def message(self, guider: Any, summary: dict[str, Any]) -> dict[str, Any]:
        rec = guider.records[-1]
        roi = guider._roi
        ps = self.plate_scale or 0.0
        sky = sky_matrix(ps, self.pa, self.parity) if ps else np.zeros((2, 2))
        east_m, north_m = sky @ np.array([rec.ex, rec.ey])
        east, north = sky @ np.array([rec.ux, rec.uy])

        def f(v: float) -> float:
            return float(v) if math.isfinite(v) else 0.0

        return {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "x_mispointing": f(rec.x),
            "y_mispointing": f(rec.y),
            "north_mispointing": f(north_m),
            "east_mispointing": f(east_m),
            "dx": f(rec.ux),
            "dy": f(rec.uy),
            "north": f(north),
            "east": f(east),
            "counts": f(rec.flux),
            "fwhm": f(summary.get("fwhm", math.nan)),
            "platescale": ps,
            "roi_x1": roi.x if roi else 0,
            "roi_x2": roi.x + roi.width if roi else 0,
            "roi_y1": roi.y if roi else 0,
            "roi_y2": roi.y + roi.height if roi else 0,
        }

    def publish(self, guider: Any, summary: dict[str, Any]) -> None:
        """Send one message, off the event loop (a slow Redis never delays
        the guide loop)."""
        msg = json.dumps(self.message(guider, summary))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._send(msg)
            return
        loop.run_in_executor(None, self._send, msg)

    def _send(self, msg: str) -> None:
        try:
            self.client.publish(self.channel, msg)
            self.published += 1
        except Exception as exc:
            log.warning("redis publish to %s failed: %r", self.channel, exc)
