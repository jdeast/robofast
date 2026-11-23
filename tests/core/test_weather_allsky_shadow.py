"""The all-sky cloud map in the weather bridge: shadow only.

The ``allsky`` source reads the watcher's ``latest.json``
(``python -m robofast.allsky watch``). Its status is in every evaluation
(and the ``--json`` output) but never in the verdict: whatever it reads, or
if it fails, IsSafe is what it would be without it.

A site turns it on by listing cameras in the weather config's ``allsky``
section: one shadow source per camera (``allsky_<camera>``) plus the pooled
map (``allsky``) when two or more are listed; none listed, no source. The
same guarantees hold for each of them.
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


def _camera(when, clear: float | None, age: float = 40.0) -> dict:
    """One camera's entry in latest.json's ``per_camera``."""
    return {
        "frame": "x.jpg",
        "age_s": age,
        "used": True,
        "time": when.isoformat(),
        "whole_sky_clear": clear,
        "zenith60_clear": clear,
        "coverage": 0.7,
        "flags": [],
        "trend": {"whole_sky": {"slope_per_hour": 0.1, "state": "stable"}},
    }


def _latest(
    path: Path, when, clear: float | None, coverage: float = 0.8, cameras=("mmto", "ridge")
) -> None:
    path.write_text(
        json.dumps(
            {
                "time": when.isoformat(),
                "whole_sky_clear": clear,
                "zenith60_clear": clear,
                "coverage": coverage,
                "frames": [{"camera": "mmto", "frame": "x.jpg", "age_s": 40.0, "used": True}],
                "per_camera": {c: _camera(when, clear) for c in cameras},
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


CAMERAS = ("mmto", "ridge")
#: The sources two listed cameras make.
CAMERA_SOURCES = {"allsky_mmto", "allsky_ridge", "allsky"}


def _with_cameras(raw: dict, path: Path, cameras=CAMERAS) -> dict:
    """``raw`` with the cameras listed in its ``allsky`` section."""
    data = copy.deepcopy(raw)
    data["allsky"] = {
        "cameras": {c: {"dir": f"/nonexistent/{c}"} for c in cameras},
        "output": str(path),
        "stale_after": 600,
        "limits": {"clear": {"open": [0.7, None], "close": [0.5, None]}},
    }
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


# -- cameras listed in the site config --------------------------------------------------


def _allsky_names(cfg) -> set[str]:
    return {s.name for s, _ in cfg.sources if s.type == "allsky"}


@pytest.mark.parametrize(
    "section", [None, {}, {"cameras": {}}, {"cameras": None, "output": "~/x.json"}]
)
def test_no_camera_listed_makes_no_allsky_source(raw, section):
    data = copy.deepcopy(raw)
    data.pop("allsky", None)
    if section is not None:
        data["allsky"] = section
    cfg = parse_config(data)
    assert _allsky_names(cfg) == set()
    assert all(not spec.shadow for _, spec in cfg.sources)


def test_two_cameras_make_two_shadow_sources_and_the_pooled_map(raw, tmp_path):
    p = tmp_path / "latest.json"
    cfg = parse_config(_with_cameras(raw, p))
    assert _allsky_names(cfg) == CAMERA_SOURCES
    for scfg, spec in cfg.sources:
        if scfg.type != "allsky":
            continue
        assert spec.shadow and not spec.required
        assert scfg.options["path"] == str(p)
        assert scfg.options.get("camera") == (
            None if scfg.name == "allsky" else scfg.name.removeprefix("allsky_")
        )
        assert spec.stale_after == 600 and "clear" in spec.limits
    srcs = {s.name: s for s in build_sources(cfg)}
    assert srcs["allsky_ridge"].camera == "ridge" and srcs["allsky"].camera is None
    # One camera: its own source; nothing to pool.
    assert _allsky_names(parse_config(_with_cameras(raw, p, ("mmto",)))) == {"allsky_mmto"}
    # A third camera is a config change only.
    three = _with_cameras(raw, p, ("mmto", "ridge", "third"))
    assert _allsky_names(parse_config(three)) == CAMERA_SOURCES | {"allsky_third"}


@pytest.mark.parametrize(
    "change, match",
    [
        ({"loop": 1}, "unknown keys"),
        ({"cameras": {"mmto": {"dir": "x", "zoom": 2}}}, "unknown keys"),
        ({"cameras": ["mmto"]}, "mapping"),
        ({"output": None}, "output"),
        ({"output": "relative/latest.json"}, "absolute"),
    ],
)
def test_the_allsky_section_is_checked(raw, tmp_path, change, match):
    data = _with_cameras(raw, tmp_path / "latest.json")
    data["allsky"].update(change)
    with pytest.raises(ValueError, match=match):
        parse_config(data)


def test_a_listed_camera_cannot_shadow_a_source_of_the_same_name(raw, tmp_path):
    data = _with_cameras(raw, tmp_path / "latest.json")
    data["sources"]["allsky_mmto"] = {"type": "sun", "required": False}
    with pytest.raises(ValueError, match="allsky_mmto"):
        parse_config(data)


@pytest.mark.parametrize("clear", [0.0, 0.2, 1.0, None])
async def test_camera_readings_never_change_a_safe_verdict(make_bridge, raw, tmp_path, clear):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), clear)
    plain, ev = await _both(make_bridge, raw, _with_cameras(raw, p), pages, clock)
    assert plain.state == ev.state == SAFE and ev.safe
    assert ev.reasons == plain.reasons == []
    assert CAMERA_SOURCES <= set(ev.sources)
    d = json.loads(json.dumps(ev.as_dict()))
    for name in CAMERA_SOURCES:
        s = ev.sources[name]
        assert s.shadow and not s.required
        assert s.values["clear"] == clear
        assert s.state == (UNKNOWN if clear is None else UNSAFE if clear < 0.7 else SAFE)
        assert d["sources"][name]["shadow"] is True
        assert f"{name}[" in ev.line()
    assert ev.sources["allsky_mmto"].values["frame_age"] == 40.0
    assert ev.sources["allsky_ridge"].values["trend_per_hour"] == 0.1


def _camera_fault(p: Path, fault: str, now) -> None:
    """Break latest.json for the cameras' sources in one way."""
    if fault == "missing":
        return
    if fault == "garbage":
        p.write_text("{not json")
        return
    if fault == "stale":
        _latest(p, now - timedelta(hours=3), 0.0)
        return
    _latest(p, now - timedelta(seconds=60), 0.0)
    doc = json.loads(p.read_text())
    if fault == "camera_absent":
        doc["per_camera"] = {}
        doc["time"] = None
    elif fault == "camera_not_analysed":
        for c in doc["per_camera"].values():
            c.update(time=None, used=False, age_s=5000.0)
        doc["time"] = None
    elif fault == "camera_error":
        for c in doc["per_camera"].values():
            c.update(time=None, used=False, error="OSError: truncated")
        doc["time"] = None
    elif fault == "bad_values":
        for c in doc["per_camera"].values():
            c["whole_sky_clear"] = "lots"
        doc["whole_sky_clear"] = "lots"
    p.write_text(json.dumps(doc))


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "garbage",
        "stale",
        "camera_absent",
        "camera_not_analysed",
        "camera_error",
        "bad_values",
    ],
)
async def test_camera_failures_never_change_a_safe_verdict(make_bridge, raw, tmp_path, fault):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    _camera_fault(p, fault, clock.now)
    plain, ev = await _both(make_bridge, raw, _with_cameras(raw, p), pages, clock)
    assert plain.state == ev.state == SAFE
    assert ev.reasons == plain.reasons == []
    for name in CAMERA_SOURCES:
        assert ev.sources[name].state == UNKNOWN, name


async def test_clear_cameras_never_make_bad_weather_safe(make_bridge, raw, tmp_path):
    pages, clock = _good()
    pages.good(NIGHT, outsideHumidity=99.0)
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), 1.0)
    plain, ev = await _both(make_bridge, raw, _with_cameras(raw, p), pages, clock)
    assert plain.state == ev.state == UNSAFE
    assert ev.reasons == plain.reasons
    for name in CAMERA_SOURCES:
        assert ev.sources[name].state == SAFE


async def test_issafe_published_is_unchanged_by_the_camera_sources(make_bridge, raw, tmp_path):
    pages, clock = _good()
    p = tmp_path / "latest.json"
    _latest(p, clock.now - timedelta(seconds=60), 0.0)
    bridge = make_bridge(pages, clock, _with_cameras(raw, p))
    state = WeatherState(safe=False, stale_after=None)
    bridge.publish(state, await _poll(bridge))
    assert state.is_safe() is True
    status = json.loads(state.status_json())["sources"]  # the console's weather card
    assert all(status[n]["shadow"] is True for n in CAMERA_SOURCES)
    p.unlink()  # the watcher dies
    await bridge.poll()
    bridge.publish(state, bridge.evaluate())
    assert state.is_safe() is True


def test_shipped_minerva_config_lists_both_cameras():
    data = load_config(CONFIG)
    assert set(data["allsky"]["cameras"]) == set(CAMERAS)
    assert "allsky" not in data["sources"]
    cfg = parse_config(data)
    assert _allsky_names(cfg) == CAMERA_SOURCES
    for _, spec in cfg.sources:
        if spec.name in CAMERA_SOURCES:
            assert spec.shadow and not spec.required
    assert all(c.source not in CAMERA_SOURCES for c in cfg.conditions)
