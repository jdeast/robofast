"""The weather bridge end to end over real sockets.

A fake site Redis server (:mod:`robofast_core.testing.redis_feed`) and a
fake SAO page server (:mod:`robofast_core.testing.weather_feed`) serve
MINERVA's sources; the bridge (the shipped config: Redis primary, pages
advisory) polls them and publishes to the Alpaca weather server; the safety
supervisor reads that server through its Alpaca owners and closes a (fake)
dome when the bridge reports unsafe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from robofast_core import DeviceOwner, SafetyConfig, SafetySupervisor, Timeline
from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import AlpacaDevice, DriverException, NotImplementedException
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.sun import now_utc
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import FakeDome
from robofast_core.testing.redis_feed import RedisFeed, RedisFeedState
from robofast_core.testing.weather_feed import FeedServer, FeedState
from robofast_core.weather_bridge import WeatherBridge, load_config, parse_config
from robofast_core.weather_server import WeatherServer, WeatherState

CONFIG = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "weather.yaml"


@pytest.fixture
async def site(tmp_path):
    """Redis + page servers -> bridge (running) -> Alpaca weather server."""
    creds = tmp_path / "creds"
    creds.write_text("user\nsecret\n")
    feed = FeedServer(FeedState(user="user", password="secret")).start()
    redis = RedisFeed(RedisFeedState(clock=now_utc)).start()
    raw = load_config(CONFIG)
    raw["sources"]["redis"]["url"] = redis.url + "/7"
    raw["sources"]["ridge"]["url"] = feed.base_url + "/weather/weather.cur_cond"
    raw["sources"]["cloud"]["url"] = feed.base_url + "/temps/sky_temps_now"
    raw["sources"]["cloud"]["credentials_file"] = str(creds)
    for name in ("redis", "ridge", "cloud"):
        raw["sources"][name]["timeout"] = 0.5
    # CI runs at any time of day: take the Sun out of these tests.
    raw["sources"]["sun"]["limits"]["altitude"] = {"open": [-90, 90]}
    raw.update(poll_interval=0.2, evaluate_interval=0.05, server_stale_after=2.0)
    bridge = WeatherBridge(parse_config(raw))
    state = WeatherState(safe=False, stale_after=2.0)
    server = WeatherServer(state, hang_limit=5).start()
    task = asyncio.create_task(bridge.run(state))
    try:
        yield redis.feed, feed.feed, state, server, bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await bridge.aclose()
        server.stop()
        feed.stop()
        redis.stop()


async def test_alpaca_members_report_real_values(site):
    redis, _pages, state, server, _bridge = site
    sm = AlpacaDevice(server.address, "safetymonitor")
    oc = AlpacaDevice(server.address, "observingconditions")
    try:
        await wait_until(lambda: state.is_safe(), 3, what="bridge reports safe")
        assert await sm.get("IsSafe", timeout=1) is True
        # From Redis db 7: 22.69 inHg; wind read as km/h.
        assert await oc.get("Pressure", timeout=1) == pytest.approx(768.37, abs=0.01)
        assert await oc.get("WindSpeed", timeout=1) == pytest.approx(11.3 / 3.6)
        assert await oc.get("WindGust", timeout=1) == pytest.approx(38.4 / 3.6)
        assert await oc.get("WindDirection", timeout=1) == 251
        assert await oc.get("Humidity", timeout=1) == 30.0
        assert await oc.get("Temperature", timeout=1) == 8.7
        assert await oc.get("DewPoint", timeout=1) == -8.0
        assert await oc.get("SkyTemperature", timeout=1) == -45.0  # m_bolt
        assert await oc.get("RainRate", timeout=1) == 0.0  # rain_intensity
        with pytest.raises(NotImplementedException):
            await oc.get("SkyQuality", timeout=1)
        desc = await oc.get("SensorDescription", timeout=1, SensorName="SkyTemperature")
        assert "m_bolt" in desc
        assert 0 <= await oc.get("TimeSinceLastUpdate", timeout=1, SensorName="") < 2
        assert 0 <= await oc.get("TimeSinceLastUpdate", timeout=1, SensorName="Humidity") < 2
        # The reasons, for logs and alarms: an Action and a JSON endpoint.
        assert "WeatherStatus" in await sm.get("SupportedActions", timeout=1)
        status = json.loads(
            await sm.put("Action", timeout=1, Action="WeatherStatus", Parameters="")
        )
        assert status["safe"] is True and status["sources"]["redis"]["state"] == "safe"
        assert status["sources"]["ridge"]["state"] == "safe"

        redis.dbs[7] = {**redis.dbs[7], "humidity": "90.0"}
        await wait_until(lambda: not state.is_safe(), 3, what="bridge reports unsafe")
        assert await sm.get("IsSafe", timeout=1) is False
        async with httpx.AsyncClient(trust_env=False) as http:
            r = await http.get(f"http://{server.address}/weather/status")
        # The close limit tripped it; later evaluations judge by the open limit.
        (reason,) = r.json()["reasons"]
        assert reason in (
            "redis: humidity=90 > 80 (close limit)",
            "redis: humidity=90 > 75 (open limit)",
        )
    finally:
        await sm.aclose()
        await oc.aclose()


async def test_stale_redis_is_unsafe_and_its_values_unavailable(site):
    redis, _pages, state, server, _bridge = site
    oc = AlpacaDevice(server.address, "observingconditions")
    try:
        await wait_until(lambda: state.is_safe(), 3, what="bridge reports safe")
        redis.ages["time"] = 400  # the server keeps answering, with old data
        await wait_until(lambda: not state.is_safe(), 3, what="stale redis data -> unsafe")
        with pytest.raises(DriverException, match="no current reading"):
            await oc.get("Humidity", timeout=1)
        with pytest.raises(DriverException):
            await oc.get("TimeSinceLastUpdate", timeout=1, SensorName="Humidity")
        redis.ages["time"] = 0
        await wait_until(lambda: state.is_safe(), 3, what="fresh again -> safe")
    finally:
        await oc.aclose()


async def test_pages_down_do_not_close(site):
    """The SAO pages are advisory: failing (or stale) they do not make it unsafe."""
    _redis, pages, state, _server, bridge = site
    await wait_until(lambda: state.is_safe(), 3, what="bridge reports safe")
    pages.status = 500
    await wait_until(
        lambda: (
            bridge.last is not None
            and bridge.last.sources["ridge"].state == "unknown"
            and bridge.last.sources["cloud"].state == "unknown"
        ),
        3,
        what="both pages unknown",
    )
    assert state.is_safe()
    assert any(r.startswith("ridge (advisory): fetch failed") for r in state.status["reasons"])
    pages.status = 200
    pages.ridge_age = 400
    await asyncio.sleep(0.5)
    assert state.is_safe()
    # ... but a page that answers "unsafe" still closes.
    pages.ridge_age = 0
    pages.ridge["windSpeed"] = 45.0
    await wait_until(lambda: not state.is_safe(), 3, what="ridge wind -> unsafe")


async def test_supervisor_closes_when_the_bridge_reports_unsafe(site):
    redis, _pages, state, server, _bridge = site
    tl = Timeline()
    dome = FakeDome()
    owners = [
        DeviceOwner(
            "dome", lambda: dome, tl, default_timeout=0.5, privileged_members=ENCLOSURE_MEMBERS
        ),
        alpaca_owner("safetymonitor", "safetymonitor", server.address, tl, default_timeout=0.5),
        alpaca_owner(
            "observingconditions", "observingconditions", server.address, tl, default_timeout=0.5
        ),
    ]
    sup = SafetySupervisor(
        "safety",
        dome=owners[0],
        safety_monitor=owners[1],
        observing_conditions=owners[2],
        timeline=tl,
        config=SafetyConfig(
            poll_interval=0.05,
            read_timeout=0.5,
            stale_after=2.0,
            reopen_hold=0.3,
            lease_ttl=2.0,
            dome_timeout=0.5,
        ),
    )
    task = asyncio.create_task(sup.run())
    try:
        lease = await asyncio.wait_for(sup.request_open("observer"), 5)

        async def renew() -> None:
            while lease.live:
                lease.renew()
                await asyncio.sleep(0.1)

        renewer = asyncio.create_task(renew())
        await asyncio.wait_for(lease.wait_open(), 5)
        assert dome.state() == 0

        # Gusts above the close limit (40 mph) in Redis: 80 km/h = 49.7 mph.
        redis.dbs[7] = {**redis.dbs[7], "wind_gust": "80.0"}
        await wait_until(lambda: dome.state() == 1, 5, what="dome closed")
        assert lease.revoked.is_set()
        assert "IsSafe=False" in lease.revoke_reason
        assert "redis: wind_gust=49.7" in json.dumps(state.status)
        renewer.cancel()
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)
        for o in owners:
            o.close()
