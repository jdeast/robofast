"""The site Redis source, and the shipped MINERVA config (Redis primary, pages advisory).

A fake Redis server (:mod:`robofast_core.testing.redis_feed`) speaks RESP on
a loopback port; the SAO pages come from an in-memory HTTP transport. With
an injected clock, the fake server stamps db 7's timestamp keys from that
clock (minus a per-key age), as the site's writer would. ``REDIS_DB7`` is
the capture of 2026-10-31 02:27 MST; db 8 is the stale old station.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from robofast_core import resp
from robofast_core.testing.redis_feed import (
    GOOD_REDIS,
    REDIS_DB7,
    REDIS_DB8,
    RedisFeed,
    RedisFeedState,
)
from robofast_core.weather_bridge import WeatherBridge, build_sources, load_config, parse_config
from robofast_core.weather_eval import SAFE, UNKNOWN, UNSAFE
from robofast_core.weather_sources import RedisSource, SourceConfig, SourceError, convert
from tests.core.test_weather_bridge import CLOUD_URL, NIGHT, RIDGE_URL, Clock, Pages

CONFIG = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "weather.yaml"
#: Just after the capture: 02:27:20 MST = 09:27:20 UTC.
CAPTURE_NOW = datetime(2026, 10, 31, 9, 28, 0, tzinfo=UTC)


@pytest.fixture
def raw(tmp_path: Path) -> dict:
    """The shipped config, cloud credentials in a temp file, the Sun out of the way."""
    creds = tmp_path / "creds.txt"
    creds.write_text("u\np\n")
    data = load_config(CONFIG)
    data["sources"]["cloud"]["credentials_file"] = str(creds)
    data["sources"]["redis"]["timeout"] = 1.0
    return data


class Site:
    """A fake Redis server + in-memory pages + a clock, and bridges built on them."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.clock = Clock(NIGHT)
        self.redis = RedisFeed(
            RedisFeedState({7: dict(GOOD_REDIS), 8: dict(REDIS_DB8)}, clock=self.clock)
        ).start()
        self.raw["sources"]["redis"]["url"] = self.redis.url + "/7"
        self.pages = Pages("", "")
        self.pages.good(self.clock.now)
        self.bridges: list[WeatherBridge] = []

    @property
    def db(self) -> dict[str, str]:
        return self.redis.feed.dbs[7]

    def bridge(self, raw: dict | None = None) -> WeatherBridge:
        cfg = parse_config(raw or self.raw)
        sources = build_sources(cfg, transport=httpx.MockTransport(self.pages))
        self.bridges.append(WeatherBridge(cfg, sources, clock=self.clock))
        return self.bridges[-1]

    async def poll(self, bridge: WeatherBridge, advance: float = 0.0):
        """Advance the clock, refresh the pages' timestamps, poll, evaluate."""
        self.clock.advance(advance)
        self.pages.good(self.clock.now - timedelta(seconds=5))
        await bridge.poll()
        return bridge.evaluate()

    async def close(self) -> None:
        for b in self.bridges:
            await b.aclose()
        self.redis.stop()


@pytest.fixture
async def site(raw):
    s = Site(raw)
    yield s
    await s.close()


# -- the RESP client --------------------------------------------------------------


@pytest.mark.parametrize(
    "command", [b"SET", b"set", b"DEL", b"FLUSHALL", b"FLUSHDB", b"CONFIG", b"EVAL", b"MSET"]
)
def test_resp_client_refuses_anything_but_reads(command):
    with pytest.raises(resp.RedisError, match="read-only"):
        resp.encode(command, "time", "x")


def test_resp_encoding():
    assert resp.encode(b"MGET", "a", "bc") == b"*3\r\n$4\r\nMGET\r\n$1\r\na\r\n$2\r\nbc\r\n"
    assert resp.encode("SELECT", 7) == b"*2\r\n$6\r\nSELECT\r\n$1\r\n7\r\n"


async def _reply(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return await resp.read_reply(reader)


async def test_resp_replies():
    assert await _reply(b"+OK\r\n") == "OK"
    assert await _reply(b":42\r\n") == 42
    assert await _reply(b"$-1\r\n") is None
    assert await _reply(b"*3\r\n$3\r\n8.7\r\n$-1\r\n$0\r\n\r\n") == [b"8.7", None, b""]
    with pytest.raises(resp.RedisError, match="server error: ERR nope"):
        await _reply(b"-ERR nope\r\n")
    with pytest.raises(resp.RedisError, match="closed mid-reply"):
        await _reply(b"$5\r\nab")
    with pytest.raises(resp.RedisError, match="too large"):
        await _reply(b"$99999999\r\n")
    with pytest.raises(resp.RedisError, match="unexpected reply"):
        await _reply(b"HTTP/1.1 400 Bad Request\r\n")


async def test_mget_selects_the_database_and_sends_nothing_else():
    feed = RedisFeedState({7: {"a": "1"}, 8: {"a": "2"}})
    with RedisFeed(feed) as server:
        host, port = server.server_address[:2]
        assert await resp.mget(host, port, 7, ["a", "b"], timeout=2) == [b"1", None]
        assert await resp.mget(host, port, 8, ["a"], timeout=2) == [b"2"]
    assert feed.commands == [["SELECT", "7"], ["MGET", "a", "b"], ["SELECT", "8"], ["MGET", "a"]]


async def test_mget_is_bounded_by_its_timeout():
    feed = RedisFeedState({7: {"a": "1"}})
    feed.delay = 5
    with RedisFeed(feed) as server:
        host, port = server.server_address[:2]
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await resp.mget(host, port, 7, ["a"], timeout=0.3)
        assert asyncio.get_running_loop().time() - t0 < 2


# -- the source: config and parsing ------------------------------------------------


def _source(raw: dict, **changes) -> RedisSource:
    spec = copy.deepcopy(raw["sources"]["redis"])
    spec.update(changes)
    options = {
        k: v
        for k, v in spec.items()
        if k in ("fields", "time_key", "timestamps", "time_format", "utc_offset_hours")
    }
    cfg = SourceConfig(
        "redis",
        "redis",
        url=spec["url"],
        credentials_file=spec.get("credentials_file"),
        stale_after=spec["stale_after"],
        options=options,
    )
    return RedisSource(cfg)


def _bytes(db: dict[str, str]) -> dict[str, bytes]:
    return {k: v.encode() for k, v in db.items()}


def test_capture_parses_with_mst_timestamps_and_converted_wind(raw):
    src = _source(raw)
    assert (src.host, src.port, src.db) == ("starlord.sao.arizona.edu", 6379, 7)
    r = src.parse(_bytes(REDIS_DB7))
    # 02:27:20 MST (UTC-7, no DST) = 09:27:20 UTC; the mjd key agrees.
    assert r.time == datetime(2026, 10, 31, 9, 27, 20, tzinfo=UTC)
    assert (61344.393993 - 40587.0) * 86400 == pytest.approx(r.time.timestamp(), abs=1)
    v = r.values
    assert v["temperature"] == 8.7 and v["humidity"] == 100.0 and v["dewpoint"] == 8.7
    assert v["pressure"] == 22.69
    # wind read as km/h, reported in mph (the limits' unit): 11.3 km/h = 7.02 mph
    assert v["wind"] == pytest.approx(7.0215, abs=1e-4)
    assert v["wind_gust"] == pytest.approx(38.4 / 1.609344)
    assert v["rain_wxt"] == 137.91 and v["rain_total"] == 2240.33
    assert v["rain_intensity"] == 0.0 and v["sky_limit"] == -35.0
    assert v["mearth"] == -1.3 and v["aurora"] == -14.8
    # boltwood read -0.0, legacy's "sensor down"
    assert v["boltwood"] is None
    assert "boltwood: sensor-down value -0" in r.notes
    # each cloud sensor has its own timestamp and staleness
    stamp = datetime(2026, 10, 31, 9, 27, 24, tzinfo=UTC)
    assert r.field_times == {"mearth": stamp, "boltwood": stamp}
    assert r.field_stale_after == {"mearth": 300.0, "boltwood": 300.0}


def test_units_are_configurable(raw):
    fields = copy.deepcopy(raw["sources"]["redis"]["fields"])
    fields["wind"] = {"key": "wind", "unit": "mph"}  # if it turns out to be mph already
    r = _source(raw, fields=fields).parse(_bytes(REDIS_DB7))
    assert r.values["wind"] == 11.3
    assert convert(11.3, "km/h", "mph") == pytest.approx(7.0215, abs=1e-4)
    assert convert(22.69, "inHg", "hPa") == pytest.approx(768.37, abs=0.01)


def test_missing_or_bad_keys(raw):
    src = _source(raw)
    db = _bytes(REDIS_DB7)
    del db["humidity"]
    db["wind"] = b"n/a"
    db["mbolt_time"] = b"yesterday"
    r = src.parse(db)
    assert r.values["humidity"] is None and "humidity: key humidity absent" in r.notes
    assert r.values["wind"] is None and any("wind: wind=b'n/a'" in n for n in r.notes)
    assert r.values["mearth"] is None and "mearth" not in r.field_times
    assert any("timestamp mbolt_time=b'yesterday' unreadable" in n for n in r.notes)
    del db["time"]
    with pytest.raises(SourceError, match="time=None is not a timestamp"):
        src.parse(db)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"url": "http://starlord:6379/7"}, "redis://host"),
        ({"url": "redis://user:pw@starlord:6379/7"}, "no credentials"),
        ({"url": "redis://starlord:6379/seven"}, "not a number"),
        ({"credentials_file": "~/x"}, "take no credentials"),
        ({"time_key": None}, "time_key|not in timestamps"),
        ({"fields": {"x": {"key": "x", "time": "other_time"}}}, "not in timestamps"),
        ({"fields": {"x": {"time": "time"}}}, "needs a key"),
        ({"fields": {"x": {"key": "x", "unit": "km/h", "to": "hPa"}}}, "cannot convert"),
        ({"fields": {"x": {"key": "x", "colour": 1}}}, "unknown"),
        ({"fields": {}}, "no fields"),
        ({"timestamps": {"time": 60}}, "is the time_key"),
    ],
)
def test_source_config_errors(raw, change, match):
    if change.get("time_key", 1) is None:
        spec = copy.deepcopy(raw["sources"]["redis"])
        del spec["time_key"]
        raw["sources"]["redis"] = spec
        change = {}
    with pytest.raises(ValueError, match=match):
        _source(raw, **change)


def test_shipped_config_uses_db7_and_only_reads(raw):
    cfg = parse_config(raw)
    (src,) = [s for s in build_sources(cfg) if s.name == "redis"]
    assert isinstance(src, RedisSource) and src.db == 7
    spec = next(sp for s, sp in cfg.sources if s.name == "redis")
    assert spec.required and not spec.enforce_rain and spec.dynamic_limits == {}


# -- the shipped config end to end ------------------------------------------------


async def test_capture_evaluates_unsafe_for_humidity_and_cloud(raw):
    with RedisFeed(RedisFeedState({7: dict(REDIS_DB7), 8: dict(REDIS_DB8)})) as server:
        raw["sources"]["redis"]["url"] = server.url + "/7"
        pages = Pages("", "")
        pages.good(CAPTURE_NOW)
        cfg = parse_config(raw)
        bridge = WeatherBridge(
            cfg, build_sources(cfg, transport=httpx.MockTransport(pages)), clock=lambda: CAPTURE_NOW
        )
        try:
            await bridge.poll()
            ev = bridge.evaluate()
        finally:
            await bridge.aclose()
        assert server.feed.commands == [["SELECT", "7"], ["MGET", *bridge.sources["redis"].keys]]
    assert ev.state == UNSAFE
    assert "redis: humidity=100 > 75 (open limit)" in ev.reasons
    assert "redis: sky(mearth)=-1.3 > -40 (open limit)" in ev.reasons
    assert ev.sources["redis"].age == pytest.approx(40)
    cond, times, _ = bridge.observing_conditions(ev)
    assert cond["pressure"] == pytest.approx(768.37, abs=0.01)
    assert cond["windspeed"] == pytest.approx(11.3 / 3.6)
    assert cond["skytemperature"] == -1.3
    # SkyTemperature is as old as m_bolt's own timestamp, not `time`.
    assert times["skytemperature"] == datetime(2026, 10, 31, 9, 27, 24, tzinfo=UTC).timestamp()
    assert times["humidity"] == datetime(2026, 10, 31, 9, 27, 20, tzinfo=UTC).timestamp()


async def test_db8_is_stale(site):
    site.raw["sources"]["redis"]["url"] = site.redis.url + "/8"
    ev = await site.poll(site.bridge())
    assert ev.state == UNKNOWN and not ev.safe
    assert ev.sources["redis"].state == UNKNOWN
    assert ev.sources["redis"].reasons[0].startswith("stale: data ")
    assert ev.sources["redis"].values  # old values are kept for the log...
    assert ev.sources["redis"].fresh is False  # ...but not checked or served


async def test_good_weather_is_safe(site):
    bridge = site.bridge()
    ev = await site.poll(bridge)
    assert ev.state == SAFE, ev.reasons
    assert ev.reasons == [] and ev.would_close == []
    assert ev.sources["redis"].values["sky"] == -45.0
    assert site.redis.feed.commands[-2:] == [
        ["SELECT", "7"],
        ["MGET", *bridge.sources["redis"].keys],
    ]
    assert {c[0] for c in site.redis.feed.commands} == {"SELECT", "MGET"}


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("wind", "49.0", "redis: wind=30.4472 > 30 (open limit)"),
        ("wind_gust", "57.0", "redis: wind_gust=35.4182 > 35 (open limit)"),
        ("humidity", "75.5", "redis: humidity=75.5 > 75 (open limit)"),
        ("temperature", "-20.5", "redis: temperature=-20.5 < -20 (open limit)"),
        ("m_bolt", "-39.0", "redis: sky(mearth)=-39 > -40 (open limit)"),
    ],
)
async def test_each_redis_limit_trips(site, key, value, reason):
    site.db[key] = value
    ev = await site.poll(site.bridge())
    assert ev.state == UNSAFE
    assert reason in ev.reasons, ev.reasons


async def test_cloud_sensors_fall_back_and_go_stale_on_their_own_clock(site):
    bridge = site.bridge()
    site.redis.feed.ages["mbolt_time"] = 301  # m_bolt's own timestamp is old
    ev = await site.poll(bridge)
    s = ev.sources["redis"]
    assert ev.state == SAFE, ev.reasons
    assert s.values["mearth"] is None and s.values["sky"] == -40.0  # Aurora decides
    assert "mearth stale: data 301 s old (limit 300 s)" in s.notes
    assert "sky from aurora" in s.notes
    site.db["aurora"] = "0.0"  # Aurora down too
    ev = await site.poll(bridge, 10)
    assert ev.state == UNKNOWN
    assert ev.reasons == ["redis: sky: no sensor available (mearth, aurora)"]


async def test_field_goes_stale_between_polls(site):
    bridge = site.bridge()
    site.redis.feed.ages["mbolt_time"] = 250
    assert (await site.poll(bridge)).sources["redis"].values["mearth"] == -45.0
    site.clock.advance(60)  # no new fetch: m_bolt is now 310 s old
    ev = bridge.evaluate()
    assert ev.sources["redis"].values["mearth"] is None
    assert ev.sources["redis"].values["sky"] == -40.0


@pytest.mark.parametrize("fault", ["down", "drop", "hang", "error"])
async def test_redis_failure_closes_even_with_good_pages(site, fault):
    site.raw["sources"]["redis"]["timeout"] = 0.3
    bridge = site.bridge()
    assert (await site.poll(bridge)).safe
    if fault == "down":
        site.redis.stop()
    elif fault == "drop":
        site.redis.feed.drop = True
    elif fault == "hang":
        site.redis.feed.delay = 3
    else:
        site.redis.feed.dbs[7] = {}  # every key absent: no timestamp
    t0 = asyncio.get_running_loop().time()
    ev = await site.poll(bridge, 10)
    assert asyncio.get_running_loop().time() - t0 < 2.5
    assert ev.state == UNKNOWN and not ev.safe
    assert ev.sources["redis"].reasons[0].startswith("fetch failed:")
    assert ev.sources["redis"].values == {}
    assert ev.sources["ridge"].state == SAFE and ev.sources["cloud"].state == SAFE
    if fault == "down":
        site.redis = RedisFeed(site.redis.feed).start()  # for close()


@pytest.mark.parametrize(
    "fault",
    [500, httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["http500", "refused", "timeout"],
)
async def test_page_failures_do_not_close(site, fault):
    """JDE 2026-10-31: the pages are advisory; a failed page is unknown, not unsafe."""
    bridge = site.bridge()
    assert (await site.poll(bridge)).safe
    site.pages.errors[RIDGE_URL] = fault
    site.pages.errors[CLOUD_URL] = fault
    ev = await site.poll(bridge, 10)
    assert ev.state == SAFE
    assert ev.sources["ridge"].state == UNKNOWN and ev.sources["cloud"].state == UNKNOWN
    assert ev.reasons[0].startswith("ridge (advisory): fetch failed:")
    assert ev.sources["ridge"].values == {}  # still dropped at once


async def test_stale_pages_do_not_close_but_unsafe_pages_do(site):
    bridge = site.bridge()
    await site.poll(bridge)
    site.clock.advance(400)  # pages not refreshed: both stale; Redis restamps
    await bridge.poll()
    ev = bridge.evaluate()
    assert ev.state == SAFE, ev.reasons
    assert ev.sources["ridge"].state == UNKNOWN
    # An advisory page that positively says "unsafe" still closes (the
    # bridge was safe, so the close limit applies).
    site.pages.good(site.clock.now, windSpeed=36.0)
    await bridge.poll()
    ev = bridge.evaluate()
    assert ev.state == UNSAFE
    assert "ridge (advisory): windSpeed=36 > 35 (close limit)" in ev.reasons


async def test_pages_can_be_made_required(site):
    site.raw["sources"]["ridge"]["required"] = True
    bridge = site.bridge()
    site.pages.errors[RIDGE_URL] = 500
    ev = await site.poll(bridge)
    assert ev.state == UNKNOWN
    assert ev.reasons[0].startswith("ridge: fetch failed: SourceError: GET")


# -- rain: shadow by default ----------------------------------------------------------


async def test_rain_is_shadow_only_by_default(site):
    bridge = site.bridge()
    assert (await site.poll(bridge)).safe
    site.db["rain_intensity"] = "0.6"
    ev = await site.poll(bridge, 60)
    assert ev.state == SAFE and ev.safe  # not enforced: IsSafe stays true
    assert len(ev.would_close) == 1
    assert ev.would_close[0].startswith("would close: rain: redis.rain_intensity=0.6 > 0 at ")
    assert ev.would_close[0].endswith("(rain not enforced)")
    assert ev.would_close[0] in ev.reasons
    assert ev.as_dict()["would_close"] == ev.would_close
    assert "would close: rain" in ev.line()
    assert ev.rain_until is None
    # Held (for the record) for rain_hold = 3600 s, like enforced rain.
    site.db["rain_intensity"] = "0.0"
    ev = await site.poll(bridge, 3500)
    assert ev.safe and ev.would_close
    ev = await site.poll(bridge, 200)
    assert ev.safe and ev.would_close == [] and ev.reasons == []


async def test_counter_rain_and_hail_are_shadow_too(site):
    bridge = site.bridge()
    await site.poll(bridge)
    site.db["rain_current"] = "138.10"  # +0.19 mm
    ev = await site.poll(bridge, 60)
    assert ev.safe
    assert any("redis.rain_wxt +0.19" in r for r in ev.would_close)
    assert ev.sources["redis"].values["rain_wxt_rate"] == pytest.approx(0.19 * 60, rel=0.01)
    # (The hold reports the latest detection; hail on a fresh bridge.)
    site.db["rain_current"] = "137.91"
    site.db["hail_intensity"] = "2.0"
    ev = await site.poll(site.bridge(), 60)
    assert ev.safe and any("redis.hail_intensity=2 > 0" in r for r in ev.would_close)


async def test_ridge_rain_is_shadow_too(site):
    bridge = site.bridge()
    await site.poll(bridge)
    site.clock.advance(60)
    site.pages.good(site.clock.now, wxt510Rain=138.5)
    await bridge.poll()
    ev = bridge.evaluate()
    assert ev.safe and any("ridge.wxt510Rain +" in r for r in ev.would_close)


async def test_enforced_rain_closes_for_an_hour(site):
    site.raw["sources"]["redis"]["enforce_rain"] = True
    bridge = site.bridge()
    assert (await site.poll(bridge)).safe
    site.db["rain_intensity"] = "0.6"
    ev = await site.poll(bridge, 60)
    assert ev.state == UNSAFE and ev.would_close == []
    assert any(r.startswith("rain: redis.rain_intensity=0.6 > 0 at ") for r in ev.reasons)
    site.db["rain_intensity"] = "0.0"
    assert (await site.poll(bridge, 3500)).state == UNSAFE
    assert (await site.poll(bridge, 200)).state == SAFE


async def test_cloud_page_rain_sentinel_stays_enforced(site):
    bridge = site.bridge()
    site.clock.advance(10)
    site.pages.good(site.clock.now)
    site.pages.cloud = site.pages.cloud.replace("-45.0", "998.0")
    await bridge.poll()
    ev = bridge.evaluate()
    assert ev.state == UNSAFE
    assert any(r.startswith("rain: cloud reports rain") for r in ev.reasons)


async def test_missing_rain_data_only_matters_when_enforced(site):
    del site.db["rain_intensity"]
    ev = await site.poll(site.bridge())
    assert ev.state == SAFE
    assert "rain rate rain_intensity missing" in ev.sources["redis"].notes
    site.raw["sources"]["redis"]["enforce_rain"] = True
    ev = await site.poll(site.bridge())
    assert ev.state == UNKNOWN
    assert ev.reasons == ["redis: rain rate rain_intensity missing"]


def test_enforce_rain_must_be_a_boolean(raw):
    raw["sources"]["redis"]["enforce_rain"] = "no"
    with pytest.raises(ValueError, match="enforce_rain must be true or false"):
        parse_config(raw)


# -- the dynamic (FLWO observers') sky limit -----------------------------------------


async def test_dynamic_sky_limit_off_by_default(site):
    site.db["m_bolt"] = "-39.0"  # inside sky_limit - 2 = -37, outside the static -40
    ev = await site.poll(site.bridge())
    assert ev.state == UNSAFE
    assert "redis: sky(mearth)=-39 > -40 (open limit)" in ev.reasons


async def test_dynamic_sky_limit_when_enabled(site):
    site.raw["sources"]["redis"]["dynamic_limits"]["mearth"]["enabled"] = True
    bridge = site.bridge()
    site.db["m_bolt"] = "-39.0"
    ev = await site.poll(bridge)
    assert ev.state == SAFE, ev.reasons  # open limit sky_limit - 2 = -37
    assert "mearth: sky_limit=-35: limits open -37, close -35" in ev.sources["redis"].notes
    site.db["m_bolt"] = "-35.5"
    assert (await site.poll(bridge, 10)).state == SAFE  # safe now: close limit -35
    site.db["m_bolt"] = "-34.5"
    ev = await site.poll(bridge, 10)
    assert "redis: sky(mearth)=-34.5 > -35 (close limit)" in ev.reasons
    # A sky_limit outside its sanity range, or missing, falls back to static.
    site.db["m_bolt"] = "-39.0"
    site.db["sky_limit"] = "10"
    ev = await site.poll(bridge, 10)
    assert "redis: sky(mearth)=-39 > -40 (open limit)" in ev.reasons
    assert "mearth: sky_limit 10 outside [-50.0, -25.0]: static limits" in ev.sources["redis"].notes
    del site.db["sky_limit"]
    ev = await site.poll(bridge, 10)
    assert "mearth: sky_limit missing: static limits" in ev.sources["redis"].notes


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"open_offset": 1.0}, "open_offset looser than close_offset"),
        ({"side": "middle"}, "side must be"),
        ({"colour": 1}, "unknown keys"),
    ],
)
def test_dynamic_limit_config_errors(raw, change, match):
    raw["sources"]["redis"]["dynamic_limits"]["mearth"].update(change)
    with pytest.raises(ValueError, match=match):
        parse_config(raw)


# -- shadow mode from the command line ------------------------------------------------


def test_shadow_once_json_reports_would_close(raw, tmp_path, capsys):
    from robofast_core.sun import now_utc
    from robofast_core.testing.weather_feed import FeedServer, FeedState
    from robofast_core.weather_bridge import main

    feed = FeedServer(FeedState(user="u", password="p")).start()
    redis = RedisFeed(RedisFeedState(clock=now_utc)).start()
    try:
        raw["sources"]["redis"]["url"] = redis.url + "/7"
        raw["sources"]["ridge"]["url"] = feed.base_url + "/weather/weather.cur_cond"
        raw["sources"]["cloud"]["url"] = feed.base_url + "/temps/sky_temps_now"
        raw["sources"]["sun"]["limits"]["altitude"] = {"open": [-90, 90]}
        path = tmp_path / "weather.json"
        path.write_text(json.dumps(raw))
        assert main(["--config", str(path), "--shadow", "--once"]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1 and " SAFE limits=open reasons=[-]" in out[0]
        assert "redis[safe," in out[0]
        redis.feed.dbs[7]["rain_intensity"] = "1.5"
        assert main(["--config", str(path), "--shadow", "--once", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["safe"] is True
        assert status["would_close"][0].startswith("would close: rain: redis.rain_intensity=1.5")
        assert {c[0] for c in redis.feed.commands} == {"SELECT", "MGET"}
    finally:
        redis.stop()
        feed.stop()
