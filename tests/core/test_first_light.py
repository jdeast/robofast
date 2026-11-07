"""``python -m robofast``: first light, from a telescope plugged in to a
solved light frame, with no file written by hand (robofast.first_light,
docs/first_light.md). The telescope is robofast's simulated one on the
synthetic sky (robofast.testing.sky_device), found by Alpaca discovery on
loopback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from robofast.catalog import separation
from robofast.first_light import main
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera, FakeTelescope
from robofast.testing.sky_device import SkyDevice

SITE = "42.38,-71.13"


@pytest.fixture
def device(tmp_path):
    d = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")},
        pointing_error={"IH": 150.0, "ID": -40.0, "NP": 20.0},
    ).start()
    yield d
    d.stop()


def _args(device, tmp_path, *extra):
    return [
        "--site", SITE,
        "--address", "127.0.0.1",
        "--discovery-port", str(device.discovery_port),
        "--dir", str(tmp_path / "run"),
        "--plugin", "robofast.testing.sky_device:register",
        "--catalog", "synthetic",
        "--catalog-dir", str(tmp_path / "sky"),
        "--exptime", "1",
        *extra,
    ]  # fmt: skip


def test_one_command_to_a_solved_first_light_frame(device, tmp_path, capsys):
    main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    out = capsys.readouterr().out
    entry = json.loads(out.strip().splitlines()[-1])["first_light"]
    solve = entry["solve"]
    assert solve["solved"] and solve["n_matched"] >= 10 and solve["rms"] < 1.0
    target = json.loads((tmp_path / "run" / "observatory.json").read_text())["plan"]["targets"][1]
    off = separation(solve["ra"], solve["dec"], target["ra_hours"] * 15, target["dec_deg"]) * 3600
    assert off < 5.0, off  # the pointing calibration's model put it on the axis
    assert set(entry["model"]) >= {"IH", "ID"}
    printed = next(ln for ln in out.splitlines() if ln.startswith("first light frame: "))
    assert Path(printed.removeprefix("first light frame: ")).is_file()  # the printed path opens
    assert device.mount.AtPark  # parked at the end
    config = json.loads((tmp_path / "run" / "observatory.json").read_text())
    assert config["enclosure"] == "none" and "devices" not in config
    assert config["images"]["telescopes"]["t1"]["plate_scale"] == pytest.approx(1.5, rel=1e-4)


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--yes"], "one of the arguments --no-enclosure --dome is required"),
        (["--dome", "127.0.0.1:9/0", "--yes"], "--dome needs --safety-monitor"),
    ],
)
def test_what_is_never_guessed_must_be_said(device, tmp_path, capsys, extra, message):
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, *extra))
    assert exc.value.code == 2 and message in capsys.readouterr().err


def test_the_site_is_required(device, tmp_path, capsys):
    args = _args(device, tmp_path, "--no-enclosure", "--yes")
    i = args.index("--site")
    with pytest.raises(SystemExit) as exc:
        main(args[:i] + args[i + 2 :])
    assert exc.value.code == 2 and "--site" in capsys.readouterr().err


def test_an_existing_config_is_not_overwritten(device, tmp_path, capsys):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "observatory.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    assert exc.value.code == 2 and "not overwritten" in capsys.readouterr().err
    assert (tmp_path / "run" / "observatory.json").read_text() == "{}"


def test_no_operator_no_start(device, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, "--no-enclosure"))
    assert exc.value.code == 1 and "not started" in capsys.readouterr().err
    assert device.camera.exposures_started == 0 and device.mount.AtPark


def test_a_camera_without_a_pixel_size_stops_naming_it(tmp_path, capsys):
    tel, cam = FakeTelescope(), FakeCamera()
    tel.FocalLength = 0.5
    del cam.PixelSizeX
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--site", SITE, "--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run")])  # fmt: skip
    finally:
        server.stop()
    assert exc.value.code == 1
    assert "PixelSizeX" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_two_telescopes_are_not_chosen_between(tmp_path, capsys):
    devices = {}
    for n in (0, 1):
        t, c = FakeTelescope(), FakeCamera()
        t.FocalLength = 0.5
        devices[f"telescope/{n}"], devices[f"camera/{n}"] = t, c
    server = FakeAlpacaServer(devices).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--site", SITE, "--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run")])  # fmt: skip
    finally:
        server.stop()
    assert exc.value.code == 1 and "first light runs one telescope; 2" in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="on Windows the real loop runs: test_one_command_to_a_solved_first_light_frame",
)
def test_runs_where_the_event_loop_has_no_signal_handlers(device, tmp_path, capsys, monkeypatch):
    """Windows' asyncio loops have no ``add_signal_handler`` (they raise
    NotImplementedError; Ctrl-C arrives as KeyboardInterrupt instead):
    first light runs there all the same. Stood in for here by a loop that
    raises as Windows' does."""

    def unavailable(self, *args, **kwargs):
        raise NotImplementedError

    from asyncio import unix_events

    monkeypatch.setattr(unix_events._UnixSelectorEventLoop, "add_signal_handler", unavailable)
    main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["first_light"]
    assert entry["solve"]["solved"]


def test_an_enclosure_with_unsafe_weather_stays_closed_and_says_why(device, tmp_path, capsys):
    from robofast.testing.fakes import FakeDome, FakeSafetyMonitor, FakeWeather

    weather = FakeWeather(safe=False)
    dome = FakeDome()
    enclosure = FakeAlpacaServer({"dome": dome, "safetymonitor": FakeSafetyMonitor(weather)})
    enclosure.start()
    a = enclosure.address
    try:
        with pytest.raises(SystemExit) as exc:
            main(_args(device, tmp_path, "--dome", f"{a}/0", "--safety-monitor", f"{a}/0",
                       "--timeout", "15"))  # fmt: skip
    finally:
        enclosure.stop()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not done after 15 s" in err and "SafetyMonitor.IsSafe=False" in err
    assert device.camera.exposures_started == 0  # nothing exposed
    assert dome.state() == 1  # closed


def test_an_enclosure_is_closed_when_it_returns(device, tmp_path, capsys, monkeypatch):
    """With safe weather the enclosure opens (after the reopen hold, shortened
    here by the test alone), first light solves, and the command returns only
    once the enclosure reads closed, saying so."""
    from robofast import first_light
    from robofast.testing.fakes import FakeDome, FakeSafetyMonitor, FakeWeather

    config = first_light.first_light_config

    def short_hold(*args, **kwargs):
        c = config(*args, **kwargs)
        c["safety"] = {"reopen_hold": 1.0, "poll_interval": 0.5}  # the test's, not robofast's
        return c

    monkeypatch.setattr(first_light, "first_light_config", short_hold)
    dome = FakeDome(travel=2.0)
    safe = FakeSafetyMonitor(FakeWeather(safe=True))
    enclosure = FakeAlpacaServer({"dome": dome, "safetymonitor": safe}).start()
    a = enclosure.address
    try:
        main(_args(device, tmp_path, "--dome", f"{a}/0", "--safety-monitor", f"{a}/0", "--yes"))
        state = dome.state()
    finally:
        enclosure.stop()
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1])["first_light"]["solve"]["solved"]
    assert state == 1, state  # closed, not closing
    assert "enclosure closed" in out


def _ctrl_c_after_the_first_exposure(device):
    import os
    import signal
    import threading
    import time

    def interrupt() -> None:
        deadline = time.monotonic() + 120
        while device.camera.exposures_started < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to itself: POSIX only")
def test_ctrl_c_stops_parks_and_says_so(device, tmp_path, capsys):
    _ctrl_c_after_the_first_exposure(device)
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "first light: stopped; the telescope parked" in err, err
    assert device.mount.AtPark


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to itself: POSIX only")
def test_ctrl_c_says_loudly_when_the_telescope_did_not_park(device, tmp_path, capsys):
    from robofast.testing.fakes import FakeDeviceError

    def refuses() -> None:
        raise FakeDeviceError("Park: mount refused (limit switch)")

    device.mount.Park = refuses
    _ctrl_c_after_the_first_exposure(device)
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "first light: stopped; the telescope DID NOT PARK" in err, err
    assert "the telescope parked" not in err and not device.mount.AtPark


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in imports unix_events")
def test_ctrl_c_where_the_loop_has_no_signal_handlers(device, tmp_path, capsys, monkeypatch):
    from asyncio import unix_events

    def unavailable(self, *args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(unix_events._UnixSelectorEventLoop, "add_signal_handler", unavailable)
    _ctrl_c_after_the_first_exposure(device)
    with pytest.raises(SystemExit) as exc:  # not a KeyboardInterrupt
        main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "first light: stopped; the telescope parked" in err, err


def test_the_zenith_is_the_sites_not_the_mounts(device, tmp_path, capsys):
    """The mount's sidereal time is not read (its clock or site may be off):
    with the mount's clock 24 min fast, the frame is still at the zenith of
    the site given, by this computer's clock."""
    from datetime import UTC, datetime

    from robofast.catalog import separation
    from robofast.first_light import _lst_hours

    mount_cls = type(device.mount)
    true_lst = mount_cls.SiderealTime.fget
    device.mount.__class__ = type(
        "FastClockMount", (mount_cls,), {"SiderealTime": property(lambda s: true_lst(s) + 0.4)}
    )
    main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    out = capsys.readouterr().out
    solve = json.loads(out.strip().splitlines()[-1])["first_light"]["solve"]
    taken = datetime.fromisoformat(solve["date_obs"]).replace(tzinfo=UTC)
    lat, lon = (float(v) for v in SITE.split(","))
    zenith_ra = _lst_hours((lat, lon, 0.0), taken) * 15.0
    assert separation(solve["ra"], solve["dec"], zenith_ra, lat) < 1.0
    assert "arcmin from the zenith" in out


def test_a_server_without_a_device_list_is_named_and_passed(device, tmp_path, capsys):
    """As robofast-discover does: a server that gives no device list is named,
    and first light goes on with the others."""
    main(_args(device, tmp_path, "--no-enclosure", "--yes",
               "--server", "127.0.0.1:9", "--server", device.address))  # fmt: skip
    captured = capsys.readouterr()
    assert "first light: 127.0.0.1:9: no device list" in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.out.strip().splitlines()[-1])["first_light"]["solve"]["solved"]
