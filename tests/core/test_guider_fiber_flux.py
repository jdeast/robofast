"""The guider's fiber flux and its report to the KiwiSpec service
(docs/kiwispec.md, "Flux from the FAU guide frames").

Fast tier: simulated guide camera (simulated time where it can be) and the
spectrograph-PC simulator on loopback.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque

import httpx
import numpy as np
import pytest

from robofast.guider.base import ROI, Frame, RealClock, VirtualClock
from robofast.guider.flux_report import FluxReportConfig, FluxReporter
from robofast.guider.loop import fiber_counts
from robofast.guider.service import GuiderService
from robofast.guider.sim import Fiber
from tests.core.test_guider_loop import TARGET, _rig, _service_config
from tests.core.test_kiwispec import FAST, service_config


def _frame(data: np.ndarray, x0: int = 100, y0: int = 50) -> Frame:
    ny, nx = data.shape
    return Frame(data, ROI(x0, y0, nx, ny), 1.0, 0.0, 1.0)


def test_fiber_counts_is_background_subtracted():
    yy, xx = np.mgrid[0:64, 0:64]
    star = (
        1e5 / (2 * math.pi * 1.5**2) * np.exp(-((xx - 30.3) ** 2 + (yy - 31.6) ** 2) / (2 * 1.5**2))
    )
    data = star + 200.0
    # the fiber at (30.3, 31.6) in the window, (130.3, 81.6) on the detector
    got = fiber_counts(_frame(data), 130.3, 81.6, radius=6.0)
    assert got == pytest.approx(1e5, rel=0.01)
    half = fiber_counts(_frame(star * 0.5 + 200.0), 130.3, 81.6, radius=6.0)
    assert half == pytest.approx(got / 2, rel=0.01)
    # the annulus must be inside the window
    assert fiber_counts(_frame(data), 103.0, 81.6, radius=6.0) is None


async def test_guide_frames_measure_the_fiber_flux():
    """Behind a beamsplitter the star is whole on the fiber: the counts follow
    the light reaching it (here: cloud halving the transparency)."""
    clock = VirtualClock()
    g, world = _rig(
        clock,
        star=TARGET,
        config={"exposure": 0.05, "fiber_flux": True, "fiber_aperture": 6.0},
        world_kw={"fiber": Fiber.behind_beamsplitter(*TARGET)},
    )
    g.start()
    await g.wait_state(("guiding", "failed"), 30)
    assert g.state == "guiding", g.reason
    t0 = clock.now()
    while clock.now() < t0 + 1.0:
        await clock.sleep(0.05)
    clear = [c for _, _, c in list(g.flux_frames)[-10:]]
    world.transparency = 0.5
    await clock.sleep(0.2)
    t1 = clock.now()
    while clock.now() < t1 + 1.0:
        await clock.sleep(0.05)
    cloudy = [c for _, _, c in list(g.flux_frames)[-10:]]
    await g.stop()
    assert np.median(cloudy) == pytest.approx(np.median(clear) / 2, rel=0.15)
    t_start, t_end, _ = g.flux_frames[-1]
    assert t_end - t_start == pytest.approx(0.05)
    assert math.isfinite(g.records[-1].fiber_counts)


class _FakeGuider:
    def __init__(self) -> None:
        self.clock = RealClock()
        self.flux_frames: deque = deque()


async def test_reporter_posts_unix_times_and_keeps_frames_on_failure():
    from robofast.devices.kiwispec_service import KiwiSpecServer
    from robofast.devices.kiwispec_sim import KiwiSpecSim, SimConfig

    g = _FakeGuider()
    now, unix_now = g.clock.now(), time.time()  # the same moment on both clocks
    g.flux_frames.extend([(now - 1.0, now - 0.5, 100.0), (now - 0.5, now, 120.0)])
    # nobody listening: the frames are kept for the next report
    down = FluxReporter(
        g,
        FluxReportConfig.from_dict(
            {"url": "http://127.0.0.1:9", "telescope": "T2", "timeout": 0.5}
        ),
    )
    assert await down.report() is False
    assert len(down.pending) == 2 and down.status()["failures"] == 1

    sim = await KiwiSpecSim(SimConfig(**FAST)).start()
    server = await KiwiSpecServer(service_config(sim, flux_source="fau")).start(status_loop=False)
    try:
        rep = FluxReporter(g, FluxReportConfig(server.url, "T2", timeout=2.0))
        rep.pending = down.pending
        g.flux_frames.append((now, now + 0.5, 130.0))
        assert await rep.report() is True
        assert rep.pending == [] and rep.sent == 3
        frames = server.kiwispec.flux.window(["T2"], time.time() - 10, time.time() + 10)["T2"]
        assert [c for *_, c in frames] == [100.0, 120.0, 130.0]
        # Unix time: the frame's end on the guider's clock, mapped (not the
        # time now: starting the KiwiSpec service can take seconds when loaded).
        assert frames[-1][1] == pytest.approx(unix_now + 0.5, abs=0.2)
        assert server.kiwispec.status()["flux_reports"]["T2"]["frames"] == 3
        # an unknown telescope is refused, and the frames are kept
        bad = FluxReporter(g, FluxReportConfig(server.url, "T9", timeout=2.0))
        g.flux_frames.append((now + 1, now + 1.5, 1.0))
        assert await bad.report() is False and len(bad.pending) == 1
    finally:
        await server.stop()
        await sim.stop()


async def test_guider_service_flux_report_config(tmp_path):
    cfg = _service_config(
        tmp_path,
        calibrations={},
        flux_report={"url": "http://127.0.0.1:9", "telescope": "T2", "interval": 30},
    )
    svc = GuiderService(cfg)
    assert svc.guider.config.fiber_flux is True
    await svc.start()
    try:
        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=10) as http:
            st = (await http.get("/status")).json()
        assert st["flux_report"]["telescope"] == "T2" and st["flux_report"]["sent"] == 0
    finally:
        await svc.stop()
    with pytest.raises(ValueError, match="flux_report"):
        FluxReportConfig.from_dict({"url": "http://x", "telescope": "T1", "bogus": 1})
    with pytest.raises(ValueError, match="flux_report"):
        FluxReportConfig.from_dict({"url": "", "telescope": "T1"})


async def test_reporter_loop_runs(tmp_path):
    g = _FakeGuider()
    rep = FluxReporter(g, FluxReportConfig("http://127.0.0.1:9", "T1", interval=0.05, timeout=0.2))
    g.flux_frames.append((g.clock.now(), g.clock.now() + 0.1, 5.0))
    rep.start()
    await asyncio.sleep(0.3)
    await rep.stop()
    assert rep.failures >= 1 and len(rep.pending) == 1
