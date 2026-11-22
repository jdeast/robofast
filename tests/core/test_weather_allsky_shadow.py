"""The all-sky cloud map in the weather bridge: shadow only.

The ``allsky`` source reads the watcher's ``latest.json``
(``python -m robofast.allsky watch``). Its status is in every evaluation
(and the ``--json`` output) but never in the verdict: whatever it reads, or
if it fails, IsSafe is what it would be without it.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from robofast.weather_bridge import WeatherBridge, build_sources, load_config, parse_config
from robofast.weather_eval import SAFE, UNKNOWN, UNSAFE
from robofast.weather_server import WeatherState
from robofast.weather_sources import AllskySource, SourceConfig, SourceError
from tests.core.test_weather_bridge import CONFIG, NIGHT, _good, _poll, pages_only


@pytest.fixture
def raw(tmp_path: Path) -> dict:
    """The SAO pages config of test_weather_bridge (credentials in tmp)."""
    creds = tmp_path / "creds.txt"
    creds.write_text("u\np\n")
    creds.chmod(0o600)
    data = pages_only(load_config(CONFIG))
    data["sources"]["cloud"]["credentials_file"] = str(creds)
    return data


@pytest.fixture
async def make_bridge(raw):
    made: list[WeatherBridge] = []

    def make(pages, clock, config: dict) -> WeatherBridge:
        cfg = parse_config(config)
        sources = build_sources(cfg, transport=httpx.MockTransport(pages))
        made.append(WeatherBridge(cfg, sources, clock=clock))
        return made[-1]

    yield make
    for b in made:
        await b.aclose()


def _latest(path: Path, when, clear: float | None, coverage: float = 0.8) -> None:
    path.write_text(
        json.dumps(
            {
                "time": when.isoformat(),
                "whole_sky_clear": clear,
                "zenith60_clear": clear,
                "coverage": coverage,
                "frames": [{"camera": "mmto", "frame": "x.jpg", "age_s": 40.0, "used": True}],
                "errors": [],
                "trend": {"whole_sky": {"slope_per_hour": -0.4, "state": "falling"}},
            }
        )
    )


def _with_allsky(raw: dict, path: Path, limits: bool = True) -> dict:
    data = copy.deepcopy(raw)
    spec = {"type": "allsky", "path": str(path), "stale_after": 600}
    if limits:
        # Limits a real source would be judged by: the shadow verdict is
        # computed (and logged) with them, and still ignored.
        spec["limits"] = {"clear": {"open": [0.7, None], "close": [0.5, None]}}
    data["sources"]["allsky"] = spec
    return data


async def _both(make_bridge, raw, data, pages, clock):
    """(evaluation without the shadow source, evaluation with it)."""
    plain = await _poll(make_bridge(pages, clock, copy.deepcopy(raw)))
    shadowed = await _poll(make_bridge(pages, clock, data))
    return plain, shadowed


@pytest.mark.parametrize("clear", [0.0, 0.2, 1.0, None])
async def test_shadow_reading_never_changes_a_safe_verdict(make_bridge, raw, tmp_path, clear):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), clear)
    plain, ev = await _both(make_bridge, raw, _with_allsky(raw, p), pages, clock)
    assert plain.state == SAFE
    assert ev.state == SAFE and ev.safe
    assert ev.reasons == plain.reasons == []
    s = ev.sources["allsky"]
    assert s.shadow and not s.required
    assert s.values["clear"] == clear
    if clear is not None and clear < 0.7:
        assert s.state == UNSAFE  # judged, logged, not enforced
    # In every evaluation's dict (the --json output) and log line.
    d = json.loads(json.dumps(ev.as_dict()))
    assert d["sources"]["allsky"]["shadow"] is True
    assert d["safe"] is True
    assert "allsky[" in ev.line() and ",shadow" in ev.line()


@pytest.mark.parametrize("fault", ["missing", "garbage", "no_time", "stale"])
async def test_shadow_failure_never_changes_a_safe_verdict(make_bridge, raw, tmp_path, fault):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    if fault == "garbage":
        p.write_text("{not json")
    elif fault == "no_time":
        p.write_text(json.dumps({"time": None, "whole_sky_clear": None}))
    elif fault == "stale":
        _latest(p, clock.now - timedelta(hours=3), 0.0)
    plain, ev = await _both(make_bridge, raw, _with_allsky(raw, p), pages, clock)
    assert plain.state == ev.state == SAFE
    assert ev.reasons == []
    assert ev.sources["allsky"].state == UNKNOWN


async def test_shadow_clear_sky_never_makes_bad_weather_safe(make_bridge, raw, tmp_path):
    pages, clock = _good()
    pages.good(NIGHT, outsideHumidity=99.0)
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), 1.0)
    plain, ev = await _both(make_bridge, raw, _with_allsky(raw, p), pages, clock)
    assert plain.state == ev.state == UNSAFE
    assert ev.reasons == plain.reasons
    assert ev.sources["allsky"].state == SAFE


async def test_issafe_published_is_unchanged_by_the_shadow_source(make_bridge, raw, tmp_path):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), 0.0)
    bridge = make_bridge(pages, clock, _with_allsky(raw, p))
    ev = await _poll(bridge)
    state = WeatherState(safe=False, stale_after=None)
    bridge.publish(state, ev)
    assert state.is_safe() is True
    assert json.loads(state.status_json())["sources"]["allsky"]["shadow"] is True
    p.unlink()  # the watcher dies
    await bridge.poll()
    ev = bridge.evaluate()
    bridge.publish(state, ev)
    assert state.is_safe() is True


def test_allsky_is_shadow_only(raw, tmp_path):
    p = tmp_path / "latest.json"
    data = _with_allsky(raw, p)
    cfg = parse_config(data)
    spec = next(s for _, s in cfg.sources if s.name == "allsky")
    assert spec.shadow and not spec.required
    for bad in ({"shadow": False}, {"required": True}):
        d = copy.deepcopy(data)
        d["sources"]["allsky"].update(bad)
        with pytest.raises(ValueError, match="shadow"):
            parse_config(d)
    d = copy.deepcopy(data)
    d["conditions"]["cloudcover"] = {"source": "allsky", "field": "clear", "unit": "percent"}
    with pytest.raises(ValueError, match="shadow-only"):
        parse_config(d)


def test_a_shadow_source_does_not_count_as_the_required_source(raw, tmp_path):
    data = _with_allsky(raw, tmp_path / "latest.json")
    for name in ("ridge", "cloud"):
        data["sources"][name]["enabled"] = False
    data["conditions"] = {}
    with pytest.raises(ValueError, match="required weather source"):
        parse_config(data)


async def test_allsky_source_reads_the_watchers_file(tmp_path):
    p = tmp_path / "latest.json"
    _latest(p, NIGHT, 0.42, coverage=0.5)
    src = AllskySource(SourceConfig("allsky", "allsky", required=False, options={"path": str(p)}))
    r = await src.fetch(NIGHT)
    assert r.time == NIGHT
    assert r.values == {
        "clear": 0.42,
        "zenith60_clear": 0.42,
        "coverage": 0.5,
        "trend_per_hour": -0.4,
        "frame_age": 40.0,
    }
    p.write_text("[]")
    with pytest.raises(SourceError):
        await src.fetch(NIGHT)
    with pytest.raises(ValueError, match="path"):
        AllskySource(SourceConfig("allsky", "allsky", required=False))


def test_build_sources_makes_an_allsky_source(raw, tmp_path):
    cfg = parse_config(_with_allsky(raw, tmp_path / "latest.json", limits=False))
    srcs = {s.name: s for s in build_sources(cfg)}
    assert isinstance(srcs["allsky"], AllskySource)
    bridge = WeatherBridge(cfg, list(srcs.values()))
    assert bridge.evaluator.specs["allsky"].shadow


def test_shipped_minerva_config_has_the_shadow_source_ready():
    data = load_config(CONFIG)
    spec = data["sources"]["allsky"]
    assert spec["type"] == "allsky" and spec["enabled"] is False
    data["sources"]["allsky"]["enabled"] = True
    cfg = parse_config(data)
    shadow = next(s for _, s in cfg.sources if s.name == "allsky")
    assert shadow.shadow and not shadow.required
