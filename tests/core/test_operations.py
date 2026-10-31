"""Night operations on fakes: idle telescopes release the enclosure, dark
frames are taken without a lease, filters and M3 ports are selected, and an
interrupted observation can be resumed rather than re-taken (design.md 3.4)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from robofast_core.config import load_config
from robofast_core.plan import CalibrationPlan, Idle, Observation, StaticPlan
from robofast_core.sequencer_service import build, build_units, load_plan
from robofast_core.testing.checks import wait_until
from robofast_core.timeline import Timeline

BOUND = 3.0
SITES = Path(__file__).resolve().parents[2] / "sites" / "minerva"


class SwitchPlan:
    """Each telescope's answer is set by the test: ``"work"`` (a light
    observation), ``"dark"``, ``"idle"`` or ``"done"``. Has ``peek``."""

    def __init__(self, modes: dict[str, str], idle_seconds: float = 0.05) -> None:
        self.modes = dict(modes)
        self.idle_seconds = idle_seconds
        self.history: list[tuple[str, str, str, int]] = []
        self.asked: list[tuple[float, str, str]] = []
        self.n = 0

    def _answer(self, telescope: str) -> Observation | Idle | None:
        mode = self.modes[telescope]
        if mode == "work":
            return Observation(f"{telescope}-sci{self.n}", exptime=0.2, count=2)
        if mode == "dark":
            return Observation(f"{telescope}-dark{self.n}", exptime=0.1, count=1, light=False)
        if mode == "idle":
            return Idle(self.idle_seconds, "nothing up")
        return None

    async def peek(self, telescope: str) -> Observation | Idle | None:
        self.asked.append((time.monotonic(), telescope, "peek"))
        return self._answer(telescope)

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        self.asked.append((time.monotonic(), telescope, "next"))
        self.n += 1
        return self._answer(telescope)

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        self.history.append((telescope, obs.name, outcome, frames))


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


# -- idle telescopes release the enclosure ------------------------------------


async def test_idle_telescopes_release_the_enclosure_then_reopen_for_work(make_rig):
    plan = SwitchPlan({"t1": "work", "t2": "work"})
    rig = make_rig(
        telescopes=2, plan=plan, seq=dict(idle_release_after=0.6), safety=dict(reopen_hold=0.1)
    )
    await rig.start()
    await wait_until(rig.exposing, 5, what="both observing")
    plan.modes = {"t1": "idle", "t2": "idle"}
    t_idle = time.monotonic()
    await wait_until(lambda: _events(rig, "idle_release"), 5, what="idle release")
    t_release = _events(rig, "idle_release")[0].t
    # Both had to be idle for idle_release_after (each from its own first Idle).
    assert t_release - t_idle >= 0.6
    await wait_until(
        lambda: rig.dome_closed() and rig.parked() and _events(rig, "lease_released"),
        BOUND,
        what="parked, lease released, closed",
    )
    finished = _events(rig, "observe_finished")
    assert finished[-1].detail["outcome"] == "idle"
    # Closed and idle: the plan is polled (peek), no lease is requested.
    n_peeks = sum(1 for _, _, how in plan.asked if how == "peek")
    await asyncio.sleep(0.5)
    assert sum(1 for _, _, how in plan.asked if how == "peek") > n_peeks
    assert _events(rig, "closed_idle")
    assert len(rig.timeline.events("lease_granted")) == 1 and rig.dome_closed()
    # Something observable again: a new lease, and t1 observes.
    plan.modes["t1"] = "work"
    await wait_until(lambda: len(_events(rig, "observe_start")) == 2, 5, what="new generation")
    await wait_until(lambda: rig.dome_open() and rig.exposing(0), 5, what="t1 observing")
    assert rig.supervisor.lease.live


async def test_one_busy_telescope_keeps_the_enclosure_open(make_rig):
    """A short idle stops tracking and stays put; while another telescope
    works, the enclosure stays open however long the idle lasts."""
    plan = SwitchPlan({"t1": "work", "t2": "work"})
    rig = make_rig(telescopes=2, plan=plan, seq=dict(idle_release_after=0.4))
    await rig.start()
    await wait_until(rig.exposing, 5, what="both observing")
    plan.modes["t2"] = "idle"
    await wait_until(lambda: _events(rig, "telescope_idle"), 3, what="t2 idle")
    await asyncio.sleep(1.2)
    assert not _events(rig, "idle_release")
    assert rig.dome_open() and len(_events(rig, "observe_start")) == 1
    assert not rig.tels[1]._tracking and not rig.parked(1)  # tracking off, not parked
    assert rig.exposing(0) or rig.cams[0].exposures_started > 2


async def test_no_release_when_disabled(make_rig):
    plan = SwitchPlan({"t1": "work"})
    rig = make_rig(plan=plan, seq=dict(idle_release_after=None))
    await rig.start()
    await wait_until(rig.exposing, 5)
    plan.modes["t1"] = "idle"
    await asyncio.sleep(1.0)
    assert not _events(rig, "idle_release") and rig.dome_open()


async def test_a_preemption_wakes_the_closed_phase(make_rig):
    """Closed after an idle release, waiting out a long Idle: a ToO's wake
    (a refused preemption) makes the sequencer ask the plan at once."""
    plan = SwitchPlan({"t1": "work"}, idle_seconds=0.05)
    rig = make_rig(plan=plan, seq=dict(idle_release_after=0.3), safety=dict(reopen_hold=0.1))
    await rig.start()
    await wait_until(rig.exposing, 5)
    plan.modes["t1"] = "idle"
    await wait_until(lambda: _events(rig, "lease_released"), 5, what="released")
    plan.idle_seconds = 60.0
    await wait_until(
        lambda: any(e.detail["seconds"] == 60.0 for e in _events(rig, "closed_idle")), 3
    )
    plan.modes["t1"] = "work"
    t0 = time.monotonic()
    assert rig.sequencer.preempt("t1", "next", "ToO") is False  # refused, but wakes it
    await wait_until(lambda: len(_events(rig, "observe_start")) == 2, 3, what="reopen")
    assert _events(rig, "observe_start")[1].t - t0 < 1.0


# -- calibrations: dark frames without a lease -----------------------------------


async def test_calibrations_never_open_the_enclosure(make_rig):
    plan = CalibrationPlan(["t1", "t2"], biases=2, darks=[[0.1, 2]])
    rig = make_rig(telescopes=2, plan=plan)
    await rig.start()
    seq_task = rig.tasks[-1]
    await asyncio.wait_for(asyncio.shield(seq_task), 10)
    assert [c.exposures_started for c in rig.cams] == [4, 4]
    assert not rig.timeline.events("lease_granted")
    assert rig.dome.opens == 0 and rig.dome_closed() and rig.parked()
    assert _events(rig, "closed_start") and not _events(rig, "observe_start")
    assert _events(rig, "plan_exhausted")
    assert all(h[2] == "complete" for h in plan.history) and len(plan.history) == 4


async def test_darks_first_then_a_lease_for_the_light_frames(make_rig):
    plan = StaticPlan(
        [
            {"name": "dark", "light": False, "exptime": 0.1, "count": 2},
            {"name": "science", "exptime": 0.1, "count": 2},
        ],
        ["t1"],
    )
    rig = make_rig(plan=plan, safety=dict(reopen_hold=0.1))
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    assert plan.history == [("t1", "dark", "complete", 2), ("t1", "science", "complete", 2)]
    granted = rig.timeline.events("lease_granted")[0].t
    darks = [
        e for e in _events(rig, "exposure_complete") if e.detail["gen"] == 1
    ]  # the closed generation
    assert len(darks) == 2 and all(e.t < granted for e in darks)
    assert _events(rig, "closed_start")[0].detail["gen"] == 1
    assert _events(rig, "observe_start")[0].detail["gen"] == 2


async def test_a_plan_without_peek_gets_its_answer_back(make_rig):
    """A plan without ``peek``: the closed phase takes the answer and gives
    it back (``interrupted``, no frames), so the generation asks afresh."""
    names = iter(["first", "second", "third"])
    seen = []

    def fn():
        name = next(names, None)
        seen.append(name)
        return None if name is None else Observation(name, exptime=0.1, count=1)

    rig = make_rig(plan=fn, safety=dict(reopen_hold=0.1))
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    history = rig.sequencer.plan.history
    assert history[0] == ("t1", "first", "interrupted", 0)
    assert ("t1", "second", "complete", 1) in history
    assert _events(rig, "observation_returned")[0].detail["target"] == "first"


# -- filters and M3 ports ----------------------------------------------------------


async def test_filter_and_port_are_selected_before_the_exposures(make_rig):
    plan = StaticPlan(
        [{"name": "x", "filter": "R", "port": "2", "exptime": 0.1, "count": 2}], ["t1"]
    )
    rig = make_rig(plan=plan, filterwheels=True, port_action="SelectPort")
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    assert plan.history == [("t1", "x", "complete", 2)]
    assert rig.wheels[0].Position == rig.wheels[0].Names.index("R") and rig.wheels[0].moves == 1
    assert rig.tels[0].port == "2"
    first_exposure = _events(rig, "exposure_started")[0].t
    assert _events(rig, "filter_selected")[0].t < first_exposure
    assert _events(rig, "port_selected")[0].t < first_exposure


@pytest.mark.parametrize(
    ("target", "kw", "error"),
    [
        ({"port": "2"}, {}, "cannot select M3 port"),
        ({"filter": "R"}, {}, "has no filter wheel"),
        ({"filter": "Halpha"}, {"filterwheels": True}, "no filter 'Halpha'"),
    ],
)
async def test_an_unsupported_filter_or_port_fails_the_observation(make_rig, target, kw, error):
    plan = StaticPlan([{"name": "x", "exptime": 0.1, "count": 1, **target}], ["t1"])
    rig = make_rig(plan=plan, seq=dict(max_consecutive_errors=1), **kw)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    errors = _events(rig, "telescope_error")
    assert errors and error in errors[0].detail["error"]
    assert rig.cams[0].exposures_started == 0  # never observed through the wrong one
    assert plan.history[0][2] == "failed"


async def test_port_action_refused_by_the_mount_is_an_error(make_rig):
    plan = StaticPlan([{"name": "x", "port": "9", "exptime": 0.1, "count": 1}], ["t1"])
    rig = make_rig(plan=plan, port_action="SelectPort", seq=dict(max_consecutive_errors=1))
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 10)
    assert "no port '9'" in _events(rig, "telescope_error")[0].detail["error"]
    assert rig.cams[0].exposures_started == 0


# -- re-taking an interrupted observation ---------------------------------------


@pytest.mark.parametrize("allow_partial", [False, True])
async def test_interrupted_observation_retakes_whole_or_the_rest(make_rig, allow_partial):
    plan = StaticPlan(
        [{"name": "x", "exptime": 0.3, "count": 6, "allow_partial": allow_partial}], ["t1"]
    )
    rig = make_rig(plan=plan, safety=dict(reopen_hold=0.1))
    await rig.start()
    await wait_until(
        lambda: sum(1 for f in rig.sequencer.frames if f["gen"] == 1) >= 2, 10, what="2 frames"
    )
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.timeline.events("lease_released"), BOUND)
    rig.weather.set_safe(True)
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 15)
    (_, _, first, taken), (_, _, second, retaken) = plan.history
    assert first == "interrupted" and 2 <= taken < 6
    assert second == "complete"
    assert retaken == (6 - taken if allow_partial else 6)


# -- the service's config -----------------------------------------------------------


async def test_example_calibration_plan_and_telescope_options():
    sites = SITES
    plan = load_plan(sites / "calibration.example.json", ["t1"])
    first = await plan.peek("t1")
    assert first.name == "bias" and not first.light
    config = load_config(sites / "aqawan1.json")
    config["telescopes"]["t2"].update(filterwheel="127.0.0.1:11122/1", port_action="SelectPort")
    seq, remote = build(config, Timeline())
    try:
        t1, t2 = seq.units["t1"], seq.units["t2"]
        assert t1.filterwheel is None and t1.port_action is None
        assert t2.filterwheel.name == "t2.filterwheel" and t2.port_action == "SelectPort"
        assert seq.config.idle_release_after == 900
    finally:
        await remote.aclose()
    for bad in ({"port_action": ""}, {"guider": "127.0.0.1:1"}):
        entry = {"telescope": "127.0.0.1:1", "camera": "127.0.0.1:1", **bad}
        with pytest.raises(ValueError):
            build_units({"telescopes": {"t1": entry}}, Timeline(), 1.0)
