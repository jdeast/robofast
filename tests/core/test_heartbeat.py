"""The enclosure heartbeat: supervisor side, fake hardware, OmniSim watchdog."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from robofast_core import DeviceOwner, SafetyConfig, SafetySupervisor, Timeline
from robofast_core.heartbeat_watchdog import HeartbeatWatchdog
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import FakeDeviceError, FakeDome

HEARTBEAT = dict(heartbeat_interval=0.05, heartbeat_timeout=0.4, heartbeat_max_loop_age=0.3)


async def test_healthy_supervisor_keeps_the_enclosure_open(make_rig):
    rig = make_rig(safety=HEARTBEAT, heartbeat_timeout=0.4)
    await rig.start()
    await wait_until(rig.exposing, 5)
    await asyncio.sleep(1.0)  # well past the heartbeat timeout
    assert rig.dome_open() and rig.dome.watchdog_trips == 0
    assert rig.supervisor.status()["pinged_ago"] < 0.2


async def test_stuck_enclosure_loop_withholds_the_heartbeat(make_rig, monkeypatch):
    """The process is alive and its event loop runs, but the enforcement loop
    is stuck: the pings stop, and the hardware closes on its own."""
    rig = make_rig(safety=HEARTBEAT, heartbeat_timeout=0.4)
    await rig.start()
    await wait_until(rig.exposing, 5)

    async def stuck() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(rig.supervisor, "_enforce_once", stuck)
    t0 = time.monotonic()
    dt = await wait_until(rig.dome_closed, 2, what="hardware close")
    assert rig.dome.watchdog_trips == 1
    assert any("heartbeat withheld" in a for a in rig.supervisor.alarms)
    # loop age + device timeout + travel (+ one in-flight pass and slack)
    assert dt < 0.3 + 0.4 + 0.2 + 0.4
    print(f"stuck enclosure loop: hardware closed {time.monotonic() - t0:.2f} s later")


def test_fake_dome_refuses_to_open_without_heartbeat():
    dome = FakeDome(travel=0.0, heartbeat_timeout=0.05)
    time.sleep(0.1)
    with pytest.raises(FakeDeviceError, match="heartbeat"):
        dome.OpenShutter()
    dome.Action("Heartbeat")
    dome.OpenShutter()
    assert dome.state() == 0
    time.sleep(0.1)
    assert dome.state() == 1 and dome.watchdog_trips == 1


def test_heartbeat_interval_needs_a_heartbeat_device():
    tl = Timeline()
    owner = DeviceOwner("x", lambda: None, tl)
    with pytest.raises(ValueError, match="heartbeat"):
        SafetySupervisor(
            "s",
            dome=owner,
            safety_monitor=owner,
            timeline=tl,
            config=SafetyConfig(heartbeat_interval=1.0, heartbeat_timeout=60.0),
        )


def test_heartbeat_interval_is_checked_against_the_device_timeout():
    """At least four pings per device timeout (legacy: "ensure 4 heartbeats
    before timeout"); a config that cannot be checked is rejected too."""
    assert SafetyConfig(heartbeat_interval=15, heartbeat_timeout=60).heartbeat_interval == 15
    assert SafetyConfig().heartbeat_interval is None  # no heartbeat device: fine
    with pytest.raises(ValueError, match="needs heartbeat_timeout"):
        SafetyConfig(heartbeat_interval=10)
    with pytest.raises(ValueError, match="4 pings"):
        SafetyConfig(heartbeat_interval=16, heartbeat_timeout=60)
    with pytest.raises(ValueError, match="> 0"):
        SafetyConfig(heartbeat_interval=0, heartbeat_timeout=60)


async def test_watchdog_closes_a_dome_without_a_heartbeat():
    """The OmniSim stand-in: pings keep it quiet; when they stop, it closes."""
    dome = FakeDome(travel=0.1)  # no heartbeat of its own (like OmniSim)
    server = FakeAlpacaServer({"dome": dome}).start()
    wd = HeartbeatWatchdog(server.address, timeout=0.3, check_interval=0.05).start()
    url = f"http://{wd.address}/api/v1/dome/0/action"
    try:
        async with httpx.AsyncClient(trust_env=False) as http:

            async def ping() -> None:
                r = await http.put(url, data={"Action": "Heartbeat", "Parameters": ""})
                assert r.json()["ErrorNumber"] == 0

            await ping()
            dome.OpenShutter()
            for _ in range(10):  # 0.5 s of pings: stays open
                await ping()
                await asyncio.sleep(0.05)
            assert dome.state() == 0 and wd.trips == 0
            t0 = time.monotonic()
            await wait_until(lambda: dome.state() == 1, 2, what="watchdog close")
            assert wd.trips == 1 and wd.closes_sent == 1
            assert time.monotonic() - t0 < 0.3 + 0.05 + 0.1 + 0.3
    finally:
        wd.stop()
        server.stop()
