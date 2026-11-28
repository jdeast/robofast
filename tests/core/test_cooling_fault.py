"""A camera whose cooler cannot reach its setpoint (robofast/cooling.py,
docs/cooling.md, "A cooler that cannot reach its setpoint").

MINERVA T4, nights of 2026-11-27 and 2026-11-28 (Andor, "Ascom Driver for
Andor CCD Cameras" 6.0 over ASCOM Remote): the cooler was on at 100%
CoolerPower with its setpoint -50 C, and the CCD stayed at 20.7 C all night.
robofast warned (``cooling_not_settled``, "exposing anyway") and went on
scheduling and taking imaging frames with a room-temperature CCD. A power
cycle of the camera's outlet the next day did not bring the cooler back: a
hardware fault. Now that is a camera fault: the camera's recovery ladder
(the real policy's ``reconnect`` and ``powercycle``, its guards), each rung
followed by setting the cooler again and waiting for the CCD; a camera the
ladder cannot bring back is disabled for the night -- no more exposures on
it, imaging is not scheduled on that telescope -- and the telescope goes on
feeding spectra (which need only the guide camera and the spectrograph).

Fakes on a virtual-time loop, so the real settle time (900 s) costs nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from robofast.cooling import CameraCooling, CoolingConfig, CoolingFault, is_cooling_fault
from robofast.plan import Idle, Observation
from robofast.recovery import DevicePolicy, RecoveryLadder
from robofast.testing.fakes import FakeCamera

from .test_dispatch import group, make, tlimits
from .test_rv_spectra import run_to_end, rv_rig, spectrum_target

pytestmark = pytest.mark.virtual_time

#: T4's real cooling entry and camera ladder (the dry-run config).
T4_COOLING = {"setpoint": -50.0, "tolerance": 0.75, "settle_timeout": 900}
T4_CAMERA_LADDER = {
    "steps": ["reconnect", "powercycle"],
    "powercycle": {"pdu": "pdu-t4:8000", "switch": 2, "boot_time": 60},
    "requires_parked": True,
    "camera_warm_above": -5,
    "max_powercycles": 1,
    "min_interval": 1800,
}
#: The CCD's temperature all night (C).
STUCK = 20.7


def image(name: str) -> dict:
    return {"name": name, "exptime": 0.2, "count": 1, "ra_hours": 1.0, "dec_deg": 10.0}


class FakePDU:
    """The PDU server: a power cycle of the camera's outlet, recorded;
    ``fixes``: the cycle brings the cooler back."""

    def __init__(self, cam: FakeCamera, fixes: bool) -> None:
        self.cam, self.fixes = cam, fixes
        self.cycles: list[int] = []

    def __call__(self, address: str) -> FakePDU:
        return self

    async def cycle(self, switch: int, action: str = "PowerCycle") -> None:
        self.cycles.append(switch)
        self.cam.Connected = False  # the camera lost power: a new connection after
        if self.fixes:
            self.cam.coldest = None


class MixedPlan:
    """Imaging and spectra for t1, nothing of its own for t2: a minimal
    stand-in for the dispatch scheduler. A failed image is handed out again;
    while t1's camera is disabled (``set_camera_available``), its imaging
    goes to t2 instead. ``tell``: False leaves the hook out (the sequencer's
    backstop alone)."""

    def __init__(self, t1: list[dict], tell: bool = True) -> None:
        self.queues: dict[str, list[dict]] = {"t1": list(t1), "t2": []}
        self.camera_down: dict[str, str] = {}
        self.calls: list[tuple[str, bool, str]] = []
        self.history: list[tuple[str, str, str]] = []
        self.unsupported_calls: list[tuple[str, str, str]] = []
        self.given: dict[str, dict] = {}
        if not tell:
            self.set_camera_available = None

    def _move_imaging(self) -> None:
        for tel in list(self.camera_down):
            keep = []
            for d in self.queues[tel]:
                (keep if d.get("kind") == "spectrum" else self.queues["t2"]).append(d)
            self.queues[tel] = keep

    def _next(self, telescope: str, take: bool):
        self._move_imaging()
        q = self.queues.get(telescope) or []
        if not q:
            if any(self.queues.values()):
                return Idle(5.0, "another telescope's work may come here")
            return None
        d = q.pop(0) if take else q[0]
        if take:
            self.given[d["name"]] = d
        return Observation.from_dict(d)

    async def next_observation(self, telescope):
        return self._next(telescope, True)

    async def peek(self, telescope):
        return self._next(telescope, False)

    def report(self, telescope, obs, outcome, frames, files=None):
        self.history.append((telescope, obs.name, outcome))
        if outcome == "failed" and obs.kind != "spectrum":
            self.queues[telescope].insert(0, self.given[obs.name])

    def unsupported(self, telescope, obs, reason):
        self.unsupported_calls.append((telescope, obs.name, reason))

    def set_camera_available(self, telescope, available, reason=""):
        self.calls.append((telescope, available, reason))
        if available:
            self.camera_down.pop(telescope, None)
        else:
            self.camera_down[telescope] = reason


def rig_with(make_rig, tmp_path, plan, *, ladder: bool = True, fixes: bool = False, **cooling):
    """Two telescopes and the spectrograph (tests/core/test_rv_spectra.py);
    t1's CCD stuck at 20.7 C with its cooler on (T4's camera), cooled to
    T4's setpoint, with T4's camera ladder."""
    rig = rv_rig(make_rig, tmp_path, plan)
    cam = rig.cams[0]
    cam.ambient = cam.coldest = STUCK
    cam.CCDTemperature = STUCK
    cam.CoolerPower = 100.0
    unit = rig.sequencer.units["t1"]
    unit.cooling = CameraCooling("t1", CoolingConfig.from_dict({**T4_COOLING, **cooling}))
    rig.pdu = FakePDU(cam, fixes)
    if ladder:

        async def no_sleep(s):
            await asyncio.sleep(0)

        unit.recovery = RecoveryLadder(
            "t1",
            {"camera": DevicePolicy.from_dict(T4_CAMERA_LADDER)},
            pdu=rig.pdu,
            sleep=no_sleep,
        )
    return rig


def events(rig, what):
    return rig.timeline.events(what, "seq")


T1_NIGHT = [
    image("img1"),
    spectrum_target("rv1", feed=["t1"]),
    image("img2"),
    spectrum_target("rv2", feed=["t1"]),
]


async def test_regression_20261128_a_ccd_stuck_warm_disables_the_camera_and_spectra_go_on(
    make_rig, tmp_path
):
    """The real failure: cooler on at 100%, CCD at 20.7 C, setpoint -50 C.
    The first light frame waits the settle time and faults (no exposure on
    a warm CCD); t1 is parked; the camera ladder reconnects (the cooler set
    again, the CCD still warm after the settle time), power-cycles the
    camera's outlet (parked, idle, warm: the guards pass; still warm), is
    exhausted and escalated; the camera is disabled for the night. Imaging
    goes to t2, and t1 goes on feeding spectra."""
    plan = MixedPlan(T1_NIGHT)
    rig = rig_with(make_rig, tmp_path, plan)
    await run_to_end(rig, timeout=20000)
    seq, cam = rig.sequencer, rig.cams[0]
    unit = seq.units["t1"]

    [fault] = events(rig, "cooling_fault")
    assert fault.detail["ccd_temp"] == STUCK and fault.detail["level"] == "critical"
    assert "not within 0.75 C of its setpoint -50.0 C 900 s after" in fault.detail["text"]
    assert not events(rig, "cooling_not_settled")  # no longer "exposing anyway"
    # Not one frame on the warm CCD.
    assert cam.exposures_started == 0
    assert ("t1", "img1", "failed") in plan.history

    # The ladder: both rungs tried, each waited for the CCD, and failed.
    [start] = events(rig, "recovery_start")
    assert start.detail["device"] == "camera"
    [done] = events(rig, "recovery_done")
    assert not done.detail["recovered"]
    tried = done.detail["tried"]
    assert [t.split(":")[0] for t in tried] == ["reconnect", "powercycle"]
    assert all("camera cooling fault" in t and "20.7 C" in t for t in tried)
    assert rig.pdu.cycles == [2]  # the guards passed: parked, idle, CCD warm
    assert len(events(rig, "cooling_not_recovered")) == 2
    sets = [e.detail["why"] for e in events(rig, "cooling_set")]
    assert sets.count("recovery") == 2  # the cooler set again after each rung

    # Escalated (one summary for a person), and the camera disabled.
    [inc] = events(rig, "incident")
    assert inc.detail["device"] == "camera" and inc.detail["outcome"] == "dropped"
    [off] = events(rig, "camera_disabled")
    assert off.detail["level"] == "critical"
    assert off.detail["reason"].startswith(
        "camera cooling fault: CCD at 20.7 C, not within 0.75 C of its setpoint -50.0 C"
        " 900 s after the cooler was set (cooler on); recovery ladder exhausted (reconnect:"
    )
    assert "escalation: no agent configured" in off.detail["reason"]
    assert unit.camera_disabled and any("camera DISABLED" in a for a in unit.alarms)
    assert plan.calls[0] == ("t1", True, "")  # the start of the night
    assert plan.calls[-1][:2] == ("t1", False)
    assert seq.needs_operator

    # The telescope did not give up: its spectra were taken, its imaging
    # went to t2.
    assert not unit.gave_up and not events(rig, "giving_up")
    spectra = [e.detail for e in events(rig, "spectrum_done")]
    assert {d["spectrum"] for d in spectra} == {"rv1", "rv2"}
    assert all(d["telescopes"] == ["t1"] for d in spectra)
    assert ("t2", "img1", "complete") in plan.history
    assert ("t2", "img2", "complete") in plan.history
    assert cam.exposures_started == 0 and rig.cams[1].exposures_started == 2
    assert not plan.unsupported_calls


async def test_regression_20261128_without_the_plan_hook_imaging_is_unsupported(make_rig, tmp_path):
    """A plan that is not told (no ``set_camera_available``): the
    sequencer's backstop reports t1's imaging ``unsupported`` without
    slewing or exposing; spectra still go."""
    plan = MixedPlan(T1_NIGHT, tell=False)
    rig = rig_with(make_rig, tmp_path, plan, settle_timeout=120)
    await run_to_end(rig, timeout=20000)
    cam = rig.cams[0]
    assert rig.sequencer.units["t1"].camera_disabled
    assert cam.exposures_started == 0
    [(tel, _, why)] = [u for u in plan.unsupported_calls if u[1] == "img2"]
    assert tel == "t1" and "camera is disabled tonight" in why
    assert ("t1", "img2", "unsupported") in plan.history
    assert {e.detail["spectrum"] for e in events(rig, "spectrum_done")} == {"rv1", "rv2"}


async def test_a_cooler_that_comes_back_after_the_powercycle_keeps_the_camera(make_rig, tmp_path):
    """The power cycle brings the cooler back: the CCD reaches its setpoint
    after the cycle, the ladder counts it recovered, and imaging goes on on
    t1."""
    plan = MixedPlan([image("img1"), image("img2")])
    rig = rig_with(make_rig, tmp_path, plan, fixes=True, settle_timeout=120)
    await run_to_end(rig, timeout=20000)
    cam = rig.cams[0]
    unit = rig.sequencer.units["t1"]
    [done] = events(rig, "recovery_done")
    assert done.detail["recovered"]
    assert [t.split(":")[0] for t in done.detail["tried"]] == ["reconnect", "powercycle"]
    assert done.detail["tried"][1] == "powercycle: ok"
    assert events(rig, "cooling_recovered")
    assert unit.camera_disabled is None and not events(rig, "camera_disabled")
    assert ("t1", "img1", "complete") in plan.history
    assert ("t1", "img2", "complete") in plan.history
    assert cam.exposures_started == 2 and cam.CCDTemperature == -50.0
    assert [c for c in plan.calls if not c[1]] == []


async def test_without_a_camera_ladder_the_camera_is_disabled_at_once(make_rig, tmp_path):
    plan = MixedPlan(T1_NIGHT)
    rig = rig_with(make_rig, tmp_path, plan, ladder=False, settle_timeout=120)
    await run_to_end(rig, timeout=20000)
    unit = rig.sequencer.units["t1"]
    assert not events(rig, "recovery_start") and rig.pdu.cycles == []
    [off] = events(rig, "camera_disabled")
    assert "no camera recovery ladder" in off.detail["reason"]
    assert unit.camera_disabled and not unit.gave_up
    assert rig.cams[0].exposures_started == 0
    assert {e.detail["spectrum"] for e in events(rig, "spectrum_done")} == {"rv1", "rv2"}
    assert ("t2", "img2", "complete") in plan.history


async def test_not_settled_warn_keeps_the_old_behaviour(make_rig, tmp_path):
    """``not_settled: "warn"``: a warning, and the frames are taken."""
    plan = MixedPlan([image("img1")])
    rig = rig_with(make_rig, tmp_path, plan, not_settled="warn", settle_timeout=120)
    await run_to_end(rig, timeout=20000)
    assert events(rig, "cooling_not_settled") and not events(rig, "cooling_fault")
    assert rig.cams[0].exposures_started == 1
    assert rig.sequencer.units["t1"].camera_disabled is None


class NoHeater(FakeCamera):
    """A thermoelectric cooler: it cannot warm the CCD above the ambient."""

    def _target(self) -> float:
        return min(super()._target(), self.ambient)


@pytest.mark.parametrize(
    "case", ["unsupported", "warm setpoint reached", "colder than a warm setpoint"]
)
async def test_cameras_that_are_not_cooled_to_their_setpoint_still_work(make_rig, tmp_path, case):
    """Only a commanded setpoint not reached (too warm, cooler on) is a
    fault: a camera that cannot cool (``cooling_unsupported``), a warm
    setpoint the CCD reaches (MINERVA T2: 7.3 C), and a CCD colder than a
    warm setpoint (a cold night) all go on exposing."""
    from robofast.owner import DeviceOwner

    plan = MixedPlan([image("img1"), image("img2")])
    rig = rv_rig(make_rig, tmp_path, plan)
    unit = rig.sequencer.units["t1"]
    cam = NoHeater()
    cam.ambient = 15.0
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    setpoint = -50.0 if case == "unsupported" else 7.3
    if case == "unsupported":
        cam.can_cool = False
    elif case == "colder than a warm setpoint":
        cam.ambient = 0.0
    cam.CCDTemperature = cam.ambient
    unit.cooling = CameraCooling(
        "t1",
        CoolingConfig.from_dict({"setpoint": setpoint, "tolerance": 0.75, "settle_timeout": 60}),
    )
    await run_to_end(rig, timeout=20000)
    assert not events(rig, "cooling_fault") and not events(rig, "camera_disabled")
    assert cam.exposures_started == 2
    assert ("t1", "img2", "complete") in plan.history
    if case == "unsupported":
        assert events(rig, "cooling_unsupported")
    elif case == "colder than a warm setpoint":
        assert events(rig, "cooling_not_settled")  # a warning, as before


# -- the pieces -------------------------------------------------------------------------------


def test_config_keys():
    c = CoolingConfig.from_dict(T4_COOLING)
    assert c.not_settled == "fault" and c.recovery_settle == 900
    c = CoolingConfig.from_dict({**T4_COOLING, "recovery_settle_timeout": 600})
    assert c.recovery_settle == 600
    for bad, why in (
        ({"not_settled": "ignore"}, "not_settled"),
        ({"recovery_settle_timeout": -1}, "recovery_settle_timeout"),
        ({"recovery_settle_timeout": "x"}, "a number"),
    ):
        with pytest.raises(ValueError, match=why):
            CoolingConfig.from_dict({**T4_COOLING, **bad})


async def test_check_faults_only_past_the_settle_time_and_too_warm():
    cam = FakeCamera()
    cam.ambient = cam.coldest = STUCK
    now = [0.0]
    c = CameraCooling(
        "t4", CoolingConfig.from_dict({**T4_COOLING, "poll": 1}), clock=lambda: now[0]
    )
    await c.apply(cam, "test")
    # Within the settle time a dark frame does not wait and does not fault.
    st = await c.check(cam, wait=False)
    assert st is not None and not st.settled
    now[0] = 900.0
    with pytest.raises(CoolingFault, match=r"t4 camera cooling fault: CCD at 20\.7 C") as exc:
        await c.check(cam, wait=False)
    assert is_cooling_fault(repr(exc.value))
    # The cooler off (found off, rate-limited): not this fault.
    c.restarted_at = now[0]
    cam.CoolerOn = False
    st = await c.check(cam, wait=False)
    assert st is not None and st.cooler_on is False


async def test_settle_sets_the_cooler_and_waits_for_the_setpoint():
    cam = FakeCamera()
    cam.cooling_rate = 1.0  # C/s
    cam.CCDTemperature = 0.0
    cam.CoolerOn = False
    events = []
    c = CameraCooling(
        "t4",
        CoolingConfig.from_dict({**T4_COOLING, "poll": 5, "recovery_settle_timeout": 120}),
    )
    c.event = lambda what, **kw: events.append(what)
    assert await c.settle(cam, "recovery") <= -49.25
    assert cam.CoolerOn and cam.SetCCDTemperature == -50.0
    assert events == ["cooling_set", "cooling_recovered"]
    cam.coldest = -20.0  # no longer gets there
    cam.CCDTemperature = 0.0
    with pytest.raises(CoolingFault, match="120 s after"):
        await c.settle(cam, "recovery")


# -- the dispatch scheduler -------------------------------------------------------------------


async def test_dispatch_schedules_no_imaging_on_a_disabled_camera():
    """Told t1's camera is disabled, the scheduler gives t1 only what needs
    no camera (the spectrum); the imaging goes to t2, or waits for t1 when it
    is only for t1 -- never refused (FAILED)."""
    spec = group("rv", instrument="spectrograph")
    sc = spec["requests"][0]["configurations"][0]
    sc["type"] = "SPECTRUM"
    sc["instrument_configs"] = [{"exposure_time": 60}]
    limits = {
        "t1": tlimits(instruments=["imager", "spectrograph"]),
        "t2": tlimits(instruments=["imager"]),
    }
    d = make(
        [group("img", priority=10.0), group("t1img", telescopes=["t1"]), spec],
        limits=limits,
        instruments={"spectrograph": {"type": "kiwispec"}},
    )
    assert {c.request.id for c in d.evaluate("t1") if c.ok} == {"img-r", "t1img-r", "rv-r"}
    d.set_camera_available("t1", False, "camera cooling fault: CCD at 20.7 C")
    ok = {c.request.id: c for c in d.evaluate("t1")}
    assert ok["rv-r"].ok
    for rid in ("img-r", "t1img-r"):
        assert not ok[rid].ok
        assert any("t1's camera is disabled (camera cooling fault" in r for r in ok[rid].reasons)
    assert (await d.next_observation("t1")).kind == "spectrum"
    assert {c.request.id for c in d.evaluate("t2") if c.ok} >= {"img-r"}
    for r in d.requests.values():
        assert all(c.status.schedulable for c in r.configurations)  # waiting, not refused
    assert not d.refused
    # Available again (the next night): t1 takes imaging again.
    d.set_camera_available("t1", True)
    assert {c.request.id: c for c in d.evaluate("t1")}["t1img-r"].ok
