"""Plan providers, and the enclosure config the services read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robofast_core import supervisor_service
from robofast_core.config import device_address, load_config
from robofast_core.plan import (
    CalibrationPlan,
    CallablePlan,
    Observation,
    StaticPlan,
    plan_from_config,
)
from robofast_core.sequencer_service import build, load_plan
from robofast_core.timeline import Timeline

SITES = Path(__file__).resolve().parents[2] / "sites"


async def _drain(plan, telescope: str) -> list[str]:
    names = []
    while (obs := await plan.next_observation(telescope)) is not None:
        names.append(obs.name)
        plan.report(telescope, obs, "complete", obs.count)
    return names


async def test_static_plan_assigns_targets_to_telescopes_in_order():
    targets = [
        {"name": "a", "ra_hours": 1.0, "dec_deg": 2.0},
        {"name": "b", "telescopes": ["t2"]},
        {"name": "c", "exptime": 5, "count": 3},
    ]
    plan = StaticPlan(targets, ["t1", "t2"])
    assert await _drain(plan, "t1") == ["a", "c"]
    assert await _drain(plan, "t2") == ["a", "b", "c"]
    assert await plan.next_observation("t3") is None


async def test_static_plan_retries_what_did_not_complete():
    plan = StaticPlan([{"name": "a"}, {"name": "b"}], ["t1"])
    a = await plan.next_observation("t1")
    plan.report("t1", a, "interrupted", 0)
    a = await plan.next_observation("t1")
    assert a.name == "a"
    plan.report("t1", a, "failed", 0)
    assert (await plan.next_observation("t1")).name == "a"
    assert plan.history == [("t1", "a", "interrupted", 0), ("t1", "a", "failed", 0)]


def test_static_plan_rejects_bad_targets():
    with pytest.raises(ValueError, match="unknown telescope 't9'"):
        StaticPlan([{"name": "a", "telescopes": ["t9"]}], ["t1"])
    with pytest.raises(ValueError, match="unknown keys"):
        StaticPlan([{"name": "a", "exposure": 3}], ["t1"])


async def test_calibration_plan_is_dark_frames_on_every_telescope():
    plan = CalibrationPlan(["t1", "t2"], biases=5, darks=[[60, 3], [300, 2]])
    for tel in ("t1", "t2"):
        frames = []
        while (obs := await plan.next_observation(tel)) is not None:
            frames.append(obs)
            plan.report(tel, obs, "complete", obs.count)
        assert [(o.name, o.exptime, o.count) for o in frames] == [
            ("bias", 0.0, 5),
            ("dark60", 60.0, 3),
            ("dark300", 300.0, 2),
        ]
        assert not any(o.light for o in frames)


async def test_callable_plan_with_or_without_the_telescope():
    assert (await CallablePlan(lambda: Observation("x")).next_observation("t1")).name == "x"
    plan = CallablePlan(lambda tel: Observation(f"x-{tel}"))
    assert (await plan.next_observation("t2")).name == "x-t2"


async def test_plan_from_config(tmp_path):
    (tmp_path / "targets.json").write_text(json.dumps([{"name": "a"}]))
    plan = plan_from_config({"type": "static", "file": "targets.json"}, ["t1"], base=tmp_path)
    assert await _drain(plan, "t1") == ["a"]
    plan = plan_from_config({"type": "calibration", "biases": 1}, ["t1"])
    assert await _drain(plan, "t1") == ["bias"]
    with pytest.raises(ValueError, match="plan type"):
        plan_from_config({"type": "lookahead"}, ["t1"])
    # --plan: a bare list is a static plan
    assert await _drain(load_plan(tmp_path / "targets.json", ["t1"]), "t1") == ["a"]


async def test_example_plan_file():
    plan = load_plan(SITES / "minerva" / "targets.example.json", ["t1", "t2"])
    assert await _drain(plan, "t1") == ["HD 10700"]
    assert await _drain(plan, "t2") == ["HD 10700", "HD 185144"]


def test_device_address():
    assert device_address("127.0.0.1:11121") == ("127.0.0.1:11121", 0)
    assert device_address("127.0.0.1:11121/2") == ("127.0.0.1:11121", 2)


async def test_minerva_aqawan1_config_builds_both_services():
    """The example enclosure config: both services build from it (no
    connections are made), and they agree on the telescopes."""
    config = load_config(SITES / "minerva" / "aqawan1.json")
    tl = Timeline()
    sup = supervisor_service.build(config, tl)
    assert [g.owner.name for g in sup._tels] == ["t1.telescope", "t2.telescope"]
    assert sup.config.park_before_close
    seq, remote = build(config, tl)
    try:
        assert list(seq.units) == ["t1", "t2"]
        assert seq.name == "aqawan1-sequencer"
        assert remote.url == "http://127.0.0.1:8100"
        assert await _drain(seq.plan, "t2") == ["bias", "dark60"]
    finally:
        await remote.aclose()
    addresses = [config["devices"].values()] + [e.values() for e in config["telescopes"].values()]
    assert all(a.startswith("127.0.0.1:") for group in addresses for a in group)


def test_yaml_needs_pyyaml_or_says_so(tmp_path):
    path = tmp_path / "enclosure.yaml"
    path.write_text("name: x\ntelescopes: {}\n")
    try:
        import yaml  # noqa: F401
    except ImportError:
        with pytest.raises(SystemExit, match="PyYAML"):
            load_config(path)
    else:
        assert load_config(path) == {"name": "x", "telescopes": {}}
