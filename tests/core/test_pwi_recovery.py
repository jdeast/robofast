"""Recovering a dead or restarted PWI (docs/recovery.md, docs/pwi.md): the
``home`` rung and the PWI reconnect of the recovery ladder, the PWI
server's ``pointing_models`` (PWI2 ``setmodel``), what the rehearsal's
mount guard forwards for a ladder, and the rehearsal's preflight (a failing
telescope gets its ladder once, then is dropped; refused without a policy).

Fast tier: the PWI simulators and server run in this process; the restart
hooks are commands (a Python one-liner asking the simulator to restart).
The whole night with a PWI2 simulator killed and restarted mid-night is in
tests/e2e/test_rehearsal_recovery.py."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from robofast.alpaca_client import AlpacaDevice, DriverException
from robofast.devices.pwi_client import PWI2_COMMANDS, PWI4Client, PWIError
from robofast.devices.pwi_server import PWIConfig, PWIServer, same_model
from robofast.devices.pwi_sim import SIM_LATITUDE, SIM_LONGITUDE, PWISim
from robofast.owner import DeviceOwner
from robofast.recovery import DevicePolicy, RecoveryLadder
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.checks import wait_until
from robofast.testing.fakes import FakeCamera, FakeFocuser, FakeTelescope
from robofast.testing.mount_guard import Endpoint, MountGuard
from robofast.testing.rehearsal import (
    MAX_ALT,
    MIN_ALT,
    MINERVA,
    RealTelescope,
    Rehearsal,
    RehearsalRefused,
    RehearsalSettings,
)
from robofast.timeline import Timeline

NIGHT = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
PORT1, PORT2 = "Mount_Model_Port1.PXP", "Mount_Model_Port2.PXP"


def pwi2_config(port: int, **kw) -> PWIConfig:
    return PWIConfig.from_dict({**_base(port), **kw})


def _base(port: int) -> dict:
    return {
        "name": "t1",
        "backend": "pwi2",
        "latitude": SIM_LATITUDE,
        "longitude": SIM_LONGITUDE,
        "elevation": 2000.0,
        "min_alt": 20.0,
        "max_alt": 85.0,
        "park_alt": 30.0,
        "park_az": 90.0,
        "focus_min": 100,
        "focus_max": 33000,
        "ports": {"imager": 1, "fau": 2},
        "listen": "127.0.0.1:0",
        "pwi": {"host": "127.0.0.1", "port": port, "timeout": 2.0},
        "status_max_age": 0.05,
        "slew_report_min": 0.2,
        "watch_interval": 0.1,
        "m3_timeout": 5.0,
        "request_timeout": 5.0,
        "allow_find_home": True,
        "pointing_models": {"1": PORT1, "fau": PORT2},
        "home_record": str(Path(tempfile.mkdtemp(prefix="pwi-home-")) / "home.json"),
    }


@pytest.fixture
def pwi2():
    """A PWI2 simulator (ready, homed, the port-1 model loaded) and the PWI
    server over it."""
    made: list = []

    def build(**kw):
        sim = PWISim(
            "pwi2",
            slew_rate=20.0,
            home_time=0.3,
            m3_time=0.3,
            ready=True,
            homed=True,
            model_file=PORT1,
        ).start()
        made.append(sim)
        server = PWIServer(pwi2_config(sim.port, **kw)).start()
        made.append(server)
        return sim, server

    yield build
    for x in reversed(made):
        x.stop()


def restart_hook(sim: PWISim) -> dict:
    """A restart hook command: the simulator restarts as PWI would."""
    url = f"http://{sim.address}/robofast-sim/restart"
    return {"command": [sys.executable, "-c", f"import urllib.request as u; u.urlopen({url!r})"]}


def telescope(server: PWIServer) -> tuple[DeviceOwner, object]:
    owner = DeviceOwner(
        "t1.telescope",
        lambda: AlpacaDevice(server.address, "telescope"),
        Timeline(),
        default_timeout=5,
    )
    return owner, owner.grant("test")


async def no_sleep(s: float) -> None:
    await asyncio.sleep(min(s, 0.05))


async def climb(ladder: RecoveryLadder, owner: DeviceOwner, grant):
    return await ladder.recover(
        "telescope",
        reconnect=lambda: owner.reconnect("recovery"),
        probe=lambda m: grant.get(m),
        call=lambda m, *a: grant.call(m, *a),
    )


POLICY = {
    "steps": ["reconnect", "restart", "home"],
    "verify_timeout": 1.0,
    "step_timeout": 20.0,
    "home_timeout": 20.0,
}


# -- the ladder on a restarted PWI2 ---------------------------------------------------------


async def test_a_dead_pwi2_comes_back_by_restart_home_and_its_model(pwi2):
    sim, server = pwi2()
    sim.set_mode("http500")  # PWI is gone: the server's reads fail
    owner, grant = telescope(server)
    events: list = []
    ladder = RecoveryLadder(
        "t1",
        {"telescope": DevicePolicy.from_dict({**POLICY, "restart": restart_hook(sim)})},
        event=lambda what, **kw: events.append((what, kw)),
        sleep=no_sleep,
    )
    res = await climb(ladder, owner, grant)
    assert res.recovered, res.tried
    assert [r for r, _ in res.tried] == ["reconnect", "restart", "home"]
    assert res.tried[1][1] == "failed: the mount is not homed"
    m = sim.model
    assert m.connected and all(m.enabled) and m.homed
    # The server put the port-1 model back once PWI was connected again.
    assert m.model_file == PORT1
    cmds = [c.split("&")[0] for c in m.commands]
    assert cmds.index("mount:connect") < cmds.index("mount:enable") < cmds.index("mount:findhome")
    assert "mount:setmodel" in cmds
    whats = [w for w, _ in events]
    assert "recovery_mount_connect" in whats and "recovery_home" in whats
    status = json.loads(await grant.call("Action", "Status", ""))
    assert status["homed"] is True and status["pointing_model"] == PORT1
    assert status["model_loads"] == [[1, PORT1, None]]


async def test_restart_alone_recovers_a_pwi_that_kept_its_home(pwi2):
    """The PWI reconnect on the restart rung: no home needed."""
    sim, server = pwi2()
    owner, grant = telescope(server)

    hook_calls = []

    async def hook(h, timeout):
        hook_calls.append(h)
        with sim.model.lock:
            sim.model.connected = False
            sim.model.enabled = [False, False]
        return "restarted"

    ladder = RecoveryLadder(
        "t1",
        {
            "telescope": DevicePolicy.from_dict(
                {**POLICY, "steps": ["restart", "home"], "restart": {"command": ["true"]}}
            )
        },
        hook=hook,
        sleep=no_sleep,
    )
    res = await climb(ladder, owner, grant)
    assert res.tried == [("restart", "ok")] and hook_calls
    assert sim.model.connected and all(sim.model.enabled)
    assert not any(c.startswith("mount:findhome") for c in sim.model.commands)


async def test_the_home_rung_is_skipped_when_the_server_may_not_home(pwi2):
    sim, server = pwi2(allow_find_home=False)
    with sim.model.lock:
        sim.model.axes_homed = [False, False]
    owner, grant = telescope(server)
    alarms: list[str] = []
    ladder = RecoveryLadder(
        "t1",
        {"telescope": DevicePolicy.from_dict({**POLICY, "steps": ["reconnect", "home"]})},
        alarm=alarms.append,
        sleep=no_sleep,
    )
    res = await climb(ladder, owner, grant)
    assert not res.recovered and res.exhausted
    assert res.tried[0] == ("reconnect", "failed: the mount is not homed")
    assert res.tried[1][0] == "home" and res.tried[1][1].startswith("skipped:")
    assert "CanFindHome" in res.tried[1][1]
    assert any("recovery home unavailable" in a for a in alarms)
    assert not any(c.startswith("mount:findhome") for c in sim.model.commands)


async def test_findhome_not_implemented_is_skipped_not_success():
    """A server that answers NotImplemented to FindHome (CanFindHome not
    false: a server that cannot say) skips the rung."""

    class Fake:
        homed = False

        async def probe(self, member):
            return {
                "AtPark": False,
                "CanFindHome": True,
                "Slewing": False,
                "SupportedActions": ["SelectPort", "Status"],
            }[member]

        async def call(self, member, *args):
            if member == "Action" and args[0] == "Status":
                return json.dumps({"pwi": {"connected": True}, "homed": self.homed})
            if member == "FindHome":
                from robofast.alpaca_client import NotImplementedException

                raise NotImplementedException("t1.telescope.FindHome: not implemented")
            raise AssertionError(member)

    f = Fake()
    ladder = RecoveryLadder(
        "t1", {"telescope": DevicePolicy.from_dict({**POLICY, "steps": ["home"]})}, sleep=no_sleep
    )
    res = await ladder.recover("telescope", reconnect=lambda: None, probe=f.probe, call=f.call)
    assert not res.recovered and res.tried[0][1].startswith("skipped: FindHome not allowed")


def test_home_is_a_telescope_step():
    with pytest.raises(ValueError, match="telescope step"):
        RecoveryLadder("t1", {"camera": DevicePolicy(steps=["reconnect", "home"])})
    RecoveryLadder("t1", {"telescope": DevicePolicy(steps=["reconnect", "home"])})


async def test_a_device_error_names_its_owner():
    """Alpaca errors carry the owner's name, so the sequencer's ladder finds
    the failed device (RecoveryLadder.device_for)."""

    class Driver:
        async def get(self, member, *, timeout):
            raise DriverException("telescope/0@127.0.0.1:1 GET AtPark: DriverException", 0x502)

    owner = DeviceOwner("t1.telescope", Driver, Timeline())
    with pytest.raises(DriverException) as e:
        await owner.grant("x").get("AtPark")
    assert str(e.value).startswith("t1.telescope.AtPark: ") and e.value.number == 0x502
    ladder = RecoveryLadder("t1", {"telescope": DevicePolicy()})
    assert ladder.device_for(repr(e.value), {"telescope": "t1.telescope"}) == "telescope"


# -- pointing models (PWI2) -----------------------------------------------------------------


async def test_pointing_model_follows_the_port_and_a_pwi_restart(pwi2):
    sim, server = pwi2()
    m = sim.model
    await wait_until(lambda: server.mount._pwi_up and not server.mount._model_due, timeout=5)
    with m.lock:
        m.model_file = "Default_Mount_Model.PXP"
    # The server checks only when PWI comes back or the port changes.
    await asyncio.sleep(0.5)
    assert m.model_file == "Default_Mount_Model.PXP"
    dev = AlpacaDevice(server.address, "telescope")
    await dev.connect(timeout=5)
    try:
        await dev.call("Action", "SelectPort", "fau", timeout=10)
        await wait_until(lambda: m.model_file == PORT2, timeout=5)
        await dev.call("Action", "SelectPort", "1", timeout=10)
        await wait_until(lambda: m.model_file == PORT1, timeout=5)
        # PWI restarts (its default model, disconnected): the server sends
        # setmodel once the mount is connected again.
        with m.lock:
            m.pwi_restart()
        await wait_until(
            lambda: server.mount._pwi_up and server.mount._was_connected is False, timeout=5
        )
        assert m.model_file == "Default_Mount_Model.PXP"
        await dev.call("Action", "MountConnect", "", timeout=5)
        await wait_until(lambda: m.model_file == PORT1, timeout=5)
        # And after PWI did not answer for a while.
        sim.set_mode("http500")
        with m.lock:
            m.model_file = "Default_Mount_Model.PXP"
        await wait_until(lambda: not server.mount._pwi_up, timeout=5)
        sim.set_mode("ok")
        await wait_until(lambda: m.model_file == PORT1, timeout=5)
    finally:
        await dev.aclose()
    loads = [c for c in m.commands if c.startswith("mount:setmodel")]
    assert loads == [
        f"mount:setmodel&filename={PORT2}",
        f"mount:setmodel&filename={PORT1}",
        f"mount:setmodel&filename={PORT1}",
        f"mount:setmodel&filename={PORT1}",
    ]


async def test_the_right_model_is_left_alone(pwi2):
    sim, server = pwi2()
    with sim.model.lock:
        sim.model.model_file = "C:\\PlaneWave\\Models\\" + PORT1.lower()
    await server.mount.mount_connect()
    assert not any(c.startswith("mount:setmodel") for c in sim.model.commands)
    assert same_model("c:\\x\\A.PXP", "a.pxp") and not same_model(None, "a.pxp")


def test_pointing_models_config():
    with pytest.raises(ValueError, match="PWI2 only"):
        PWIConfig.from_dict({**_base(1), "backend": "pwi4"})
    with pytest.raises(ValueError, match="not port 1, 2"):
        pwi2_config(1, pointing_models={"3": "x.PXP"})
    with pytest.raises(ValueError, match="a file name"):
        pwi2_config(1, pointing_models={"1": ""})
    assert pwi2_config(1).model_files() == {1: PORT1, 2: PORT2}
    assert ("mount", "setmodel") in PWI2_COMMANDS and not PWI2_COMMANDS[("mount", "setmodel")]


async def test_pwi4_client_sends_no_model_request():
    c = PWI4Client("127.0.0.1", 1)
    try:
        with pytest.raises(PWIError, match="does not load pointing models"):
            await c.set_model("x.PXP")
        assert c.sent == []
    finally:
        await c.aclose()


# -- the rehearsal's guard ------------------------------------------------------------------


def _guard(ep: Endpoint, recovery=frozenset()) -> MountGuard:
    return MountGuard(
        {"t1": {"telescope": ep}},
        limits=(MIN_ALT, MAX_ALT),
        site=(MINERVA.latitude, MINERVA.longitude),
        clock=lambda: NIGHT,
        recovery=recovery,
    ).start()


def _put(g: MountGuard, path: str, **form) -> dict:
    data = {"ClientID": "7", **{k: str(v) for k, v in form.items()}}
    return httpx.put(f"http://{g.address}/api/v1/{path}", data=data).json()


def test_the_guard_forwards_a_ladder_only_for_a_telescope_with_one(pwi2):
    sim, server = pwi2()
    ep = Endpoint.parse(f"{server.address}/0")
    plain, rec = _guard(ep), _guard(ep, {"t1"})
    try:
        for g in (plain, rec):
            assert _put(g, "telescope/0/connected", Connected=True)["ErrorNumber"] == 0
            st = _put(g, "telescope/0/action", Action="Status", Parameters="")
            assert st["ErrorNumber"] == 0 and json.loads(st["Value"])["homed"] is True
        for action in ("MountConnect", "MotorsEnable"):
            r = _put(plain, "telescope/0/action", Action=action, Parameters="")
            assert r["ErrorNumber"] == 0x40B
            assert _put(rec, "telescope/0/action", Action=action, Parameters="")["ErrorNumber"] == 0
        assert _put(plain, "telescope/0/findhome")["ErrorNumber"] == 0x40B
        assert _put(rec, "telescope/0/findhome")["ErrorNumber"] == 0
        # Never anything else, recovery or not.
        r = _put(rec, "telescope/0/action", Action="MotorsDisable", Parameters="")
        assert r["ErrorNumber"] == 0x40B
        assert [x["what"] for x in rec.recovery_commands] == [
            "Action MountConnect",
            "Action MotorsEnable",
            "findhome",
        ]
        assert len(plain.refused) == 3 and len(rec.refused) == 1
        # A dead mount does not read as parked (or anything else).
        sim.set_mode("http500")
        r = httpx.get(f"http://{rec.address}/api/v1/telescope/0/atpark").json()
        assert r["ErrorNumber"] != 0
    finally:
        plain.stop()
        rec.stop()


# -- the rehearsal's preflight --------------------------------------------------------------


def _dead_endpoint() -> Endpoint:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return Endpoint("127.0.0.1", port, 0)


@pytest.fixture
def fake_real():
    """A fake real telescope (mount, camera) served over Alpaca."""
    made: list = []

    def build() -> RealTelescope:
        mount = FakeTelescope(slew_time=0.05)
        mount._parked = False
        srv = FakeAlpacaServer({"telescope": mount, "focuser": FakeFocuser()}).start()
        cam = FakeAlpacaServer({"camera": FakeCamera(64, 64)}).start()
        made.extend([srv, cam])
        ep = Endpoint.parse(f"{srv.address}/0")
        return RealTelescope(ep, Endpoint.parse(f"{cam.address}/0"), ep, None)

    yield build
    for s in made:
        s.stop()


def _night(tmp_path: Path, real: dict, recovery: dict) -> Rehearsal:
    return Rehearsal(
        tmp_path,
        RehearsalSettings(real=real, recovery=recovery, console_port=None, start=NIGHT),
    )


RECONNECT_ONLY = {"telescope": {"steps": ["reconnect"], "verify_timeout": 0.5}}


async def test_preflight_refuses_a_failing_telescope_without_a_policy(tmp_path, fake_real):
    t2 = fake_real()
    dead = _dead_endpoint()
    t1 = RealTelescope(dead, fake_real().camera, dead, None)
    night = _night(tmp_path, {"t1": t1, "t2": t2}, {"t2": RECONNECT_ONLY})
    with pytest.raises(RehearsalRefused, match=r"t1 telescope at .*ConnectError"):
        await night._preflight()
    assert night.r.real.keys() == {"t1", "t2"} and not night.r.dropped


async def test_preflight_drops_a_telescope_its_ladder_cannot_fix(tmp_path, fake_real):
    t2 = fake_real()
    dead = _dead_endpoint()
    t1 = RealTelescope(dead, fake_real().camera, dead, None)
    settings = RehearsalSettings(
        real={"t1": t1, "t2": t2}, recovery={"t1": RECONNECT_ONLY}, start=NIGHT, console_port=None
    )
    night = Rehearsal(tmp_path, settings)
    seen = await night._preflight()
    assert night.r.real.keys() == {"t2"} and "t1" in night.r.dropped
    assert night.r.active == ["t2"] and "ConnectError" in seen["t1"]["dropped"]
    assert settings.real.keys() == {"t1", "t2"}  # the caller's settings untouched
    [rec] = night.outcome.preflight_recovery
    assert rec["telescope"] == "t1" and not rec["recovered"]
    assert rec["tried"][0].startswith("reconnect: failed")
    # The night's plan is t2's alone.
    from robofast.testing.rehearsal import catalog, make_requests, observable

    targets = observable(catalog(), NIGHT, NIGHT.replace(hour=6))
    groups = make_requests(night.r, NIGHT, NIGHT.replace(hour=6), targets)
    assert not any(g["id"].startswith("IMG-t1") for g in groups)
    assert all(
        g["requests"][0]["extra_params"]["telescopes"] == ["t2"]
        for g in groups
        if g["id"].startswith("REH")
    )


async def test_preflight_refuses_when_no_real_telescope_is_left(tmp_path, fake_real):
    cam = fake_real().camera
    dead = _dead_endpoint()
    night = _night(tmp_path, {"t1": RealTelescope(dead, cam, dead, None)}, {"t1": RECONNECT_ONLY})
    with pytest.raises(RehearsalRefused, match="no real telescope left: t1 dropped"):
        await night._preflight()


async def test_preflight_recovers_a_restarted_pwi2_through_a_guard(tmp_path, pwi2, fake_real):
    """PWI2 is down at the start: the ladder restarts it (a hook command),
    reconnects PWI, homes, and the telescope takes part (never a real Park
    or Unpark: the mount is reached through a guard)."""
    sim, server = pwi2()
    sim.set_mode("http500")
    cam = fake_real().camera
    ep = Endpoint.parse(f"{server.address}/0")
    policy = {"telescope": {**POLICY, "restart": restart_hook(sim)}}
    night = _night(tmp_path, {"t1": RealTelescope(ep, cam, ep, ep)}, {"t1": policy})
    seen = await night._preflight()
    assert night.r.real.keys() == {"t1"} and not night.r.dropped
    assert "dropped" not in seen["t1"] and seen["t1"]["tracking"] is False
    [rec] = night.outcome.preflight_recovery
    assert rec["recovered"] and [t.split(":")[0] for t in rec["tried"]] == [
        "reconnect",
        "restart",
        "home",
    ]
    assert sim.model.homed and sim.model.model_file == PORT1
    assert [x["what"] for x in night.outcome.recovery_commands] == [
        "Action MountConnect",
        "Action MotorsEnable",
        "findhome",
    ]
    assert not any(c.startswith(("mount:move", "mount:park")) for c in sim.model.commands)


def test_recovery_options(tmp_path):
    from robofast.testing.rehearsal import settings_from_args
    from tests.core.test_rehearsal_guard import args

    f = tmp_path / "t1.json"
    f.write_text(
        json.dumps(
            {
                "telescope": {
                    "steps": ["reconnect", "restart", "home"],
                    "restart": {"command": ["true"]},
                }
            }
        )
    )
    s = settings_from_args(args(recovery=[f"t1={f}"]))
    assert s.recovery["t1"]["telescope"]["steps"][-1] == "home"
    for bad, why in (
        ([f"t9={f}"], "not a real telescope"),
        (["t1=/nonexistent.json"], "No such file"),
        (["t1"], "NAME=FILE"),
    ):
        with pytest.raises(RehearsalRefused, match=why):
            settings_from_args(args(recovery=bad))
    f.write_text(
        json.dumps(
            {
                "telescope": {
                    "steps": ["powercycle"],
                    "powercycle": {"pdu": "127.0.0.1:1", "switch": 0},
                }
            }
        )
    )
    with pytest.raises(RehearsalRefused, match="never switches a PDU"):
        settings_from_args(args(recovery=[f"t1={f}"]))
    f.write_text(json.dumps({"camera": {"steps": ["home"]}}))
    with pytest.raises(RehearsalRefused, match="telescope step"):
        settings_from_args(args(recovery=[f"t1={f}"]))


async def test_a_slow_setmodel_is_waited_for_not_an_error(pwi2, caplog):
    """night1108: PWI2 on T2 reported the new model over 3 s after setmodel
    answered, and the server logged a false ERROR. It now re-reads in the
    background, bounded (model_verify_timeout), and logs the outcome."""
    sim, server = pwi2()
    m = sim.model
    await wait_until(lambda: server.mount._pwi_up and not server.mount._model_due, timeout=5)
    m.model_delay = 1.5
    caplog.set_level(logging.INFO, logger="robofast.devices.pwi_server")
    dev = AlpacaDevice(server.address, "telescope")
    await dev.connect(timeout=5)
    try:
        t0 = time.monotonic()
        await dev.call("Action", "SelectPort", "fau", timeout=10)
        # The port change does not wait for the model.
        assert m.model_file != PORT2 and time.monotonic() - t0 < 1.5
        await wait_until(lambda: server.mount.model_loads, timeout=10, what="the outcome")
    finally:
        await dev.aclose()
    assert server.mount.model_loads == [(2, PORT2, None)]
    assert m.model_file == PORT2
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("PWI reports it after" in r.getMessage() for r in caplog.records)


async def test_a_setmodel_pwi_never_takes_is_an_error_after_the_wait(pwi2, caplog):
    sim, server = pwi2(model_verify_timeout=1.0)
    m = sim.model
    await wait_until(lambda: server.mount._pwi_up and not server.mount._model_due, timeout=5)
    m.model_delay = None
    caplog.set_level(logging.INFO, logger="robofast.devices.pwi_server")
    dev = AlpacaDevice(server.address, "telescope")
    await dev.connect(timeout=5)
    try:
        await dev.call("Action", "SelectPort", "fau", timeout=10)
        await wait_until(lambda: server.mount.model_loads, timeout=10, what="the outcome")
    finally:
        await dev.aclose()
    [(port, name, err)] = server.mount.model_loads
    assert (port, name) == (2, PORT2) and "still reports" in err
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        f"setmodel {PORT2}: {err}"
    ]
