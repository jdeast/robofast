"""The self-correcting mount calibration (robofast.guider.selfcal;
docs/guider.md, "Self-correcting mount calibration").

The simulator's truth is a mount matrix from a PA (``SimWorld.pa``); the
guider is given one from a different PA, as when the rotator angle is
poorly known (legacy MINERVA, Dec 2025 - Apr 2026: 57% of spectra hit the
750 s acquire timeout, mostly from a rotated calibration)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from robofast.guider.base import VirtualClock
from robofast.guider.calibration import Calibration, CalibrationStore, sky_matrix
from robofast.guider.loop import GuideConfig, Guider
from robofast.guider.selfcal import SelfCal, SelfCalConfig, estimate
from robofast.guider.service import GuiderRemote, GuiderService
from robofast.guider.sim import Jitter, SimCamera, SimMount, SimWorld
from robofast.guider.tiptilt import MountOffset
from robofast.registry import ValueRegistry

TARGET = (320.0, 240.0)
PA = 30.0
#: Where each acquisition starts: 216 px from the target (108" at 0.5"/px).
START = np.array([500.0, 360.0])


def _matrix(pa: float, parity: int = 1) -> np.ndarray:
    return -np.linalg.inv(sky_matrix(0.5, pa, parity))


def _rig(
    clock,
    *,
    true_pa: float,
    true_parity: int = 1,
    selfcal=True,
    iterations: int = 6,
    seeing: bool = True,
):
    """The guider's mount matrix is from ``PA``; the sky's from ``true_pa``.
    The mount is the fine device (no tip/tilt): seeded 1 px rms seeing."""
    world = SimWorld(
        clock=clock,
        star=tuple(START),
        pa=true_pa,
        parity=true_parity,
        jitter=Jitter(rms=1.0, knee=1.0, seed=11) if seeing else None,
        seed=4,
    )
    sm = SimMount(clock=clock, tau=0.3, delay=0.05)
    world.mount = sm
    mount = MountOffset(sm, max_offset=200.0)
    mount.clock = clock
    cfg = GuideConfig(
        target=TARGET,
        acq_exposure=0.2,
        coarse_settle=1.5,
        coarse_iterations=iterations,
        max_reacquire=0,
        exposure=0.1,
        scope="t2",
        mount_selfcal=selfcal,
    )
    cal = Calibration("mount", "fau", _matrix(PA).tolist(), units="arcsec", plate_scale=0.5, pa=PA)
    g = Guider(SimCamera(world), cfg, mount=mount, mount_cal=cal, clock=clock)
    return g, world


def _put_star(world: SimWorld, where) -> None:
    """Put the true star at pixel ``where`` (whatever the mount's offsets)."""
    t = world.clock.now()
    world.star = tuple(np.asarray(where, float) - world.mount_matrix @ world.mount.position_at(t))


async def _acquire(g: Guider, world: SimWorld, where=START) -> str:
    _put_star(world, where)
    g.start()
    state = await g.wait_state(("guiding", "failed"), 200)
    await g.stop()
    return state


def _events(g: Guider, kind: str) -> list[dict]:
    return [e for e in g.events if e["kind"] == kind]


# -- the estimate ---------------------------------------------------------------------


def test_one_move_measures_the_rotation_and_two_tell_a_mirror():
    m = _matrix(PA)
    truth = _matrix(PA + 25.0)
    rng = np.random.default_rng(1)
    u1 = np.array([60.0, -40.0])
    d1 = truth @ u1 + rng.normal(0, 1.0, 2)
    e = estimate(m, [_pair(u1, d1)])
    assert e is not None and e.theta == pytest.approx(-25.0, abs=1.5)
    assert e.scale == pytest.approx(1.0, abs=0.02)
    # A mirror: one move cannot tell it from a rotation, two can.
    mirror = _matrix(PA, -1)
    sc = SelfCal(SelfCalConfig())
    sc.begin()
    assert sc.add(m, u1, mirror @ u1) is None
    assert not sc.parity_flipped()
    assert sc.add(m, (10.0, 70.0), mirror @ np.array([10.0, 70.0])) is None
    assert sc.parity_flipped()


def test_badly_measured_moves_are_not_used():
    m = _matrix(PA)
    sc = SelfCal(SelfCalConfig(min_move=15.0))
    sc.begin()
    assert "move" in sc.add(m, (2.0, 1.0), m @ np.array([2.0, 1.0]))  # 4.5 px: noise
    assert "another star" in sc.add(m, (60.0, 0.0), 5 * (m @ np.array([60.0, 0.0])))
    assert sc.add(m, (60.0, 0.0), m @ np.array([60.0, 0.0]), saturated=True) == "star saturated"
    assert sc.add(m, (60.0, 0.0), m @ np.array([60.0, 0.0]), clipped=True) == "move clipped"
    assert sc.current is None and sc.rejected == 4


def _pair(u, d):
    from robofast.guider.selfcal import Pair

    return Pair(tuple(u), tuple(d))


# -- during an acquisition ------------------------------------------------------------


@pytest.mark.parametrize("error", [25.0, 40.0])
async def test_regression_20261125_a_rotated_calibration_is_corrected_during_acquisition(error):
    """Legacy MINERVA: a rotator angle known to tens of degrees makes the
    star spiral in or walk away. The first coarse move measures the
    rotation; the rest of the acquisition uses the corrected matrix."""
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA + error)
    assert await _acquire(g, world) == "guiding", g.reason
    [ev] = _events(g, "mount_cal_corrected")
    # The truth is the stored matrix rotated by -error (parity +1).
    assert ev["theta_deg"] == pytest.approx(-error, abs=1.5)
    assert ev["pairs"] >= 1
    assert ev["pa"]["pa_change_deg"] == pytest.approx(error, abs=1.5)
    assert ev["pa"]["rotcal_offset_change_deg"] == pytest.approx(error, abs=1.5)
    assert len(g.mount.sent) <= 3  # a couple of moves, not coarse_iterations
    assert "not_converging" not in [a.kind for a in g.alarms]
    # Above alarm_deg (20): "run the full calibration".
    assert "mount_cal_suspect" in [a.kind for a in g.alarms]
    assert g.mount_cal_suspect is not None
    assert g.status()["mount_cal"]["estimate"]["pairs"] >= 1


async def test_regression_20261125_without_the_correction_it_does_not_converge():
    """The same 40 deg error with the legacy behaviour (``mount_selfcal``
    off): the coarse iterations run out (or not_converging)."""
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA + 40.0, selfcal=False)
    assert await _acquire(g, world) == "failed"
    assert g.reason.startswith("acquisition failed")
    assert len(g.mount.sent) >= 6
    assert not g.events


async def test_a_sign_error_is_a_180_degree_rotation_and_is_corrected():
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA)
    g.mount_cal = Calibration("mount", "fau", (-g.mount_cal.m).tolist())
    assert await _acquire(g, world, where=(380.0, 200.0)) == "guiding", g.reason
    [ev] = _events(g, "mount_cal_corrected")
    assert abs(ev["theta_deg"]) == pytest.approx(180.0, abs=2.0)


async def test_a_small_error_changes_nothing():
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA + 2.0)
    for _ in range(3):
        assert await _acquire(g, world) == "guiding", g.reason
    assert not g.events and not g.alarms
    assert g.mount_cal_suspect is None
    assert g.mount_cal.matrix == _matrix(PA).tolist()
    assert g.selfcal.persisted == 0


async def test_a_parity_flip_alarms_and_fails_rather_than_being_corrected():
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA, true_parity=-1)
    assert await _acquire(g, world, where=(400.0, 300.0)) == "failed"
    kinds = [a.kind for a in g.alarms]
    assert "mount_cal_parity" in kinds and "not_converging" in kinds
    assert "mirrored" in g.reason
    assert g.mount_cal_suspect is not None and "parity" in g.mount_cal_suspect
    assert g.mount_cal.matrix == _matrix(PA).tolist()  # never "fixed"
    assert g.selfcal.persisted == 0


# -- across acquisitions --------------------------------------------------------------


async def test_three_consistent_acquisitions_persist_with_provenance(tmp_path):
    """T4 in April 2026: a stale calibration -15 deg off every night."""
    clock = VirtualClock()
    store = CalibrationStore(tmp_path)
    g, world = _rig(clock, true_pa=PA + 15.0)
    g.on_mount_cal_persist = store.save
    for k in range(2):
        assert await _acquire(g, world) == "guiding", g.reason
        assert store.load("mount", "fau") is None  # not yet
        assert g.mount_cal_suspect is not None and f"{k + 1} of 3" in g.mount_cal_suspect
    assert await _acquire(g, world) == "guiding", g.reason
    saved = store.load("mount", "fau")
    assert saved is not None
    prov = saved.extra["online"]
    assert prov["source"] == "online" and prov["acquisitions"] == 3 and prov["pairs"] >= 3
    assert prov["theta_deg"] == pytest.approx(-15.0, abs=1.0)
    assert prov["time"] and prov["previous_matrix"] == _matrix(PA).tolist()
    assert saved.pa == pytest.approx(PA + 15.0, abs=1.0)
    assert np.allclose(saved.m, world.mount_matrix, atol=0.03 * np.abs(world.mount_matrix).max())
    assert g.mount_cal.matrix == saved.matrix
    assert g.mount_cal_suspect is None
    [ev] = _events(g, "mount_cal_persisted")
    assert ev["acquisitions"] == 3
    # The next acquisition needs no correction.
    n = len(_events(g, "mount_cal_corrected"))
    assert await _acquire(g, world) == "guiding"
    assert len(_events(g, "mount_cal_corrected")) == n


async def test_a_single_outlier_does_not_persist():
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA + 30.0)
    persisted: list[Calibration] = []
    g.on_mount_cal_persist = persisted.append
    assert await _acquire(g, world) == "guiding"
    world.pa = PA + 12.0  # one acquisition disagrees
    assert await _acquire(g, world) == "guiding"
    world.pa = PA + 30.0
    assert await _acquire(g, world) == "guiding"
    assert not persisted
    assert any(a.kind == "mount_cal_suspect" and "unstable" in a.message for a in g.alarms)
    assert g.mount_cal.matrix == _matrix(PA).tolist()
    # A storeless outlier: an acquisition that agrees with the store resets
    # the run, so one error never accumulates towards a save.
    world.pa = PA
    assert await _acquire(g, world) == "guiding"
    assert g.mount_cal_suspect is None and not g.selfcal.history
    assert not persisted


async def test_the_suspect_flag_is_set_and_cleared():
    clock = VirtualClock()
    registry = ValueRegistry()
    g, world = _rig(clock, true_pa=PA + 25.0)
    g.registry = registry
    assert g.mount_cal_suspect is None
    assert await _acquire(g, world) == "guiding"
    assert g.status()["mount_cal_suspect"] is True
    assert registry.get("t2.guide_cal_suspect").value is True
    # The rotator is put right: the next acquisition agrees with the store.
    world.pa = PA
    assert await _acquire(g, world) == "guiding"
    assert g.status()["mount_cal_suspect"] is False
    g._publish(force=True)
    assert registry.get("t2.guide_cal_suspect").value is False
    # not_converging sets it; a new calibration clears it.
    g.selfcal.mark("acquisition not converging (40.0 -> 60.0 px)")
    assert g.mount_cal_suspect is not None
    g.mount_cal = Calibration("mount", "fau", world.mount_matrix.tolist())
    assert g.mount_cal_suspect is None


async def test_not_converging_marks_the_calibration_suspect():
    clock = VirtualClock()
    g, world = _rig(clock, true_pa=PA, true_parity=-1, seeing=False)
    g.selfcal.config.min_move = 1e6  # no move counts: the plain not_converging path
    assert await _acquire(g, world, where=(400.0, 300.0)) == "failed"
    assert "not_converging" in [a.kind for a in g.alarms]
    assert g.mount_cal_suspect is not None and "not converging" in g.mount_cal_suspect


def test_config():
    assert SelfCalConfig.from_any(False) is None
    assert SelfCalConfig.from_any(True) == SelfCalConfig()
    assert SelfCalConfig.from_any({"correct_deg": 4.0}).correct_deg == 4.0
    with pytest.raises(ValueError):
        GuideConfig.from_dict({"mount_selfcal": {"bogus": 1}})
    with pytest.raises(ValueError):
        SelfCalConfig.from_any({"correct_deg": 30.0, "alarm_deg": 20.0})


# -- reaching the sequencer -----------------------------------------------------------


def test_the_remote_forwards_events_alarms_and_the_flag_once():
    remote = GuiderRemote("http://127.0.0.1:9", scope="t2", registry=ValueRegistry())
    events: list[tuple[str, dict]] = []
    alarms: list[str] = []
    remote.on_event = lambda what, **kw: events.append((what, kw))
    remote.on_alarm = alarms.append
    st = {
        "state": "guiding",
        "events": [{"kind": "mount_cal_corrected", "t": 5.0, "utc": "x", "theta_deg": -30.0}],
        "alarms": [{"kind": "mount_cal_suspect", "t": 5.0, "message": "run the calibration"}],
        "mount_cal": {"suspect": True, "suspect_reason": "off by -30 deg"},
    }
    remote._forward(st)
    remote._forward(st)
    remote._publish(st)
    assert events == [("mount_cal_corrected", {"theta_deg": -30.0})]
    assert alarms == ["guider t2: mount_cal_suspect: run the calibration"]
    assert remote.cal_suspect == "off by -30 deg"
    assert remote.registry.get("t2.guide_cal_suspect").value is True


async def test_the_sequencer_puts_guider_events_on_its_timeline(make_rig):
    from robofast.plan import StaticPlan

    rig = make_rig(plan=StaticPlan([{"name": "x", "exptime": 0.1, "count": 1}], ["t1"]))
    unit = rig.sequencer.units["t1"]
    unit.guider = GuiderRemote("http://127.0.0.1:9", scope="t1")
    rig.sequencer._wire_pointing()
    unit.guider.on_event("mount_cal_corrected", message="rotated", theta_deg=-30.0)
    [ev] = rig.timeline.events("mount_cal_corrected")
    assert ev.detail["telescope"] == "t1" and ev.detail["theta_deg"] == -30.0


def test_the_service_saves_a_persisted_calibration(tmp_path):
    from robofast.guider.service import GuiderServiceConfig

    cfg = GuiderServiceConfig.from_dict(
        {
            "name": "t2-guider",
            "scope": "t2",
            "listen": "127.0.0.1:0",
            "port": "fau",
            "calibration_dir": str(tmp_path / "cal"),
            "camera": {"kind": "sim"},
            "mount": {"kind": "sim"},
            "guide": {"target": [320.0, 240.0], "plate_scale": 0.5},
            "calibrations": {"mount": {"matrix": _matrix(PA).tolist()}},
        }
    )
    svc = GuiderService(cfg)
    assert svc.guider.on_mount_cal_persist == svc._persist_mount_cal
    m = _matrix(PA + 10.0).tolist()
    svc.guider.on_mount_cal_persist(
        Calibration("simmount", "fau", m, extra={"online": {"source": "online"}})
    )
    got = svc.store.load("simmount", "fau")
    assert got is not None and got.extra["online"]["source"] == "online"
    assert math.isclose(got.m[0, 0], m[0][0])
