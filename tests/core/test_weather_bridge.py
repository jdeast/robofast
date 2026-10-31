"""The site weather bridge: MINERVA page parsing, limits, staleness, rain, sun.

Most tests drive :class:`WeatherBridge` with an in-memory HTTP transport and
an injected clock, using the real pages captured from the site on
2026-10-30 (``RIDGE_FIXTURE``, ``CLOUD_FIXTURE``) and synthetic variants.
The shipped ``sites/minerva/weather.yaml`` is the config under test.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from robofast_core.sites.minerva import MearthSource, SkyTempsSource
from robofast_core.sun import sun_altitude, sun_ra_dec
from robofast_core.testing.weather_feed import (
    CLOUD_FIXTURE,
    GOOD_CLOUD,
    GOOD_RIDGE,
    RIDGE_FIXTURE,
    cloud_page,
    ridge_page,
)
from robofast_core.weather_bridge import (
    WeatherBridge,
    build_sources,
    load_config,
    main,
    parse_config,
)
from robofast_core.weather_eval import (
    SAFE,
    UNKNOWN,
    UNSAFE,
    Evaluator,
    FieldLimits,
    SourceSpec,
)
from robofast_core.weather_sources import SourceConfig, SourceError, read_credentials, to_ascom

CONFIG = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "weather.yaml"
RIDGE_URL = "http://linmax.sao.arizona.edu/weather/weather.cur_cond"
CLOUD_URL = "http://linmax.sao.arizona.edu/temps/sky_temps_now"

#: Shortly after the fixtures were captured (20:16 MST = 03:16 UTC); night.
FIXTURE_NOW = datetime(2026, 10, 31, 3, 17, 0, tzinfo=UTC)
#: Local midnight a night later; the Sun is far down.
NIGHT = datetime(2026, 11, 1, 7, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Pages:
    """An httpx MockTransport handler serving the two SAO pages."""

    def __init__(self, ridge: str, cloud: str, auth: tuple[str, str] = ("u", "p")) -> None:
        self.ridge, self.cloud = ridge, cloud
        self.auth = auth
        self.errors: dict[str, Exception | int] = {}
        self.delay = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.delay:
            await asyncio.sleep(self.delay)
        err = self.errors.get(url)
        if isinstance(err, Exception):
            raise err
        if err is not None:
            return httpx.Response(err, text="fault")
        if url == RIDGE_URL:
            return httpx.Response(200, text=self.ridge)
        if url == CLOUD_URL:
            token = base64.b64encode(":".join(self.auth).encode()).decode()
            if request.headers.get("Authorization") != f"Basic {token}":
                return httpx.Response(401, text="auth required")
            return httpx.Response(200, text=self.cloud)
        return httpx.Response(404)

    def good(self, when: datetime, **ridge: float) -> None:
        self.ridge = ridge_page({**GOOD_RIDGE, **ridge}, when)
        self.cloud = cloud_page(GOOD_CLOUD, when)


@pytest.fixture
def creds(tmp_path: Path) -> Path:
    p = tmp_path / "creds.txt"
    p.write_text("u\np\n")
    p.chmod(0o600)
    return p


@pytest.fixture
def raw(creds: Path) -> dict:
    data = load_config(CONFIG)
    data["sources"]["cloud"]["credentials_file"] = str(creds)
    return data


@pytest.fixture
async def make_bridge(raw):
    made: list[WeatherBridge] = []

    def make(pages: Pages, clock: Clock, config: dict | None = None) -> WeatherBridge:
        cfg = parse_config(config or raw)
        sources = build_sources(cfg, transport=httpx.MockTransport(pages))
        made.append(WeatherBridge(cfg, sources, clock=clock))
        return made[-1]

    yield make
    for b in made:
        await b.aclose()


async def _poll(bridge: WeatherBridge):
    await bridge.poll()
    return bridge.evaluate()


def _good(when: datetime = NIGHT) -> tuple[Pages, Clock]:
    pages = Pages("", "")
    pages.good(when)
    return pages, Clock(when + timedelta(seconds=20))


# -- the real fixtures ----------------------------------------------------------------


async def test_ridge_fixture_parses_with_utc_timestamp(make_bridge):
    bridge = make_bridge(Pages(RIDGE_FIXTURE, CLOUD_FIXTURE), Clock(FIXTURE_NOW))
    reading = await bridge.sources["ridge"].fetch(FIXTURE_NOW)
    assert reading.time == datetime(2026, 10, 31, 3, 16, 2, 12000, tzinfo=UTC)
    assert reading.values["barometer"] == 22.68
    assert reading.values["wxt510Rain"] == 137.91
    assert len(reading.values) == 9


async def test_cloud_fixture_local_time_and_five_field_layout(make_bridge):
    bridge = make_bridge(Pages(RIDGE_FIXTURE, CLOUD_FIXTURE), Clock(FIXTURE_NOW))
    reading = await bridge.sources["cloud"].fetch(FIXTURE_NOW)
    # 20:16:01 MST (UTC-7, no DST in Arizona) is 03:16:01 UTC the next day.
    assert reading.time == datetime(2026, 10, 31, 3, 16, 1, tzinfo=UTC)
    assert reading.values == {"mearth": -3.9, "hat": None, "aurora": -3.9, "minerva": -3.9}


async def test_fixtures_evaluate_unsafe_with_reasons(make_bridge):
    bridge = make_bridge(Pages(RIDGE_FIXTURE, CLOUD_FIXTURE), Clock(FIXTURE_NOW))
    ev = await _poll(bridge)
    assert ev.state == UNSAFE and not ev.safe
    assert ev.limits == "open"  # startup: not safe yet, so the strict limits
    assert "ridge: outsideHumidity=100 > 75 (open limit)" in ev.reasons
    assert "cloud: sky(mearth)=-3.9 > -40 (open limit)" in ev.reasons
    assert ev.sources["sun"].state == SAFE
    assert ev.sources["sun"].values["altitude"] == pytest.approx(-34.81, abs=0.02)
    # The shadow-mode line carries the verdict, the reasons and the readings.
    line = ev.line()
    assert line.startswith("UNSAFE limits=open reasons=[")
    assert "windSpeed=7.21" in line and "sky=-3.9" in line


async def test_fixture_conditions_in_ascom_units(make_bridge):
    bridge = make_bridge(Pages(RIDGE_FIXTURE, CLOUD_FIXTURE), Clock(FIXTURE_NOW))
    ev = await _poll(bridge)
    cond, times, desc = bridge.observing_conditions(ev)
    assert cond["pressure"] == pytest.approx(768.03, abs=0.01)  # 22.68 inHg
    assert cond["windspeed"] == pytest.approx(3.223, abs=0.001)  # 7.21 mph
    assert cond["windgust"] == pytest.approx(9.110, abs=0.001)
    assert cond["temperature"] == 8.7 and cond["humidity"] == 100.0
    assert cond["dewpoint"] == 8.7 and cond["winddirection"] == 226
    assert cond["skytemperature"] == -3.9
    assert cond["rainrate"] is None  # one counter sample: no rate yet
    assert times["humidity"] == datetime(2026, 10, 31, 3, 16, 2, 12000, tzinfo=UTC).timestamp()
    assert "MEarth" in desc["skytemperature"]


# -- each limit trips ------------------------------------------------------------------


async def test_good_weather_is_safe(make_bridge):
    pages, clock = _good()
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == SAFE, ev.reasons
    assert ev.reasons == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("windSpeed", 30.5, "windSpeed=30.5 > 30"),
        ("windGustSpeed", 35.5, "windGustSpeed=35.5 > 35"),
        ("outsideHumidity", 75.5, "outsideHumidity=75.5 > 75"),
        ("outsideTemp", -20.5, "outsideTemp=-20.5 < -20"),
        ("outsideTemp", 50.5, "outsideTemp=50.5 > 50"),
        ("outsideDewPt", 101, "outsideDewPt=101 > 100"),
        ("windDirectionDegrees", 361, "windDirectionDegrees=361 > 360"),
        ("barometer", -1, "barometer=-1 < 0"),
    ],
)
async def test_each_ridge_limit_trips(make_bridge, field, value, reason):
    pages, clock = _good()
    pages.good(NIGHT, **{field: value})
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNSAFE
    assert any(reason in r for r in ev.reasons), ev.reasons


@pytest.mark.parametrize(
    ("cloud", "reason"),
    [
        ([-39.0, -40.0, -30.0], "sky(mearth)=-39 > -40"),
        ([0.0, -31.5, -20.0], "sky(minerva)=-20 > -24"),  # MEarth down: MINERVA next
        ([0.0, -31.5, 0.0], "sky(aurora)=-31.5 > -32"),  # then Aurora
    ],
)
async def test_cloud_limits_trip_in_legacy_priority_order(make_bridge, cloud, reason):
    pages, clock = _good()
    pages.cloud = cloud_page(cloud, NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNSAFE
    assert any(reason in r for r in ev.reasons), ev.reasons


async def test_sun_limit(make_bridge):
    # 2026-10-31 01:00 UTC (18:00 MST): the Sun is at about -5.8 deg: safe;
    # 2026-10-30 22:00 UTC (15:00 MST): well up: unsafe.
    when = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
    pages, clock = _good(when)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.sources["sun"].values["altitude"] == pytest.approx(-5.91, abs=0.02)
    assert ev.state == SAFE, ev.reasons
    when = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
    pages, clock = _good(when)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNSAFE
    assert any(r.startswith("sun: altitude=") and "> 6 (open limit)" in r for r in ev.reasons)


# -- hysteresis ------------------------------------------------------------------------


async def test_hysteresis_open_vs_close_limits(make_bridge):
    pages, clock = _good()
    bridge = make_bridge(pages, clock)

    async def humidity(h: float) -> str:
        clock.advance(60)
        pages.good(clock.now - timedelta(seconds=5), outsideHumidity=h)
        ev = await _poll(bridge)
        return f"{ev.state}/{ev.limits}"

    # Not safe at startup: the stricter open limit (75 %) applies.
    assert await humidity(78) == "unsafe/open"
    assert await humidity(70) == "safe/open"
    # Now safe: the looser close limit (80 %) applies.
    assert await humidity(78) == "safe/close"
    assert await humidity(80.5) == "unsafe/close"
    # Unsafe again: back to the open limit.
    assert await humidity(78) == "unsafe/open"
    assert await humidity(74) == "safe/open"


# -- staleness, errors, missing data --------------------------------------------------


async def test_stale_ridge_data_is_unknown_even_though_the_fetch_succeeds(make_bridge):
    pages, clock = _good()
    pages.ridge = ridge_page(GOOD_RIDGE, clock.now - timedelta(seconds=301))
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN and not ev.safe
    assert ev.sources["ridge"].state == UNKNOWN
    assert ev.reasons == ["ridge: stale: data 301 s old (limit 300 s)"]


async def test_data_goes_stale_between_polls_without_a_new_fetch(make_bridge):
    """The legacy bug: staleness judged against a time frozen at startup."""
    pages, clock = _good()
    bridge = make_bridge(pages, clock)
    assert (await _poll(bridge)).safe
    clock.advance(300)  # ridge data now 320 s old, cloud 320 s (limit 360)
    ev = bridge.evaluate()
    assert ev.state == UNKNOWN
    assert ev.sources["ridge"].state == UNKNOWN and ev.sources["cloud"].state == SAFE
    cond, _, _ = bridge.observing_conditions(ev)
    assert cond["humidity"] is None  # stale values are not served
    clock.advance(41)
    assert bridge.evaluate().sources["cloud"].state == UNKNOWN


async def test_stale_cloud_page(make_bridge):
    pages, clock = _good()
    pages.cloud = cloud_page(GOOD_CLOUD, clock.now - timedelta(seconds=361))
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN
    assert ev.reasons == ["cloud: stale: data 361 s old (limit 360 s)"]


async def test_future_timestamp_is_unknown(make_bridge):
    pages, clock = _good()
    pages.ridge = ridge_page(GOOD_RIDGE, clock.now + timedelta(minutes=10))
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN
    assert "in the future" in ev.reasons[0]


@pytest.mark.parametrize(
    "fault",
    [500, 404, httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["http500", "http404", "refused", "readtimeout"],
)
async def test_fetch_error_makes_the_source_unknown_and_drops_old_values(make_bridge, fault):
    pages, clock = _good()
    bridge = make_bridge(pages, clock)
    assert (await _poll(bridge)).safe
    pages.errors[RIDGE_URL] = fault
    clock.advance(10)
    ev = await _poll(bridge)
    assert ev.state == UNKNOWN
    assert ev.sources["ridge"].reasons[0].startswith("fetch failed:")
    assert ev.sources["ridge"].values == {}  # the last good reading is not kept
    del pages.errors[RIDGE_URL]
    assert (await _poll(bridge)).safe


async def test_fetch_that_hangs_is_bounded(make_bridge, raw):
    raw["sources"]["ridge"]["timeout"] = 0.2
    raw["sources"]["cloud"]["timeout"] = 0.2
    pages, clock = _good()
    bridge = make_bridge(pages, clock, raw)
    pages.delay = 30
    t0 = asyncio.get_running_loop().time()
    ev = await _poll(bridge)
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert ev.state == UNKNOWN
    assert all("fetch failed" in r for r in ev.reasons)


async def test_garbage_page_is_unknown(make_bridge):
    pages, clock = _good()
    pages.ridge = "<html>maintenance</html>\n"
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN
    assert "bad ridge timestamp" in ev.reasons[0]


async def test_missing_required_field_is_unknown(make_bridge):
    pages, clock = _good()
    ridge = dict(GOOD_RIDGE)
    del ridge["windGustSpeed"]
    pages.ridge = ridge_page(ridge, NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN
    assert ev.reasons == ["ridge: windGustSpeed missing"]


async def test_non_numeric_value_is_missing(make_bridge):
    pages, clock = _good()
    pages.ridge = ridge_page({**GOOD_RIDGE, "windSpeed": "nan"}, NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.reasons == ["ridge: windSpeed missing"]


async def test_no_credentials_file_means_unknown(make_bridge, raw, tmp_path):
    raw["sources"]["cloud"]["credentials_file"] = str(tmp_path / "absent")
    pages, clock = _good()
    ev = await _poll(make_bridge(pages, clock, raw))
    assert ev.state == UNKNOWN
    assert "cannot read credentials file" in ev.sources["cloud"].reasons[0]
    # and without any credentials the page refuses (no defaults are sent)
    del raw["sources"]["cloud"]["credentials_file"]
    ev = await _poll(make_bridge(pages, clock, raw))
    assert "HTTP 401" in ev.sources["cloud"].reasons[0]


def test_credentials_path_expands_environment(tmp_path, monkeypatch):
    p = tmp_path / "c"
    p.write_text("alice\nhunter2\n")
    monkeypatch.setenv("ROBOFAST_TEST_CREDS", str(p))
    assert read_credentials("$ROBOFAST_TEST_CREDS") == ("alice", "hunter2")
    monkeypatch.delenv("ROBOFAST_TEST_CREDS")
    with pytest.raises(SourceError, match="unset environment variable"):
        read_credentials("$ROBOFAST_TEST_CREDS")


# -- the cloud page: layouts, sentinels, calibration --------------------------------


def _sky(raw_options: dict) -> SkyTempsSource:
    cfg = SourceConfig("cloud", "minerva.sky_temps", url="http://x/", options=raw_options)
    return SkyTempsSource(cfg)


@pytest.fixture
def sky(raw) -> SkyTempsSource:
    opts = {
        k: copy.deepcopy(v)
        for k, v in raw["sources"]["cloud"].items()
        if k in ("layouts", "sensors", "utc_offset_hours")
    }
    return _sky(opts)


def test_cloud_layouts_by_field_count(sky):
    six = sky.parse("Oct-30-2026 20:16:01 -41.0 -42.0 -33.0 -25.0\n", NIGHT)
    assert six.values["mearth"] == -41.0 and six.values["aurora"] == -33.0
    assert six.values["minerva"] == -25.0
    # HAT is calibrated: (raw - 1.2899657) / 0.98378265
    assert six.values["hat"] == pytest.approx((-42.0 - 1.2899657) / 0.98378265)
    five = sky.parse(CLOUD_FIXTURE, NIGHT)
    assert five.values == {"mearth": -3.9, "hat": None, "aurora": -3.9, "minerva": -3.9}
    four = sky.parse("Oct-30-2026 20:16:01 -41.0 -33.0\n", NIGHT)
    # The 4-field page's first column was MEarth, disabled in legacy: ignored.
    assert four.values == {"mearth": None, "hat": None, "aurora": -33.0, "minerva": None}


@pytest.mark.parametrize(
    "line", ["Oct-30-2026 20:16:01 -41.0", "Oct-30-2026 20:16:01 1 2 3 4 5", ""]
)
def test_cloud_unknown_layout_is_an_error_not_a_column_shift(sky, line):
    with pytest.raises(SourceError):
        sky.parse(line, NIGHT)


def test_cloud_connection_loss_and_rain_sentinels(sky):
    r = sky.parse("Oct-30-2026 20:16:01 -999.9 0.0 -30.0\n", NIGHT)
    assert r.values["mearth"] is None and r.values["aurora"] is None
    assert r.values["minerva"] == -30.0 and not r.rain
    assert any("connection-loss sentinel -999.9" in n for n in r.notes)
    r = sky.parse("Oct-30-2026 20:16:01 998.0 -33.0 -30.0\n", NIGHT)
    assert r.rain and r.values["mearth"] is None


def test_cloud_page_timezone_across_midnight_and_year():
    s = _sky(
        {"layouts": {3: ["a"]}, "sensors": {"a": {}}, "utc_offset_hours": -7},
    )
    r = s.parse("Dec-31-2026 23:30:00 -40\n", NIGHT)
    assert r.time == datetime(2027, 1, 1, 6, 30, tzinfo=UTC)
    # Arizona has no DST: a July timestamp is still UTC-7.
    r = s.parse("Jul-04-2026 12:00:00 -40\n", NIGHT)
    assert r.time == datetime(2026, 7, 4, 19, 0, tzinfo=UTC)


async def test_all_cloud_sensors_down_is_unknown(make_bridge):
    pages, clock = _good()
    pages.cloud = cloud_page([0.0, -999.9, 0.0], NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNKNOWN
    assert ev.reasons == ["cloud: sky: no sensor available (mearth, hat, minerva, aurora)"]


async def test_cloud_sensor_down_falls_back_to_next(make_bridge):
    pages, clock = _good()
    pages.cloud = cloud_page([0.0, -40.0, -30.0], NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == SAFE
    assert ev.sources["cloud"].values["sky"] == -30.0
    assert "sky from minerva" in ev.sources["cloud"].notes


# -- rain ---------------------------------------------------------------------------


async def test_rain_from_counter_increase_holds_then_clears(make_bridge):
    pages, clock = _good()
    bridge = make_bridge(pages, clock)

    async def step(seconds: float, wxt: float) -> str:
        clock.advance(seconds)
        pages.good(clock.now - timedelta(seconds=5), wxt510Rain=wxt)
        return (await _poll(bridge)).state

    assert await step(0, 137.91) == SAFE
    assert await step(60, 137.95) == SAFE  # +0.04 mm: below the 0.1 mm threshold
    assert await step(60, 138.05) == UNSAFE  # +0.14 mm within 600 s
    ev = bridge.last
    assert any(r.startswith("rain: ridge.wxt510Rain +") for r in ev.reasons), ev.reasons
    rate = ev.sources["ridge"].values["wxt510Rain_rate"]
    assert rate == pytest.approx(0.14 / 120 * 3600)
    # It stops raining (no increase within the window any more), but the hold
    # keeps it unsafe for 1800 s after the last detection.
    assert await step(600, 138.05) == UNSAFE
    assert not bridge.evaluator._counters[("ridge", "wxt510Rain")].raining()
    assert await step(1100, 138.05) == UNSAFE  # 1700 s after the rain
    assert await step(200, 138.05) == SAFE  # 1900 s after


async def test_rain_counter_reset_is_not_rain(make_bridge):
    pages, clock = _good()
    bridge = make_bridge(pages, clock)

    async def step(wxt: float) -> str:
        clock.advance(60)
        pages.good(clock.now - timedelta(seconds=5), wxt510Rain=wxt)
        return (await _poll(bridge)).state

    assert await step(137.91) == SAFE
    assert await step(0.0) == SAFE  # the counter was reset
    assert any("counter reset" in n for n in bridge.last.sources["ridge"].notes)
    assert await step(0.05) == SAFE
    assert await step(0.15) == UNSAFE  # rain counted from the reset value


async def test_rain_sentinel_from_cloud_sensor(make_bridge):
    pages, clock = _good()
    pages.cloud = cloud_page([998.0, -40.0, -30.0], NIGHT)
    ev = await _poll(make_bridge(pages, clock))
    assert ev.state == UNSAFE
    assert any("cloud reports rain" in r for r in ev.reasons)
    assert ev.sources["cloud"].state == SAFE  # MINERVA's sensor decides the sky


# -- required vs advisory -------------------------------------------------------------


async def test_advisory_source(make_bridge, raw):
    raw["sources"]["cloud"]["required"] = False
    pages, clock = _good()
    pages.errors[CLOUD_URL] = 500
    bridge = make_bridge(pages, clock, raw)
    ev = await _poll(bridge)
    assert ev.state == SAFE  # an unknown advisory source does not block
    assert ev.reasons[0].startswith("cloud (advisory): fetch failed")
    del pages.errors[CLOUD_URL]
    pages.cloud = cloud_page([-10.0, -40.0, -30.0], NIGHT)
    assert (await _poll(bridge)).state == UNSAFE  # but its "unsafe" counts


def test_all_weather_sources_unknown():
    # (A config needs one required source, so this is the evaluator alone.)
    limits = {"x": FieldLimits.parse({"open": [0, 1]}, "x")}
    ev = Evaluator(
        [SourceSpec("a", required=False, limits=limits), SourceSpec("b", required=False)]
    ).evaluate(NIGHT, {"a": "HTTP 500", "b": None})
    assert ev.state == UNKNOWN
    assert ev.reasons == [
        "a (advisory): fetch failed: HTTP 500",
        "b (advisory): no reading yet",
        "every weather source is unknown",
    ]


async def test_bridge_error_fails_closed(make_bridge):
    pages, clock = _good()
    bridge = make_bridge(pages, clock)
    assert (await _poll(bridge)).safe

    def boom(*a, **k):
        raise RuntimeError("bug")

    bridge.evaluator.evaluate = boom
    ev = bridge.evaluate()
    assert ev.state == UNKNOWN and ev.reasons == ["bridge error: RuntimeError('bug')"]


# -- config validation ----------------------------------------------------------------


def test_shipped_config_parses():
    cfg = parse_config(load_config(CONFIG))
    assert [s.name for s, _ in cfg.sources] == ["ridge", "cloud", "sun"]  # MEarth disabled
    assert cfg.poll_interval == 60


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r.update(bogus=1), "unknown config keys"),
        (
            lambda r: r["sources"]["ridge"]["limits"].update(
                windSpeed={"open": [0, 40], "close": [0, 35]}
            ),
            "open high limit looser than close",
        ),
        (lambda r: r["sources"]["cloud"]["layouts"].update({5: ["mearth"]}), "needs 3 columns"),
        (lambda r: r["conditions"]["pressure"].update(unit="mph"), "does not convert"),
        (lambda r: r["sources"]["ridge"].update(type="nope"), "type 'nope'"),
        (lambda r: r["sources"]["ridge"].update(color="red"), "unknown options"),
        (
            lambda r: [r["sources"][s].update(required=False) for s in ("ridge", "cloud")],
            "at least one required weather source",
        ),
    ],
)
def test_config_errors(raw, mutate, match):
    mutate(raw)
    with pytest.raises(ValueError, match=match):
        cfg = parse_config(raw)
        build_sources(cfg)


def test_mearth_page_parser():
    src = MearthSource(SourceConfig("mearth", "minerva.mearth", url="http://x/"))
    # MJD 61344.125 = 2026-10-31 03:00 UTC
    line = "61344.125 226 11.6 8.7 -1.0 50 580.1 0 0 0 0 0 0 -35.2 18.0"
    r = src.parse(line, NIGHT)
    assert r.time == datetime(2026, 10, 31, 3, 0, tzinfo=UTC)
    assert r.values["windSpeed"] == 11.6 and r.values["skyTemp"] == -35.2
    assert r.values["windGustSpeed"] == 18.0 and r.values["barometer"] == 580.1
    with pytest.raises(SourceError, match="expected 15"):
        src.parse("61344.125 1 2", NIGHT)


def test_unit_conversions():
    assert to_ascom(22.68, "inHg") == pytest.approx(768.03, abs=0.01)
    assert to_ascom(10, "mph") == pytest.approx(4.4704)
    assert to_ascom(36, "km/h") == pytest.approx(10.0)
    assert to_ascom(760, "mmHg") == pytest.approx(1013.25, abs=0.01)
    assert to_ascom(32, "degF") == pytest.approx(0.0)


# -- the Sun -----------------------------------------------------------------------------

# Geometric (no refraction), topocentric altitudes of the Sun's centre from
# astropy 7.2 (get_body + AltAz, pressure=0), computed offline: the
# reference for our low-precision formula, which agrees to < 0.005 deg.
SUN_REFERENCE = [
    ("2026-10-31T03:16:02", 31.680407, -110.878977, -34.607),
    ("2026-10-31T01:00:00", 31.680407, -110.878977, -5.844),
    ("2026-06-21T19:00:00", 31.680407, -110.878977, 80.023),
    ("2026-12-21T14:00:00", 31.680407, -110.878977, -4.519),
    ("2026-03-20T13:30:00", 31.680407, -110.878977, -0.212),
    ("2000-01-01T12:00:00", 51.4769, 0.0, 15.485),
    ("2045-07-04T08:00:00", -30.2446, -70.7494, -46.632),
    ("1990-02-15T22:45:00", 19.8207, -155.4681, 57.586),
]


@pytest.mark.parametrize(("when", "lat", "lon", "alt"), SUN_REFERENCE)
def test_sun_altitude_reference_values(when, lat, lon, alt):
    t = datetime.fromisoformat(when).replace(tzinfo=UTC)
    assert sun_altitude(t, lat, lon) == pytest.approx(alt, abs=0.02)


def test_sun_position_meeus_example():
    # Meeus, Astronomical Algorithms (2nd ed.), example 25.a: 1992 Oct 13.0 TD,
    # apparent RA 198.38083 deg, declination -7.78507 deg.
    ra, dec = sun_ra_dec(datetime(1992, 10, 13, 0, 0, tzinfo=UTC))
    assert ra == pytest.approx(198.38083, abs=0.01)
    assert dec == pytest.approx(-7.78507, abs=0.01)


def test_sun_needs_aware_time():
    with pytest.raises(ValueError):
        sun_altitude(datetime(2026, 1, 1), 0, 0)


# -- shadow mode ---------------------------------------------------------------------


def test_shadow_once_prints_one_line(raw, tmp_path, capsys, monkeypatch):
    from robofast_core.testing.weather_feed import FeedServer, FeedState

    feed = FeedServer(FeedState(user="u", password="p")).start()
    try:
        raw["sources"]["ridge"]["url"] = feed.base_url + "/weather/weather.cur_cond"
        raw["sources"]["cloud"]["url"] = feed.base_url + "/temps/sky_temps_now"
        # Whatever the time of day the test runs, keep the Sun out of it.
        raw["sources"]["sun"]["limits"]["altitude"] = {"open": [-90, 90]}
        path = tmp_path / "weather.json"
        path.write_text(json.dumps(raw))
        assert main(["--config", str(path), "--shadow", "--once"]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1 and " SAFE limits=open reasons=[-]" in out[0]
        feed.feed.ridge["windSpeed"] = 31
        assert main(["--config", str(path), "--shadow", "--once", "--json"]) == 1
        status = json.loads(capsys.readouterr().out)
        assert status["safe"] is False
        assert status["reasons"] == ["ridge: windSpeed=31 > 30 (open limit)"]
    finally:
        feed.stop()
