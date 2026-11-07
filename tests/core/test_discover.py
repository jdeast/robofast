"""robofast-discover: Alpaca discovery, the device list, the starter config,
and a first exposure from it (docs/discover.md).

The "plugged-in" devices are fakes behind :class:`FakeAlpacaServer`, which
answers the management API and, with ``discovery_port``, UDP discovery on
loopback.
"""

from __future__ import annotations

import json

import pytest

from robofast.discover import Device, configured_devices, discover, main, starter_config
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.checks import wait_until
from robofast.testing.fakes import (
    FakeCamera,
    FakeDome,
    FakeFocuser,
    FakeSafetyMonitor,
    FakeTelescope,
    FakeWeather,
)
from robofast.testing.processes import SequencerProcess, SupervisorProcess

from .conftest import FAST_REMOTE, FAST_SAFETY, FAST_SEQ

#: Never written into a starter config: the operator's (SAFETY.md).
OPERATOR_KEYS = {"devices", "limits", "site", "mode", "safety"}


@pytest.fixture
def plugged_in():
    """A mount, a camera, a focuser, a dome and a safety monitor on one
    server that answers discovery on loopback."""
    weather = FakeWeather()
    telescope = FakeTelescope()
    telescope.FocalLength = 0.5  # m, as an Alpaca Telescope reports it
    server = FakeAlpacaServer(
        {
            "telescope": telescope,
            "camera": FakeCamera(),  # PixelSizeX = PixelSizeY = 9.0 um
            "focuser": FakeFocuser(),
            "dome": FakeDome(),
            "safetymonitor": FakeSafetyMonitor(weather),
        },
        discovery_port=0,
    ).start()
    yield server
    server.stop()


def test_discover_finds_the_server_on_loopback(plugged_in):
    assert plugged_in.discovery is not None
    found = discover("127.0.0.1", plugged_in.discovery.port, timeout=1.0)
    assert found == [plugged_in.address]


def test_discover_with_nobody_listening_finds_nothing(plugged_in):
    port = plugged_in.discovery.port
    plugged_in.discovery.stop()
    assert discover("127.0.0.1", port, timeout=0.5) == []


def test_configured_devices_lists_each_device(plugged_in):
    devices = configured_devices(plugged_in.address)
    assert {(d.type, d.number) for d in devices} == {
        ("Telescope", 0),
        ("Camera", 0),
        ("Focuser", 0),
        ("Dome", 0),
        ("SafetyMonitor", 0),
    }
    assert all(d.server == plugged_in.address for d in devices)


def test_starter_config_pairs_and_leaves_safety_to_the_operator(plugged_in):
    config = starter_config(configured_devices(plugged_in.address), "classroom")
    a = plugged_in.address
    assert config["telescopes"] == {
        "t1": {"telescope": f"{a}/0", "camera": f"{a}/0", "focuser": f"{a}/0"}
    }
    assert config["name"] == "classroom"
    assert config["images"]["data_root"] == "data"
    assert config["plan"] == {"type": "calibration", "biases": 1}
    assert not OPERATOR_KEYS & set(config)
    notes = "\n".join(config["_note"])
    assert f'"dome": "{a}/0"' in notes and f'"safetymonitor": "{a}/0"' in notes


def test_two_telescopes_one_camera_pairs_no_camera():
    devices = [
        Device("10.0.0.2:11111", "Telescope", 0),
        Device("10.0.0.3:11111", "Telescope", 0),
        Device("10.0.0.4:11111", "Camera", 0),
    ]
    config = starter_config(devices)
    assert config["telescopes"] == {
        "t1": {"telescope": "10.0.0.2:11111/0"},
        "t2": {"telescope": "10.0.0.3:11111/0"},
    }
    notes = "\n".join(config["_note"])
    assert "t1: no camera paired" in notes and "t2: no camera paired" in notes
    assert "Camera 10.0.0.4:11111/0" in notes
    assert "No Dome or SafetyMonitor answered" in notes


def test_same_number_pairs_on_one_server():
    s = "10.0.0.2:11111"
    devices = [Device(s, "Telescope", n) for n in (0, 1)] + [Device(s, "Camera", n) for n in (1, 0)]
    assert starter_config(devices)["telescopes"] == {
        "t1": {"telescope": f"{s}/0", "camera": f"{s}/0"},
        "t2": {"telescope": f"{s}/1", "camera": f"{s}/1"},
    }


def test_write_never_overwrites(plugged_in, tmp_path, capsys):
    path = tmp_path / "observatory.json"
    path.write_text("{}")
    with pytest.raises(SystemExit) as exc:
        main(["--server", plugged_in.address, "--write", str(path)])
    assert exc.value.code == 2
    assert path.read_text() == "{}"


def test_no_answer_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--address", "127.0.0.1", "--port", "9", "--timeout", "0.3"])
    assert exc.value.code == 1
    assert "no Alpaca server answered" in capsys.readouterr().err


async def test_first_exposure_from_the_starter_config(plugged_in, tmp_path, capsys):
    """Discover, write, add the two lines the operator owns, start the
    supervisor and the sequencer: one bias on disk."""
    path = tmp_path / "observatory.json"
    main(
        [
            "--address", "127.0.0.1",
            "--port", str(plugged_in.discovery.port),
            "--timeout", "1",
            "--write", str(path),
            "--name", "classroom",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert "Telescope" in out and f"wrote {path}" in out
    config = json.loads(path.read_text())
    a = plugged_in.address
    # The operator's lines (docs/discover.md, step 2), and test timings.
    config["devices"] = {"dome": f"{a}/0", "safetymonitor": f"{a}/0"}
    config["listen"] = "127.0.0.1:0"
    config["safety"] = FAST_SAFETY
    services = []
    try:
        sup = SupervisorProcess(config, tmp_path)
        services.append(sup)
        config.update(supervisor_url=sup.url, sequencer=FAST_SEQ, lease_client=FAST_REMOTE)
        services.append(SequencerProcess(config, tmp_path))
        frames = tmp_path / "data" / "classroom" / "t1"
        await wait_until(lambda: bool(list(frames.glob("n*/*.bias.0001.fits"))), timeout=60)
        assert plugged_in.devices["camera/0"].exposures_started == 1
        assert plugged_in.devices["dome/0"].ShutterStatus == 1  # closed: nothing opened
    finally:
        for s in services:
            s.stop()
