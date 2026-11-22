"""Config-defined sequences (robofast/sequences.py, docs/sequences.md):
the checks when a config is loaded, and full TRES calibration sequences on
the simulators -- the TRES front end's server (calibration stage), the lamp
server on its simulated Pulizzi, and the rig's camera -- including the
cleanup after a failure, a lease revoked and a cancellation."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from robofast.alpaca import alpaca_owner
from robofast.config import load_config
from robofast.devices.pulizzi_server import PulizziServer
from robofast.devices.tres_frontend_server import TRESFrontEndConfig, TRESFrontEndServer
from robofast.lamps import LampSetup
from robofast.plan import CalibrationPlan, Observation, StaticPlan, plan_from_config
from robofast.request_model import NO_LAMPS
from robofast.sequencer_service import check_plan_sequences
from robofast.sequences import SEQUENCE_STEPS, StepType, sequences_from_config
from robofast.testing.checks import wait_until
from robofast.testing.processes import ROOT
from tests.core.test_pulizzi import LAMPS, lamp_config, switching
from tests.core.test_request_api import make_api  # noqa: F401 (fixture)

THAR = {
    "instrument": "camera_spectrograph",
    "steps": [
        {"calstage": "in"},
        {"lamp": "ThAr1", "on": True, "warmup": 0.2},
        {"expose": {"type": "ARC", "exptime": 0.2, "n": 3}},
        {"lamp": "ThAr1", "on": False},
        {"calstage": "out"},
    ],
    "cleanup": [{"calstage": "out"}],
}


def config(sequences: dict, **tel) -> dict:
    entry = {
        "telescope": "127.0.0.1:1/0",
        "camera": "127.0.0.1:1/0",
        "calstage": "127.0.0.1:1/0",
        "lamps": {
            "switch": "127.0.0.1:1/0",
            "frame_overhead": 1.0,
            "lamps": {"ThAr1": {"warmup": 0.2, "max_on": 600}, "Incand": {"warmup": 0}},
        },
        **tel,
    }
    return {"name": "e", "telescopes": {"t1": entry}, "sequences": sequences}


# -- the checks when the config is loaded --------------------------------------------


def test_a_sequence_loads_with_what_it_does():
    (seq,) = sequences_from_config(config({"thar": THAR})).values()
    assert [s.type for s in seq.steps] == ["calstage", "lamp", "expose", "lamp", "calstage"]
    assert not seq.light and seq.lamps == ("ThAr1",) and seq.runs_on("t1")
    assert seq.lamp_time["t1"]["ThAr1"] == pytest.approx(0.2 + 3 * 1.2)
    # A bare list is its steps; an EXPOSE step makes it light.
    (light,) = sequences_from_config(
        config({"sky": [{"calstage": "sky"}, {"expose": {"type": "EXPOSE", "exptime": 5}}]})
    ).values()
    assert light.light and light.cleanup == ()
    assert sequences_from_config({"telescopes": {}}) == {}


def _with(change):
    d = copy.deepcopy(THAR)
    change(d)
    return d


@pytest.mark.parametrize(
    ("seq", "tel", "match"),
    [
        (
            _with(lambda d: d["steps"].append({"lmap": 1})),
            {},
            r"steps\[5\]: unknown step \['lmap'\]",
        ),
        (
            _with(lambda d: d["steps"].insert(0, {"wait": 1, "calstage": "x"})),
            {},
            r"steps\[0\]: one step type per step",
        ),
        (
            _with(lambda d: d["steps"][1].update(on="yes")),
            {},
            r"steps\[1\]: lamp ThAr1: say \"on\"",
        ),
        (
            _with(lambda d: d["steps"][1].update(lamp="ThAr9")),
            {},
            r"steps\[1\] \(on t1\): no lamp 'ThAr9' on t1",
        ),
        (
            _with(lambda d: d["steps"].pop(3)),
            {},
            r"tres\.steps: leaves lamp\(s\) \['ThAr1'\] on",
        ),
        (
            _with(lambda d: d["steps"].pop(1)),
            {},
            r"steps\[1\] \(on t1\): a ARC frame needs a lamp on",
        ),
        (
            _with(lambda d: d["steps"][2]["expose"].update(n=600)),
            {},
            r"keeps lamp ThAr1 on for about 720 s, more than its max_on 600 s",
        ),
        (
            _with(lambda d: d.update(instrument="imager"))
            | {"steps": [{"expose": {"type": "SPECTRUM"}}]},
            {},
            r"steps\[0\]: expose\.type: one of",
        ),
        (
            _with(lambda d: d["steps"].insert(0, {"expose": {"type": "EXPOSE", "exptime": 1}}))
            | {"instrument": "camera_spectrograph"},
            {"calstage": "127.0.0.1:1/0"},
            None,  # camera_spectrograph takes EXPOSE: fine
        ),
        (THAR, {"calstage": None}, r"steps\[0\] \(on t1\): telescope 't1' has no calibration"),
        (
            _with(lambda d: d["steps"].insert(0, {"focus": 100})),
            {"focuser": "127.0.0.1:1/0"},
            r"has no autofocus entry",
        ),
        (
            _with(lambda d: d["cleanup"].append({"lamp": "ThAr1", "on": True})),
            {},
            r"cleanup\[1\]: not a cleanup step",
        ),
        (
            _with(lambda d: d["cleanup"].append({"expose": {"type": "DARK", "exptime": 1}})),
            {},
            r"cleanup\[1\]: not a cleanup step",
        ),
        (_with(lambda d: d.update(telescopes=["t9"])), {}, r"tres\.telescopes: unknown \['t9'\]"),
        (_with(lambda d: d.update(instrument="nope")), {}, r"tres\.instrument"),
        (_with(lambda d: d.update(colour="red")), {}, r"tres: unknown keys \['colour'\]"),
        (
            _with(lambda d: d["steps"].append({"spectrograph": [{"kind": "dark", "exptime": 1}]})),
            {},
            r"the enclosure has no spectrograph",
        ),
    ],
)
def test_bad_sequences_are_refused_with_the_steps_path(seq, tel, match):
    cfg = config({"tres": seq}, **tel)
    cfg["telescopes"]["t1"] = {k: v for k, v in cfg["telescopes"]["t1"].items() if v is not None}
    if match is None:
        sequences_from_config(cfg, where="e.json")
        return
    with pytest.raises(ValueError, match=match):
        sequences_from_config(cfg, where="e.json")


def test_the_shipped_tres_sequences_and_calibration_plan():
    """The observers' calibrations (flwo60.json): the nightly biases, ThAr
    and flats on each fiber with 20 s between groups, and the per-target
    ThAr, all from the generic templates."""
    cfg = load_config(ROOT / "sites/flwo60/flwo60.json")
    seqs = sequences_from_config(cfg)
    nightly = ["tres_bias", "tres_thar_science", "tres_thar_sky", "tres_flat_science"]
    nightly.append("tres_flat_sky")
    assert set(seqs) == {"tres_thar", "tres_flat", *nightly}
    for name, seq in seqs.items():
        assert not seq.light and seq.instrument == "camera_spectrograph"
        if name == "tres_bias":
            assert [s.type for s in seq.steps] == ["expose", "wait"] and not seq.cleanup
            assert seq.steps[0].params["n"] == 5 and seq.steps[1].params == 20
            continue
        fiber = "sky" if name.endswith("_sky") else "science"
        assert seq.steps[0].params == fiber
        assert [s.params for s in seq.cleanup] == ["out"]
        frames = next(s.params for s in seq.steps if s.type == "expose")
        assert (frames["exptime"], frames["n"]) == {
            "tres_thar": (5, 1),
            "tres_thar_science": (10, 5),
            "tres_thar_sky": (10, 5),
        }.get(name, (0.05, 11))
        # 20 s between the nightly groups; nothing after the last.
        waits = [s.params for s in seq.steps if s.type == "wait"]
        assert waits == ([20] if name in nightly[:-1] else [])
    stage = json.loads((ROOT / "sites/flwo60/tres_frontend.json").read_text())["positions"]
    assert stage == {
        "out": 24.99,
        "sky": 8.0,
        "science": 3.05,
        "block_sky": 15.65,
        "block_science": 0.0,
    }
    entry = json.loads((ROOT / "sites/flwo60/calibration.example.json").read_text())
    for plan_entry in (entry, cfg["plan"]):
        plan = plan_from_config(plan_entry, ["tillinghast"])
        check_plan_sequences(plan, seqs)
        assert [o.name for o in plan.queues["tillinghast"]] == nightly
    # MINERVA defines none: its calibrations are the calibration plan's, unchanged.
    assert sequences_from_config(load_config(ROOT / "sites/minerva/aqawan1.json")) == {}


def test_plans_naming_sequences_are_checked():
    seqs = sequences_from_config(
        config({"thar": THAR, "sky": [{"expose": {"type": "EXPOSE", "exptime": 1}}]})
    )
    with pytest.raises(ValueError, match="no sequence 'nope'"):
        check_plan_sequences(CalibrationPlan(["t1"], sequences=["nope"]), seqs)
    with pytest.raises(ValueError, match="takes sky light; a calibration plan never opens"):
        check_plan_sequences(CalibrationPlan(["t1"], sequences=["sky"]), seqs)
    check_plan_sequences(
        StaticPlan([{"name": "s", "kind": "sequence", "sequence": "sky"}], ["t1"]), seqs
    )
    with pytest.raises(ValueError, match="unknown telescope 't2'"):
        CalibrationPlan(["t1"], sequences=[{"sequence": "thar", "telescopes": ["t2"]}])
    with pytest.raises(ValueError, match="a sequence names its definition"):
        Observation("x", kind="sequence")


def test_a_plug_in_adds_a_step_type():
    class Beep(StepType):
        name = "beep"
        cleanup = True

        def parse(self, value, options, where):
            if not isinstance(value, int):
                raise ValueError(f"{where}: beep: a count")
            return value

        async def run(self, params, r):
            r.event("beeped", n=params)

    SEQUENCE_STEPS.register("beep", Beep())
    try:
        (seq,) = sequences_from_config(
            config(
                {
                    "b": [{"beep": 2}],
                }
            )
        ).values()
        assert seq.steps[0].type == "beep"
        with pytest.raises(ValueError, match=r"steps\[0\]: beep: a count"):
            sequences_from_config(config({"b": [{"beep": "x"}]}))
    finally:
        SEQUENCE_STEPS.unregister("beep")


# -- on the simulators ---------------------------------------------------------------


@pytest.fixture
def servers():
    made: list = []

    def start():
        lamps = PulizziServer(lamp_config()).start()
        stage = TRESFrontEndServer(
            TRESFrontEndConfig.from_dict(
                {
                    "name": "tres",
                    "listen": "127.0.0.1:0",
                    "port": {"type": "sim", "sim": {"velocity": 100.0}},
                    "positions": {"out": 0.0, "sky": 10.0, "science": 20.0, "in": 25.0},
                    "poll_interval": 0.02,
                    "status_max_age": 0.02,
                    "request_timeout": 3.0,
                    "start_grace": 0.2,
                }
            )
        ).start()
        made.extend([lamps, stage])
        return lamps, stage

    yield start
    for s in made:
        s.stop()


def tres_rig(make_rig, servers, plan, sequences: dict, **kw):
    lamps, stage = servers()
    rig = make_rig(plan=plan, **kw)
    unit = rig.sequencer.units["t1"]
    unit.lamps = alpaca_owner("t1.lamps", "switch", lamps.address, rig.timeline, default_timeout=5)
    unit.calstage = alpaca_owner(
        "t1.calstage", "filterwheel", stage.address, rig.timeline, default_timeout=5
    )
    rig.owners["t1.lamps"], rig.owners["t1.calstage"] = unit.lamps, unit.calstage
    cfg = config(sequences)
    unit.lamp_setup = LampSetup.from_dict(cfg["telescopes"]["t1"]["lamps"])
    rig.sequencer.sequences = sequences_from_config(cfg)
    return rig, lamps, stage


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


def stage_at(stage: TRESFrontEndServer, mm: float) -> bool:
    return not stage.sim.moving and stage.sim.position == pytest.approx(mm)


async def test_a_full_tres_calibration_sequence(servers, make_rig):
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"tres_thar": THAR})
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 60)
    assert plan.history == [("t1", "thar", "complete", 3)]
    t = {
        w: _events(rig, w)[0].t
        for w in ("calstage_selected", "lamp_on", "exposure_started", "lamp_off", "sequence_done")
    }
    assert t["calstage_selected"] < t["lamp_on"] < t["exposure_started"] < t["lamp_off"]
    assert t["exposure_started"] - t["lamp_on"] >= 0.2  # the warm-up
    assert _events(rig, "lamp_off")[0].t > _events(rig, "exposure_complete")[-1].t
    stages = [e.detail["calstage"] for e in _events(rig, "calstage_selected")]
    assert stages == ["in", "out", "out"]  # the last: the cleanup, which always runs
    assert switching(lamps.sim)[:2] == ["N02", "F02"] and lamps.sim.lit() == [3, 5]
    assert stage_at(stage, 0.0) and rig.cams[0].exposures_started == 3
    assert rig.dome.opens == 0  # a dark sequence: no lease, the dome stays shut
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True


async def test_cancelled_mid_sequence_the_lamps_go_off_and_the_stage_out(servers, make_rig):
    slow = _with(lambda d: d["steps"][2]["expose"].update(exptime=2.0, n=5))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"tres_thar": slow})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 30.0, what="exposing")
    assert 2 in lamps.sim.lit() and stage_at(stage, 25.0)
    rig.tasks[-1].cancel()  # the sequencer stopping (SIGTERM)
    await asyncio.wait([rig.tasks[-1]], timeout=20)
    assert plan.history[0][2] == "interrupted"
    assert lamps.sim.lit() == [3, 5]
    await wait_until(lambda: stage_at(stage, 0.0), 30.0, what="stage out")
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True
    assert any(c.parked for c in rig.sequencer.cleanups) or rig.parked()


async def test_a_second_cancel_during_the_lamp_off_still_moves_the_stage_out(servers, make_rig):
    """CI 2026-11-22 (master 2746b0d, 3.14): the sequencer cancelled mid
    exposure; a second cancel (a TaskGroup passing the first on a loop step
    later) landed while the cleanup switched the lamp off, before the task
    for the cleanup steps existed, so the calibration stage stayed in. Here
    the lamp-off is held and the second cancel sent while it waits."""
    slow = _with(lambda d: d["steps"][2]["expose"].update(exptime=2.0, n=5))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"tres_thar": slow})
    seq = rig.sequencer
    entered, release = asyncio.Event(), asyncio.Event()
    lamp_off, cleanup = seq._sequence_lamp_off, seq._sequence_cleanup
    running: list[asyncio.Task] = []  # the task running the sequence

    async def held(r, lamp, sid):
        entered.set()
        await release.wait()
        await lamp_off(r, lamp, sid)

    async def watched(r, **kw):
        running.append(asyncio.current_task())
        await cleanup(r, **kw)

    seq._sequence_lamp_off, seq._sequence_cleanup = held, watched
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 30.0, what="exposing")
    assert stage_at(stage, 25.0)
    main = rig.tasks[-1]
    main.cancel()
    await asyncio.wait_for(entered.wait(), 10)
    running[0].cancel()  # the second cancel, while the lamp is being switched off
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait([main], timeout=20)
    await wait_until(lambda: stage_at(stage, 0.0), 30.0, what="stage out")
    assert lamps.sim.lit() == [3, 5]
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True


async def test_a_failure_mid_sequence_still_cleans_up(servers, make_rig):
    slow = _with(lambda d: d["steps"][2]["expose"].update(exptime=0.5, n=4))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(
        make_rig, servers, plan, {"tres_thar": slow}, seq=dict(max_consecutive_errors=1)
    )
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 30.0, what="exposing")
    rig.cams[0].fail("camera gone")
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 60)
    assert plan.history[0][2] == "failed"
    assert lamps.sim.lit() == [3, 5] and stage_at(stage, 0.0)


async def test_a_lease_revoked_mid_sequence_cleans_up_as_it_closes(servers, make_rig):
    sky = {
        "steps": [{"calstage": "science"}, {"expose": {"type": "EXPOSE", "exptime": 1.0, "n": 20}}],
        "cleanup": [{"calstage": "out"}],
    }
    plan = StaticPlan([{"name": "sky", "kind": "sequence", "sequence": "sky"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"sky": sky})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 30.0, what="exposing")
    assert stage_at(stage, 20.0)
    rig.weather.safe = False
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 30.0, what="closed, parked")
    await wait_until(lambda: stage_at(stage, 0.0), 30.0, what="stage out")
    await wait_until(lambda: plan.history, 30.0, what="reported")
    assert plan.history[0][2] == "interrupted"
    assert lamps.sim.lit() == [3, 5]


async def test_a_sequence_that_cannot_fit_in_max_on_is_refused_before_any_lamp(servers, make_rig):
    long = _with(lambda d: d["steps"][2]["expose"].update(exptime=20.0, n=40))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {})
    cfg = config({"tres_thar": long})
    cfg["telescopes"]["t1"]["lamps"]["lamps"]["ThAr1"].pop("max_on")  # only the switch knows
    rig.sequencer.sequences = sequences_from_config(cfg)
    await rig.start()
    await wait_until(lambda: plan.history, 30.0, what="refused")
    assert plan.history[0][2] == "failed"
    failed = _events(rig, "observation_failed")
    assert failed and "max_on 600 s" in failed[0].detail["error"]
    assert switching(lamps.sim) == [] and rig.cams[0].exposures_started == 0
    assert stage_at(stage, 0.0)  # nothing moved either


async def test_an_unknown_sequence_fails_the_observation(servers, make_rig):
    plan = StaticPlan([{"name": "x", "kind": "sequence", "sequence": "nope"}], ["t1"])
    rig, *_ = tres_rig(make_rig, servers, plan, {"tres_thar": THAR})
    await rig.start()
    await wait_until(lambda: plan.history, 30.0, what="failed")
    assert plan.history[0][2] == "failed"
    assert "no sequence 'nope'" in _events(rig, "observation_failed")[0].detail["error"]


assert LAMPS  # the switch's lamps (tests/core/test_pulizzi.py): ThAr1, Incand


async def test_a_preempted_sequence_cleans_up_before_the_next_observation(servers, make_rig):
    """A preemption ("now": a ToO, the console's stop) cancels the sequence
    alone, with no generation cleanup after it: the next observation (here
    the same, re-queued by the plan) waits for its cleanup."""
    sky = {
        "steps": [{"calstage": "science"}, {"expose": {"type": "EXPOSE", "exptime": 0.3, "n": 50}}],
        "cleanup": [{"calstage": "out"}],
    }
    plan = StaticPlan([{"name": "sky", "kind": "sequence", "sequence": "sky"}], ["t1"])
    rig, *_ = tres_rig(make_rig, servers, plan, {"sky": sky})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 30.0, what="exposing")
    assert rig.sequencer.preempt("t1", "now", "a ToO")
    await wait_until(lambda: len(_events(rig, "target_start")) >= 2, 30.0, what="the next one")
    done = _events(rig, "sequence_cleanup_done")[0].t
    assert done < _events(rig, "target_start")[1].t
    assert plan.history[0][2] == "interrupted"


# -- templates (docs/sequences.md, "Templates") ---------------------------------------

#: THAR, made from the generic arc set.
THAR_SET = {
    "template": "arc_set",
    "instrument": "camera_spectrograph",
    "lamp": "ThAr1",
    "warmup": 0.2,
    "exptime": 0.2,
    "count": 3,
    "calstage": "in",
    "calstage_after": "out",
}


def _steps(seq) -> list:
    return [(s.type, s.params) for s in seq.steps], [(s.type, s.params) for s in seq.cleanup]


def test_the_generic_arc_set_is_the_written_out_sequence():
    seqs = sequences_from_config(config({"thar": THAR, "thar_set": THAR_SET}))
    assert _steps(seqs["thar_set"]) == _steps(seqs["thar"])
    s = seqs["thar_set"]
    assert s.template == "arc_set" and s.params["count"] == 3 and s.frame_types == ("ARC",)
    assert s.description == "arcs on one lamp" and s.lamp_time == seqs["thar"].lamp_time
    # A parameter left null leaves out its step, or its option: no stage
    # moves, no cleanup, the lamps entry's warm-up.
    (bare,) = sequences_from_config(
        config({"b": {"template": "lamp_flat_set", "lamp": "Incand", "exptime": 1, "count": 2}})
    ).values()
    assert [s.type for s in bare.steps] == ["lamp", "expose", "lamp"]
    assert bare.steps[0].params.get("warmup") is None and bare.cleanup == ()
    assert bare.instrument == "imager" and bare.frame_types == ("LAMP_FLAT",)


def test_the_generic_bias_and_dark_sets():
    seqs = sequences_from_config(
        config(
            {
                "bias": {"template": "bias_set", "count": 5, "description": "morning biases"},
                "dark": {"template": "dark_set", "exptime": 60, "count": 3, "filter": "dark"},
            },
            filterwheel="127.0.0.1:1/0",
        )
    )
    assert _steps(seqs["bias"])[0] == [
        ("expose", {"type": "BIAS", "exptime": 0.0, "n": 5, "object": None})
    ]
    assert seqs["bias"].description == "morning biases" and seqs["bias"].lamps == ()
    assert [s.type for s in seqs["dark"].steps] == ["filter", "expose"]
    assert not seqs["dark"].light and seqs["dark"].frame_types == ("DARK",)


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"template": "nope"}, r"sequences\.x\.template: one of arc_set, bias_set"),
        ({"template": "arc_set", "lamp": "ThAr1"}, r"template arc_set\): give exptime, count"),
        ({**THAR_SET, "lamps": "x"}, r"unknown parameter\(s\) \['lamps'\]"),
        ({**THAR_SET, "lamp": "ThAr9"}, r"x\.steps\[1\] \(on t1\): no lamp 'ThAr9'"),
        ({**THAR_SET, "count": 600}, r"more than its max_on 600 s"),
        ({**THAR_SET, "telescopes": ["t9"]}, r"x\.telescopes: unknown \['t9'\]"),
    ],
)
def test_a_template_instance_is_checked_as_any_sequence(entry, match):
    with pytest.raises(ValueError, match=match):
        sequences_from_config(config({"x": entry}))


def test_a_site_template_overrides_a_built_in_and_adds_its_own():
    cfg = config(
        {
            "x": {"template": "arc_set", "lamp": "ThAr1", "exptime": 1, "count": 2},
            "y": {"template": "both", "exptime": 1},
        }
    )
    cfg["sequence_templates"] = {
        "_note": "a comment",
        # The site's arc set waits for the lamp to settle after the frames too.
        "arc_set": {
            "params": {"lamp": {}, "exptime": {}, "count": {"default": 1}},
            "steps": [
                {"lamp": "$lamp", "on": True},
                {"expose": {"type": "ARC", "exptime": "$exptime", "n": "$count"}},
                {"wait": 1},
                {"lamp": "$lamp", "on": False},
            ],
        },
        "both": {
            "params": {"exptime": {}},
            "steps": [
                {"lamp": "ThAr1", "on": True},
                {"lamp": "Incand", "on": True},
                {"expose": {"type": "LAMP_FLAT", "exptime": "$exptime"}},
                {"lamp": "ThAr1", "on": False},
                {"lamp": "Incand", "on": False},
            ],
        },
    }
    seqs = sequences_from_config(cfg)
    assert [s.type for s in seqs["x"].steps] == ["lamp", "expose", "wait", "lamp"]
    assert seqs["y"].lamps == ("Incand", "ThAr1")
    cfg["sequence_templates"]["both"]["steps"][2]["expose"]["n"] = "$count"
    with pytest.raises(ValueError, match=r"sequence_templates\.both: \$count not among its"):
        sequences_from_config(cfg)
    cfg["sequence_templates"]["both"] = {"params": {"telescopes": {}}, "steps": []}
    with pytest.raises(ValueError, match=r"parameter name\(s\) \['telescopes'\] are taken"):
        sequences_from_config(cfg)


def test_wait_after_pauses_after_a_generic_set_and_is_dropped_when_null():
    """``wait_after`` (every built-in template): a wait after the set, for
    the pause between a night's calibration groups; left out, no wait."""
    cfg = config(
        {
            "a": {"template": "arc_set", "lamp": "ThAr1", "exptime": 1, "count": 1},
            "b": {"template": "bias_set", "count": 2, "wait_after": 20},
            "f": {
                "template": "lamp_flat_set",
                "lamp": "ThAr1",
                "exptime": 1,
                "count": 1,
                "wait_after": 5,
            },
        }
    )
    seqs = sequences_from_config(cfg)
    assert "wait" not in [s.type for s in seqs["a"].steps]
    assert seqs["b"].steps[-1].type == "wait" and seqs["b"].steps[-1].params == 20
    assert seqs["f"].steps[-1].params == 5


def test_a_plug_in_adds_a_template():
    from robofast.sequences import REQUIRED, SEQUENCE_TEMPLATES, SequenceTemplate

    t = SequenceTemplate("waits", {"s": REQUIRED}, {"steps": [{"wait": "$s"}, {"wait": "$s"}]})
    SEQUENCE_TEMPLATES.register("waits", t)
    try:
        (seq,) = sequences_from_config(config({"w": {"template": "waits", "s": 2}})).values()
        assert [s.params for s in seq.steps] == [2.0, 2.0] and seq.estimate == 4.0
        assert t.as_json()["params"] == {"s": {}}
    finally:
        SEQUENCE_TEMPLATES.unregister("waits")


def test_rebuild_sets_parameters_and_checks_again():
    seqs = sequences_from_config(config({"thar": THAR, "set": THAR_SET}))
    again = seqs["set"].rebuild({"exptime": 2.0, "count": 4})
    assert again.steps[2].params["exptime"] == 2.0 and again.steps[2].params["n"] == 4
    assert again.lamp_time["t1"]["ThAr1"] == pytest.approx(0.2 + 4 * 3.0)
    with pytest.raises(ValueError, match="max_on 600 s"):
        seqs["set"].rebuild({"exptime": 20.0, "count": 40})
    with pytest.raises(ValueError, match=r"takes no \['lamp_power'\]"):
        seqs["set"].rebuild({"lamp_power": 3})
    assert seqs["thar"].rebuild is None


def test_which_sequences_a_request_may_name():
    from robofast.dispatch import TelescopeLimits
    from robofast.sequences import check_request_sequences, requestable

    seqs = sequences_from_config(
        config(
            {
                "thar": THAR,
                "set": THAR_SET,
                "bias": {"template": "bias_set", "count": 2},
                "sky": {"template": "dark_set", "exptime": 1, "count": 1},
            }
        )
    )
    assert requestable(seqs["set"], "ARC") is None
    assert "takes ARC, not LAMP_FLAT" in requestable(seqs["set"], "LAMP_FLAT")
    assert "not made from a template" in requestable(seqs["thar"], "ARC")
    assert "takes no lamp frames, not ARC" in requestable(seqs["sky"], "ARC")
    assert "takes no exptime" in requestable(seqs["bias"], "ARC")

    def lim(*names):
        return {"t1": TelescopeLimits(["imager"], 20, 90, sequences=list(names))}

    assert set(check_request_sequences(lim("set"), seqs)) == {"set"}
    with pytest.raises(ValueError, match=r"telescopes\.t1\.sequences: 'nope' is not in"):
        check_request_sequences(lim("nope"), seqs)
    with pytest.raises(ValueError, match=r"'thar': no request can name it: .*not made from"):
        check_request_sequences(lim("thar"), seqs)


def test_the_shipped_flwo60_sequences_are_the_generic_sets_and_requestable(tmp_path):
    import shutil

    from robofast.sequences import check_request_sequences

    cfg = load_config(ROOT / "sites/flwo60/flwo60.json")
    seqs = sequences_from_config(cfg)
    assert {s.template for s in seqs.values()} == {"arc_set", "lamp_flat_set", "bias_set"}
    for name in ("dispatch.example.json", "requests.example.json"):
        shutil.copy(ROOT / "sites/flwo60" / name, tmp_path / name)
    plan = plan_from_config(
        json.loads((tmp_path / "dispatch.example.json").read_text()),
        ["tillinghast"],
        base=tmp_path,
    )
    assert set(check_request_sequences(plan.limits, seqs)) == {"tres_thar", "tres_flat"}
    check_plan_sequences(plan, seqs)
    assert set(plan.sequences) == {"tres_thar", "tres_flat"}


# -- requests naming a site sequence (docs/sequences.md, "Requests") --------------------


def seq_group(gid="arcs", sequence="set", ctype="ARC", exptime=0.1, count=2, **ic_extra) -> dict:
    from tests.core.test_dispatch import group

    ic = {"exposure_time": exptime, "exposure_count": count}
    ic["extra_params"] = {"sequence": sequence}
    ic.update(ic_extra)
    g = group(gid, ics=[ic])
    g["requests"][0]["configurations"][0]["type"] = ctype
    return g


def test_a_lamp_request_may_name_a_sequence_instead_of_a_lamp():
    from robofast.request_model import RequestGroup

    g = RequestGroup.from_dict(seq_group())
    (c,) = g.configurations()
    ic = c.instrument_configs[0]
    assert ic.sequence == "set" and ic.lamp is None and not ic.light
    again = RequestGroup.from_dict(g.to_dict())
    assert again.configurations()[0].instrument_configs[0].sequence == "set"
    with pytest.raises(ValueError, match="not both: the sequence says its lamps"):
        RequestGroup.from_dict(seq_group(optical_elements={"lamp": "ThAr1"}))
    with pytest.raises(ValueError, match=r"extra_params\.sequence: only for ARC and LAMP_FLAT"):
        RequestGroup.from_dict(seq_group(ctype="EXPOSE"))
    with pytest.raises(ValueError, match=r"extra_params\.sequence: a sequence name"):
        RequestGroup.from_dict(seq_group(sequence="a b"))


async def test_dispatch_runs_a_named_sequence_with_the_requests_exptime_and_count():
    from robofast.dispatch import TelescopeLimits
    from tests.core.test_dispatch import NIGHT, Clock, make

    seqs = sequences_from_config(
        config({"set": THAR_SET, "flat": {**THAR_SET, "template": "lamp_flat_set"}})
    )
    limits = {
        "t1": TelescopeLimits(["imager"], 20, 90),
        "t2": TelescopeLimits(["imager"], 20, 90, sequences=["set", "flat"]),
    }
    d = make(
        [seq_group(), seq_group("flats", sequence="set", ctype="LAMP_FLAT")],
        clock=Clock(NIGHT),
        limits=limits,
    )
    d.sequences = seqs
    assert any("has no sequence 'set'" in r for r in by_id(d, "t1")["arcs-r"].reasons)
    flats = by_id(d, "t2")["flats-r"]
    assert any("takes ARC, not LAMP_FLAT" in r for r in flats.reasons)
    obs = await d.next_observation("t2")
    assert isinstance(obs, Observation), obs
    assert obs.kind == "sequence" and obs.sequence == "set" and not obs.light
    assert obs.sequence_params == {"exptime": 0.1, "count": 2} and obs.count == 2
    d.report("t2", obs, "complete", 2)
    assert d.requests["arcs-r"].done


def by_id(d, telescope):
    return {c.request.id: c for c in d.evaluate(telescope)}


async def test_the_request_api_checks_a_named_sequence(make_api):  # noqa: F811
    api = await make_api()
    enc = api.app.enclosures["aqawan1"]
    seqs = sequences_from_config(config({"set": THAR_SET}))
    r = await api.post("tom", "/requestgroups/", seq_group("a0"))
    assert r.status_code == 400 and NO_LAMPS in r.text, r.text  # no lamps, no sequences
    enc.planner.limits["t1"].sequences = ["other"]
    r = await api.post("tom", "/requestgroups/", seq_group("a0"))
    assert r.status_code == 400 and "no sequence 'set' on" in r.text, r.text
    enc.planner.limits["t2"].sequences = ["set"]
    enc.sequences = enc.planner.sequences = seqs
    r = await api.post("tom", "/requestgroups/", seq_group("a1", ctype="LAMP_FLAT"))
    assert r.status_code == 400 and "takes ARC, not LAMP_FLAT" in r.text, r.text
    r = await api.post("tom", "/requestgroups/", seq_group("a2", exptime=20.0, count=40))
    assert r.status_code == 400 and "max_on 600 s" in r.text, r.text
    r = await api.post("tom", "/requestgroups/", seq_group("a3"))
    assert r.status_code == 202, r.text


async def test_a_requested_sequence_runs_with_its_values_and_cleans_up(servers, make_rig):
    obs = {
        "name": "arcs",
        "kind": "sequence",
        "sequence": "set",
        "sequence_params": {"exptime": 0.1, "count": 2},
    }
    plan = StaticPlan([obs], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"set": THAR_SET})
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 60)
    assert plan.history == [("t1", "arcs", "complete", 2)]
    assert rig.cams[0].exposures_started == 2
    assert lamps.sim.lit() == [3, 5] and stage_at(stage, 0.0)
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True


async def test_a_requested_sequence_that_breaks_max_on_fails_before_anything_moves(
    servers, make_rig
):
    obs = {
        "name": "arcs",
        "kind": "sequence",
        "sequence": "set",
        "sequence_params": {"exptime": 20.0, "count": 40},
    }
    plan = StaticPlan([obs], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"set": THAR_SET})
    await rig.start()
    await wait_until(lambda: plan.history, 30.0, what="refused")
    assert plan.history[0][2] == "failed"
    assert "max_on 600 s" in _events(rig, "observation_failed")[0].detail["error"]
    assert switching(lamps.sim) == [] and rig.cams[0].exposures_started == 0
    assert stage_at(stage, 0.0)


def test_a_static_plans_sequence_params_are_checked_at_start():
    seqs = sequences_from_config(config({"thar": THAR, "set": THAR_SET}))
    ok = {"name": "s", "kind": "sequence", "sequence": "set", "sequence_params": {"count": 2}}
    check_plan_sequences(StaticPlan([ok], ["t1"]), seqs)
    bad = {**ok, "sequence_params": {"count": 900}}
    with pytest.raises(ValueError, match=r"sequence_params: .*max_on"):
        check_plan_sequences(StaticPlan([bad], ["t1"]), seqs)
    fixed = {**ok, "sequence": "thar"}
    with pytest.raises(ValueError, match="not made from a template"):
        check_plan_sequences(StaticPlan([fixed], ["t1"]), seqs)
    with pytest.raises(ValueError, match="'sequence_params' is for kind 'sequence'"):
        Observation("x", sequence_params={"count": 1})
