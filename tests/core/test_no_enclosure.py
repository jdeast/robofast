"""A telescope with no enclosure (docs/no_enclosure.md): the sky as the
dome and the operator's duty as the safety monitor
(robofast.devices.no_enclosure_server), and the supervisor's one path.

A classroom telescope on a tripod observes only while an operator is on
duty, and parks when the lease goes. Adapted from Guven Gurkan's tests in
PR #125."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal

import httpx
import pytest

from robofast import DeviceOwner, Observation, Sequencer, SequencerConfig, Timeline
from robofast.alpaca import alpaca_owner
from robofast.devices.no_enclosure_server import NoEnclosureConfig, NoEnclosureServer
from robofast.safety import ENCLOSURE_MEMBERS, SafetyConfig, SafetySupervisor, Shutter
from robofast.supervisor_service import build
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.checks import await_until, wait_until
from robofast.testing.fakes import FakeCamera, FakeTelescope
from robofast.testing.processes import SequencerProcess, Service, SupervisorProcess

from .conftest import FAST_REMOTE, FAST_SAFETY, FAST_SEQ

TEL = "127.0.0.1:9/0"
SKY = "127.0.0.1:10/0"

#: The fast timings, without FAST_SAFETY's opt-in to running unattended
#: (refused with no enclosure), and the park-before-close a site with no
#: enclosure must have.
SAFETY = {
    **{k: v for k, v in FAST_SAFETY.items() if k != "allow_unattended_without_watchdog"},
    "park_before_close": True,
    "require_park_before_close": True,
    "park_wait": 4.0,
}


def _config(**changes):
    config = {
        "name": "classroom",
        "enclosure": "none",
        "devices": {"dome": SKY, "safetymonitor": SKY},
        "telescopes": {"t1": {"telescope": TEL, "camera": TEL}},
        "safety": dict(SAFETY),
    }
    config.update(changes)
    return config


# -- the devices ------------------------------------------------------------------


class Duty:
    """A duty source the test sets."""

    def __init__(self) -> None:
        self.on: bool | Exception = False

    def __call__(self) -> tuple[bool, str]:
        if isinstance(self.on, Exception):
            raise self.on
        return bool(self.on), "teacher" if self.on else "nobody"


@pytest.fixture
def sky():
    duty = Duty()
    cfg = NoEnclosureConfig(
        name="classroom", listen="127.0.0.1:0", supervisor="-", poll_interval=0.05, stale_after=0.5
    )
    server = NoEnclosureServer(cfg, duty=duty).start()
    yield server, duty
    server.stop()


async def test_the_sky_follows_the_command(sky):
    server, _ = sky
    tl = Timeline()
    dome = alpaca_owner("dome", "dome", server.address, tl, privileged_members=ENCLOSURE_MEMBERS)
    g = dome.grant("safety", privileged=True)
    assert await g.get("ShutterStatus") == Shutter.CLOSED  # closed at start
    await g.call("OpenShutter")
    assert await g.get("ShutterStatus") == Shutter.OPEN
    await g.call("CloseShutter")
    assert await g.get("ShutterStatus") == Shutter.CLOSED
    assert await g.get("Slewing") is False


async def test_safe_only_while_an_operator_is_on_duty(sky):
    server, duty = sky
    sm = alpaca_owner("sm", "safetymonitor", server.address, Timeline()).grant("t")
    assert await sm.get("IsSafe") is False
    duty.on = True
    await await_until(lambda: server.duty.safe, timeout=5)
    assert await sm.get("IsSafe") is True
    status = json.loads(await sm.call("Action", "Status", ""))
    assert status["safe"] is True and "teacher" in status["reason"]
    duty.on = False
    await await_until(lambda: not server.duty.safe, timeout=5)
    assert await sm.get("IsSafe") is False


async def test_unsafe_when_the_supervisor_stops_answering(sky):
    server, duty = sky
    duty.on = True
    await await_until(lambda: server.duty.safe, timeout=5)
    duty.on = ConnectionError("supervisor gone")
    await await_until(lambda: not server.duty.safe, timeout=5)  # stale_after 0.5 s
    assert "no answer" in server.duty.status()["reason"]


# -- the supervisor's config ------------------------------------------------------


def test_build_with_the_sky_and_operator_duty():
    sup = build(_config(), Timeline())
    assert sup.mode == "attended" and "shadow" not in sup.modes.modes
    status = sup.status()
    assert status["duty"]["attended_only"] is True
    assert status["open_allowed"] is False  # nobody on duty


def test_an_enclosure_config_is_unchanged():
    config = _config(enclosure=None, safety={**FAST_SAFETY})
    del config["enclosure"]
    sup = build(config, Timeline())
    assert "shadow" in sup.modes.modes


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"enclosure": "tripod"}, "enclosure: 'tripod'"),
        (
            {"devices": {"dome": SKY, "safetymonitor": SKY, "observingconditions": SKY}},
            "devices.observingconditions",
        ),
        ({"devices": {"dome": SKY, "safetymonitor": SKY, "heartbeat": SKY}}, "devices.heartbeat"),
        ({"telescopes": {}}, "telescopes: name at least one"),
        (
            {"safety": {**SAFETY, "park_before_close": False, "require_park_before_close": False}},
            "safety.park_before_close: must be true",
        ),
        (
            {"safety": {**SAFETY, "require_park_before_close": False}},
            "safety.require_park_before_close: must be true",
        ),
        (
            {"safety": {**SAFETY, "fault_action": "Faults"}},
            "safety.fault_action: needs an enclosure",
        ),
        (
            {"safety": {**SAFETY, "heartbeat_interval": 10, "heartbeat_timeout": 60}},
            "safety.heartbeat_interval: needs an enclosure",
        ),
        (
            {"safety": {**SAFETY, "allow_unattended_without_watchdog": True}},
            "safety.allow_unattended_without_watchdog",
        ),
        (
            {
                "safety": {
                    **SAFETY,
                    "attended_only": False,
                    "allow_unattended_without_watchdog": True,
                }
            },
            "safety.attended_only: must be true",
        ),
        ({"modes": {"watch": {"like": "shadow"}}}, "enclosure 'watch' needs an enclosure"),
        (
            {"modes": {"lax": {"like": "attended", "duty_expiry": "alarm"}}},
            "must not run unattended",
        ),
    ],
)
def test_a_half_written_config_names_what_is_wrong(changes, message):
    with pytest.raises(ValueError) as exc:
        build(_config(**changes), Timeline())
    assert message in str(exc.value)


def test_the_cli_names_a_half_written_config(tmp_path, capsys):
    from robofast.supervisor_service import main

    path = tmp_path / "classroom.json"
    path.write_text(
        json.dumps(_config(devices={"dome": SKY, "safetymonitor": SKY, "heartbeat": SKY}))
    )
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(path)])
    assert exc.value.code == 2
    assert "devices.heartbeat" in capsys.readouterr().err


# -- in process: the supervisor on the sky and the operator's duty ------------------


@contextlib.asynccontextmanager
async def _classroom(*, sequencer: bool = True, **safety):
    """An in-process supervisor on a :class:`NoEnclosureServer` (its duty
    read from the supervisor itself) and, with ``sequencer``, a sequencer
    on a fake telescope and camera observing one light frame after another."""
    tl = Timeline()
    tel, cam = FakeTelescope(), FakeCamera()
    holder: list[SafetySupervisor] = []

    def duty() -> tuple[bool, str]:
        who = holder[0].duty_status()["on_duty"] if holder else None
        return who is not None, f"on duty: {who}"

    cfg = NoEnclosureConfig(
        name="classroom", listen="127.0.0.1:0", supervisor="-", poll_interval=0.05, stale_after=0.5
    )
    server = NoEnclosureServer(cfg, duty=duty).start()
    tel_owner = DeviceOwner("t1.telescope", lambda: tel, tl, default_timeout=0.5)
    sup = SafetySupervisor(
        "classroom",
        dome=alpaca_owner("dome", "dome", server.address, tl, privileged_members=ENCLOSURE_MEMBERS),
        safety_monitor=alpaca_owner("sm", "safetymonitor", server.address, tl),
        telescopes=[tel_owner],
        timeline=tl,
        config=SafetyConfig(**{**SAFETY, **safety}),
    )
    holder.append(sup)
    tasks = [asyncio.create_task(sup.run())]
    if sequencer:
        seq = Sequencer(
            "seq",
            supervisor=sup,
            telescope=tel_owner,
            camera=DeviceOwner("t1.camera", lambda: cam, tl, default_timeout=0.5),
            plan=lambda: Observation("target", exptime=0.3, count=1000),
            timeline=tl,
            config=SequencerConfig(**FAST_SEQ),
        )
        tasks.append(asyncio.create_task(seq.run()))
    try:
        yield sup, server, tel, cam
    finally:
        for t in reversed(tasks):
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 10)
        server.stop()


async def test_observes_only_with_an_operator_on_duty():
    async with _classroom() as (sup, sky, tel, cam):
        await asyncio.sleep(1.0)
        assert sup.outstanding() is None and cam.exposures_started == 0 and tel._parked
        sup.duty_on("teacher", "first light")
        await await_until(lambda: cam.exposures_started >= 1, timeout=10)
        lease = sup.outstanding()
        assert lease is not None and lease.opened.is_set() and not tel._parked
        assert sky.shutter == Shutter.OPEN
        sup.duty_off("teacher", "class over")
        await await_until(lambda: tel._parked and cam._state == 0, timeout=10)
        await await_until(lambda: sky.shutter == Shutter.CLOSED, timeout=10)
        assert lease.revoked.is_set()
        started = cam.exposures_started
        await asyncio.sleep(1.0)
        assert cam.exposures_started == started  # nothing more without an operator


async def test_an_expired_duty_parks():
    async with _classroom(duty_ttl=1.0, duty_grace=0.5) as (sup, sky, tel, cam):
        sup.duty_on("teacher")

        def renewed() -> bool:  # the console page, until the first light frame
            sup.duty_renew("teacher")
            return cam.exposures_started >= 1

        await await_until(renewed, timeout=10)
        await await_until(lambda: tel._parked, timeout=10)  # not renewed: revoked, parked
        assert sup.critical()  # the duty-expired alarm
        await await_until(lambda: sky.shutter == Shutter.CLOSED, timeout=10)


async def _orphan(sup: SafetySupervisor, tel: FakeTelescope):
    """A lease granted, its telescope unparked, the sequencer dead (never
    renewed, never released)."""
    sup.duty_on("teacher", "first light")
    lease = await asyncio.wait_for(sup.request_open("seq"), 10)
    tel.Unpark()  # what the sequencer does next
    tel.Tracking = True
    return lease


async def test_a_dead_sequencers_telescope_is_parked_before_the_sky_closes():
    async with _classroom(sequencer=False) as (sup, sky, tel, _):
        lease = await _orphan(sup, tel)
        await await_until(lambda: sky.shutter == Shutter.OPEN, timeout=10)
        # Not renewed (lease_ttl 1 s): revoked; park_wait / 2 for its
        # cleanup, then the supervisor parks, then closes the sky.
        await await_until(lambda: lease.revoked.is_set(), timeout=5)
        await await_until(lambda: tel._parked and sky.shutter == Shutter.CLOSED, timeout=15)
        assert any("did not finish cleanup" in a for a in sup.alarms), list(sup.alarms)
        assert await sup.clear(lease.id, "teacher", "the telescope is parked")


async def test_every_orphaned_lease_is_parked_not_only_the_first():
    async with _classroom(sequencer=False) as (sup, sky, tel, _):
        for n in (1, 2):
            lease = await _orphan(sup, tel)
            await await_until(lambda lease=lease: lease.revoked.is_set(), timeout=5)
            await await_until(lambda: tel._parked and sky.shutter == Shutter.CLOSED, timeout=15)
            assert await sup.clear(lease.id, "teacher", "parked"), n
            assert sup.outstanding() is None


async def test_a_park_that_failed_is_tried_again_until_confirmed():
    """The mount does not answer when the supervisor parks for a dead
    sequencer: the interlock holds the sky open and the park is tried
    again on later passes until it is confirmed; only then does it close."""
    async with _classroom(sequencer=False, park_wait=1.0) as (sup, sky, tel, _):
        lease = await _orphan(sup, tel)
        await await_until(lambda: sky.shutter == Shutter.OPEN, timeout=10)
        tel.fail("mount unreachable")
        await await_until(lambda: lease.revoked.is_set(), timeout=5)
        await await_until(lambda: sup.status()["close_interlock"]["blocked"], timeout=10)
        await asyncio.sleep(1.5)
        assert sky.shutter == Shutter.OPEN and not tel._parked
        tel.unfail()
        await await_until(lambda: tel._parked and sky.shutter == Shutter.CLOSED, timeout=15)
        assert any("held back" in a for a in sup.alarms), list(sup.alarms)


async def test_going_off_duty_with_a_dead_sequencer_parks():
    async with _classroom(sequencer=False) as (sup, sky, tel, _):
        lease = await _orphan(sup, tel)
        await await_until(lambda: sky.shutter == Shutter.OPEN, timeout=10)
        sup.duty_off("teacher", "gone")
        assert lease.revoked.is_set()
        await await_until(lambda: tel._parked and sky.shutter == Shutter.CLOSED, timeout=15)


# -- as processes -----------------------------------------------------------------


class SkyProcess(Service):
    def __init__(self, workdir, supervisor: str) -> None:
        path = workdir / "sky.json"
        path.write_text(
            json.dumps(
                {
                    "name": "classroom",
                    "listen": "127.0.0.1:0",
                    "supervisor": supervisor,
                    "poll_interval": 0.1,
                    "stale_after": 0.5,
                }
            )
        )
        super().__init__(
            "robofast.devices.no_enclosure_server",
            "--config",
            str(path),
            log_path=workdir / "sky.log",
        )
        self.address = json.loads(self.first_line())["url"].removeprefix("http://")


@contextlib.contextmanager
def _processes(tmp_path, **extra):
    """A mount and a camera behind FakeAlpacaServer, the sky server and the
    supervisor as processes; yields (config, services, tel, cam, sup)."""
    tel, cam = FakeTelescope(), FakeCamera()
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    a = server.address
    port = _free_port()
    services: list[Service] = []
    try:
        sky = SkyProcess(tmp_path, f"http://127.0.0.1:{port}")
        services.append(sky)
        config = _config(
            listen=f"127.0.0.1:{port}",
            devices={"dome": f"{sky.address}/0", "safetymonitor": f"{sky.address}/0"},
            telescopes={"t1": {"telescope": f"{a}/0", "camera": f"{a}/0"}},
            **extra,
        )
        sup = SupervisorProcess(config, tmp_path)
        services.append(sup)
        config.update(supervisor_url=sup.url, sequencer=FAST_SEQ, lease_client=FAST_REMOTE)
        yield config, services, tel, cam, sup
    finally:
        for s in reversed(services):
            s.stop()
        server.stop()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _on_duty(url: str) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        r = await client.post(f"{url}/duty", json={"operator": "teacher", "op": "on"})
        r.raise_for_status()


async def test_first_exposure_with_only_a_telescope_and_a_camera(tmp_path):
    """The classroom: a mount, a camera and the sky server; one bias with
    nobody on duty (calibration frames need no lease)."""
    extra = {
        "images": {"data_root": "data", "site": "classroom"},
        "plan": {"type": "calibration", "biases": 1},
    }
    with _processes(tmp_path, **extra) as (config, services, _, cam, _sup):
        services.append(SequencerProcess(config, tmp_path))
        frames = tmp_path / "data" / "classroom" / "t1"
        await wait_until(lambda: bool(list(frames.glob("n*/*.bias.0001.fits"))), timeout=60)
        assert cam.exposures_started == 1


STATIC = {"type": "static", "targets": [{"name": "target", "exptime": 30, "count": 1000}]}


async def test_a_killed_supervisor_parks_the_telescope(tmp_path):
    """A supervisor that dies must not leave the telescope observing: the
    sequencer's lease lapses and it parks."""
    with _processes(tmp_path) as (config, services, tel, cam, sup):
        await _on_duty(sup.url)
        config.update(sequencer_name="seq", plan=STATIC)
        services.append(SequencerProcess(config, tmp_path))
        await wait_until(lambda: cam.exposures_started >= 1 and not tel._parked, timeout=30)
        sup.signal(signal.SIGKILL)
        t = await wait_until(lambda: tel._parked and cam._state == 0, timeout=15)
        assert t < 10  # lease_ttl 1 s, the client's timeout, then the park


async def test_a_killed_sequencer_leaves_no_telescope_unparked(tmp_path):
    """A sequencer that dies mid-exposure: the supervisor revokes its lease
    (not renewed) and, before the sky "closes", parks the telescope."""
    with _processes(tmp_path) as (config, services, tel, cam, sup):
        await _on_duty(sup.url)
        config.update(sequencer_name="seq", plan=STATIC)
        seq = SequencerProcess(config, tmp_path)
        services.append(seq)
        await wait_until(lambda: cam.exposures_started >= 1 and not tel._parked, timeout=30)
        seq.signal(signal.SIGKILL)
        # lease_ttl (1 s) to revoke, park_wait / 2 for its cleanup, then the park
        t = await wait_until(lambda: tel._parked, timeout=15)
        assert t < 1.0 + 4.0 + 5.0, t
