"""The 2016-2020 MINERVA RV findings, for the guider (docs/guider.md, "Guide log,
offset alarm and header collection"):

(a) the legacy control code collected FITS headers at each exposure start
    in the thread that also guided: in 30-50 % of (exposure, telescope)
    pairs no guide frame was taken for a median ~215 s, and the star was a
    median 1.7" off the fiber afterwards (~2 m/s). robofast's guide loop
    must keep imaging while headers are collected;
(b) T2 held stars 7-11" off its set point for months: alarm on a
    sustained offset, and log star and fiber positions at the guide rate;
(c) FLUXMID was UNKNOWN: a per-telescope flux log (FAU photometry) at the
    guide cadence.
"""

from __future__ import annotations

import asyncio
import csv
import time
from datetime import UTC, datetime

import httpx
import numpy as np
import pytest

from robofast.clock import NightRule
from robofast.guider.base import RealClock, VirtualClock
from robofast.guider.calibration import Calibration
from robofast.guider.guide_log import COLUMNS, GuideLog, GuideLogConfig
from robofast.guider.loop import GuideConfig, Guider
from robofast.guider.service import GuiderRemote, GuiderService, GuiderServiceConfig
from robofast.guider.sim import Jitter, SimCamera, SimTipTilt, SimWorld
from robofast.registry import ValueRegistry

TARGET = (320.0, 240.0)
TT = [[0.08, 0.03], [-0.03, 0.08]]


def _guider(clock, *, star=(322.0, 238.5), **config) -> Guider:
    tt = SimTipTilt(clock=clock, limits=((-100.0, 100.0), (-100.0, 100.0)))
    world = SimWorld(
        clock=clock,
        star=star,
        jitter=Jitter(rms=0.05, knee=1.0, shake=[], drift=(0.0, 0.0), seed=7),
        tiptilt=tt,
        tt_matrix=np.array(TT),
    )
    cfg = GuideConfig(
        **{
            "target": TARGET,
            "acq_roi": (160, 120, 320, 240),
            "acq_exposure": 0.2,
            "scope": "t2",
            "exposure": 0.05,
            "plate_scale": 1.0,
            **config,
        }
    )
    return Guider(
        SimCamera(world), cfg, tiptilt=tt, tt_cal=Calibration("simtt", "fau", TT), clock=clock
    )


async def _run(g: Guider, clock, seconds: float) -> None:
    g.start()
    assert await g.wait_state(("guiding", "failed"), 30) == "guiding", g.reason
    t0 = clock.now()
    while clock.now() < t0 + seconds:
        await clock.sleep(0.1)


# -- (b) the offset alarm ------------------------------------------------------------


async def test_a_sustained_offset_alarms_once_it_has_lasted():
    """Open loop (measure only): the star stays 2.5 px = 2.5" from the
    setpoint. No alarm before ``offset_alarm_time``; one after."""
    clock = VirtualClock()
    g = _guider(
        clock, mode="open", offset_alarm=1.5, offset_alarm_time=5.0, offset_alarm_window=2.0
    )
    await _run(g, clock, 4.0)
    assert "offset_high" not in [a.kind for a in g.alarms]
    assert g.summary()["offset_arcsec"] == pytest.approx(2.5, abs=0.2)
    t = clock.now()
    while clock.now() < t + 4.0:
        await clock.sleep(0.1)
    await g.stop()
    [a] = [a for a in g.alarms if a.kind == "offset_high"]
    assert a.extra["offset_arcsec"] == pytest.approx(2.5, abs=0.2)
    assert "arcsec from the setpoint" in a.message


async def test_a_star_held_on_the_setpoint_never_alarms():
    clock = VirtualClock()
    g = _guider(clock, offset_alarm=1.5, offset_alarm_time=2.0, offset_alarm_window=1.0)
    await _run(g, clock, 6.0)
    await g.stop()
    assert "offset_high" not in [a.kind for a in g.alarms]
    assert g.summary()["offset_arcsec"] < 0.5


async def test_an_offset_that_comes_back_in_time_does_not_alarm():
    """Offsets shorter than ``offset_alarm_time`` (an acquisition settling,
    a gust) reset the watch; one that stays alarms. Open loop: the star
    goes where the simulated sky puts it."""
    clock = VirtualClock()
    g = _guider(
        clock, mode="open", offset_alarm=1.5, offset_alarm_time=3.0, offset_alarm_window=0.5
    )
    world = g.camera.world
    off, on = (322.0, 238.5), TARGET

    async def hold(star, seconds):
        world.star = star
        t = clock.now()
        while clock.now() < t + seconds:
            await clock.sleep(0.05)

    await _run(g, clock, 0.5)
    for _ in range(4):
        await hold(off, 2.0)
        await hold(on, 1.0)
    assert "offset_high" not in [a.kind for a in g.alarms]
    await hold(off, 4.0)
    await g.stop()
    assert [a.kind for a in g.alarms].count("offset_high") == 1


def test_offset_alarm_config_is_checked():
    with pytest.raises(ValueError, match="offset_alarm must be positive"):
        GuideConfig.from_dict({"offset_alarm": 0})
    with pytest.raises(ValueError, match="offset_alarm_window"):
        GuideConfig.from_dict({"offset_alarm_window": 0})
    assert GuideConfig.from_dict({"offset_alarm": None}).offset_alarm is None
    assert GuideConfig().offset_alarm == 1.5


async def test_the_offset_is_a_fits_value():
    clock = VirtualClock()
    reg = ValueRegistry()
    g = _guider(clock, mode="open")
    g.registry = reg
    await _run(g, clock, 12.0)
    await g.stop()
    v = reg.get("t2.guide_offset_arcsec")
    assert v.keyword == "GUIDOFFA" and v.value == pytest.approx(2.5, abs=0.2)


# -- (b, c) the guide log ----------------------------------------------------------------


async def test_the_guide_log_has_every_guide_frame(tmp_path):
    clock = RealClock()
    g = _guider(clock, guide_log=True, fiber_flux=True, exposure=0.02)
    site = {"timezone": "America/Phoenix"}
    glog = GuideLog(
        g, GuideLogConfig.from_dict({"dir": str(tmp_path), "interval": 0.2, "site": site})
    )
    glog.start()
    t0 = time.time()
    try:
        await _run(g, clock, 1.0)
        await g.stop()
    finally:
        await glog.stop()
    night = NightRule.from_site(site).name(datetime.now(UTC))
    path = tmp_path / f"t2.{night}.guide.csv"
    assert glog.status()["file"] == str(path) and glog.status()["failures"] == 0
    rows = list(csv.DictReader(path.open()))
    assert list(rows[0]) == list(COLUMNS)
    # One row per guide frame, at the guide rate.
    guide_frames = len(rows)
    assert guide_frames == glog.written and guide_frames > 20
    ends = [float(r["t_end"]) for r in rows]
    assert ends == sorted(ends) and t0 - 1 < ends[0] < ends[-1] < time.time() + 1
    assert np.median(np.diff(ends)) < 0.1
    last = rows[-1]
    assert last["valid"] == "1"
    assert (float(last["setpoint_x"]), float(last["setpoint_y"])) == TARGET
    assert (float(last["fiber_x"]), float(last["fiber_y"])) == TARGET
    assert abs(float(last["star_x"]) - TARGET[0]) < 1.0  # guided onto the fiber
    assert float(last["offset_arcsec"]) < 1.0
    assert float(last["flux"]) > 0 and last["fiber_counts"] != ""
    assert float(rows[0]["t_start"]) < float(rows[0]["t_end"])


async def test_the_guide_log_keeps_rows_while_it_cannot_write(tmp_path):
    clock = VirtualClock()
    g = _guider(clock, guide_log=True)
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    glog = GuideLog(g, GuideLogConfig.from_dict({"dir": str(blocker / "logs")}))
    await _run(g, clock, 1.0)
    await g.stop()
    assert await glog.flush() is False
    kept = len(glog.pending)
    assert kept > 0 and glog.failures == 1 and glog.last_error
    glog.config.dir = str(tmp_path / "logs")
    assert await glog.flush() is True
    assert glog.written == kept and not glog.pending


def test_guide_log_config_is_checked():
    with pytest.raises(ValueError, match="needs dir"):
        GuideLogConfig.from_dict({})
    with pytest.raises(ValueError, match="unknown keys"):
        GuideLogConfig.from_dict({"dir": "x", "every": 2})
    with pytest.raises(ValueError, match="site"):
        GuideLogConfig.from_dict({"dir": "x", "site": {"timezone": "Mars/Olympus"}})


# -- (a) header collection never stalls the guide loop ------------------------------------


def _service_config(tmp_path, **kw) -> GuiderServiceConfig:
    d = {
        "name": "t2-guider",
        "scope": "t2",
        "listen": "127.0.0.1:0",
        "port": "fau",
        "camera": {"kind": "sim"},
        "tiptilt": {"kind": "sim", "limits": [[-100, 100], [-100, 100]]},
        "calibrations": {"tiptilt": {"matrix": TT}},
        "guide": {
            "exposure": 0.02,
            "acq_exposure": 0.05,
            "acq_roi": [200, 160, 240, 160],
            "target": [321.5, 239.5],
            "plate_scale": 0.265,
            "window": 1000,  # every frame of the test stays in g.records
        },
        "sim": {
            "star": [323.0, 241.0],
            "jitter": {"rms": 0.1, "knee": 1.0, "seed": 5},
            "tt_matrix": TT,
        },
        "guide_log": {"dir": str(tmp_path / "guidelog"), "interval": 0.5},
    }
    d.update(kw)
    return GuiderServiceConfig.from_dict(d)


async def test_header_collection_never_stalls_the_guide_loop(tmp_path):
    """The legacy stall (finding a): headers were gathered at exposure start
    in the loop that guided. In robofast the sequencer's FITS headers take
    the guider's values from its own registry, which :class:`GuiderRemote`
    refreshes in the background; what reaches the guide computer is HTTP
    reads. Here every header source a sequencer could ask for at an
    exposure start -- ``/status``, ``/values``, ``/telemetry``, the
    console's ``/frame`` -- is asked by eight clients at once, as fast as the
    service answers, for 3 s, through a remote that is polling too; the
    guide loop keeps imaging: no gap between guide frames longer than a
    small multiple of the frame time, and the guide log has them all."""
    svc = await GuiderService(_service_config(tmp_path)).start()
    reg = ValueRegistry()
    remote = GuiderRemote(svc.url, scope="t2", registry=reg, acquire_timeout=20, poll_interval=0.1)
    stop = asyncio.Event()
    asked = 0

    async def collector(paths: list[str]) -> None:
        nonlocal asked
        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=30) as http:
            while not stop.is_set():
                for p in paths:
                    r = await http.get(p)
                    assert r.status_code == 200, (p, r.text)
                    asked += 1

    try:
        await remote.start(dec=20.0)
        g = svc.guider
        frames0 = g.frames
        t0 = g.clock.now()
        clients = [
            asyncio.create_task(
                collector(["/status", "/values", "/telemetry?n=200", "/frame?max=1024"])
            )
            for _ in range(8)
        ]
        await asyncio.sleep(3.0)
        stop.set()
        await asyncio.gather(*clients)
        await remote.check()
        ends = [r.t for r in g.records if r.t >= t0]
        assert reg.get("t2.guide_state").value == "guiding"
    finally:
        await remote.stop("done")
        await svc.stop()
    assert asked > 100
    assert g.frames - frames0 > 40
    gaps = np.diff(ends)
    # 20 ms frames; a loaded CI runner may hiccup, but never the legacy
    # minutes (or even a second).
    assert gaps.max() < 0.5, f"guide loop stalled {gaps.max():.2f} s"
    assert np.median(gaps) < 0.06
    rows = list(csv.DictReader(next((tmp_path / "guidelog").glob("t2.n*.guide.csv")).open()))
    assert len(rows) >= g.frames - frames0


async def test_the_offset_alarm_reaches_the_sequencer_once(tmp_path):
    """The service's ``offset_high`` reaches the sequencer's alarm (status,
    console, notifier) through :class:`GuiderRemote`, once per alarm."""
    cfg = _service_config(
        tmp_path,
        guide={
            "exposure": 0.02,
            "acq_exposure": 0.05,
            "acq_roi": [200, 160, 240, 160],
            "target": [321.5, 239.5],
            "plate_scale": 1.0,
            "mode": "open",
            "offset_alarm": 1.5,
            "offset_alarm_time": 0.5,
            "offset_alarm_window": 0.2,
        },
    )
    svc = await GuiderService(cfg).start()
    got: list[str] = []
    remote = GuiderRemote(svc.url, scope="t2", registry=ValueRegistry(), poll_interval=0.1)
    remote.on_alarm = got.append
    try:
        await remote.start(dec=20.0)
        deadline = time.monotonic() + 10
        while not got and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)  # more polls: still one
    finally:
        await remote.stop("done")
        await svc.stop()
    assert len(got) == 1 and got[0].startswith("guider t2: offset_high: star 2.")


async def test_the_sequencer_raises_the_guiders_forwarded_alarm(make_rig):
    from robofast.plan import StaticPlan

    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 1}], ["t1"])
    rig = make_rig(plan=plan)
    unit = rig.sequencer.units["t1"]
    unit.guider = GuiderRemote("http://127.0.0.1:9", scope="t1")
    rig.sequencer._wire_pointing()
    unit.guider.on_alarm("guider t1: offset_high: star 2.10 arcsec from the setpoint")
    assert unit.alarms == ["guider t1: offset_high: star 2.10 arcsec from the setpoint"]
    [ev] = rig.timeline.events("alarm")
    assert ev.detail["telescope"] == "t1"
