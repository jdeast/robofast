"""Autofocus in the sequencer, the plan triggers, the request model and the
service config (fake devices; the simulated focuser and star field)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from robofast_core.autofocus import AutofocusSetup, FocusModel, FocusStore
from robofast_core.autofocus_plan import AutofocusTriggers, TriggerConfig
from robofast_core.config import load_config
from robofast_core.owner import DeviceOwner
from robofast_core.plan import CallablePlan, Idle, Observation, StaticPlan
from robofast_core.registry import ValueRegistry
from robofast_core.request_model import RequestGroup, load_request_groups
from robofast_core.sequencer_service import build
from robofast_core.testing.checks import wait_until
from robofast_core.testing.focus_sim import SimFocuser, StarfieldCamera
from robofast_core.timeline import Timeline

from .test_autofocus import OPTICS, TRUE_FOCUS, _calibration_store, scene, tolerance
from .test_imaging import attach, headers, run_night

SITES = Path(__file__).resolve().parents[2] / "sites"
AF_BASE = {
    "optics": {
        "f_ratio": OPTICS.f_ratio,
        "pixel_size": OPTICS.pixel_size,
        "pixel_scale": OPTICS.pixel_scale,
        "obstruction": OPTICS.obstruction,
    },
    "exptime": 0.01,
    "poll_interval": 0.005,
    "max_start_error": 300,
    "readout_margin": 5.0,
    "move_timeout": 3.0,
}
FIELD = {"name": "field", "ra_hours": 1.0, "dec_deg": 10.0, "exptime": 0.05, "count": 2}


def focus_rig(make_rig, plan, *, start=TRUE_FOCUS + 200, seeing=2.0, model=None, **kw):
    """make_rig with t1's camera replaced by the star field and a focuser
    the sequencer may move."""
    rig = make_rig(plan=plan, **kw)
    unit = rig.sequencer.units["t1"]
    foc = SimFocuser(position=round(start))
    cam = StarfieldCamera(foc, scene(seeing, seed=31))
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=5)
    unit.focuser = DeviceOwner("t1.focuser", lambda: foc, rig.timeline, default_timeout=2)
    store = FocusStore()
    store.data["calibrations"] = dict(_calibration_store().data["calibrations"])
    unit.autofocus = AutofocusSetup(dict(AF_BASE), store=store, model=model or FocusModel())
    rig.focuser, rig.starcam = foc, cam
    return rig, unit


async def run_to_end(rig, timeout=60.0):
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), timeout)


# -- the sequencer --------------------------------------------------------------------


async def test_start_of_night_autofocus_then_the_field(make_rig):
    inner = StaticPlan([FIELD], ["t1"])
    plan = AutofocusTriggers(inner)
    rig, unit = focus_rig(make_rig, plan)
    plan.bind(rig.sequencer.registry)
    await run_to_end(rig)
    # The autofocus came first, its result was used, and the field followed.
    assert plan.history == [("t1", "start of night")]
    assert inner.history == [("t1", "field", "complete", 2)]
    [res] = unit.autofocus_results
    assert res.success and res.used == "vcurve" and res.exposures == 2
    assert abs(rig.focuser.optical - TRUE_FOCUS) < tolerance(2.0)
    assert unit.focus_restore is None
    # Results are in the registry (FITS AF* keywords) and the timeline.
    reg = rig.sequencer.registry
    assert reg.get("t1.autofocus.focus").value == pytest.approx(res.focus, abs=0.1)
    assert reg.get("t1.focuser.temperature").value == 10.0
    assert rig.timeline.events("autofocus_done")[0].detail["success"] is True
    # The field's exposures were taken at the new focus.
    assert all(abs(p - TRUE_FOCUS) < tolerance(2.0) for p in rig.starcam.exposed_at[-2:])
    assert plan.due("t1") is None
    assert not rig.timeline.events("telescope_error")


async def test_autofocus_frames_are_saved_and_later_frames_carry_af_keywords(make_rig, tmp_path):
    plan = AutofocusTriggers(StaticPlan([FIELD], ["t1"]))
    rig, _ = focus_rig(make_rig, plan)
    imaging = attach(rig, tmp_path, measure_fwhm=True)
    rig.sequencer.registry = imaging.registry
    plan.bind(imaging.registry)
    await run_night(rig, imaging, timeout=60)
    frames = [h for _, h in headers(tmp_path, "t1")]
    assert [h["IMAGETYP"] for h in frames] == ["FOCUS", "FOCUS", "LIGHT", "LIGHT"]
    assert frames[0]["OBJECT"] == "autofocus" and frames[0]["NAXIS1"] == 512
    light = frames[-1]
    assert light["AFMETHOD"] == "vcurve" and light["AFOK"] is True
    assert abs(light["AFFOCUS"] - TRUE_FOCUS) < tolerance(2.0)
    assert light["AFNEXP"] == 2
    # The pipeline measured the light frames' FWHM for the FWHM trigger (in
    # the header config's 0.6"/px; the scene's stars are 2 px FWHM).
    fwhm = imaging.registry.get("t1.image.fwhm")
    assert fwhm.unit == "arcsec" and fwhm.value == pytest.approx(2.0 * 0.6, rel=0.12)


async def test_a_failed_autofocus_is_not_the_telescopes_fault(make_rig):
    inner = StaticPlan([FIELD], ["t1"])
    plan = AutofocusTriggers(inner)
    rig, unit = focus_rig(make_rig, plan)
    rig.starcam.scene.transparency = 0.0  # clouds
    unit.autofocus.base["fallback"] = False
    plan.bind(rig.sequencer.registry)
    await run_to_end(rig)
    [res] = unit.autofocus_results
    assert not res.success
    # Reported failed, no backoff; the focuser went back; the field was observed.
    assert rig.timeline.events("observation_failed")
    assert not rig.timeline.events("telescope_error")
    assert rig.focuser.Position == TRUE_FOCUS + 200
    assert inner.history == [("t1", "field", "complete", 2)]
    assert rig.sequencer.registry.get("t1.autofocus.success").value is False


async def test_an_autofocus_cut_short_is_undone_before_the_next_observation(make_rig):
    af = Observation("focus", ra_hours=1.0, dec_deg=10.0, kind="autofocus")
    plan = StaticPlan([], ["t1"])
    plan.queues["t1"].extend([af, Observation(**FIELD)])
    rig, unit = focus_rig(make_rig, plan, start=TRUE_FOCUS + 100)
    unit.autofocus.base["method"] = "sweep"

    async def preempt_mid_focus():
        await wait_until(lambda: rig.timeline.events("autofocus_point"), timeout=30)
        rig.sequencer.preempt("t1", "now", "test")

    helper = asyncio.create_task(preempt_mid_focus())
    await run_to_end(rig)
    await helper
    # The cut-short run left the focuser defocused; it went back to where
    # it was before the next observation (the autofocus, re-queued).
    restored = rig.timeline.events("focus_restored")
    assert restored and restored[0].detail["position"] == TRUE_FOCUS + 100
    assert [h[1:3] for h in plan.history] == [
        ("focus", "interrupted"),
        ("focus", "complete"),
        ("field", "complete"),
    ]
    assert unit.autofocus_results[-1].success
    assert abs(rig.focuser.optical - TRUE_FOCUS) < tolerance(2.0)


async def test_autofocus_needs_a_focuser(make_rig):
    af = Observation("focus", ra_hours=1.0, dec_deg=10.0, kind="autofocus")
    plan = StaticPlan([], ["t1"])
    plan.queues["t1"].append(af)
    rig = make_rig(plan=plan, seq={"max_consecutive_errors": 1})
    await run_to_end(rig)
    [err] = rig.timeline.events("telescope_error")
    assert "cannot autofocus" in err.detail["error"]


# -- the triggers ---------------------------------------------------------------------


class Inner:
    """A plan with peek, a record and an extra hook."""

    def __init__(self, n: int = 10, peek: bool = True) -> None:
        self.n = n
        self.reports: list = []
        self.record = Record()
        if not peek:
            self.peek = None  # type: ignore[assignment]

    def _obs(self) -> Observation | None:
        return Observation(f"obs{self.n}", ra_hours=2.0, dec_deg=5.0, filter="V", port="imager")

    async def next_observation(self, telescope):
        self.n -= 1
        return self._obs() if self.n >= 0 else None

    async def peek(self, telescope):  # type: ignore[no-redef]
        return self._obs() if self.n > 0 else None

    def report(self, telescope, obs, outcome, frames, files=None):
        self.reports.append((obs.name, outcome, files))

    def current(self):
        return {"t1": "x"}


class Record:
    def __init__(self) -> None:
        self.entries: list = []

    def write(self, when, kind, fields, snapshot=None):
        self.entries.append((kind, fields))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _publish(reg, name, value):
    reg.publish(name, value, max_age=None)


async def test_triggers_start_temperature_fwhm_interval_and_retry():
    reg, clock, inner = ValueRegistry(), Clock(), Inner()
    _publish(reg, "t1.focuser.temperature", 10.0)
    trig = AutofocusTriggers(
        inner,
        TriggerConfig(temperature_change=1.0, fwhm_degradation=1.5, max_interval=3600),
        registry=reg,
        clock=clock,
    )
    assert trig.current() == {"t1": "x"}  # passed through
    # Start of night: the autofocus copies the next observation's setup.
    af = await trig.next_observation("t1")
    assert af.kind == "autofocus" and af.filter == "V" and af.port == "imager"
    assert (af.ra_hours, af.dec_deg) == (2.0, 5.0) and "start of night" in af.name
    assert inner.n == 10  # nothing taken from the plan
    trig.report("t1", af, "complete", 2)
    trig.autofocus_result("t1", af, {"success": True, "focus": 123.0, "used": "vcurve"})
    assert inner.reports == [] and inner.record.entries[0][0] == "autofocus"
    assert inner.record.entries[0][1]["focus"] == 123.0
    # Now the plan's own observations, with files passed through.
    obs = await trig.next_observation("t1")
    assert obs.name == "obs9"
    _publish(reg, "t1.image.fwhm", 2.0)
    trig.report("t1", obs, "complete", 1, files=["a.fits"])
    assert inner.reports == [("obs9", "complete", ["a.fits"])]
    assert trig.due("t1") is None
    # The temperature moves 1.5 C.
    _publish(reg, "t1.focuser.temperature", 11.5)
    assert trig.due("t1").startswith("temperature changed +1.50")
    af = await trig.next_observation("t1")
    trig.report("t1", af, "complete", 2)
    assert trig.due("t1") is None
    # The FWHM degrades (the baseline is re-learned after each autofocus).
    obs = await trig.next_observation("t1")
    _publish(reg, "t1.image.fwhm", 2.2)
    trig.report("t1", obs, "complete", 1)
    _publish(reg, "t1.image.fwhm", 3.5)
    assert trig.due("t1").startswith("FWHM 3.50 > 1.5 x 2.20")
    af = await trig.next_observation("t1")
    # An interrupted autofocus is due again at once; a failed one waits.
    trig.report("t1", af, "interrupted", 0)
    assert trig.due("t1") is not None
    af = await trig.next_observation("t1")
    trig.report("t1", af, "failed", 2)
    assert trig.due("t1") is None
    clock.t += 1800
    assert trig.due("t1") == "retry"
    af = await trig.next_observation("t1")
    trig.report("t1", af, "complete", 2)
    _publish(reg, "t1.image.fwhm", 2.0)
    # And the interval.
    clock.t += 3601
    assert "since the last autofocus" in trig.due("t1")


async def test_triggers_hold_the_observation_of_a_plan_without_peek():
    inner = Inner(n=2, peek=False)
    trig = AutofocusTriggers(inner, TriggerConfig(), registry=ValueRegistry())
    assert trig.peek is None  # the sequencer asks next_observation
    af = await trig.next_observation("t1")
    assert af.kind == "autofocus" and inner.n == 1  # obs1 taken, and held
    trig.report("t1", af, "complete", 2)
    assert (await trig.next_observation("t1")).name == "obs1"
    assert (await trig.next_observation("t1")).name == "obs0"  # no new autofocus


async def test_triggers_ignore_dark_frames_idle_and_other_telescopes():
    seq = iter([Observation("dark", light=False, exptime=1), Idle(5), Observation("o", ra_hours=1)])
    trig = AutofocusTriggers(CallablePlan(lambda: next(seq)), telescopes=["t1"])
    assert (await trig.next_observation("t1")).name == "dark"
    assert isinstance(await trig.next_observation("t1"), Idle)
    assert (await trig.next_observation("t2")).name == "o"  # t2 has no autofocus


# -- the request model and dispatch --------------------------------------------------------


def test_auto_focus_configuration_round_trips_and_dispatches():
    from .test_dispatch import group, make

    g = group("focus", ics=[{"exptime": 3.0, "filter": "V"}])
    g["requests"][0]["configurations"][0]["type"] = "AUTO_FOCUS"
    [rg] = load_request_groups([g])
    c = rg.requests[0].configurations[0]
    assert c.type == "AUTO_FOCUS"
    assert RequestGroup.from_dict(json.loads(json.dumps(rg.to_dict()))).to_dict() == rg.to_dict()
    bad = json.loads(json.dumps(g))
    bad["requests"][0]["configurations"][0]["type"] = "FOCUS_PLEASE"
    with pytest.raises(ValueError, match="type"):
        load_request_groups([bad])
    two = json.loads(json.dumps(g))
    two["requests"][0]["configurations"][0]["instrument_configs"].append({"exptime": 1})
    with pytest.raises(ValueError, match="AUTO_FOCUS"):
        load_request_groups([two])

    async def go():
        d = make([g], limits=None)
        obs = await d.next_observation("t1")
        assert obs.kind == "autofocus" and obs.autofocus == {"exptime": 3.0}
        assert obs.filter == "V" and obs.count == 1

    asyncio.run(go())


def test_observation_kind_is_validated():
    with pytest.raises(ValueError, match="kind"):
        Observation("x", kind="guess")
    with pytest.raises(ValueError, match="light"):
        Observation("x", kind="autofocus", light=False)
    assert Observation.from_dict({"name": "f", "kind": "autofocus"}).kind == "autofocus"


# -- the service config -------------------------------------------------------------------


async def test_service_builds_autofocus_and_triggers(tmp_path):
    config = load_config(SITES / "minerva" / "aqawan1.json")
    t1 = config["telescopes"]["t1"]
    t1.setdefault("focuser", "127.0.0.1:11999")
    t1["autofocus"] = {**AF_BASE, "store": "focus_t1.json"}
    config["autofocus_triggers"] = {"temperature_change": 0.5}
    tl = Timeline()
    seq, remote = build(config, tl, base=tmp_path)
    try:
        unit = seq.units["t1"]
        assert unit.focuser is not None and unit.autofocus is not None
        assert unit.autofocus.store.path == tmp_path / "focus_t1.json"
        assert seq.units["t2"].autofocus is None
        # aqawan1.json has operator_control: the console's hold wraps the triggers.
        triggers = seq.plan.inner
        assert isinstance(triggers, AutofocusTriggers)
        assert triggers.registry is seq.registry
        assert triggers.config.temperature_change == 0.5
        assert triggers._only == {"t1"}
    finally:
        await remote.aclose()
    t1["autofocus"]["method"] = "guess"
    with pytest.raises(ValueError, match="t1"):
        build(config, tl, base=tmp_path)
    del t1["autofocus"]
    with pytest.raises(ValueError, match="no telescope has an autofocus"):
        build(config, tl, base=tmp_path)
    config.pop("autofocus_triggers")
    t1.pop("focuser")
    t1["autofocus"] = dict(AF_BASE)
    with pytest.raises(ValueError, match="needs a focuser"):
        build(config, tl, base=tmp_path)


def test_example_autofocus_config_is_valid():
    path = SITES / "minerva" / "autofocus.example.json"
    d = json.loads(path.read_text())
    setup = AutofocusSetup.from_dict(d["telescopes"]["t2"]["autofocus"])
    assert setup.config_for("imager", "V").method == "vcurve"
    assert setup.config_for("fau", "V").optics.pixel_scale == 0.265
    TriggerConfig.from_dict(d["autofocus_triggers"])
