"""The TCSGO Alpaca server, end to end against the TCSGalil simulator.

Fast tier: the simulator and the server run in this process (on their own
threads) and are driven through robofast's own Alpaca client and
:class:`~robofast.owner.DeviceOwner`, then by the safety supervisor and
the sequencer on the FLWO 60-inch site config. Nothing here leaves loopback.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx
import pytest

from robofast import (
    DeviceOwner,
    Observation,
    SafetyConfig,
    SafetySupervisor,
    Sequencer,
    SequencerConfig,
    Timeline,
)
from robofast.alpaca import alpaca_owner
from robofast.alpaca_client import (
    ActionNotImplementedException,
    AlpacaDevice,
    InvalidOperationException,
    InvalidValueException,
    NotConnectedException,
    NotImplementedException,
    ParkedException,
)
from robofast.config import device_address, telescopes
from robofast.devices.tcsgo_client import HardwareNotAllowed
from robofast.devices.tcsgo_server import (
    CLOSED,
    CLOSING,
    OPEN,
    OPENING,
    TCSGOConfig,
    TCSGOServer,
    to_j2000,
)
from robofast.devices.tcsgo_sim import SIM_SERVER_SITE, TCSGOSim
from robofast.safety import ENCLOSURE_MEMBERS
from robofast.sequencer import TelescopeUnit
from robofast.testing.checks import wait_until
from robofast.testing.fakes import (
    FakeCamera,
    FakeDome,
    FakeObservingConditions,
    FakeSafetyMonitor,
    FakeWeather,
)
from robofast.testing.processes import ROOT

from .conftest import FAST_SAFETY, FAST_SEQ

NOT_LOOPBACK = "192.0.2.1"  # RFC 5737 documentation address; never contacted
TCS_FAST = dict(connect_timeout=1.0, command_timeout=1.0, ack_window=0.02, min_interval=0.005)


def server_config(sim: TCSGOSim, **kw) -> TCSGOConfig:
    base = {
        "name": "test",
        **SIM_SERVER_SITE,
        "listen": "127.0.0.1:0",
        "tcs": {"host": "127.0.0.1", "port": sim.port, **TCS_FAST},
        "status_max_age": 0.05,
        "slew_report_min": 0.2,
        "focus_settle": 0.1,
        "request_timeout": 4.0,
    }
    tcs = {**base["tcs"], **kw.pop("tcs", {})}
    return TCSGOConfig.from_dict({**base, **kw, "tcs": tcs})


@dataclass
class Rig:
    sim: TCSGOSim
    server: TCSGOServer
    owners: list[DeviceOwner] = field(default_factory=list)

    def owner(self, kind: str, name: str | None = None, **kw) -> DeviceOwner:
        addr = self.server.address
        o = DeviceOwner(
            name or kind,
            lambda: AlpacaDevice(addr, kind),
            Timeline(),
            default_timeout=5.0,
            **kw,
        )
        self.owners.append(o)
        return o

    def telescope(self):
        return self.owner("telescope").grant("test")

    def focuser(self):
        return self.owner("focuser").grant("test")

    def dome(self):
        return self.owner("dome", privileged_members=ENCLOSURE_MEMBERS).grant("t", privileged=True)


@pytest.fixture
async def make_rig():
    rigs: list[Rig] = []

    def build(sim_kw: dict | None = None, **config) -> Rig:
        sim_kw = {"slew_rate": 40.0, "shutter_time": 0.3, "focus_rate": 5000.0, **(sim_kw or {})}
        sim = TCSGOSim(**sim_kw).start()
        try:
            server = TCSGOServer(server_config(sim, **config)).start()
        except BaseException:
            sim.stop()
            raise
        rig = Rig(sim, server)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        for o in rig.owners:
            o.close()
        await asyncio.sleep(0)
        rig.server.stop()
        rig.sim.stop()


async def until_false(tel, member: str, timeout: float) -> float:
    t0 = time.monotonic()
    while await tel.get(member):
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"{member} still true after {timeout} s")
        await asyncio.sleep(0.05)
    return time.monotonic() - t0


async def target_above(tel, ha: float = 1.0, dec: float = 20.0) -> tuple[float, float]:
    return (await tel.get("SiderealTime") - ha) % 24.0, dec


# -- telescope ------------------------------------------------------------------------


async def test_slew_track_abort_park_unpark(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    assert await tel.get("AtPark") is True and await tel.get("Tracking") is False
    assert await tel.get("CanSlewAsync") is True and await tel.get("CanSync") is False
    assert await tel.get("EquatorialSystem") == 2 and await tel.get("AlignmentMode") == 1
    ra, dec = await target_above(tel)
    with pytest.raises(ParkedException):
        await tel.call("SlewToCoordinatesAsync", ra, dec)
    await tel.call("Unpark")
    assert await tel.get("AtPark") is False
    with pytest.raises(InvalidOperationException, match="tracking is off"):
        await tel.call("SlewToCoordinatesAsync", ra, dec)
    await tel.set("Tracking", True)
    assert await tel.get("Tracking") is True
    await tel.call("SlewToCoordinatesAsync", ra, dec)
    assert await tel.get("Slewing") is True
    await until_false(tel, "Slewing", 5)
    assert await tel.get("RightAscension") == pytest.approx(ra, abs=1e-4)
    assert await tel.get("Declination") == pytest.approx(dec, abs=1e-4)
    assert await tel.get("TargetRightAscension") == pytest.approx(ra)
    assert await tel.get("Tracking") is True and rig.sim.state()["mode"] == 1
    assert 30 < await tel.get("Altitude") < 90 and 0 <= await tel.get("Azimuth") < 360
    nums = rig.sim.numbers()
    assert nums[nums.index(6) + 1 :].count(12) >= 1  # #6 then #12,2
    # AbortSlew mid-slew: the mount keeps tracking from where it stopped.
    ra2, dec2 = await target_above(tel, -2.0, 60.0)
    await tel.call("SlewToCoordinatesAsync", ra2, dec2)
    await asyncio.sleep(0.15)
    await tel.call("AbortSlew")
    await until_false(tel, "Slewing", 2)
    assert await tel.get("Tracking") is True
    assert abs(await tel.get("Declination") - dec2) > 0.1
    # Tracking off holds position.
    await tel.set("Tracking", False)
    assert rig.sim.state()["mode"] == 0
    await tel.call("Park")
    assert await tel.get("Slewing") is True
    await wait_until(lambda: rig.sim.parked(), 5, what="parked")
    await until_false(tel, "Slewing", 3)
    assert await tel.get("AtPark") is True and await tel.get("Altitude") == pytest.approx(90)
    assert 58 in rig.sim.numbers()


async def test_slew_refusals(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    await tel.call("Unpark")
    await tel.set("Tracking", True)
    lst = await tel.get("SiderealTime")
    with pytest.raises(InvalidOperationException, match="below min_alt"):
        await tel.call("SlewToCoordinatesAsync", (lst + 12) % 24, 0.0)
    with pytest.raises(InvalidValueException):
        await tel.call("SlewToCoordinatesAsync", 25.0, 0.0)
    with pytest.raises(InvalidValueException):
        await tel.call("SlewToCoordinatesAsync", 1.0, 91.0)
    # Out of the TCS's own travel (the simulator's Dec limit), though above min_alt.
    rig.sim.dec_limits = (-40.0, 50.0)
    with pytest.raises(InvalidOperationException, match="out of range"):
        await tel.call("SlewToCoordinatesAsync", *(await target_above(tel, 0.0, 70.0)))
    rig.sim.dec_limits = (-40.0, 89.5)
    rig.sim.set_drives(False)
    with pytest.raises(InvalidOperationException, match="drives"):
        await tel.call("SlewToCoordinatesAsync", *(await target_above(tel)))
    rig.sim.set_drives(True)
    rig.sim.set_stale(True)
    with pytest.raises(InvalidOperationException, match="stale"):
        await tel.call("SlewToCoordinatesAsync", *(await target_above(tel)))
    rig.sim.set_stale(False)
    # A tripped travel limit (a trip wire) locks the mount out: refuse, never clear it.
    rig.sim.hit_limit("west")  # the mount holds (tracking off)
    with pytest.raises(InvalidOperationException, match=r"travel limit tripped.*west_limit"):
        await tel.set("Tracking", True)
    with pytest.raises(InvalidOperationException, match="travel limit"):
        await tel.call("Park")
    assert rig.sim.hard_limits == {"west"}
    rig.sim.clear_limit()
    assert 12 not in rig.sim.numbers()  # no slew was ever started


async def test_offset_action_and_pulse_guide(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    with pytest.raises(ParkedException):
        await tel.call("Action", "Offset", json.dumps({"east": 1, "north": 1}))
    await tel.call("Unpark")
    with pytest.raises(InvalidOperationException, match="hold mode"):
        await tel.call("Action", "Offset", json.dumps({"north": 1}))
    await tel.set("Tracking", True)
    await tel.call("SlewToCoordinatesAsync", *(await target_above(tel)))
    await until_false(tel, "Slewing", 5)
    ra0, dec0 = await tel.get("RightAscension"), await tel.get("Declination")
    target0 = rig.sim.target_values(time.monotonic())[2:5]
    n0 = len(rig.sim.numbers())
    reply = json.loads(await tel.call("Action", "Offset", json.dumps({"east": 30, "north": -36})))
    # The live TRES guider's jog(): #6 at mount + offset, then chase.
    assert [c[0] for c in reply["sent"]] == [6, 12] and reply["sent"][1] == [12, 3]
    assert reply["sent"][0][2] == pytest.approx(dec0 - 0.01, abs=1e-6)
    await wait_until(lambda: rig.sim.state()["mode"] == 1, 5, what="back to follow")
    await until_false(tel, "Slewing", 5)
    assert (await tel.get("Declination") - dec0) * 3600 == pytest.approx(-36, abs=0.1)
    cosd = math.cos(math.radians(dec0))
    dra = (await tel.get("RightAscension") - ra0) * 15 * 3600 * cosd
    assert dra == pytest.approx(30, abs=0.2)
    assert await tel.get("Tracking") is True
    sent = [(n, a) for _, n, a in rig.sim.log[n0:] if n not in (25, 26, 32)]
    assert [n for n, _ in sent] == [6, 12, 12, 6]  # target, chase, follow, restore
    assert sent[1][1] == (3.0,) and sent[2][1] == (1.0,)
    assert sent[3][1] == pytest.approx(target0)  # the original target is back
    await asyncio.sleep(0.3)  # follow: restoring the target does not move the mount
    assert (await tel.get("Declination") - dec0) * 3600 == pytest.approx(-36, abs=0.1)
    with pytest.raises(InvalidValueException, match="max_offset"):
        await tel.call("Action", "Offset", json.dumps({"east": 5000}))
    with pytest.raises(InvalidValueException):
        await tel.call("Action", "Offset", "east=3")
    with pytest.raises(ActionNotImplementedException):
        await tel.call("Action", "Nudge", "")
    assert set(await tel.get("SupportedActions")) == {"Offset", "Status", "Faults"}
    # PulseGuide: ASCOM directions; IsPulseGuiding during it.
    d0 = await tel.get("Declination")
    await tel.call("PulseGuide", 0, 300)
    assert await tel.get("IsPulseGuiding") is True
    await until_false(tel, "IsPulseGuiding", 3)
    assert (await tel.get("Declination") - d0) * 3600 == pytest.approx(5 * 0.3, abs=0.2)
    with pytest.raises(InvalidValueException):
        await tel.call("PulseGuide", 4, 300)


async def test_offset_never_sends_7(make_rig):
    """Regression (FLWO's live TRES guider: #7 is "broken on the DFM side"):
    an offset, from follow or chase, and back to back, never sends #7; a
    second offset before capture keeps the first one's target to restore."""
    rig = make_rig()
    tel = rig.telescope()
    await tel.call("Unpark")
    await tel.set("Tracking", True)
    await tel.call("SlewToCoordinatesAsync", *(await target_above(tel)))
    await until_false(tel, "Slewing", 5)
    target0 = rig.sim.target_values(time.monotonic())[2:5]
    dec0 = await tel.get("Declination")
    await tel.call("Action", "Offset", json.dumps({"north": 600}))
    assert await tel.get("Slewing") is True  # chasing until captured
    await tel.call("Action", "Offset", json.dumps({"north": 60}))  # re-aimed mid-chase
    await wait_until(lambda: rig.sim.state()["mode"] == 1, 10, what="back to follow")
    await asyncio.sleep(0.1)
    assert 7 not in rig.sim.numbers()
    assert rig.sim.target_values(time.monotonic())[2:5] == pytest.approx(target0)
    # The second offset starts from wherever the chase had got to.
    assert 60 < (await tel.get("Declination") - dec0) * 3600 <= 660.1


async def test_offset_out_of_range_restores_target(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    await tel.call("Unpark")
    await tel.set("Tracking", True)
    await tel.call("SlewToCoordinatesAsync", *(await target_above(tel, 0.0, 49.9)))
    await until_false(tel, "Slewing", 5)
    target0 = rig.sim.target_values(time.monotonic())[2:5]
    rig.sim.dec_limits = (-40.0, 50.0)
    with pytest.raises(InvalidOperationException, match="out of range"):
        await tel.call("Action", "Offset", json.dumps({"north": 900}))
    assert rig.sim.target_values(time.monotonic())[2:5] == pytest.approx(target0)
    assert 7 not in rig.sim.numbers() and rig.sim.numbers()[-1] != 12


async def test_zenith_stow_holds_and_parks_the_dome(make_rig):
    """park_command "zenith": the legacy 60-inch stow (#10, #12,2, then #12,0
    once at the zenith), and the dome to dome_park_az when robofast drives it."""
    rig = make_rig({"dome_rate": 60.0}, park_command="zenith", dome=True, dome_park_az=235.0)
    tel = rig.telescope()
    await tel.call("Unpark")
    await tel.set("Tracking", True)
    await tel.call("SlewToCoordinatesAsync", *(await target_above(tel, 2.0, 0.0)))
    await until_false(tel, "Slewing", 5)
    n0 = len(rig.sim.numbers())
    await tel.call("Park")
    t0 = time.monotonic()
    while not await tel.get("AtPark"):  # AtPark needs hold mode: the #12,0 has landed
        assert time.monotonic() - t0 < 10, "not parked after 10 s"
        await asyncio.sleep(0.05)
    assert rig.sim.state()["mode"] == 0 and await tel.get("Altitude") == pytest.approx(90)
    sent = [(n, a) for _, n, a in rig.sim.log[n0:] if n not in (25, 26, 32, 62, 64)]
    assert [n for n, _ in sent] == [10, 12, 43, 20, 12]
    assert sent[1][1] == (2.0,) and sent[2][1] == (235.0,) and sent[4][1] == (0.0,)
    assert 58 not in rig.sim.numbers()
    await wait_until(lambda: abs(rig.sim.state()["dome_az"] - 235.0) < 0.5, 10, what="dome")
    # Abort mid-stow: nothing more is sent (no hold from a stale sequence).
    await tel.call("Unpark")
    await tel.set("Tracking", True)
    await tel.call("SlewToCoordinatesAsync", *(await target_above(tel, 2.0, 0.0)))
    await until_false(tel, "Slewing", 5)
    await tel.call("Park")
    await asyncio.sleep(0.05)
    await tel.call("AbortSlew")
    n1 = len(rig.sim.numbers())
    await asyncio.sleep(1.0)
    assert [n for n in rig.sim.numbers()[n1:] if n not in (25, 26, 62)] == []


async def test_status_and_faults_actions(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    st = json.loads(await tel.call("Action", "Status", ""))
    assert st["status"]["mode"] == "hold" and st["atpark"] is True
    assert st["mount"]["altitude_deg"] == pytest.approx(90)
    assert json.loads(await tel.call("Action", "Faults", "")) == {
        "fault": False,
        "reasons": [],
        "mode": "hold",
    }
    rig.sim.hit_limit("east")
    rig.sim.set_drives(False)
    f = json.loads(await tel.call("Action", "Faults", ""))
    assert (
        f["fault"] and "drives switch down" in f["reasons"] and "limit: east_limit" in f["reasons"]
    )


async def test_hung_tcs_is_bounded(make_rig):
    rig = make_rig(request_timeout=2.0, tcs={"command_timeout": 0.3, "max_attempts": 2})
    tel = rig.telescope()
    await tel.get("AtPark")
    rig.sim.set_mode("hang")
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    with pytest.raises(Exception, match=r"no reply|no answer|no completion"):
        await tel.get("Tracking", timeout=5)
    assert time.monotonic() - t0 < 3.0
    rig.sim.set_mode("ok")
    assert await tel.get("Tracking") is False


async def test_garbled_tcs_is_an_error(make_rig):
    rig = make_rig()
    tel = rig.telescope()
    await tel.get("AtPark")
    rig.sim.set_mode("garble")
    await asyncio.sleep(0.1)
    with pytest.raises(Exception, match=r"expected 8 values|malformed"):
        await tel.get("Slewing")
    rig.sim.set_mode("ok")


# -- focuser and dome -----------------------------------------------------------------------


async def test_focuser(make_rig):
    rig = make_rig(focus_min=-1000.0, focus_max=1000.0)
    foc = rig.focuser()
    assert await foc.get("MaxStep") == 2000 and await foc.get("Absolute") is True
    assert await foc.get("Position") == 1000  # TCS 0 microns
    await foc.call("Move", 700)
    assert await foc.get("IsMoving") is True
    await until_false(foc, "IsMoving", 3)
    assert await foc.get("Position") == 700 and rig.sim.focus == pytest.approx(-300)
    with pytest.raises(InvalidValueException):
        await foc.call("Move", 2001)
    with pytest.raises(NotImplementedException):
        await foc.call("Halt")


async def test_tcs_dome(make_rig):
    rig = make_rig({"dome_rate": 90.0}, dome=True)
    dome = rig.dome()
    assert await dome.get("ShutterStatus") == CLOSED
    await dome.call("OpenShutter")
    assert await dome.get("ShutterStatus") == OPENING and await dome.get("Slewing") is True
    await wait_until(lambda: rig.sim.state()["shutters"] == {"upper": "open", "lower": "open"}, 3)
    assert await dome.get("ShutterStatus") == OPEN
    nums = rig.sim.numbers()
    assert nums.index(36) < nums.index(44)  # upper first (shutter_open_order)
    await dome.call("CloseShutter")
    assert await dome.get("ShutterStatus") == CLOSING
    await wait_until(lambda: rig.sim.state()["shutters"]["upper"] == "closed", 3)
    assert await dome.get("ShutterStatus") == CLOSED
    await dome.call("SlewToAzimuth", 90.0)
    await wait_until(lambda: abs(rig.sim.state()["dome_az"] - 90) < 0.1, 5, what="dome at 90")
    assert await dome.get("Azimuth") == pytest.approx(90.0, abs=0.1)
    await dome.set("Slaved", True)
    assert await dome.get("Slaved") is True
    await dome.set("Slaved", False)
    assert await dome.get("Slaved") is False


async def test_management_connection_and_unimplemented(make_rig):
    rig = make_rig()
    base = f"http://{rig.server.address}"

    def checks() -> None:
        r = httpx.get(base + "/management/v1/configureddevices", trust_env=False, timeout=5)
        kinds = [d["DeviceType"] for d in r.json()["Value"]]
        assert kinds == ["Telescope", "Focuser"]  # no dome unless configured
        r = httpx.get(base + "/api/v1/dome/0/name", trust_env=False, timeout=5)
        assert r.status_code == 400

    await asyncio.to_thread(checks)
    d = AlpacaDevice(rig.server.address, "telescope")
    try:
        with pytest.raises(NotConnectedException):
            await d.get("Tracking", timeout=3)
        await d.connect(timeout=3)
        with pytest.raises(NotImplementedException):
            await d.put("SyncToCoordinates", timeout=3, RightAscension=1, Declination=1)
        with pytest.raises(NotImplementedException):
            await d.put("MoveAxis", timeout=3, Axis=0, Rate=1)
        with pytest.raises(InvalidOperationException):
            await d.get("TargetDeclination", timeout=3)
    finally:
        await d.aclose()


# -- configuration and the hardware gate ---------------------------------------------------


def test_server_refuses_a_non_loopback_tcs(tmp_path):
    sim = TCSGOSim()  # not started
    for allow, hardware in ((False, False), (True, False), (False, True)):
        cfg = server_config(sim, allow_hardware=allow)
        cfg.tcs.host = NOT_LOOPBACK
        with pytest.raises(HardwareNotAllowed):
            TCSGOServer(cfg, hardware=hardware)
    path = tmp_path / "tcsgo.json"
    path.write_text(
        json.dumps({"name": "t", **SIM_SERVER_SITE, "tcs": {"host": NOT_LOOPBACK, "port": 5003}})
    )
    proc = subprocess.run(
        [sys.executable, "-m", "robofast.devices.tcsgo_server", "--config", str(path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2 and "not loopback" in proc.stderr
    with pytest.raises(ValueError, match="unknown config keys"):
        TCSGOConfig.from_dict({"allow_hardwar": True})
    with pytest.raises(ValueError, match="park_command"):
        TCSGOConfig.from_dict({"name": "t", **SIM_SERVER_SITE, "park_command": "stow"})
    # Site values have no defaults (docs/design.md 3.10).
    with pytest.raises(ValueError, match=r"missing required key\(s\) name, latitude, longitude"):
        TCSGOConfig.from_dict({})
    site = {k: v for k, v in SIM_SERVER_SITE.items() if k != "focus_max"}
    with pytest.raises(ValueError, match=r"focuser: true.: missing required key\(s\) focus_max"):
        TCSGOConfig.from_dict({"name": "t", **site})
    assert not TCSGOConfig.from_dict({"name": "t", **site, "focuser": False}).focuser


def test_shipped_flwo60_configs():
    cfg = TCSGOConfig.from_dict(json.loads((ROOT / "sites/flwo60/tcsgo.json").read_text()))
    assert cfg.tcs.host == "127.0.0.1" and not cfg.allow_hardware and not cfg.dome
    # The legacy 60-inch stow, never #58 (docs/tcsgo.md Q6).
    assert cfg.park_command == "zenith" and cfg.dome_park_az == 235.0
    site = json.loads((ROOT / "sites/flwo60/flwo60.json").read_text())
    # No watchdog (JDE 2026-10-31): attended only.
    assert SafetyConfig(**site["safety"]).attended_only is True
    SequencerConfig(**site["sequencer"])
    (entry,) = telescopes(site).values()
    assert device_address(entry["telescope"]) == (cfg.listen, 0)
    text = (ROOT / "sites/flwo60/flwo60.json").read_text() + json.dumps(cfg.__dict__, default=str)
    hosts = {device_address(v)[0].rpartition(":")[0] for v in site["devices"].values()}
    assert hosts == {"127.0.0.1"} and "192.168" not in text


def test_to_j2000():
    assert to_j2000(5.0, 10.0, 2000.0) == (5.0, 10.0)
    ra, dec = to_j2000(5.0, 10.0, 2026.8)  # about 27 years of precession
    assert (ra - 5.0) * 15 * 3600 == pytest.approx(-27 * 46.1 / 1.0, rel=0.15)
    assert abs(dec - 10.0) * 3600 < 27 * 20.1 * 1.1


# -- the FLWO 60-inch scenario ----------------------------------------------------------------


@pytest.mark.parametrize("dome_kind", ["fake", "tcs"])
async def test_flwo60_unsafe_weather_mid_exposure(make_rig, dome_kind):
    """Weather turns unsafe mid-exposure on the 60-inch site config: the
    sequencer aborts the exposure and parks the telescope through the TCSGO
    server, and the supervisor closes the dome (a fake one, since the 60-inch
    dome is not known to be TCS-controlled; and the TCS's own, if it is)."""
    site = json.loads((ROOT / "sites/flwo60/flwo60.json").read_text())
    shipped = json.loads((ROOT / "sites/flwo60/tcsgo.json").read_text())
    rig = make_rig(
        {"slew_rate": 30.0, "shutter_time": 0.3},
        dome=dome_kind == "tcs",
        latitude=site["site"]["latitude"],
        park_command=shipped["park_command"],
        dome_park_az=shipped["dome_park_az"],
        sequence_poll=0.05,
    )

    def stowed() -> bool:
        st = rig.sim.state()
        return st["mode"] == 0 and st["alt"] > 89.9

    (tel_name,) = telescopes(site)
    tl = Timeline()
    weather = FakeWeather(safe=True)
    cam = FakeCamera()
    fake_dome = FakeDome()
    sm, oc = FakeSafetyMonitor(weather), FakeObservingConditions(weather)

    def owner(name, obj, **kw):
        return DeviceOwner(name, lambda: obj, tl, default_timeout=1.0, **kw)

    tel = alpaca_owner(
        f"{tel_name}.telescope", "telescope", rig.server.address, tl, default_timeout=5
    )
    sup_tel = alpaca_owner("sup.telescope", "telescope", rig.server.address, tl, default_timeout=5)
    if dome_kind == "tcs":
        dome = alpaca_owner(
            "dome",
            "dome",
            rig.server.address,
            tl,
            default_timeout=5,
            privileged_members=ENCLOSURE_MEMBERS,
        )
    else:
        dome = owner("dome", fake_dome, privileged_members=ENCLOSURE_MEMBERS)
    rig.owners += [tel, sup_tel, dome]
    sup = SafetySupervisor(
        "safety",
        dome=dome,
        safety_monitor=owner("safetymonitor", sm),
        observing_conditions=owner("observingconditions", oc),
        telescopes=[sup_tel],  # nothing reopens on an unparked 60-inch
        timeline=tl,
        config=SafetyConfig(
            **{
                **FAST_SAFETY,
                **site["safety"],  # attended only: the 60-inch has no watchdog
                "dome_timeout": 5.0,
                "shutter_timeout": 5.0,
            }
        ),
    )
    assert sup.config.attended_only
    sup.duty_on("observer", "the 60-inch night")  # no lease without it
    seq = Sequencer(
        site["sequencer_name"],
        supervisor=sup,
        telescopes=[TelescopeUnit(tel_name, tel, owner(f"{tel_name}.camera", cam))],
        plan=lambda: Observation("HD 1234 (TRES)", exptime=30.0, count=10),
        timeline=tl,
        config=SequencerConfig(
            **{**site["sequencer"], **FAST_SEQ, "slew_timeout": 10.0, "park_timeout": 10.0}
        ),
    )

    def dome_state() -> str:
        if dome_kind == "fake":
            return {0: "open", 1: "closed"}.get(fake_dome.state(), "moving")
        sh = rig.sim.state()["shutters"]
        return sh["upper"] if sh["upper"] == sh["lower"] else "moving"

    tasks = [asyncio.create_task(sup.run()), asyncio.create_task(seq.run())]
    try:
        await wait_until(lambda: cam.CameraState == 2, 20, what="TRES exposing")
        assert dome_state() == "open" and rig.sim.tracking() and not rig.sim.parked()
        weather.set_safe(False)
        t0 = time.monotonic()
        await wait_until(
            lambda: dome_state() == "closed" and stowed() and cam._state == 0,
            15,
            what="closed, parked, camera idle",
        )
        dt = time.monotonic() - t0
        assert cam.exposures_aborted == 1 and not seq.frames
        await wait_until(lambda: tl.events("cleanup_done"), 5)
        report = seq.cleanups[0]
        assert report.aborted == [tel_name] and report.parked == [tel_name] and not report.errors
        assert 58 not in rig.sim.numbers() and 10 in rig.sim.numbers()  # the legacy stow
        print(f"FLWO 60-inch ({dome_kind} dome): safe state {dt:.2f} s after unsafe")
    finally:
        for t in reversed(tasks):
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
