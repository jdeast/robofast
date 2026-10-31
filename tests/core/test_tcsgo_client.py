"""The TCSGalil client against the TCSGalil simulator, one command at a time.

Fast tier: the simulator runs on a loopback thread in this process. Nothing
here leaves loopback.
"""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest

from robofast_core.devices.tcsgo_client import (
    ALLOWED,
    COMMANDS,
    EAST,
    NORTH,
    HardwareNotAllowed,
    MountCoords,
    TCSClient,
    TCSProtocolError,
    TCSStatus,
    TCSTimeout,
    check_tcs_address,
    encode,
    parse_reply,
)
from robofast_core.devices.tcsgo_sim import TCSGOSim, altaz_to_hadec, hadec_to_altaz

NOT_LOOPBACK = "192.0.2.1"  # RFC 5737 documentation address; never contacted
FAST = dict(connect_timeout=1.0, command_timeout=1.0, ack_window=0.05, min_interval=0.01)


@pytest.fixture
async def rig():
    sims: list[TCSGOSim] = []
    clients: list[TCSClient] = []

    def build(sim_kw: dict | None = None, **client_kw) -> tuple[TCSGOSim, TCSClient]:
        sim = TCSGOSim(**{"slew_rate": 30.0, "shutter_time": 0.3, **(sim_kw or {})}).start()
        sims.append(sim)
        client = TCSClient("127.0.0.1", sim.port, **{**FAST, **client_kw})
        clients.append(client)
        return sim, client

    yield build
    for c in clients:
        await c.aclose()
    for s in sims:
        s.stop()


async def until(pred, timeout: float, what: str = "condition") -> None:
    t0 = time.monotonic()
    while not await pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.03)


# -- encoding, parsing, allowlist -----------------------------------------------------


def test_encode_and_parse():
    assert encode(6, 18.3457, 35.3456, 2002.5) == b"#6,18.3457,35.3456,2002.5;\r"
    assert encode(26) == b"#26;\r"
    assert encode(61, 0, 1250) == b"#61,0,1250;\r"
    with pytest.raises(TypeError):
        encode(6, 1.0)
    with pytest.raises(ValueError):
        encode(11)  # no such command
    with pytest.raises(ValueError):
        encode(27, float("nan"))
    # The manual's own status example.
    st = TCSStatus.from_values(parse_reply(26, b"#58,0,30,0,12,94,24,0;"))
    assert st.track_switch_up and not st.drives_switch_up
    assert st.east_limit and st.west_limit and st.north_limit and st.south_limit
    assert st.focus_outer_limit and st.focus_inner_limit
    assert st.tcp_connected and st.use_cos_dec and st.galil_stale
    assert st.mode == "hold" and st.target_captured  # 24: bits 3 and 4
    assert st.limits == ["east_limit", "west_limit", "north_limit", "south_limit"]
    # Round trip, every flag.
    flags = {name: True for name in TCSStatus.__dataclass_fields__ if name != "raw"}
    assert TCSStatus.from_values(TCSStatus.encode(**flags)).raw == (
        63,
        127,
        255,
        127,
        63,
        127,
        31,
        63,
    )
    for bad in (b"#1,2,3;", b"58,0,30,0,12,94,24;", b"#58,0,30,x,12,94,24,0;", b"garbage"):
        with pytest.raises(TCSProtocolError):
            parse_reply(26, bad)
    with pytest.raises(TCSProtocolError):
        TCSStatus.from_values((256.0, 0, 0, 0, 0, 0, 0, 0))
    m = MountCoords.from_values(
        parse_reply(25, b"#2.03,20.03,33.48,1994.5,1.35,15.6,138.2,22.03,5.25,2026.8;")
    )
    assert m.altitude_deg == pytest.approx(74.4) and m.equinox == 1994.5


def test_allowlist_excludes_configuration_commands():
    never = {2, 3, 4, 5, 8, 9, 14, 15, 16, 18, 19, 22, 23, 24, 29, 30, 31, 37, 38, 39}
    never |= {42, 51, 53, 54, 55, 56, 57, 60, 63, 66, 67, 68, 69, 70, 72, 73}
    assert ALLOWED.isdisjoint(never)
    assert ALLOWED | never == set(COMMANDS) - {28}
    # The relative commands are never re-sent.
    assert {n for n, c in COMMANDS.items() if not c.idempotent} == {7, 61}


async def test_refuses_disallowed_commands_and_hardware(rig):
    sim, c = rig()
    for n in (3, 24, 30, 31, 56, 60, 11):
        with pytest.raises(ValueError):
            await c.command(n)
    assert sim.log == []
    with pytest.raises(HardwareNotAllowed):
        TCSClient(NOT_LOOPBACK, 5003)
    with pytest.raises(HardwareNotAllowed):
        check_tcs_address(NOT_LOOPBACK, allow_hardware=False)
    assert check_tcs_address("127.0.0.1", allow_hardware=False) == "127.0.0.1"
    with pytest.raises(ValueError):
        TCSClient("localhost", 5003)  # an IP literal only


# -- every allowed command against the simulator ----------------------------------------


async def test_reads(rig):
    _sim, c = rig()
    st = await c.status()
    assert st.mode == "hold" and st.drives_switch_up and st.upper_shutter_closed
    m = await c.mount()
    assert m.altitude_deg == pytest.approx(90.0) and m.equinox == 2000.0
    assert 0 <= m.lst_hours < 24 and m.airmass == pytest.approx(1.0)
    t = await c.target()
    assert t.elapsed_s == 0.0
    assert await c.focus_position() == 0.0
    assert await c.dome_azimuth() == pytest.approx(180.0)


async def test_slew_follow_and_target(rig):
    _sim, c = rig()
    m = await c.mount()
    ra, dec = (m.lst_hours - 1.0) % 24, 20.0
    await c.command(6, ra, dec, 2000.0)  # #6 alone does not move the mount
    await asyncio.sleep(0.2)
    assert (await c.mount()).altitude_deg == pytest.approx(90.0)
    t = await c.target()
    assert t.ra_hours == pytest.approx(ra) and t.dec_deg == pytest.approx(dec)
    await c.command(12, 2)
    assert (await c.status()).mode == "slew"
    await until(lambda: _mode(c, "follow"), 5, "captured")
    st = await c.status()
    assert st.target_captured and not st.target_out_of_range
    m = await c.mount()
    assert m.ra_hours == pytest.approx(ra, abs=1e-4) and m.dec_deg == pytest.approx(dec)
    await asyncio.sleep(0.5)  # following: RA stays put while HA advances
    m2 = await c.mount()
    assert m2.ra_hours == pytest.approx(ra, abs=1e-4) and m2.ha_hours > m.ha_hours


async def _mode(c: TCSClient, mode: str) -> bool:
    return (await c.status()).mode == mode


async def test_out_of_range_target(rig):
    _sim, c = rig()
    m = await c.mount()
    await c.command(6, (m.lst_hours + 12) % 24, 0.0, 2000.0)  # below the horizon
    st = await c.status()
    assert st.target_out_of_range
    await c.command(12, 2)
    assert (await c.status()).mode != "slew"


async def test_stop_tracking_zenith_park_unpark(rig):
    sim, c = rig()
    await c.command(50)  # start tracking where it is
    assert (await c.status()).mode == "follow" and not sim.parked()
    await c.command(13)
    assert (await c.status()).mode == "hold"
    m = await c.mount()
    ha, dec = altaz_to_hadec(60.0, 90.0, sim.latitude)
    await c.command(6, (m.lst_hours - ha) % 24, dec, 2000.0)
    await c.command(12, 3)  # chase
    await until(lambda: _captured(c), 5, "chase captures")
    assert (await c.status()).mode == "chase"
    await c.command(58)  # park: slew to the park position, then hold
    await until(lambda: _mode(c, "hold"), 5, "parked")
    assert sim.parked() and (await c.mount()).altitude_deg == pytest.approx(90.0)
    await c.command(59)
    assert not sim.parked()
    await c.command(50)
    await c.command(10)  # zenith target
    await c.command(12, 2)
    await until(lambda: _mode(c, "follow"), 5, "at zenith")
    assert (await c.mount()).altitude_deg == pytest.approx(90.0, abs=0.01)


async def _captured(c: TCSClient) -> bool:
    return (await c.status()).target_captured


async def test_offset_and_pulse_guide(rig):
    sim, c = rig()
    await c.command(50)
    m0 = await c.mount()
    await c.command(7, 0.0, 36.0)  # target 36" north; follow mode: the mount waits
    await asyncio.sleep(0.2)
    assert (await c.mount()).dec_deg == pytest.approx(m0.dec_deg)
    await c.command(12, 2)
    await until(lambda: _mode(c, "follow"), 5, "offset done")
    assert (await c.mount()).dec_deg == pytest.approx(m0.dec_deg + 0.01, abs=1e-5)
    m1 = await c.mount()
    await c.command(61, NORTH, 400)
    assert (await c.status()).pulse_guiding
    await until(lambda: _not_guiding(c), 3, "pulse done")
    m2 = await c.mount()
    assert (m2.dec_deg - m1.dec_deg) * 3600 == pytest.approx(sim.guide_rate * 0.4, abs=0.3)
    await c.command(61, EAST, 400)
    await until(lambda: _not_guiding(c), 3, "pulse done")
    m3 = await c.mount()
    assert (m3.ra_hours - m2.ra_hours) * 15 * 3600 == pytest.approx(sim.guide_rate * 0.4, abs=0.3)


async def _not_guiding(c: TCSClient) -> bool:
    return not (await c.status()).pulse_guiding


async def test_focus(rig):
    sim, c = rig({"focus_rate": 2000.0})
    await c.command(27, -300)
    st = await c.status()
    assert st.focus_moving_in and not st.focus_moving_out
    await until(lambda: _focus_at(c, -300), 3, "focus")
    await c.command(27, 99999)  # clamped at the outer limit
    await until(lambda: _focus_at(c, sim.focus_max), 10, "focus at limit")
    assert (await c.status()).focus_outer_limit


async def _focus_at(c: TCSClient, v: float) -> bool:
    return await c.focus_position() == pytest.approx(v)


async def test_dome_and_shutters(rig):
    sim, c = rig({"dome_rate": 90.0})
    await c.command(43, 90.0)
    await c.command(20, 2)
    st = await c.status()
    assert st.dome_goto_mode and st.dome_rotating_left
    await until(lambda: _dome_at(c, 90.0), 5, "dome az")
    await c.command(20, 3)  # find home
    await until(lambda: _home(c), 5, "home")
    await c.command(20, 1)  # track the telescope
    assert (await c.status()).dome_track_mode
    await c.command(35)  # stop: manual
    assert (await c.status()).dome_manual_mode
    await c.command(36, 1)
    await c.command(44, 1)
    st = await c.status()
    assert not st.upper_shutter_closed and not st.upper_shutter_open
    await until(lambda: _shutters(c, True), 3, "open")
    await c.command(36, 0)
    await c.command(40)  # stop the upper part-way
    await c.command(44, 0)
    await asyncio.sleep(0.5)
    st = await c.status()
    assert st.lower_shutter_closed and not st.upper_shutter_open and not st.upper_shutter_closed
    assert sim.shutters["upper"].state == "stopped"
    await c.command(36, 0)
    await c.command(41)  # nothing moving: harmless
    await until(lambda: _shutters(c, False), 3, "closed")
    # The AUTO DOME switch down: dome modes are refused (the manual).
    sim.set_switch("auto_dome_switch_up", False)
    await c.command(20, 1)
    assert not (await c.status()).dome_track_mode


async def _dome_at(c: TCSClient, az: float) -> bool:
    return abs(await c.dome_azimuth() - az) < 0.01


async def _home(c: TCSClient) -> bool:
    return (await c.status()).dome_at_home


async def _shutters(c: TCSClient, opened: bool) -> bool:
    st = await c.status()
    if opened:
        return st.upper_shutter_open and st.lower_shutter_open
    return st.upper_shutter_closed and st.lower_shutter_closed


# -- faults -------------------------------------------------------------------------


async def test_hang_is_bounded_and_queries_retry(rig):
    sim, c = rig(command_timeout=0.3, max_attempts=2)
    sim.set_mode("hang")
    t0 = time.monotonic()
    with pytest.raises(TCSTimeout):
        await c.status()
    assert time.monotonic() - t0 < 2.0 and c.retries == 1
    sim.set_mode("ok")
    assert (await c.status()).mode == "hold"


async def test_garbled_reply_is_an_error_not_a_state(rig):
    sim, c = rig(max_attempts=3)
    sim.set_mode("garble")
    with pytest.raises(TCSProtocolError):
        await c.status()
    assert c.retries == 2  # bounded: three attempts in all
    sim.set_mode("ok")
    await c.status()


async def test_set_commands_are_never_resent_once_sent(rig):
    sim, c = rig(max_attempts=3)
    await c.command(50)
    before = c.exchanges
    await c.command(7, 1.0, 1.0)
    assert c.exchanges == before + 1 and sim.numbers().count(7) == 1
    # A TCS that never reads: the connect works, the write lands, no retry.
    sim.set_mode("hang")
    await c.command(7, 1.0, 1.0)  # documented: no reply, so nothing to wait for
    assert c.exchanges == before + 2 and c.retries == 0
    sim.set_mode("ok")


async def test_connection_refused_is_retried_then_reported(rig):
    sim, c = rig(max_attempts=2)
    sim.stop()
    with pytest.raises(Exception, match="cannot connect"):
        await c.command(13)
    assert c.retries == 1  # never reached the wire, so it was safe to retry


async def test_drives_off_and_limits(rig):
    sim, c = rig()
    sim.set_drives(False)
    st = await c.status()
    assert not st.drives_switch_up
    m = await c.mount()
    await c.command(6, (m.lst_hours - 1) % 24, 20.0, 2000.0)
    await c.command(12, 2)
    await asyncio.sleep(0.3)
    assert (await c.mount()).altitude_deg == pytest.approx(90.0)  # nothing moved
    sim.set_drives(True)
    await until(lambda: _mode(c, "follow"), 5, "moves once the drives are on")
    sim.hit_limit("west")
    st = await c.status()
    assert st.west_limit and st.mode == "hold" and st.limits == ["west_limit"]
    sim.clear_limit()
    assert (await c.status()).limits == []


async def test_tracking_into_the_horizon_hits_the_soft_limit(rig):
    sim, c = rig({"min_alt": 15.0})
    ha, dec = altaz_to_hadec(15.004, 270.0, sim.latitude)  # just above, in the west
    sim.ha, sim.dec, sim.mount_mode = ha, dec, 0
    sim.parked_flag = False
    assert hadec_to_altaz(ha, dec, sim.latitude)[0] == pytest.approx(15.004)
    assert (await c.status()).approaching_soft_limit
    await c.command(50)
    await until(lambda: _soft(c), 10, "soft limit")
    assert (await c.status()).mode == "hold"


async def _soft(c: TCSClient) -> bool:
    return (await c.status()).soft_limit


async def test_echoing_tcs_is_tolerated(rig):
    _sim, c = rig({"ack": "echo"})
    await c.command(13)
    assert c.acks == [b"#13;"]
    assert (await c.status()).mode == "hold"  # the echo did not desynchronise anything


async def test_spacing_priority_and_abandoned_commands(rig):
    sim, c = rig(min_interval=0.1)
    queries = [asyncio.create_task(c.status()) for _ in range(4)]
    await asyncio.sleep(0)
    stop = asyncio.create_task(c.command(13))
    await asyncio.gather(*queries, stop)
    times = [t for t, _, _ in sim.log]
    assert all(b - a >= 0.09 for a, b in itertools.pairwise(times))
    assert sim.numbers().index(13) <= 1  # the stop went ahead of queued queries
    # A caller that gives up while its command is queued: it is never sent.
    slow = [asyncio.create_task(c.status()) for _ in range(3)]
    with pytest.raises(TCSTimeout):
        await c.command(50, timeout=0.05)
    await asyncio.gather(*slow)
    await asyncio.sleep(0.2)
    assert 50 not in sim.numbers()
