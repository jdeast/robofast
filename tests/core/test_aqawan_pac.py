"""The Aqawan PAC protocol client against the PAC simulator (fast tier)."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from robofast_core.devices.aqawan_pac import (
    COMMANDS,
    HardwareNotAllowed,
    PACClient,
    PACError,
    PACPreempted,
    PACProtocolError,
    PACRefused,
    PACTimeout,
    TelnetFilter,
    check_pac_address,
    check_success,
    parse_status,
    split_reply,
)
from robofast_core.devices.aqawan_sim import AqawanSim

#: A documentation address (RFC 5737): never routed, never connected to here.
NOT_LOOPBACK = "192.0.2.1"
FAST = dict(connect_timeout=1.0, command_timeout=2.0, resend_after=0.15, idle_end=0.1)


@pytest.fixture
def make_sim():
    sims: list[AqawanSim] = []

    def build(**kw) -> AqawanSim:
        kw = {"open_time": 0.3, "close_time": 0.3, "settle": 0.05, **kw}
        sim = AqawanSim(**kw).start()
        sims.append(sim)
        return sim

    yield build
    for s in sims:
        s.stop()


def client(sim: AqawanSim, **kw) -> PACClient:
    return PACClient("127.0.0.1", sim.port, **{**FAST, **kw})


async def test_status_is_parsed_into_typed_values(make_sim):
    sim = make_sim()
    c = client(sim)
    st = await c.status()
    assert st.shutters == ("CLOSED", "CLOSED")
    assert st.fault is False and st.error is False and st.lights_on is True
    assert st.entry_door1 == "CLOSED" and st.op_mode == "AUTO"
    assert isinstance(st.uptime, float) and st.temperatures["EnclTemp"] == 12.0
    assert st.heartbeat == 0  # expired at power-on
    # The terminal-type/settle quirk dropped the first command; one re-send fixed it.
    assert sim.dropped == 1 and c.resends == 1


async def test_legacy_timing_one_resend(make_sim):
    """With the legacy 0.5 s re-send and a 0.3 s settle, a command costs ~0.5 s."""
    sim = make_sim(settle=0.3)
    c = PACClient("127.0.0.1", sim.port)
    t0 = time.monotonic()
    await c.status()
    assert 0.45 < time.monotonic() - t0 < 1.5
    assert c.resends == 1 and sim.dropped == 1


async def test_every_allowed_command_gets_a_reply(make_sim):
    sim = make_sim(settle=0.0)
    sim.heartbeat()
    c = client(sim)
    for command in sorted(COMMANDS):
        reply = await c.command(command)
        assert reply, command
        if command not in ("STATUS", "GET_ERRORS", "GET_FAULTS"):
            assert check_success(command, reply) == reply
    assert set(sim.commands()) == COMMANDS


async def test_command_outside_the_allowlist_is_never_sent(make_sim):
    sim = make_sim()
    c = client(sim)
    for bad in ("SIM FAULT", "OPEN_SHUTTER_3", "status", "STATUS\r\nOPEN_SHUTTERS"):
        with pytest.raises(ValueError):
            await c.command(bad)
    assert sim.commands() == [] and c.exchanges == 0


async def test_refusals_carry_the_pac_reason(make_sim):
    sim = make_sim()
    c = client(sim)
    with pytest.raises(PACRefused, match="Heartbeat timer expired") as info:
        await c.action("OPEN_SHUTTER_1")
    assert info.value.reason == "Heartbeat timer expired"
    await c.action("HEARTBEAT")
    sim.set_fault("Estop active", clearable=False)
    with pytest.raises(PACRefused, match="Estop active"):
        await c.action("OPEN_SHUTTERS")
    with pytest.raises(PACRefused, match="Estop active"):
        await c.action("CLEAR_FAULTS")
    sim.set_fault(None)
    sim.set_op_mode("MANUAL")
    with pytest.raises(PACRefused, match="not in AUTO"):
        await c.action("CLOSE_SEQUENTIAL")


async def test_open_close_and_heartbeat_watchdog(make_sim):
    sim = make_sim(heartbeat_timeout=1.5)
    c = client(sim)
    await c.action("HEARTBEAT")
    await c.action("OPEN_SHUTTER_1")
    assert (await c.status()).shutter1 == "OPENING"
    await asyncio.sleep(0.4)
    assert (await c.status()).shutter1 == "OPEN"
    # No more heartbeats: the PAC closes by itself.
    t0 = time.monotonic()
    while not sim.closed():
        assert time.monotonic() - t0 < 2.5
        await asyncio.sleep(0.02)
    assert sim.watchdog_trips == 1
    with pytest.raises(PACRefused, match="Heartbeat"):
        await c.action("OPEN_SHUTTER_2")


async def test_close_sequential_closes_one_then_the_other(make_sim):
    sim = make_sim(close_order=(2, 1), heartbeat_timeout=None)
    c = client(sim)
    await c.action("OPEN_SHUTTERS")
    await asyncio.sleep(0.4)
    await c.action("CLOSE_SEQUENTIAL")
    assert sim.shutter_states() == ("OPEN", "CLOSING")
    await asyncio.sleep(0.4)
    assert sim.shutter_states() == ("CLOSING", "CLOSED")
    await asyncio.sleep(0.3)
    assert sim.closed()


async def test_garbled_status_is_a_protocol_error(make_sim):
    sim = make_sim()
    sim.set_mode("garble")
    with pytest.raises(PACProtocolError, match="missing"):
        await client(sim).status()


async def test_hung_pac_times_out(make_sim):
    sim = make_sim()
    sim.set_mode("hang")
    c = client(sim, command_timeout=0.8)
    t0 = time.monotonic()
    with pytest.raises(PACTimeout):
        await c.status()
    assert time.monotonic() - t0 < 1.2
    sim.set_mode("ok")
    assert (await c.status()).shutter1 == "CLOSED"  # recovers


async def test_slow_pac_beyond_the_deadline_times_out(make_sim):
    sim = make_sim(slow_delay=1.0)
    sim.set_mode("slow")
    with pytest.raises(PACTimeout):
        await client(sim, command_timeout=0.6).status()


async def test_pac_that_hangs_up_is_an_error(make_sim):
    sim = make_sim()
    sim.set_mode("silent")
    t0 = time.monotonic()
    with pytest.raises(PACError):
        await client(sim).status()
    assert time.monotonic() - t0 < 1.0


async def test_nothing_listening_is_an_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(PACError, match="cannot connect"):
        await PACClient("127.0.0.1", port, **FAST).status()


async def test_resends_are_bounded(make_sim):
    """A PAC that drops everything: max_attempts sends, then a timeout."""
    sim = make_sim(settle=10.0)
    c = client(sim, resend_after=0.1, max_attempts=3, command_timeout=3.0)
    t0 = time.monotonic()
    with pytest.raises(PACTimeout, match="after 3 attempts"):
        await c.command("HEARTBEAT")
    assert time.monotonic() - t0 < 1.0
    assert sim.dropped == 3 and sim.commands() == []


@pytest.mark.parametrize(
    "quirks",
    [dict(prompt=False), dict(echo=True), dict(negotiate=False), dict(settle=0.0)],
    ids=["no-prompt", "echo", "no-negotiation", "no-settle"],
)
async def test_protocol_variants(make_sim, quirks):
    sim = make_sim(**quirks)
    c = client(sim)
    assert (await c.status()).shutters == ("CLOSED", "CLOSED")
    assert check_success("HEARTBEAT", await c.command("HEARTBEAT"))


async def test_close_preempts_a_routine_exchange(make_sim):
    sim = make_sim(slow_delay=1.5, heartbeat_timeout=None)
    c = client(sim, command_timeout=5.0)
    sim.set_mode("slow")
    status = asyncio.create_task(c.status())
    await asyncio.sleep(0.3)
    sim.set_mode("ok")
    t0 = time.monotonic()
    reply = await c.action("CLOSE_SEQUENTIAL")
    assert "Success=TRUE" in reply and time.monotonic() - t0 < 1.0
    with pytest.raises(PACPreempted):
        await status


# -- pure functions ---------------------------------------------------------------


def test_split_reply():
    assert split_reply("", "STATUS") == (None, False)
    assert split_reply("\r\n#>", "STATUS") == (None, False)  # a bare prompt
    assert split_reply("\r\n#>STATUS\r\nA=1, B=2\r\n\r\n#>", "STATUS") == ("A=1, B=2", True)
    assert split_reply("vt100\r\nSuccess=TRUE\r\n", "HEARTBEAT") == ("Success=TRUE", False)


def test_telnet_filter_refuses_options():
    f = TelnetFilter()
    assert f.feed(bytes([255, 253, 24]) + b"ab" + bytes([255, 255])) == b"ab\xff"
    assert bytes(f.replies) == bytes([255, 252, 24])  # DO TTYPE -> WONT TTYPE
    f = TelnetFilter()
    assert f.feed(bytes([255, 250, 24, 1, 255, 240]) + b"x") == b"x"


def test_parse_status_rejects_incomplete_or_malformed():
    sim = AqawanSim()
    text = sim.status_text(time.monotonic())
    assert parse_status(text).shutters == ("CLOSED", "CLOSED")
    with pytest.raises(PACProtocolError, match="missing"):
        parse_status(text[: len(text) // 2])
    with pytest.raises(PACProtocolError, match="Fault"):
        parse_status(text.replace("Fault=FALSE", "Fault=MAYBE"))
    with pytest.raises(PACProtocolError, match="EnclTemp"):
        parse_status(text.replace("EnclTemp=12.0", "EnclTemp=warm"))
    # An unknown shutter string parses (the server maps it to an error).
    assert parse_status(text.replace("Shutter1=CLOSED", "Shutter1=STUCK")).shutter1 == "STUCK"


def test_check_success():
    assert check_success("X", "Success=TRUE") == "Success=TRUE"
    with pytest.raises(PACRefused, match="Estop active"):
        check_success("X", "Success=FALSE, Estop active")
    with pytest.raises(PACProtocolError):
        check_success("X", "Shutter1=OP")


def test_hardware_addresses_are_refused_by_default():
    assert check_pac_address("127.0.0.1", allow_hardware=False) == "127.0.0.1"
    assert check_pac_address("localhost", allow_hardware=False) in ("127.0.0.1", "::1")
    with pytest.raises(HardwareNotAllowed, match="not loopback"):
        check_pac_address(NOT_LOOPBACK, allow_hardware=False)
    # Allowed only when the caller says so (no connection is made here).
    assert check_pac_address(NOT_LOOPBACK, allow_hardware=True) == NOT_LOOPBACK
    with pytest.raises(HardwareNotAllowed):
        PACClient(NOT_LOOPBACK, 22004)
    with pytest.raises(ValueError):
        PACClient("localhost", 22004)  # an IP literal only: resolve and check first
    with pytest.raises(ValueError):
        AqawanSim(NOT_LOOPBACK)
