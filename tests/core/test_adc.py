"""The 60-inch ADC: the tracking math against the legacy daemon's own
results, the motor client, its simulator, the Alpaca server's tracking
(stale input, slews, park, limits, faults) and the sequencer using it.

Fast tier: everything runs in this process against the simulated motors
(``port.type`` "sim") and a fake telescope feed (or the TCSGO server on its
simulator); no serial port is opened.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import time

import erfa
import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import (
    AlpacaDevice,
    InvalidOperationException,
    InvalidValueException,
    NotConnectedException,
    NotImplementedException,
)
from robofast_core.config import device_address, telescopes
from robofast_core.devices import adc_math
from robofast_core.devices.adc_mdrive import MOVING, MotorError, Motors, MotorTimeout
from robofast_core.devices.adc_server import (
    SWITCH_IDS,
    SWITCHES,
    ADCConfig,
    ADCServer,
    AlpacaTelescopeFeed,
    Reading,
)
from robofast_core.devices.adc_sim import ADCMotorsSim
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.imaging import Imaging, ImagingConfig
from robofast_core.plan import StaticPlan
from robofast_core.sequencer import ADC_SETTLED, ADC_TRACKING
from robofast_core.sequencer_service import build_units
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT
from robofast_core.timeline import Timeline

SITE = ROOT / "sites" / "flwo60"
VECTORS = json.loads((ROOT / "tests" / "core" / "data" / "adc_legacy_vectors.json").read_text())
FAST = dict(
    update_interval=0.05,
    poll_interval=0.02,
    idle_poll=0.5,
    settle_after_slew=0.1,
    stale_after=0.5,
    stall_time=0.5,
    move_timeout=5.0,
    home_wait=[0.05, 0.05],
    home_timeout=3.0,
    request_timeout=3.0,
    telescope_timeout=0.3,
)
LAT = 31.6809


def moving_commands(sim: ADCMotorsSim) -> list[str]:
    return [c for c in sim.commands if c[1:].split(" ", 1)[0] in {"ma", "cl"}]


def steps(sim: ADCMotorsSim) -> tuple[int, int]:
    return sim.position("A"), sim.position("B")


# -- the math against the legacy daemon --------------------------------------------------


def _sexagesimal(text: str) -> float:
    neg = "-" in text
    h, m, s = text.strip().lstrip("-").split(":")
    v = int(h) + int(m) / 60 + float(s) / 3600
    return -v if neg else v


def test_the_port_reproduces_every_legacy_vector():
    vectors = VECTORS["vectors"]
    assert len(vectors) > 200
    for v in vectors:
        ha = math.radians(_sexagesimal(v["ha"]) * 15)
        dec = math.radians(_sexagesimal(v["dec"]))
        pa = adc_math.parallactic_angle(ha, dec, adc_math.LEGACY_LATITUDE_RAD)
        assert pa == pytest.approx(v["pa"], abs=1e-6)
        defl = adc_math.legacy_deflection(v["zd"])
        assert defl == v["defl"]
        a, b = adc_math.legacy_prism_angles(v["pa"], defl)
        assert (a, b) == (pytest.approx(v["atarg"], abs=1e-6), pytest.approx(v["btarg"], abs=1e-6))
        ma = adc_math.legacy_motor_degrees(a, adc_math.HOME_ANGLES[0])
        mb = adc_math.legacy_motor_degrees(b, adc_math.HOME_ANGLES[1])
        assert (ma, mb) == (
            pytest.approx(v["a_deg"], abs=1e-4),
            pytest.approx(v["b_deg"], abs=1e-4),
        )
        assert (adc_math.steps_for(ma), adc_math.steps_for(mb)) == (v["a_steps"], v["b_steps"])
        # The closed form robofast uses is the same chain.
        ca, cb = adc_math.motor_degrees(v["pa"], defl)
        assert adc_math.angle_diff(ca, ma) < 1e-9 and adc_math.angle_diff(cb, mb) < 1e-9
        assert adc_math.prism_angle(ca, adc_math.HOME_ANGLES[0]) == pytest.approx(a % 360, abs=1e-9)


def test_the_legacy_table_edges():
    assert adc_math.legacy_deflection(0.0) == 0.0
    assert adc_math.legacy_deflection(2.0) == 0.0 and adc_math.legacy_deflection(2.01) == 2.0
    assert adc_math.legacy_deflection(67.0) == 55.0 and adc_math.legacy_deflection(67.01) == 80.0
    assert adc_math.legacy_deflection(89.0) == 80.0


@pytest.mark.parametrize("pa", [-179.0, -90.0, -12.3, 0.0, 45.0, 135.0, 179.9])
@pytest.mark.parametrize("defl", [0.0, 7.0, 32.0, 80.0])
def test_closed_form_and_the_swap_variant(pa, defl):
    for swap in (False, True):
        a, b = adc_math.legacy_prism_angles(pa, defl, swap=swap)
        ma, mb = adc_math.motor_degrees(pa, defl, swap=swap)
        assert adc_math.angle_diff(ma, adc_math.legacy_motor_degrees(a, 202.5)) < 1e-9
        assert adc_math.angle_diff(mb, adc_math.legacy_motor_degrees(b, 22.5)) < 1e-9
    # Home (step 0 on both) is a null; the motors counter-rotate by defl.
    a0, b0 = adc_math.motor_degrees(pa, 0.0)
    assert adc_math.angle_diff(a0, b0) < 1e-9
    ma, mb = adc_math.motor_degrees(pa, defl)
    assert adc_math.angle_diff(ma, a0) == pytest.approx(defl)
    assert adc_math.angle_diff(mb, b0) == pytest.approx(defl)
    # The swap variant gives each prism the other's angle.
    assert adc_math.legacy_prism_angles(pa, defl, swap=True) == tuple(
        reversed(adc_math.legacy_prism_angles(pa, defl))
    )


@pytest.mark.parametrize(("ha_h", "dec"), [(-3.0, 10.0), (0.5, -20.0), (2.2, 50.0), (-0.1, 70.0)])
def test_parallactic_angle_from_altaz_matches_ha_dec(ha_h, dec):
    phi = math.radians(LAT)
    az, el = erfa.hd2ae(math.radians(ha_h * 15), math.radians(dec), phi)
    pa, zd = adc_math.from_altaz(math.degrees(el), math.degrees(az), LAT)
    want = adc_math.parallactic_angle(math.radians(ha_h * 15), math.radians(dec), phi)
    assert pa == pytest.approx(want, abs=1e-6)
    assert zd == pytest.approx(90 - math.degrees(el), abs=1e-9)
    assert (pa < 0) == (ha_h < 0)  # east of the meridian: negative


def test_the_refraction_model_follows_the_legacy_table():
    atm = adc_math.Atmosphere()
    assert adc_math.dispersion_arcsec(45.0, 0.39, 0.91, atm) == pytest.approx(1.43, abs=0.05)
    for lo, hi in ((18, 28), (28, 38), (43, 48), (53, 58), (58, 63)):
        zd = (lo + hi) / 2
        model = adc_math.refraction_deflection(zd, 0.39, 0.91, 3.8, 80.0, atm)
        assert abs(model - adc_math.legacy_deflection(zd)) < 6.0, zd
    assert adc_math.refraction_deflection(0.0, 0.39, 0.91, 3.8, 80.0, atm) == 0.0
    assert adc_math.refraction_deflection(80.0, 0.39, 0.91, 3.8, 80.0, atm) == 80.0
    # A bluer band needs more deflection at the same zenith distance.
    blue = adc_math.refraction_deflection(40.0, 0.35, 0.91, 3.8, 80.0, atm)
    assert blue > adc_math.refraction_deflection(40.0, 0.39, 0.91, 3.8, 80.0, atm)


# -- the motor client -------------------------------------------------------------------------


async def test_motor_client_reads_and_moves_the_simulator():
    sim = ADCMotorsSim(position=(100, 3000), speed=1e5)
    motors = Motors(sim, timeout=0.5, ack_window=0.02)
    assert await motors.position("A") == 100 and await motors.position("B") == 3000
    await motors.move("A", 1442)
    await wait_until(lambda: not sim.moving(), 2.0)
    assert await motors.position("A") == 1442
    assert sim.commands == ["Apr p", "Bpr p", "Ama 1442", "Apr p"]
    await motors.home("B")
    await wait_until(lambda: not sim.moving(), 2.0)
    assert sim.position("B") == 0 and motors.last_motion == "Bcl HO"


async def test_motor_client_refuses_before_sending():
    sim = ADCMotorsSim()
    motors = Motors(sim, timeout=0.2)
    for bad in (-1, 4000, 12.5):
        with pytest.raises(MotorError, match="refused"):
            await motors.move("A", bad)  # type: ignore[arg-type]
    with pytest.raises(MotorError, match="no motor"):
        await motors.position("C")
    with pytest.raises(MotorError, match="allowlist"):
        motors._line("A", "sl 100")
    assert sim.commands == [] and MOVING == {"ma", "cl HO"}


async def test_motor_client_error_reply_and_bounded_timeout():
    class Grumpy(ADCMotorsSim):
        def handle(self, line):
            self.commands.append(line)
            self._answer(line, "?")

    with pytest.raises(MotorError, match="controller error"):
        await Motors(Grumpy(), timeout=0.3).position("A")
    with pytest.raises(MotorError, match="controller error"):
        await Motors(Grumpy(), timeout=0.3, ack_window=0.05).move("A", 5)
    sim = ADCMotorsSim()
    sim.silent = True
    t = time.monotonic()
    with pytest.raises(MotorTimeout):
        await Motors(sim, timeout=0.2).position("A")
    assert time.monotonic() - t < 1.0


# -- the server ---------------------------------------------------------------------------------


class FakeFeed:
    """The telescope as the ADC reads it; ``error`` makes reads fail."""

    def __init__(self, alt: float = 50.0, az: float = 120.0) -> None:
        self.reading = Reading(alt, az, False, False)
        self.error: Exception | None = None
        self.reads = 0

    def set(self, **kw) -> None:
        d = {
            "altitude": self.reading.altitude,
            "azimuth": self.reading.azimuth,
            "slewing": self.reading.slewing,
            "at_park": self.reading.at_park,
            **kw,
        }
        self.reading = Reading(**d)

    async def read(self) -> Reading:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.reading

    async def close(self) -> None:
        pass


@pytest.fixture
def serve():
    servers: list[ADCServer] = []

    def build(feed: FakeFeed | None = None, sim: dict | None = None, **kw) -> ADCServer:
        d = {
            "listen": "127.0.0.1:0",
            "port": {"type": "sim", "sim": {"speed": 20000.0, **(sim or {})}},
            "trust_counter": True,
            **FAST,
            **kw,
        }
        server = ADCServer(ADCConfig.from_dict(d), feed=feed).start()
        servers.append(server)
        return server

    yield build
    for s in servers:
        s.stop()


async def connected(dev: AlpacaDevice) -> AlpacaDevice:
    await dev.put("Connected", timeout=3, Connected=True)
    return dev


async def switch(server: ADCServer) -> AlpacaDevice:
    return await connected(AlpacaDevice(server.address, "switch"))


async def status(sw: AlpacaDevice) -> dict:
    return json.loads(await sw.put("Action", timeout=3, Action="Status", Parameters=""))


async def value(sw: AlpacaDevice, name: str) -> float:
    return await sw.get("GetSwitchValue", timeout=3, Id=SWITCH_IDS[name])


async def flag(sw: AlpacaDevice, name: str) -> bool:
    return await sw.get("GetSwitch", timeout=3, Id=SWITCH_IDS[name])


async def until(sw: AlpacaDevice, name: str, want: bool = True, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while await flag(sw, name) != want:
            await asyncio.sleep(0.02)


def expected_steps(alt: float, az: float, *, swap: bool = False) -> tuple[int, int]:
    pa, zd = adc_math.from_altaz(alt, az, LAT)
    a, b = adc_math.motor_degrees(pa, adc_math.legacy_deflection(zd), swap=swap)
    return adc_math.steps_for(a) % 4000, adc_math.steps_for(b) % 4000


async def test_starting_connecting_reading_and_stopping_never_move(serve):
    s = serve(FakeFeed(), sim={"position": [123, 3456]})
    sim = s.sim
    sw = await switch(s)
    rots = [await connected(AlpacaDevice(s.address, "rotator", n)) for n in (0, 1)]
    assert await sw.get("MaxSwitch", timeout=3) == len(SWITCHES)
    for i, spec in enumerate(SWITCHES):
        assert await sw.get("GetSwitchName", timeout=3, Id=i) == spec.name
        assert await sw.get("CanWrite", timeout=3, Id=i) == (spec.name == "Tracking")
        await sw.get("GetSwitchValue", timeout=3, Id=i)
    assert await value(sw, "PrismA") == pytest.approx(123 * 0.09)
    assert await rots[1].get("MechanicalPosition", timeout=3) == pytest.approx(3456 * 0.09)
    for r in rots:
        await r.get("Position", timeout=3)
        await r.get("DeviceState", timeout=3)
    st = await status(sw)
    assert st["tracking"] is False and st["referenced"] is True and st["fault"] is None
    # Tracking off when it is already off moves nothing.
    await sw.put("SetSwitch", timeout=3, Id=0, State=False)
    await asyncio.sleep(0.2)
    s.stop()
    assert moving_commands(sim) == [] and set(sim.commands) == {"Apr p", "Bpr p"}


async def test_management_lists_three_devices(serve):
    s = serve(FakeFeed())
    async with __import__("httpx").AsyncClient() as http:
        r = await http.get(f"http://{s.address}/management/v1/configureddevices")
        devices = r.json()["Value"]
        assert [(d["DeviceType"], d["DeviceNumber"]) for d in devices] == [
            ("Switch", 0),
            ("Rotator", 0),
            ("Rotator", 1),
        ]
        bad = await http.get(f"http://{s.address}/api/v1/rotator/2/position")
        assert bad.status_code == 400


async def test_not_connected(serve):
    s = serve(FakeFeed())
    with pytest.raises(NotConnectedException):
        await AlpacaDevice(s.address, "switch").get("GetSwitch", timeout=3, Id=0)
    with pytest.raises(NotConnectedException):
        await AlpacaDevice(s.address, "rotator", 1).get("Position", timeout=3)


async def test_tracking_needs_homing_and_homing_is_explicit(serve):
    s = serve(FakeFeed(), sim={"position": [500, 700]}, trust_counter=False)
    sw = await switch(s)
    assert await flag(sw, "Referenced") is False
    with pytest.raises(InvalidOperationException, match="not referenced"):
        await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    rot = await connected(AlpacaDevice(s.address, "rotator"))
    with pytest.raises(InvalidOperationException, match="not referenced"):
        await rot.put("MoveMechanical", timeout=3, Position=10.0)
    assert moving_commands(s.sim) == []
    reply = await sw.put("Action", timeout=3, Action="Home", Parameters="")
    assert "homing" in reply
    await until(sw, "Referenced")
    assert moving_commands(s.sim) == ["Acl HO", "Bcl HO"] and steps(s.sim) == (0, 0)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")


async def test_tracks_the_telescope_and_settles(serve):
    feed = FakeFeed(alt=50.0, az=120.0)
    s = serve(feed)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    want = expected_steps(50.0, 120.0)
    assert abs(steps(s.sim)[0] - want[0]) <= 2 and abs(steps(s.sim)[1] - want[1]) <= 2
    pa, zd = adc_math.from_altaz(50.0, 120.0, LAT)
    assert await value(sw, "ParallacticAngle") == pytest.approx(pa, abs=0.01)
    assert await value(sw, "ZenithDistance") == pytest.approx(zd, abs=0.01)
    assert await value(sw, "Deflection") == 20.0
    # Rising in the east: the zenith distance changes faster than the PA. The
    # legacy daemon only moved on a 2 deg PA change; this moves on either.
    n = len(moving_commands(s.sim))
    assert abs(adc_math.from_altaz(40.0, 118.0, LAT)[0] - pa) < 1.0
    feed.set(altitude=40.0, azimuth=118.0)
    await wait_until(lambda: len(moving_commands(s.sim)) > n, 3.0)
    await until(sw, "Settled")
    assert await value(sw, "Deflection") == 27.0
    # Inside the deadband: no move.
    n = len(moving_commands(s.sim))
    feed.set(azimuth=118.2)
    await asyncio.sleep(0.4)
    assert len(moving_commands(s.sim)) == n and await flag(sw, "Settled")


async def test_settled_waits_for_a_read_after_tracking_is_written(serve):
    feed = FakeFeed()
    s = serve(feed, update_interval=0.5)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    await asyncio.sleep(0.1)
    # The telescope has moved on (a short slew the ADC never saw), and the
    # sequencer writes Tracking again: Settled is false until a new read.
    feed.set(altitude=40.0, azimuth=250.0)
    before = feed.reads
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    assert await flag(sw, "Settled") is False or feed.reads > before
    await until(sw, "Settled")
    want = expected_steps(40.0, 250.0)
    assert abs(steps(s.sim)[0] - want[0]) <= 2


async def test_stale_or_invalid_input_holds(serve):
    feed = FakeFeed()
    s = serve(feed)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    n = len(moving_commands(s.sim))
    feed.error = ConnectionError("telescope server gone")
    feed.set(altitude=30.0, azimuth=200.0)  # would move, if it were read
    await until(sw, "InputFresh", False)
    assert await flag(sw, "Settled") is False
    st = await status(sw)
    assert "telescope read failed" in st["hold"] and st["tracking"]
    await asyncio.sleep(0.3)
    assert len(moving_commands(s.sim)) == n
    feed.error = None
    feed.set(altitude=float("nan"))
    await asyncio.sleep(0.3)
    assert len(moving_commands(s.sim)) == n
    assert "invalid" in (await status(sw))["hold"]
    feed.set(altitude=30.0)
    await until(sw, "Settled")
    assert len(moving_commands(s.sim)) > n


async def test_holds_while_slewing_then_follows(serve):
    feed = FakeFeed()
    s = serve(feed, settle_after_slew=0.3)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    n = len(moving_commands(s.sim))
    feed.set(slewing=True, altitude=35.0, azimuth=260.0)
    await wait_until(lambda: "slewing" in (s.adc.hold or ""), 2.0)
    await asyncio.sleep(0.3)
    assert len(moving_commands(s.sim)) == n and await flag(sw, "Settled") is False
    feed.set(slewing=False)
    await wait_until(lambda: "settling" in (s.adc.hold or ""), 2.0)
    assert len(moving_commands(s.sim)) == n
    await until(sw, "Settled")
    want = expected_steps(35.0, 260.0)
    assert abs(steps(s.sim)[1] - want[1]) <= 2


async def test_park_turns_the_prisms_neutral_once(serve):
    feed = FakeFeed()
    s = serve(feed)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    feed.set(at_park=True, altitude=90.0, azimuth=0.0)
    await wait_until(lambda: steps(s.sim) == (0, 0) and not s.sim.moving(), 3.0)
    n = len(moving_commands(s.sim))
    await asyncio.sleep(0.3)
    assert len(moving_commands(s.sim)) == n  # once
    st = await status(sw)
    assert st["hold"] == "telescope parked" and not st["settled"]


async def test_park_hold_and_the_zenith_distance_limit(serve):
    feed = FakeFeed()
    s = serve(feed, park_action="hold", max_zenith_distance=70.0)
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    n = len(moving_commands(s.sim))
    feed.set(at_park=True)
    await asyncio.sleep(0.3)
    assert len(moving_commands(s.sim)) == n
    feed.set(at_park=False, altitude=15.0)  # below the limit: hold
    await wait_until(lambda: "beyond" in (s.adc.hold or ""), 2.0)
    await asyncio.sleep(0.2)
    assert len(moving_commands(s.sim)) == n


async def test_near_zenith_it_does_not_chase_the_parallactic_angle(serve):
    feed = FakeFeed(alt=89.0, az=10.0)
    s = serve(feed, sim={"position": [0, 0]})
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    for az in (90.0, 180.0, 270.0):
        feed.set(azimuth=az)
        await asyncio.sleep(0.15)
    assert moving_commands(s.sim) == [] and await flag(sw, "Settled")
    assert await value(sw, "Deflection") == 0.0


async def test_tracking_off_turns_the_prisms_neutral(serve):
    s = serve(FakeFeed())
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    assert steps(s.sim) != (0, 0)
    await sw.put("SetSwitchValue", timeout=3, Id=0, Value=0.0)
    assert await flag(sw, "Tracking") is False
    await wait_until(lambda: steps(s.sim) == (0, 0) and not s.sim.moving(), 3.0)
    assert (await status(sw))["last_move"]["kind"] == "neutral"


async def test_a_stuck_motor_is_a_fault_and_tracking_holds(serve):
    feed = FakeFeed()
    s = serve(feed, stall_time=0.3)
    s.sim.stuck = {"B"}
    sw = await switch(s)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Fault")
    st = await status(sw)
    assert "stopped at" in st["fault"] and st["hold"] in (None, "fault") and not st["settled"]
    n = len(moving_commands(s.sim))
    feed.set(altitude=30.0, azimuth=220.0)
    await asyncio.sleep(0.4)
    assert len(moving_commands(s.sim)) == n  # no chasing after a fault
    s.sim.stuck = set()
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)  # clears it
    await until(sw, "Settled")
    assert await flag(sw, "Fault") is False


async def test_a_dead_line_is_a_fault_not_a_hang(serve):
    feed = FakeFeed()
    s = serve(feed, port={"type": "sim", "sim": {"speed": 20000.0}, "timeout": 0.2})
    sw = await switch(s)
    s.sim.silent = True
    t = time.monotonic()
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Fault")
    assert time.monotonic() - t < 3.0
    assert "move" in (await status(sw))["fault"]


async def test_rotators_move_one_prism_by_hand(serve):
    s = serve(FakeFeed())
    sw = await switch(s)
    a = await connected(AlpacaDevice(s.address, "rotator", 0))
    b = await connected(AlpacaDevice(s.address, "rotator", 1))
    assert await b.get("CanReverse", timeout=3) is True
    assert await b.get("StepSize", timeout=3) == pytest.approx(0.09)
    await b.put("MoveMechanical", timeout=3, Position=90.0)
    await wait_until(lambda: not s.sim.moving(), 3.0)
    await wait_until(lambda: not s.adc.moving(), 3.0)
    assert steps(s.sim) == (0, 1000)
    assert await b.get("MechanicalPosition", timeout=3) == pytest.approx(90.0)
    await b.put("Sync", timeout=3, Position=10.0)
    assert await b.get("Position", timeout=3) == pytest.approx(10.0)
    await b.put("Move", timeout=3, Position=-20.0)
    await wait_until(lambda: not s.adc.moving(), 3.0)
    assert await b.get("Position", timeout=3) == pytest.approx(350.0, abs=0.1)
    assert await b.get("MechanicalPosition", timeout=3) == pytest.approx(70.0, abs=0.1)
    await b.put("Reverse", timeout=3, Reverse=True)
    assert await b.get("Reverse", timeout=3) is True
    with pytest.raises(NotImplementedException):
        await a.put("Halt", timeout=3)
    with pytest.raises(InvalidValueException):
        await a.put("MoveAbsolute", timeout=3, Position=360.0)
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    with pytest.raises(InvalidOperationException, match="tracking is on"):
        await a.put("MoveAbsolute", timeout=3, Position=10.0)
    with pytest.raises(InvalidOperationException, match="tracking is on"):
        await sw.put("Action", timeout=3, Action="Home", Parameters="")


async def test_instrument_selects_the_band(serve):
    feed = FakeFeed(alt=40.0)
    s = serve(
        feed,
        deflection="refraction",
        instruments={"tres": {}, "fast": {"blue_um": 0.32, "red_um": 0.6}},
    )
    sw = await switch(s)
    assert await sw.put("Action", timeout=3, Action="Instrument", Parameters="") == "tres"
    await sw.put("SetSwitch", timeout=3, Id=0, State=True)
    await until(sw, "Settled")
    tres = await value(sw, "Deflection")
    assert await sw.put("Action", timeout=3, Action="Instrument", Parameters="fast") == "fast"
    await until(sw, "Settled")
    assert await value(sw, "Deflection") != tres
    with pytest.raises(InvalidValueException):
        await sw.put("Action", timeout=3, Action="Instrument", Parameters="keplercam")


def test_hardware_needs_the_config_and_the_flag():
    cfg = ADCConfig.from_dict(json.loads((SITE / "adc.json").read_text()))
    assert cfg.port.type == "serial" and not cfg.allow_hardware
    for hardware in (False, True):
        with pytest.raises(HardwareNotAllowed, match="allow_hardware"):
            ADCServer(cfg, hardware=hardware)
    cfg.allow_hardware = True
    with pytest.raises(HardwareNotAllowed):
        ADCServer(cfg, hardware=False)


def test_config_validation():
    for bad, match in (
        ({"bogus": 1}, "unknown config keys"),
        ({"deflection": "magic"}, "deflection"),
        ({"park_action": "stow"}, "park_action"),
        ({"instrument": "keplercam"}, "not in instruments"),
        ({"instruments": {"tres": {"blue_um": 0.9, "red_um": 0.4}}}, "blue_um"),
        ({"atmosphere": {"wind": 3}}, "atmosphere"),
        ({"motors": ["A", "A"]}, "distinct"),
        ({"deadband": 0}, "deadband"),
        ({"port": {"sim": {"bogus": 1}}}, "port.sim"),
    ):
        with pytest.raises(ValueError, match=match):
            ADCConfig.from_dict(bad)


def test_shipped_site_configs():
    cfg = ADCConfig.from_dict(json.loads((SITE / "adc.json").read_text()))
    assert cfg.listen.startswith("127.0.0.1:") and cfg.telescope.startswith("127.0.0.1:")
    assert not cfg.trust_counter and cfg.deflection == "legacy_table"
    site = json.loads((SITE / "flwo60.json").read_text())
    (entry,) = telescopes(site).values()
    assert device_address(entry["adc"]) == (cfg.listen, 0)
    units = build_units(site, Timeline(), 5.0)
    assert units[0].adc is not None
    assert (ADC_TRACKING, ADC_SETTLED) == (SWITCH_IDS["Tracking"], SWITCH_IDS["Settled"])


def test_the_command_line_refuses_hardware_and_runs_the_simulator(tmp_path):
    cfg = str(SITE / "adc.json")
    mod = "robofast_core.devices.adc_server"
    p = subprocess.run(
        [sys.executable, "-m", mod, "--config", cfg, "--hardware"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert p.returncode == 2 and "allow_hardware" in p.stderr
    proc = subprocess.Popen(
        [sys.executable, "-m", mod, "--config", cfg, "--simulate", "--listen", "127.0.0.1:0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    try:
        assert proc.stdout is not None
        hello = json.loads(proc.stdout.readline())
        assert hello["port"] == "simulator" and hello["url"].startswith("http://127.0.0.1:")
    finally:
        proc.terminate()
        proc.wait(20)
    assert proc.returncode in (0, -15)


async def test_reads_robofasts_own_telescope_server():
    """The feed against the TCSGO server on its simulated TCS: the input
    the ADC uses on the 60-inch, with no legacy software in between."""
    from robofast_core.devices.tcsgo_server import TCSGOConfig, TCSGOServer
    from robofast_core.devices.tcsgo_sim import TCSGOSim

    sim = TCSGOSim().start()
    server = None
    feed = None
    try:
        server = TCSGOServer(
            TCSGOConfig.from_dict(
                {
                    "name": "t",
                    "listen": "127.0.0.1:0",
                    "tcs": {
                        "host": "127.0.0.1",
                        "port": sim.port,
                        "connect_timeout": 1.0,
                        "command_timeout": 1.0,
                        "ack_window": 0.02,
                        "min_interval": 0.005,
                    },
                    "status_max_age": 0.05,
                }
            )
        ).start()
        feed = AlpacaTelescopeFeed(server.address, 0, 3.0)
        r = await feed.read()
        tel = await connected(AlpacaDevice(server.address, "telescope"))
        assert r.altitude == pytest.approx(await tel.get("Altitude", timeout=3), abs=0.1)
        assert r.azimuth == pytest.approx(await tel.get("Azimuth", timeout=3), abs=0.1)
        assert r.slewing is False
        await tel.aclose()
    finally:
        if feed is not None:
            await feed.close()
        if server is not None:
            server.stop()
        sim.stop()


# -- the sequencer ------------------------------------------------------------------------------


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


def attach_adc(rig, server: ADCServer):
    unit = rig.sequencer.units["t1"]
    unit.adc = alpaca_owner("t1.adc", "switch", server.address, rig.timeline)
    rig.owners["t1.adc"] = unit.adc
    return unit.adc


async def test_sequencer_tracks_before_light_frames_and_neutral_at_cleanup(
    make_rig, serve, tmp_path
):
    s = serve(FakeFeed())
    plan = StaticPlan(
        [
            {"name": "star", "ra_hours": 3.0, "dec_deg": 20.0, "exptime": 0.1, "count": 2},
            {"name": "dark", "light": False, "exptime": 0.1, "count": 1},
        ],
        ["t1"],
    )
    rig = make_rig(plan=plan)
    adc = attach_adc(rig, s)
    imaging = Imaging(
        ImagingConfig(data_root=str(tmp_path / "data"), site="testsite", enclosure_interval=0.1),
        timeline=rig.timeline,
        site={"latitude": LAT, "longitude": -110.88, "elevation": 2344.0},
        adcs={"t1": adc},
    )
    rig.sequencer.imaging = imaging
    try:
        await rig.start()
        await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    finally:
        await imaging.aclose()
    assert plan.history == [("t1", "star", "complete", 2), ("t1", "dark", "complete", 1)]
    settled = _events(rig, "adc_settled")
    assert len(settled) == 1  # the dark frame does not touch the ADC
    assert _events(rig, "on_target")[0].t < settled[0].t < _events(rig, "exposure_started")[0].t
    await wait_until(lambda: not s.adc.tracking, 10.0)
    await wait_until(lambda: steps(s.sim) == (0, 0) and not s.sim.moving(), 5.0)
    from astropy.io import fits

    files = sorted((tmp_path / "data").rglob("*.fits"))
    lights = [fits.getheader(f) for f in files if fits.getheader(f)["IMAGETYP"] == "LIGHT"]
    assert lights and lights[0]["ADCTRACK"] is True and lights[0]["ADCSETTL"] is True
    assert lights[0]["ADCDEFL"] == 20.0 and 0 <= lights[0]["ADCANGA"] < 360


async def test_an_adc_that_never_settles_is_a_warning_not_a_failure(make_rig, serve):
    feed = FakeFeed()
    feed.error = ConnectionError("no telescope")
    s = serve(feed)
    plan = StaticPlan(
        [{"name": "star", "ra_hours": 3.0, "dec_deg": 20.0, "exptime": 0.1, "count": 1}], ["t1"]
    )
    rig = make_rig(plan=plan, seq=dict(adc_settle_timeout=0.5))
    attach_adc(rig, s)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    assert plan.history == [("t1", "star", "complete", 1)]
    (warn,) = _events(rig, "adc_not_settled")
    assert warn.detail["error"] == "not settled in time"
    assert moving_commands(s.sim) == []
