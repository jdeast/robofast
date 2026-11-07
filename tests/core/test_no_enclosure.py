"""A telescope with no enclosure (``"enclosure": "none"``; robofast.safety,
"No enclosure"): a classroom telescope on a tripod, with no dome and no
safety monitor, observes only while an operator is on duty, and parks when
the lease goes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal

import httpx
import pytest

from robofast import DeviceOwner, Observation, Sequencer, SequencerConfig, Timeline
from robofast.modes import modes_from_config
from robofast.safety import NO_ENCLOSURE_TEXT, SafetyConfig, SafetySupervisor
from robofast.supervisor_service import build
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.checks import await_until, wait_until
from robofast.testing.fakes import FakeCamera, FakeDome, FakeTelescope
from robofast.testing.processes import SequencerProcess, SupervisorProcess

from .conftest import FAST_REMOTE, FAST_SAFETY, FAST_SEQ

TEL = "127.0.0.1:9/0"

#: The fast timings, without FAST_SAFETY's opt-in to running unattended
#: (which needs an enclosure, so it is refused here).
SAFETY = {k: v for k, v in FAST_SAFETY.items() if k != "allow_unattended_without_watchdog"}


def _config(**changes):
    config = {
        "name": "classroom",
        "enclosure": "none",
        "telescopes": {"t1": {"telescope": TEL, "camera": TEL}},
    }
    config.update(changes)
    return config


def test_build_without_dome_or_safety_monitor():
    sup = build(_config(), Timeline())
    assert sup.enclosure is False
    assert sup.mode == "attended" and "shadow" not in sup.modes.modes
    status = sup.status()
    assert status["reason"] == NO_ENCLOSURE_TEXT
    assert status["duty"]["attended_only"] is True
    assert status["open_allowed"] is False  # nobody on duty


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"enclosure": "tripod"}, "enclosure: 'tripod'"),
        ({"enclosure": None}, "missing required key(s) devices"),
        ({"devices": {"dome": TEL}}, "devices.dome given"),
        (
            {"devices": {"safetymonitor": TEL, "observingconditions": TEL}},
            "devices.safetymonitor, devices.observingconditions given",
        ),
        ({"telescopes": {}}, "name at least one telescope"),
        ({"safety": {"park_before_close": True}}, "park_before_close needs an enclosure"),
        ({"safety": {"fault_action": "Faults"}}, "fault_action (a fault channel) needs"),
        (
            {"safety": {"allow_unattended_without_watchdog": True}},
            "allow_unattended_without_watchdog needs an enclosure",
        ),
        ({"modes": {"watch": {"like": "shadow"}}}, "enclosure 'watch' needs an enclosure"),
    ],
)
def test_a_half_written_config_names_what_is_wrong(changes, message):
    with pytest.raises(ValueError) as exc:
        build(_config(**changes), Timeline())
    assert message in str(exc.value)


def test_a_dome_without_a_safety_monitor_is_refused():
    tl = Timeline()
    dome = DeviceOwner("dome", lambda: FakeDome(), tl)
    with pytest.raises(ValueError, match="go together"):
        SafetySupervisor("x", dome=dome, safety_monitor=None, timeline=tl)


@pytest.fixture
async def classroom():
    """An in-process supervisor with no enclosure and a sequencer on a
    fake telescope and camera, observing one light frame after another."""
    tl = Timeline()
    tel, cam = FakeTelescope(), FakeCamera()
    tel_owner = DeviceOwner("t1.telescope", lambda: tel, tl, default_timeout=0.5)
    sup = SafetySupervisor(
        "classroom",
        dome=None,
        safety_monitor=None,
        telescopes=[tel_owner],
        timeline=tl,
        config=SafetyConfig(**SAFETY),
    )
    seq = Sequencer(
        "seq",
        supervisor=sup,
        telescope=tel_owner,
        camera=DeviceOwner("t1.camera", lambda: cam, tl, default_timeout=0.5),
        plan=lambda: Observation("target", exptime=0.3, count=1000),
        timeline=tl,
        config=SequencerConfig(**FAST_SEQ),
    )
    tasks = [asyncio.create_task(sup.run()), asyncio.create_task(seq.run())]
    yield sup, tel, cam
    for t in reversed(tasks):
        t.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(t, 10)


async def test_observes_only_with_an_operator_on_duty(classroom):
    sup, tel, cam = classroom
    await asyncio.sleep(1.0)
    assert sup.outstanding() is None and cam.exposures_started == 0 and tel._parked
    sup.duty_on("teacher", "first light")
    await await_until(lambda: cam.exposures_started >= 1, timeout=10)
    lease = sup.outstanding()
    assert lease is not None and lease.opened.is_set() and not tel._parked
    sup.duty_off("teacher", "class over")
    await await_until(lambda: tel._parked and cam._state == 0, timeout=10)
    assert lease.revoked.is_set()
    started = cam.exposures_started
    await asyncio.sleep(1.0)
    assert cam.exposures_started == started  # nothing more without an operator


async def test_an_expired_duty_parks(classroom):
    sup, tel, cam = classroom
    sup.config.duty_ttl, sup.config.duty_grace = 1.0, 0.5
    sup.duty_on("teacher")
    await await_until(lambda: cam.exposures_started >= 1, timeout=10)
    await await_until(lambda: tel._parked, timeout=10)  # not renewed: revoked, parked
    assert sup.critical()  # the duty-expired alarm


async def test_first_exposure_with_only_a_telescope_and_a_camera(tmp_path):
    """The classroom: a mount and a camera, nothing else; one bias, as
    robofast-discover's starter config plans it."""
    tel, cam = FakeTelescope(), FakeCamera()
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    a = server.address
    config = {
        "name": "classroom",
        "enclosure": "none",
        "listen": "127.0.0.1:0",
        "telescopes": {"t1": {"telescope": f"{a}/0", "camera": f"{a}/0"}},
        "images": {"data_root": "data", "site": "classroom"},
        "plan": {"type": "calibration", "biases": 1},
        "safety": SAFETY,
    }
    services = []
    try:
        sup = SupervisorProcess(config, tmp_path)
        services.append(sup)
        config.update(supervisor_url=sup.url, sequencer=FAST_SEQ, lease_client=FAST_REMOTE)
        services.append(SequencerProcess(config, tmp_path))
        frames = tmp_path / "data" / "classroom" / "t1"
        await wait_until(lambda: bool(list(frames.glob("n*/*.bias.0001.fits"))), timeout=60)
        assert cam.exposures_started == 1
    finally:
        for s in services:
            s.stop()
        server.stop()


def test_the_cli_names_a_half_written_config(tmp_path, capsys):
    from robofast.supervisor_service import main

    path = tmp_path / "classroom.json"
    path.write_text(json.dumps(_config(devices={"dome": TEL})))
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(path)])
    assert exc.value.code == 2
    assert "devices.dome given" in capsys.readouterr().err


def _bare(tl, **kw):
    tel = FakeTelescope()
    t1 = DeviceOwner("t1.telescope", lambda: tel, tl, default_timeout=0.5)
    args = dict(dome=None, safety_monitor=None, telescopes=[t1], timeline=tl)
    args.update(kw)
    return SafetySupervisor("classroom", **args)


@pytest.mark.parametrize(
    "modes",
    [
        # No attended floor: robotic offered, granting with nobody on duty.
        lambda: modes_from_config({}, attended_floor=False, fault_action=False, enclosure=False),
        # A duty that expires into an alarm only: open with nobody on duty.
        lambda: modes_from_config(
            {"modes": {"lax": {"like": "attended", "duty_expiry": "alarm"}}},
            attended_floor=False,
            fault_action=False,
            enclosure=False,
        ),
    ],
)
def test_no_mode_may_observe_with_nobody_on_duty(modes):
    with pytest.raises(ValueError, match="may observe with nobody on duty"):
        _bare(Timeline(), modes=modes())


def test_the_constructor_refuses_what_has_no_use_without_an_enclosure():
    tl = Timeline()
    oc = DeviceOwner("oc", lambda: FakeTelescope(), tl)
    with pytest.raises(ValueError, match="no observing conditions"):
        _bare(tl, observing_conditions=oc)
    with pytest.raises(ValueError, match="name the telescopes"):
        _bare(tl, telescopes=())


async def test_a_killed_supervisor_parks_the_telescope(tmp_path):
    """Nothing can close, so a supervisor that dies must not leave the
    telescope observing: the sequencer's lease lapses and it parks."""
    tel, cam = FakeTelescope(), FakeCamera()
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    a = server.address
    config = {
        "name": "classroom",
        "enclosure": "none",
        "listen": "127.0.0.1:0",
        "telescopes": {"t1": {"telescope": f"{a}/0", "camera": f"{a}/0"}},
        "safety": SAFETY,
    }
    services = []
    try:
        sup = SupervisorProcess(config, tmp_path)
        services.append(sup)
        async with httpx.AsyncClient(trust_env=False) as client:
            r = await client.post(f"{sup.url}/duty", json={"operator": "teacher", "op": "on"})
            r.raise_for_status()
        config.update(
            sequencer_name="seq",
            supervisor_url=sup.url,
            sequencer=FAST_SEQ,
            lease_client=FAST_REMOTE,
            plan={"type": "static", "targets": [{"name": "target", "exptime": 30, "count": 1000}]},
        )
        services.append(SequencerProcess(config, tmp_path))
        await wait_until(lambda: cam.exposures_started >= 1 and not tel._parked, timeout=30)
        sup.signal(signal.SIGKILL)
        t = await wait_until(lambda: tel._parked and cam._state == 0, timeout=15)
        assert t < 10  # lease_ttl 1 s, the client's timeout, then the park
    finally:
        for s in services:
            s.stop()
        server.stop()


async def test_a_dead_sequencer_leaves_no_telescope_unparked(tmp_path):
    """Nothing can close, so a sequencer that dies mid-exposure must not
    leave the telescope tracking: the supervisor revokes its lease (not
    renewed) and, when it is not released, parks the telescope itself."""
    tel, cam = FakeTelescope(), FakeCamera()
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    a = server.address
    config = {
        "name": "classroom",
        "enclosure": "none",
        "listen": "127.0.0.1:0",
        "telescopes": {"t1": {"telescope": f"{a}/0", "camera": f"{a}/0"}},
        "safety": {**SAFETY, "park_wait": 4.0},
    }
    services = []
    try:
        sup = SupervisorProcess(config, tmp_path)
        services.append(sup)
        async with httpx.AsyncClient(trust_env=False) as client:
            r = await client.post(f"{sup.url}/duty", json={"operator": "teacher", "op": "on"})
            r.raise_for_status()
        config.update(
            sequencer_name="seq",
            supervisor_url=sup.url,
            sequencer=FAST_SEQ,
            lease_client=FAST_REMOTE,
            plan={"type": "static", "targets": [{"name": "target", "exptime": 30, "count": 1000}]},
        )
        seq = SequencerProcess(config, tmp_path)
        services.append(seq)
        await wait_until(lambda: cam.exposures_started >= 1 and not tel._parked, timeout=30)
        seq.signal(signal.SIGKILL)
        # lease_ttl (1 s) to revoke, park_wait / 2 for its cleanup, then the park
        t = await wait_until(lambda: tel._parked, timeout=15)
        assert t < 1.0 + 4.0 + 5.0, t
    finally:
        for s in services:
            s.stop()
        server.stop()
