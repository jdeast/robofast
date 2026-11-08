"""``python -m robofast``: first light, from a telescope plugged in to a
solved light frame, with no file written by hand (robofast.first_light,
docs/first_light.md). The telescope is robofast's simulated one on the
synthetic sky (robofast.testing.sky_device), found by Alpaca discovery on
loopback."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast import discover
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


#: One instant for the sky (the device's clock and first light's): the
#: synthetic field at the zenith then, the same in every run.
HELD = datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)


class _HeldClock(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return HELD if tz is None or tz is UTC else HELD.astimezone(tz)


@pytest.fixture
def held_device(tmp_path, monkeypatch):
    import robofast.first_light

    monkeypatch.setattr(robofast.first_light, "datetime", _HeldClock)
    d = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")},
        pointing_error={"IH": 150.0, "ID": -40.0, "NP": 20.0},
        clock=lambda: HELD,
    ).start()
    yield d
    d.stop()


def test_one_command_to_a_solved_first_light_frame(held_device, tmp_path, capsys):
    device = held_device
    main(_args(device, tmp_path, "--no-enclosure", "--yes"))
    out = capsys.readouterr().out
    entry = json.loads(out.strip().splitlines()[-1])["first_light"]
    solve = entry["solve"]
    assert solve["solved"] and solve["n_matched"] >= 10 and solve["rms"] < 1.0
    target = json.loads((tmp_path / "run" / "observatory.json").read_text())["plan"]["targets"][1]
    off = separation(solve["ra"], solve["dec"], target["ra_hours"] * 15, target["dec_deg"]) * 3600
    # The calibration fits the zero points only (IH, ID): the injected NP
    # (20") stays uncorrected, so the frame lands within that, not on the axis.
    assert off < 25.0, off
    assert set(entry["model"]) == {"IH", "ID"}  # the zero points only
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


def test_a_site_given_out_of_range_is_refused(device, tmp_path, capsys):
    args = _args(device, tmp_path, "--no-enclosure", "--yes")
    args[args.index("--site") + 1] = "95,-71.13"
    with pytest.raises(SystemExit) as exc:
        main(args)
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "--site: 95,-71.13: latitude -90..90" in err, err


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


class _OnTime(FakeTelescope):
    """A fake telescope whose clock agrees with SITE's sidereal time."""

    @property
    def SiderealTime(self) -> float:  # type: ignore[override]
        from datetime import UTC, datetime

        from robofast.first_light import _lst_hours

        lat, lon = (float(v) for v in SITE.split(","))
        return _lst_hours((lat, lon, 0.0), datetime.now(UTC))


def test_a_camera_without_a_pixel_size_stops_naming_it(tmp_path, capsys):
    tel, cam = _OnTime(), FakeCamera()
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
    """The zenith is the site's by this computer's clock, not the mount's:
    with the mount's clock half a minute fast (inside the clock check's
    limit), the frame is at the zenith of the site given."""
    from datetime import UTC, datetime

    from robofast.catalog import separation
    from robofast.first_light import _lst_hours

    mount_cls = type(device.mount)
    true_lst = mount_cls.SiderealTime.fget
    device.mount.__class__ = type(
        "FastClockMount",
        (mount_cls,),
        {"SiderealTime": property(lambda s: true_lst(s) + 0.5 / 60.0)},
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


def test_two_telescopes_are_refused_before_any_is_read(tmp_path, capsys):
    """Nothing is read from, or connected on, a telescope first light will
    not run: two answer, so it refuses before reading either's optics."""

    class Counted(FakeTelescope):
        reads = 0

        @property
        def FocalLength(self) -> float:
            type(self).reads += 1
            return 0.5

    devices = {}
    for n in (0, 1):
        devices[f"telescope/{n}"], devices[f"camera/{n}"] = Counted(), FakeCamera()
    server = FakeAlpacaServer(devices).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--site", SITE, "--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run")])  # fmt: skip
    finally:
        server.stop()
    assert exc.value.code == 1 and "first light runs one telescope; 2" in capsys.readouterr().err
    assert Counted.reads == 0


def test_frames_that_do_not_solve_are_said_and_stop_it_soon(device, tmp_path, capsys):
    """A plate scale 10 % off (beyond the solver's tolerance): no frame
    solves. First light says so, with the solver's errors, and stops after
    two pointing runs with nothing solved instead of waiting out --timeout."""
    import time

    device.mount.FocalLength *= 1.1
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as exc:
        main(_args(device, tmp_path, "--no-enclosure", "--yes", "--timeout", "120"))
    took = time.monotonic() - t0
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "did not solve" in err, err
    assert "matched" in err or "no match" in err, err  # the solver's own words
    assert took < 60, took


def test_first_light_asks_eight_matched_stars_of_a_solve(device, tmp_path):
    """The solver accepts 4 matched stars by default; a sparse field can
    then solve falsely. First light asks 8, for the
    calibration's points and for the first-light frame."""
    from robofast import first_light

    config = first_light.first_light_config(
        discover.configured_devices(device.address),
        site=(42.38, -71.13, 0.0),
        enclosure=None,
        catalog={"source": "synthetic"},
        plugins=[],
    )
    assert config["telescopes"]["t1"]["pointing"]["solver"]["min_matches"] == 8
    assert config["images"]["solve"]["telescopes"]["t1"]["min_matches"] == 8


def _counted_source(name, calls, device, delay=0.0, after=None):
    """A catalog source with the synthetic sky's stars that records, for
    each tile fetched, how many exposures the camera had started."""
    import dataclasses
    import time

    from robofast.catalog import CATALOG_SOURCES
    from robofast.testing.sky_device import register

    register()
    synthetic = CATALOG_SOURCES["synthetic"]

    def fetcher(url):
        fetch = synthetic.fetcher(url)

        def counted(level, pix, mag_limit):
            time.sleep(delay)
            calls.append(device.camera.exposures_started)
            if after is not None:
                after()
            return fetch(level, pix, mag_limit)

        return counted

    CATALOG_SOURCES.register(
        name,
        dataclasses.replace(
            synthetic, name=name, directory=name + "-g{mag:g}-hpx{level}", fetcher=fetcher
        ),
    )


def test_the_catalog_is_fetched_before_the_telescope_moves(device, tmp_path, capsys):
    """A tile downloaded inside a solve held the process past its
    timeout (asyncio.run waits for the solve's thread). First light fetches
    the four fields' tiles first, saying so."""
    from robofast.catalog import CATALOG_SOURCES

    calls: list[int] = []
    _counted_source("counted-f13a", calls, device)
    try:
        args = _args(device, tmp_path, "--no-enclosure", "--yes")
        args[args.index("--catalog") + 1] = "counted-f13a"
        args[args.index("--catalog-dir") + 1] = str(tmp_path / "first-light-catalog")
        main(args)
    finally:
        CATALOG_SOURCES.unregister("counted-f13a")
    out = capsys.readouterr().out
    assert '"first_light"' in out
    assert "fetching the catalog" in out, out
    assert calls, "no tile was fetched"
    assert set(calls) == {0}, calls  # every tile before the first exposure


def test_a_timeout_while_the_catalog_downloads_exits_on_time(device, tmp_path, capsys):
    import time

    from robofast.catalog import CATALOG_SOURCES

    calls: list[int] = []
    _counted_source("counted-f13b", calls, device, delay=1.0)
    args = _args(device, tmp_path, "--no-enclosure", "--yes", "--timeout", "3")
    args[args.index("--catalog") + 1] = "counted-f13b"
    args[args.index("--catalog-dir") + 1] = str(tmp_path / "first-light-catalog")
    t0 = time.monotonic()
    try:
        with pytest.raises(SystemExit) as exc:
            main(args)
    finally:
        CATALOG_SOURCES.unregister("counted-f13b")
    took = time.monotonic() - t0
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "catalog" in err, err
    assert took < 5.0, took  # not held by the download
    assert device.camera.exposures_started == 0


class _NeverParks(_OnTime):
    @property
    def AtPark(self) -> bool:  # type: ignore[override]
        return False

    def Park(self) -> None:
        from robofast.testing.fakes import FakeDeviceError

        raise FakeDeviceError("MethodNotImplemented: Park")


def test_a_mount_that_never_parks_is_named_in_the_last_line(tmp_path, capsys):
    """No lease is granted until every telescope is parked;
    the last line says so, in the supervisor's words."""
    tel, cam = _NeverParks(), FakeCamera()
    tel.FocalLength = 0.5
    cam.PixelSizeX = cam.PixelSizeY = 9.0
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--site", SITE, "--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run"), "--catalog-dir", str(tmp_path / "cat"),
                  "--plugin", "robofast.testing.sky_device:register", "--catalog", "synthetic",
                  "--timeout", "6"])  # fmt: skip
    finally:
        server.stop()
    last = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("first light")]
    assert exc.value.code == 1 and cam.exposures_started == 0
    assert "telescope not parked" in last[-1], last


def _without_site(args):
    i = args.index("--site")
    return args[:i] + args[i + 2 :]


def test_the_site_comes_from_the_telescope(device, tmp_path, capsys):
    main(_without_site(_args(device, tmp_path, "--no-enclosure", "--yes")))
    out = capsys.readouterr().out
    assert '"first_light"' in out
    site = json.loads((tmp_path / "run" / "observatory.json").read_text())["site"]
    assert site == {"latitude": 42.38, "longitude": -71.13, "elevation": 0.0}
    assert "site 42.38000, -71.13000, 0 m: the telescope's (--site to change it)" in out, out


def test_a_telescope_that_cannot_report_its_site_stops_naming_site(tmp_path, capsys):
    tel, cam = FakeTelescope(), FakeCamera()  # SiteLatitude only: no SiteLongitude
    tel.FocalLength = 0.5
    cam.PixelSizeX = cam.PixelSizeY = 9.0
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run")])  # fmt: skip
    finally:
        server.stop()
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("first light")]
    assert exc.value.code == 1 and len(lines) == 1, err
    assert "SiteLongitude" in lines[0] and "--site" in lines[0], lines
    assert not (tmp_path / "run" / "observatory.json").exists()
    assert cam.exposures_started == 0


def test_the_site_given_is_used_over_the_telescopes(device, tmp_path, capsys):
    args = _args(device, tmp_path, "--no-enclosure", "--yes")
    args[args.index("--site") + 1] = "40.0,-71.13,100"  # the longitude: the clock check's
    main(args)
    site = json.loads((tmp_path / "run" / "observatory.json").read_text())["site"]
    assert site == {"latitude": 40.0, "longitude": -71.13, "elevation": 100.0}


def test_a_telescope_reporting_its_site_as_0_0_does_not_move(tmp_path, capsys):
    """0, 0 is a mount whose site was never set: first light stops before
    anything moves, even with --yes."""
    tel, cam = FakeTelescope(), FakeCamera()
    tel.FocalLength = 0.5
    tel.SiteLatitude, tel.SiteLongitude, tel.SiteElevation = 0.0, 0.0, 0.0
    cam.PixelSizeX = cam.PixelSizeY = 9.0
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run"), "--timeout", "10",
                  "--plugin", "robofast.testing.sky_device:register", "--catalog", "synthetic",
                  "--catalog-dir", str(tmp_path / "sky")])  # fmt: skip
    finally:
        server.stop()
    err = capsys.readouterr().err
    assert exc.value.code == 1 and cam.exposures_started == 0 and not tel.slews, err
    lines = [ln for ln in err.splitlines() if ln.startswith("first light")]
    assert len(lines) == 1 and "0, 0" in lines[0] and "--site" in lines[0], lines
    assert not (tmp_path / "run" / "observatory.json").exists()


def test_the_telescopes_site_is_asked_and_a_no_moves_nothing(tmp_path, capsys, monkeypatch):
    """A mount with no GPS reports its factory site, which may be another
    continent: the question shows it, and a no stops first light."""
    far = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")}, latitude=34.1, longitude=-118.3
    ).start()
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    try:
        args = _without_site(_args(far, tmp_path, "--no-enclosure"))
        with pytest.raises(SystemExit) as exc:
            main(args)
    finally:
        far.stop()
    assert exc.value.code == 1 and "not started" in capsys.readouterr().err
    assert len(asked) == 1 and "34.10000 N, 118.30000 W, 0 m" in asked[0], asked
    assert "the telescope's" in asked[0] and "where the telescope stands" in asked[0], asked
    assert far.camera.exposures_started == 0 and not far.mount.slews


def _turning_run(tmp_path, capsys, monkeypatch, seconds):
    """First light on a sky that turns ``seconds`` for every tile fetched
    (one clock for the simulated telescope and first light): its stdout,
    and the exposures the camera had started at each fetch."""
    from datetime import timedelta

    import robofast.first_light
    from robofast.catalog import CATALOG_SOURCES

    t = [HELD]

    class Turning(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return t[0]

    monkeypatch.setattr(robofast.first_light, "datetime", Turning)
    device = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")},
        pointing_error={"IH": 150.0, "ID": -40.0, "NP": 20.0},
        clock=lambda: t[0],
    ).start()
    calls: list[int] = []

    def tick():
        t[0] += timedelta(seconds=seconds)

    _counted_source("counted-turning", calls, device, after=tick)
    try:
        args = _args(device, tmp_path, "--no-enclosure", "--yes")
        args[args.index("--catalog") + 1] = "counted-turning"
        args[args.index("--catalog-dir") + 1] = str(tmp_path / "first-light-catalog")
        main(args)
    finally:
        CATALOG_SOURCES.unregister("counted-turning")
        device.stop()
    out = capsys.readouterr().out
    assert '"first_light"' in out
    return out, calls


def test_a_slow_download_fetches_the_fields_the_run_will_reach(tmp_path, capsys, monkeypatch):
    """The sky turns while the catalog downloads: the tiles fetched are for
    where the calibration's points will be when it slews, not where they
    were when the download began (2 s of sky a tile)."""
    out, calls = _turning_run(tmp_path, capsys, monkeypatch, 2.0)
    assert calls and set(calls) == {0}, calls  # nothing fetched once exposing began
    assert "faster than the catalog" not in out


def test_a_download_slower_than_the_sky_is_bounded_and_said(tmp_path, capsys, monkeypatch):
    """At 10 s of sky a tile the fetches cannot catch the turning fields:
    first light stops after a few passes, says the rest download during
    the calibration, and the calibration still runs."""
    out, _calls = _turning_run(tmp_path, capsys, monkeypatch, 10.0)
    passes = out.count("first light: fetching the catalog")
    assert 1 <= passes <= 5, out
    assert "the sky turns faster than the catalog downloads here" in out, out


def test_the_telescopes_elevation_is_not_read(tmp_path, capsys):
    """The site's latitude and longitude are the telescope's; its elevation
    is not read (a mount without it would need a second way): 0 m unless
    --site gives one."""
    tel, cam = FakeTelescope(), FakeCamera()
    tel.FocalLength = 0.5
    tel.SiteLatitude, tel.SiteLongitude, tel.SiteElevation = 31.68, -110.88, 2345.0
    cam.PixelSizeX = cam.PixelSizeY = 9.0
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit):
            main(["--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run"), "--timeout", "3",
                  "--plugin", "robofast.testing.sky_device:register", "--catalog", "synthetic",
                  "--catalog-dir", str(tmp_path / "sky")])  # fmt: skip
    finally:
        server.stop()
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("site "))
    assert line.startswith("site 31.68000, -110.88000, 0 m: the telescope's"), line
    assert "elevation is not read" in line, line


from robofast.testing.pointing_sim import SkyMount  # noqa: E402


def _wrong_clock_device(tmp_path, hours: float) -> SkyDevice:
    """The sky device with its mount's clock fast by ``hours``: the mount
    reports RA from its own sidereal time, so it reports RA + hours for where
    it really points and lands ``hours`` west of what it is told; the camera
    sees the physical sky."""

    class WrongClockMount(SkyMount):
        @property
        def SiderealTime(self):  # type: ignore[override]
            return (super().SiderealTime + hours) % 24.0

        @property
        def RightAscension(self):  # type: ignore[override]
            return (self.ra + hours) % 24.0

        def SlewToCoordinatesAsync(self, ra, dec):
            super().SlewToCoordinatesAsync((ra - hours) % 24.0, dec)

    class PhysicalView:
        def __init__(self, mount):
            self._m = mount

        @property
        def RightAscension(self):
            return self._m.ra

        @property
        def Declination(self):
            return self._m.dec

        def __getattr__(self, name):
            return getattr(self._m, name)

    d = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")},
        pointing_error={"IH": 150.0, "ID": -40.0, "NP": 20.0},
    )
    d.mount.__class__ = WrongClockMount
    scenes = [
        v
        for v in vars(d.camera).values()
        if hasattr(v, "__dict__") and any(w is d.mount for w in vars(v).values())
    ]
    assert scenes
    for scene in scenes:
        for name, v in list(vars(scene).items()):
            if v is d.mount:
                setattr(scene, name, PhysicalView(d.mount))
    return d


def test_a_telescope_whose_clock_is_wrong_stops_before_it_moves(tmp_path, capsys):
    """A handset clock an hour off (daylight saving) points the mount 15 deg
    from where it reports, and no frame solves: first light compares the
    telescope's sidereal time with the site's from this computer's clock,
    before anything moves, and says which is wrong."""
    d = _wrong_clock_device(tmp_path, 1.0).start()
    try:
        with pytest.raises(SystemExit) as exc:
            main(_without_site(_args(d, tmp_path, "--no-enclosure", "--yes")))
        slews, exposures = list(d.mount.slews), d.camera.exposures_started
    finally:
        d.stop()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("first light")]
    assert exc.value.code == 1 and len(lines) == 1, lines
    assert "clock" in lines[0] and "60 min" in lines[0], lines
    assert not slews and exposures == 0


def test_a_telescope_clock_half_a_minute_off_runs(tmp_path, capsys):
    d = _wrong_clock_device(tmp_path, 0.5 / 60.0).start()
    try:
        main(_without_site(_args(d, tmp_path, "--no-enclosure", "--yes")))
    finally:
        d.stop()
    assert '"first_light"' in capsys.readouterr().out


def test_the_setup_reads_connect_each_device_once(tmp_path):
    """Before anything moves, first light reads the site, the clock, the
    optics and the chip in one connected session: each device is connected
    once (and once more by the sequencer), not once a read."""
    from datetime import UTC, datetime

    from robofast.first_light import _lst_hours

    lat, lon = (float(v) for v in SITE.split(","))

    class Counting:
        def _init_count(self):
            self.sets: list[bool] = []
            self._connected = False  # at power-on: not connected

        @property
        def Connected(self):  # type: ignore[override]
            return self._connected

        @Connected.setter
        def Connected(self, v):
            self.sets.append(bool(v))
            self._connected = bool(v)

    class Tel(Counting, FakeTelescope):
        def __init__(self):
            self._init_count()
            FakeTelescope.__init__(self)
            self._init_count()  # what the fake's own set-up did is not counted
            self.FocalLength = 0.5
            self.SiteLatitude, self.SiteLongitude = lat, lon

        @property
        def SiderealTime(self):  # type: ignore[override]
            return _lst_hours((lat, lon, 0.0), datetime.now(UTC))

    class Cam(Counting, FakeCamera):
        def __init__(self):
            self._init_count()
            FakeCamera.__init__(self)
            self._init_count()  # what the fake's own set-up did is not counted
            self.PixelSizeX = self.PixelSizeY = 9.0

    tel, cam = Tel(), Cam()
    server = FakeAlpacaServer({"telescope": tel, "camera": cam}).start()
    try:
        with pytest.raises(SystemExit):
            main(["--no-enclosure", "--yes", "--server", server.address,
                  "--dir", str(tmp_path / "run"), "--timeout", "5",
                  "--plugin", "robofast.testing.sky_device:register", "--catalog", "synthetic",
                  "--catalog-dir", str(tmp_path / "sky")])  # fmt: skip
    finally:
        server.stop()
    for d in (tel, cam):
        assert d.sets.count(True) <= 2 and d.sets.count(False) <= 1, (type(d).__name__, d.sets)
