"""The dispatch scheduler's operational parts, with a fixed clock: ``peek``,
filters and M3 ports as hard constraints, and allow_partial progress."""

from __future__ import annotations

from robofast_core.dispatch import DispatchScheduler, TelescopeLimits
from robofast_core.plan import Idle, Observation
from robofast_core.request_model import Status
from tests.core.test_dispatch import Clock, group, make


def _reasons(d: DispatchScheduler, rid: str, telescope: str) -> list[str]:
    return next(c for c in d.evaluate(telescope) if c.request.id == rid).reasons


async def test_peek_changes_nothing():
    d = make([group("a", count=3)])
    peeked = await d.peek("t1")
    assert isinstance(peeked, Observation) and peeked.count == 3
    assert not d.decisions and not d._current
    obs = await d.next_observation("t1")
    assert obs.name == peeked.name and len(d.decisions) == 1
    # Mid-visit, peek shows the visit's next observation.
    assert (await d.peek("t1")).name == obs.name


async def test_peek_idle_and_night_over():
    d = make([group("low", ha=7.0)])  # below the horizon
    assert isinstance(await d.peek("t1"), Idle)
    assert not d._idle  # peek does not mark the telescope idle


async def test_ports_and_filters_are_hard_constraints():
    limits = {
        "t1": TelescopeLimits(ports=["1", "2"], filters=["V", "R"]),
        "t2": TelescopeLimits(),
    }
    d = make(
        [
            group("spec", ics=[{"exptime": 60, "port": "1"}]),
            group("img", ics=[{"exptime": 60, "filter": "R"}]),
            group("ha", ics=[{"exptime": 60, "filter": "Halpha"}]),
        ],
        limits=limits,
    )
    assert _reasons(d, "spec-r", "t1") == []
    assert _reasons(d, "spec-r", "t2") == ["t2 cannot select port '1'"]
    assert _reasons(d, "img-r", "t1") == []
    assert _reasons(d, "img-r", "t2") == []  # t2's filters are not listed: the sequencer checks
    assert _reasons(d, "ha-r", "t1") == ["t1 has no filter 'Halpha'"]
    obs = await d.next_observation("t1")
    assert (obs.port, obs.filter) in (("1", None), (None, "R"))


def _partial(gid: str, ics: list, allow_partial: bool = True) -> dict:
    g = group(gid, ics=ics)
    g["requests"][0]["configurations"][0]["allow_partial"] = allow_partial
    return g


async def test_interrupted_without_allow_partial_is_retaken_whole():
    d = make([_partial("a", [{"exptime": 60, "count": 5}], allow_partial=False)])
    obs = await d.next_observation("t1")
    d.report("t1", obs, "interrupted", 2)
    again = await d.next_observation("t1")
    assert again.count == 5 and not again.allow_partial


async def test_allow_partial_takes_only_the_remaining_frames(tmp_path):
    state = tmp_path / "state.json"
    clock = Clock()
    groups = [_partial("a", [{"exptime": 60, "count": 3}, {"exptime": 30, "count": 4}])]
    d = make(groups, clock=clock, state=state)
    obs = await d.next_observation("t1")
    assert obs.count == 3 and obs.allow_partial
    d.report("t1", obs, "complete", 3)
    obs = await d.next_observation("t1")  # second instrument config
    assert obs.count == 4
    d.report("t1", obs, "interrupted", 1)
    assert d.status("a-c") is Status.ATTEMPTED
    # A restart keeps the progress: the next visit starts with 3 frames of #1.
    d2 = make(groups, clock=clock, state=state)
    obs = await d2.next_observation("t1")
    assert obs.name.endswith("[a-c#1+1]") and obs.count == 3 and obs.exptime == 30
    d2.report("t1", obs, "failed", 2)  # failed part-way: those 2 count too
    obs = await d2.next_observation("t1")
    assert obs.count == 1
    d2.report("t1", obs, "complete", 1)
    assert d2.status("a-c") is Status.COMPLETED
    assert not d2._progress and "progress" in d2._state() and not d2._state()["progress"]


async def test_partial_progress_shortens_the_planned_visit():
    d = make([_partial("a", [{"exptime": 600, "count": 10}])])
    c = d.requests["a-r"].configurations[0]
    full = d._duration(c)
    d._progress["a-c"] = (0, 8)
    assert d._duration(c) == full - 8 * (600 + d.config.frame_overhead)
