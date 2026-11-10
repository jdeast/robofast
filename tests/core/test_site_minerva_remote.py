"""MINERVA's remote (tunnelled) configs, sites/minerva/remote/ (docs/remote.md).

Every file there reaches real hardware through an SSH tunnel on a localhost
port, so each must say ``allow_hardware: true`` (the config half of the
gate) and still be refused without ``--hardware``; the enclosure runs in
engineering mode; the Aqawan shadow server is read-only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from robofast import config as enclosure_config
from robofast import hwgate
from robofast.devices.aqawan_server import AqawanConfig, AqawanServer
from robofast.devices.aqawan_sim import AqawanSim
from robofast.supervisor_service import build as build_supervisor
from robofast.testing.tunnel import FakeTunnel
from robofast.timeline import Timeline

REMOTE = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "remote"

#: The tunnel plan's localhost ports (docs/remote.md).
TUNNEL_PORTS = {7100, 7101, 7102, 7141, 7142, 7151, 7152, 7162}


def _json(name: str) -> dict:
    return json.loads((REMOTE / name).read_text())


def test_only_localhost_and_tunnel_ports():
    for path in REMOTE.iterdir():
        text = path.read_text()
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            assert ip == "127.0.0.1", f"{path.name}: {ip}"
        for port in re.findall(r"127\.0\.0\.1:(\d+)", text):
            port = int(port)
            assert port in TUNNEL_PORTS or 8000 <= port < 12000, f"{path.name}: {port}"


def test_every_device_config_opens_only_half_the_gate():
    for path in REMOTE.glob("*.json"):
        d = json.loads(path.read_text())
        if "pac" in d or "pwi" in d or "backend" in d or "pdu" in d:
            assert d["allow_hardware"] is True, path.name
            assert any("--hardware" in line for line in d["_note"]), path.name


def _aqawan(name: str, port: int) -> AqawanConfig:
    d = _json(name)
    d["pac"] = {**d["pac"], "port": port}
    d["listen"] = "127.0.0.1:0"
    return AqawanConfig.from_dict(d)


@pytest.mark.parametrize("name", ["aqawan1_shadow.json", "aqawan1_control.json"])
def test_aqawan_configs_are_refused_without_hardware(name):
    d = _json(name)
    assert d["pac"] == {"host": "127.0.0.1", "port": 7100}
    with FakeTunnel() as tunnel:
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            AqawanServer(_aqawan(name, tunnel.port))  # no --hardware
        assert not tunnel.touched


async def test_shadow_is_read_only_against_a_simulator():
    cfg = _aqawan("aqawan1_shadow.json", 0)
    assert cfg.read_only and cfg.emergency_close is None and cfg.status_max_age >= 10
    sim = AqawanSim(heartbeat_timeout=None).start()
    try:
        cfg.pac.port = sim.port
        server = AqawanServer(cfg, hardware=True).start()
        try:
            st = await _status(server)
            assert st["shutter1"] == "CLOSED"
            assert set(sim.commands()) <= {"STATUS", "GET_FAULTS", "GET_ERRORS"}
        finally:
            server.stop()
    finally:
        sim.stop()


async def _status(server: AqawanServer) -> dict:
    import httpx

    async with httpx.AsyncClient(base_url=f"http://{server.address}/api/v1/dome/0/") as c:
        await c.put("connected", data={"Connected": "true", "ClientID": "1"})
        r = await c.put("action", data={"Action": "Status", "Parameters": "", "ClientID": "1"})
        return json.loads(r.json()["Value"])


def test_enclosure_config_is_engineering_and_builds():
    config = enclosure_config.load_config(REMOTE / "aqawan1.json")
    assert config["mode"] == "engineering"
    assert config["mode_file"] != "modes/aqawan1.json"
    assert config["devices"]["dome"] == "127.0.0.1:11113"
    tel = config["telescopes"]
    assert tel["t1"]["telescope"] == "127.0.0.1:11121/0"
    assert tel["t2"]["telescope"] == "127.0.0.1:11122/0"
    assert tel["t1"]["camera"].startswith("127.0.0.1:7151")
    assert tel["t2"]["camera"].startswith("127.0.0.1:7152")
    build_supervisor(config, Timeline())


@pytest.mark.parametrize(
    "name, port",
    [
        ("t1_pwi2.json", 7101),
        ("t2_pwi2.json", 7102),
        ("t1_pwi4.json", 7141),
        ("t2_pwi4.json", 7142),
    ],
)
def test_pwi_configs_are_refused_without_hardware(name, port):
    from robofast.devices.pwi_server import PWIConfig, PWIServer

    d = _json(name)
    assert d["pwi"]["host"] == "127.0.0.1" and d["pwi"]["port"] == port
    # T1 and T2 under PWI2 may be homed by robofast (verified 2026-11-09 and
    # 2026-11-10: homing is part of recovery, docs/pwi.md), and then refuse
    # unhomed moves; the PWI4 configs home only by hand.
    pwi2 = name in ("t1_pwi2.json", "t2_pwi2.json")
    assert d["allow_find_home"] is pwi2
    if pwi2:
        assert d["allow_unhomed_moves"] is False
    with FakeTunnel() as tunnel:
        d["pwi"] = {**d["pwi"], "port": tunnel.port}
        d["listen"] = "127.0.0.1:0"
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            PWIServer(PWIConfig.from_dict(d))  # no --hardware
        assert not tunnel.touched


def test_t2_pdu_config_is_refused_without_hardware():
    from robofast.devices.apc_pdu import Credentials
    from robofast.devices.pdu_server import PDUConfig, PDUServer

    d = _json("t2_pdu.json")
    assert d["pdu"]["host"] == "127.0.0.1" and d["pdu"]["port"] == 7162 and d["pdu"]["relay"]
    # Only the camera and the mount panel can be switched; never the PC or the network.
    switchable = {o["name"] for o in d["outlets"] if o.get("writable")}
    assert switchable == {"inst", "panel"}
    with FakeTunnel() as tunnel:
        d["pdu"] = {**d["pdu"], "port": tunnel.port}
        d["listen"] = "127.0.0.1:0"
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            PDUServer(PDUConfig.from_dict(d), credentials=Credentials("r", "w"))  # no --hardware
        assert not tunnel.touched
