"""The APC PDU over SNMP, its simulator and its Alpaca Switch server
(robofast_core/devices/snmp.py, apc_pdu.py, pdu_server.py; docs/recovery.md).
Loopback only."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from robofast_core.alpaca_client import AlpacaDevice, InvalidOperationException
from robofast_core.devices import snmp
from robofast_core.devices.apc_pdu import APCPDU, APCPDUSim, Credentials, load_credentials
from robofast_core.devices.pdu_server import (
    HardwareNotAllowed,
    PDUConfig,
    PDUServer,
    check_pdu_address,
)

CREDS = Credentials("test-read", "test-write")


def test_ber_round_trip():
    m = snmp.Message(
        1,
        "c",
        snmp.SET,
        123456,
        varbinds=[("1.3.6.1.4.1.318.1.1.4.4.2.1.3.7", 3), ("1.3.6.1.2.1.1.1.0", "hello"),
                  ("1.3.6.1.2.1.1.5.0", None), ("1.3.6.1.4.1.99999.300", -129)],
    )  # fmt: skip
    back = snmp.decode_message(snmp.encode_message(m))
    assert back == m
    with pytest.raises(snmp.SNMPError):
        snmp.decode_message(b"\x30\x05\x02\x01")


async def test_client_against_the_simulator():
    sim = await APCPDUSim(CREDS, ["cam", "mount", "pac"], reboot_delay=0.2).start()
    try:
        pdu = APCPDU("127.0.0.1", sim.port, CREDS, timeout=0.5, retries=0)
        assert "simulated" in await pdu.describe()
        assert await pdu.name(2) == "mount" and await pdu.state(1) is True
        await pdu.set(1, False)
        assert await pdu.state(1) is False and not sim.is_on(1)
        await pdu.set(1, True)
        await pdu.reboot(2)
        assert await pdu.state(2) is False
        await asyncio.sleep(0.3)
        assert await pdu.state(2) is True
        assert sim.sets == [(1, 2), (1, 1), (2, 3)]
        with pytest.raises(snmp.SNMPError, match="noSuchName"):
            await pdu.state(9)
        # A read-only community may not set; a wrong one gets no answer at all.
        ro = APCPDU("127.0.0.1", sim.port, Credentials("test-read", "test-read"), timeout=0.3)
        with pytest.raises(snmp.SNMPError, match="authorizationError"):
            await ro.set(1, False)
        bad = APCPDU("127.0.0.1", sim.port, Credentials("x", "y"), timeout=0.2, retries=1)
        with pytest.raises(snmp.SNMPTimeout):
            await bad.state(1)
        assert sim.is_on(1)
    finally:
        sim.close()


def test_credentials_file(tmp_path):
    p = tmp_path / "pdu.json"
    p.write_text(json.dumps({"read": "s3cret-r", "write": "s3cret-w"}))
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        load_credentials(p)
    os.chmod(p, 0o600)
    c = load_credentials(p)
    assert (c.read, c.write) == ("s3cret-r", "s3cret-w") and "s3cret" not in repr(c)
    p.write_text(json.dumps({"read": "r"}))
    with pytest.raises(ValueError, match="write"):
        load_credentials(p)


def test_hardware_gate():
    assert check_pdu_address("127.0.0.1", allow_hardware=False) == "127.0.0.1"
    with pytest.raises(HardwareNotAllowed, match="loopback"):
        check_pdu_address("192.0.2.10", allow_hardware=False)  # TEST-NET-1, never contacted
    cfg = PDUConfig.from_dict({"pdu": {"host": "192.0.2.10"}, "allow_hardware": True})
    with pytest.raises(HardwareNotAllowed):
        PDUServer(cfg, hardware=False, credentials=CREDS)  # the config alone is not enough
    with pytest.raises(ValueError, match="cycle needs writable"):
        PDUConfig.from_dict({"outlets": [{"outlet": 1, "name": "x", "cycle": True}]})
    with pytest.raises(ValueError, match="credentials"):
        PDUServer(PDUConfig(), credentials=None)


OUTLETS = [
    {"outlet": 1, "name": "t1 camera", "writable": True, "cycle": True},
    {"outlet": 2, "name": "t1 focuser", "writable": True},
    {"outlet": 8, "name": "aqawan PAC", "writable": False},
]


@pytest.fixture
async def pdu_server():
    sim = await APCPDUSim(CREDS, [f"o{i}" for i in range(1, 9)], reboot_delay=0.1).start()
    cfg = PDUConfig.from_dict(
        {
            "name": "pdu-test",
            "listen": "127.0.0.1:0",
            "pdu": {"host": "127.0.0.1", "port": sim.port, "timeout": 0.5, "retries": 1},
            "outlets": OUTLETS,
            "min_cycle_interval": 60,
        }
    )
    server = PDUServer(cfg, credentials=CREDS).start()
    try:
        yield server, sim
    finally:
        server.stop()
        sim.close()


async def test_alpaca_switch_server(pdu_server):
    server, sim = pdu_server
    dev = AlpacaDevice(server.address, "switch", 0)
    try:
        await dev.connect(timeout=5)
        assert await dev.get("MaxSwitch", timeout=5) == 3
        assert await dev.get("InterfaceVersion", timeout=5) == 2
        names = [await dev.get("GetSwitchName", timeout=5, Id=i) for i in range(3)]
        assert names == ["t1 camera", "t1 focuser", "aqawan PAC"]
        assert [await dev.get("CanWrite", timeout=5, Id=i) for i in range(3)] == [True, True, False]
        await dev.call("SetSwitch", 1, False, timeout=5)
        assert await dev.get("GetSwitch", timeout=5, Id=1) is False and not sim.is_on(2)
        await dev.put("SetSwitchValue", timeout=5, Id=1, Value=1)
        assert await dev.get("GetSwitchValue", timeout=5, Id=1) == 1.0
        # The enclosure controller's outlet can never be switched or cycled.
        with pytest.raises(Exception, match="not writable"):
            await dev.call("SetSwitch", 2, False, timeout=5)
        with pytest.raises(InvalidOperationException, match="may not be power-cycled"):
            await dev.call("Action", "PowerCycle", "2", timeout=5)
        assert sim.is_on(8) and (8, 2) not in sim.sets
        # A power cycle: off, back on after the PDU's delay; then rate-limited.
        out = json.loads(await dev.call("Action", "PowerCycle", "0", timeout=5))
        assert out["cycled"] and sim.sets[-1] == (1, 3)
        with pytest.raises(InvalidOperationException, match="at most once"):
            await dev.call("Action", "PowerCycle", "0", timeout=5)
        await asyncio.sleep(0.2)
        status = json.loads(await dev.call("Action", "Status", "", timeout=5))
        assert [s["on"] for s in status] == [True, True, True]
    finally:
        await dev.aclose()


async def test_a_silent_pdu_is_an_error_not_a_hang(pdu_server):
    server, sim = pdu_server
    sim.silent = True
    dev = AlpacaDevice(server.address, "switch", 0)
    try:
        with pytest.raises(Exception, match="did not answer"):
            await dev.connect(timeout=5)
    finally:
        await dev.aclose()
