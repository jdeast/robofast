"""The guider's Alpaca camera backend (the slow path) against OmniSim's
camera: a guide window, the frame shape and orientation, and how long a
frame takes over HTTP."""

from __future__ import annotations

import statistics
import time

import pytest

from robofast_core.alpaca_client import AlpacaDevice
from robofast_core.guider.base import ROI, RealClock
from robofast_core.guider.camera import AlpacaCamera

pytestmark = pytest.mark.alpaca_sim


async def test_alpaca_camera_guide_window(omnisim, record_timing):
    cam = AlpacaCamera(AlpacaDevice(omnisim, "camera"), timeout=30, poll=0.01, clock=RealClock())
    try:
        await cam.open()
        w, h = cam.sensor
        assert w > 64 and h > 48
        roi = ROI(10, 20, 64, 48)
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            frame = await cam.expose(0.01, roi, None)
            times.append(time.perf_counter() - t0)
        assert frame.data.shape == (48, 64)  # rows = y, columns = x
        assert frame.roi == roi
        assert frame.t_end >= frame.t_start
        full = await cam.expose(0.01, None, None)
        assert full.data.shape == (h, w)
        record_timing(guide_window_frame_ms=1000 * statistics.median(times))
    finally:
        await cam.close()
