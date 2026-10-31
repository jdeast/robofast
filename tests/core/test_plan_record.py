"""The nightly plan record (robofast_core/plan_record.py): one JSON-lines file
per night, readable, and replayable to the same decisions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from robofast_core.dispatch import dispatch_from_config
from robofast_core.plan import Idle, Observation
from robofast_core.plan_record import PlanRecord, main, night_of, read_record, replay
from tests.core.test_dispatch import MINERVA, NIGHT, Clock, group, make, too

LON = MINERVA.longitude


def test_night_is_the_local_date_of_the_evening():
    # Mean solar time at Mt. Hopkins is UTC - 7.39 h.
    assert night_of(datetime(2026, 11, 15, 7, tzinfo=UTC), longitude=LON) == date(2026, 11, 14)
    assert night_of(datetime(2026, 11, 15, 19, tzinfo=UTC), longitude=LON) == date(2026, 11, 14)
    assert night_of(datetime(2026, 11, 15, 20, tzinfo=UTC), longitude=LON) == date(2026, 11, 15)
    # A time zone, and another rollover hour.
    tz = "America/Phoenix"  # UTC-7, no DST
    noon = datetime(2026, 11, 15, 19, tzinfo=UTC)
    assert night_of(noon - timedelta(seconds=1), timezone=tz) == date(2026, 11, 14)
    assert night_of(noon, timezone=tz) == date(2026, 11, 15)
    assert night_of(noon, timezone=tz, rollover_hour=15) == date(2026, 11, 14)
    with pytest.raises(ValueError):
        night_of(datetime(2026, 11, 15))


def test_record_validates_its_settings(tmp_path):
    with pytest.raises(ValueError):
        PlanRecord(tmp_path, night_rollover_hour=24)
    with pytest.raises(Exception):  # noqa: B017 (ZoneInfoNotFoundError)
        PlanRecord(tmp_path, timezone="Mars/Olympus_Mons")


def _scheduler(tmp_path, clock):
    groups = [
        group("a", ha=-1.0, priority=3, count=2),
        group("b", ha=-2.0, priority=2),
        group("low", ha=7.0, priority=9),  # below the horizon: rejected
        group("t2only", ha=-0.5, priority=1, telescopes=["t2"]),
        group("c", ha=-1.5, priority=1),
        group("e", ha=-2.5, priority=0.5),
    ]
    record = PlanRecord(tmp_path / "plans", longitude=LON)
    return make(groups, clock=clock, record=record)


async def test_decisions_are_written_with_their_reasons(tmp_path):
    clock = Clock()
    d = _scheduler(tmp_path, clock)
    obs = await d.next_observation("t1")
    d.report("t1", obs, "complete", obs.count)
    path = tmp_path / "plans" / "plan-2026-11-14.jsonl"
    entries = read_record(path)
    assert [e["kind"] for e in entries] == ["snapshot", "decision", "outcome"]
    snap, dec, out = entries
    assert {g["id"] for g in snap["groups"]} == {"a", "b", "low", "t2only", "c", "e"}
    assert dec["telescope"] == "t1" and dec["request"] == "a-r" and dec["config"] == "a-c"
    assert dec["score"] == pytest.approx(sum(dec["terms"].values()))
    assert dec["runners_up"][0]["request"] == "b-r"
    rejected = {r["request"]: r["reasons"] for r in dec["rejected"]}
    assert rejected["t2only-r"] == ["not for t1"]
    assert any("altitude" in why for why in rejected["low-r"])
    assert list(rejected) == ["low-r", "t2only-r"]  # by priority
    assert out["outcome"] == "complete" and out["frames"] == 2
    assert out["t"] == NIGHT.isoformat()


async def test_one_file_per_night_each_starting_with_a_snapshot(tmp_path):
    clock = Clock(datetime(2026, 11, 15, 12, tzinfo=UTC))  # local early morning
    d = _scheduler(tmp_path, clock)
    await d.next_observation("t2")
    clock.t = datetime(2026, 11, 16, 6, tzinfo=UTC)  # the next night
    await d.next_observation("t1")
    files = sorted(p.name for p in (tmp_path / "plans").iterdir())
    assert files == ["plan-2026-11-14.jsonl", "plan-2026-11-15.jsonl"]
    for name in files:
        assert read_record(tmp_path / "plans" / name)[0]["kind"] == "snapshot"


async def test_a_write_error_does_not_stop_the_scheduler(tmp_path, caplog):
    blocker = tmp_path / "plans"
    blocker.write_text("a file, not a directory")
    d = make([group("a")], record=PlanRecord(blocker))
    assert isinstance(await d.next_observation("t1"), Observation)
    assert "could not write" in caplog.text


async def _a_night(tmp_path, clock):
    """A scripted night on two telescopes: visits, an interruption, a
    telescope with nothing to do, a ToO that preempts, a cancellation."""
    d = _scheduler(tmp_path, clock)
    accepted = []
    d.set_preempt_callback(lambda tel, policy, reason: accepted.append(tel) or True)

    async def visit(tel, outcome="complete", frames=None):
        obs = await d.next_observation(tel)
        assert isinstance(obs, Observation), obs
        d.report(tel, obs, outcome, obs.count if frames is None else frames)
        return obs

    await visit("t1", "interrupted", 1)
    clock.advance(300)
    await visit("t2")
    await visit("t1")
    clock.advance(600)
    assert isinstance(await d.next_observation("t2"), Observation)  # t2 busy
    await d.submit_too(group("burst", ha=-1.5, too=too("now"), telescopes=["t2"]))
    assert accepted == ["t2"]
    d.report("t2", d._current["t2"].obs, "interrupted", 0)
    clock.advance(30)
    assert (await visit("t2")).name.startswith("burst")
    d.cancel("e")
    clock.advance(60)
    for _ in range(6):
        answer = await d.next_observation("t1")
        if not isinstance(answer, Observation):
            break
        d.report("t1", answer, "complete", answer.count)
    assert isinstance(answer, Idle)
    return d


async def test_replay_reproduces_the_nights_decisions(tmp_path):
    clock = Clock()
    await _a_night(tmp_path, clock)
    path = tmp_path / "plans" / "plan-2026-11-14.jsonl"
    entries = read_record(path)
    kinds = [e["kind"] for e in entries]
    for kind in ("snapshot", "decision", "outcome", "too", "preempt", "cancel", "idle"):
        assert kind in kinds, kind
    result = await replay(entries)
    assert result.mismatches == [] and result.ok
    assert result.compared == kinds.count("decision") + kinds.count("continue") + kinds.count(
        "idle"
    ) + kinds.count("preempt")
    # A doctored record is caught.
    i = next(i for i, e in enumerate(entries) if e["kind"] == "decision" and e["telescope"] == "t1")
    doctored = [dict(e) for e in entries]
    doctored[i]["request"] = "b-r"
    bad = await replay(doctored)
    assert not bad.ok and bad.mismatches[0][2].startswith("decision b-r")


async def test_replay_from_a_mid_night_restart(tmp_path):
    """A second process appends to the same file with its own snapshot; the
    replay starts over from it."""
    clock = Clock()
    d = await _a_night(tmp_path, clock)
    clock.advance(60)
    again = make(
        [g.to_dict() for g in d.groups.values() if g.too is None],
        clock=clock,
        record=PlanRecord(tmp_path / "plans", longitude=LON),
    )
    again.restore_state(d._state())
    await again.next_observation("t2")
    entries = read_record(tmp_path / "plans" / "plan-2026-11-14.jsonl")
    assert [e["kind"] for e in entries].count("snapshot") == 2
    assert (await replay(entries)).ok


def test_cli_shows_and_replays(tmp_path, capsys):
    asyncio.run(_a_night(tmp_path, Clock()))
    path = tmp_path / "plans" / "plan-2026-11-14.jsonl"
    main([str(path)])
    shown = capsys.readouterr().out
    assert "decision" in shown and "t2only-r" in shown and "rejected" in shown
    with pytest.raises(SystemExit) as exc:
        main([str(path), "--replay"])
    assert exc.value.code == 0
    assert "0 mismatches" in capsys.readouterr().out


def test_a_torn_last_line_is_skipped(tmp_path, caplog):
    path = tmp_path / "plan.jsonl"
    path.write_text(json.dumps({"t": "x", "kind": "idle"}) + "\n" + '{"t": "x", "ki')
    assert read_record(path) == [{"t": "x", "kind": "idle"}]
    assert "not JSON" in caplog.text


def test_record_in_the_plan_entry(tmp_path):
    entry = {
        "site": {"latitude": 31.68, "longitude": -110.88, "elevation": 2316},
        "requests": [],
        "record": {"dir": "plans", "night_rollover_hour": 14, "timezone": "America/Phoenix"},
    }
    d = dispatch_from_config(entry, ["t1"], base=tmp_path)
    assert d.record.directory == tmp_path / "plans" and d.record.rollover_hour == 14
    d = dispatch_from_config({**entry, "record": "plans"}, ["t1"], base=tmp_path)
    assert d.record.longitude == -110.88 and d.record.timezone is None
    with pytest.raises(ValueError, match="record"):
        dispatch_from_config({**entry, "record": {"night_rollover_hour": 3}}, ["t1"])
    with pytest.raises(ValueError, match="record"):
        dispatch_from_config({**entry, "record": {"dir": "x", "bogus": 1}}, ["t1"])
