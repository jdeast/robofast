"""The hardware rehearsal's safety pieces (robofast.testing.rehearsal,
robofast.testing.mount_guard; docs/demo.md): the mount guard between
robofast and a real mount, the refusals before anything starts, and the
plan. The whole rehearsal runs in tests/e2e/test_rehearsal.py."""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera, FakeFocuser, FakeTelescope
from robofast.testing.mount_guard import Endpoint, MountGuard, azimuth, lst_hours
from robofast.testing.rehearsal import (
    MAX_ALT,
    MIN_ALT,
    MINERVA,
    EndAction,
    RealTelescope,
    RehearsalRefused,
    RehearsalSettings,
    catalog,
    make_requests,
    observable,
    parse_duration,
    preflight,
    settings_from_args,
    slew_problems,
)
from robofast.testing.rv_night import SEQUENCER
from robofast.timeline import Entry, Timeline

NIGHT = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)


class Mount(FakeTelescope):
    def __init__(self) -> None:
        super().__init__(slew_time=0.05)
        self._parked = False
        self.parks = 0

    def Park(self) -> None:
        self.parks += 1
        super().Park()


@pytest.fixture
def mount():
    m = Mount()
    foc = FakeFocuser()
    srv = FakeAlpacaServer({"telescope": m, "focuser": foc}).start()
    yield m, foc, srv
    srv.stop()


@pytest.fixture
def guard(mount):
    _, _, srv = mount
    ep = Endpoint.parse(f"{srv.address}/0")
    g = MountGuard(
        {"t1": {"telescope": ep, "focuser": ep, "rotator": None}},
        limits=(MIN_ALT, MAX_ALT),
        site=(MINERVA.latitude, MINERVA.longitude),
        clock=lambda: NIGHT,
    ).start()
    yield g
    g.stop()


def put(g: MountGuard, path: str, **form) -> dict:
    r = httpx.put(f"http://{g.address}/api/v1/{path}", data={k: str(v) for k, v in form.items()})
    assert r.status_code == 200, r.text
    return r.json()


def get(g: MountGuard, path: str) -> dict:
    r = httpx.get(f"http://{g.address}/api/v1/{path}")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_guard_parks_by_stopping_tracking(mount, guard):
    m, _, _ = mount
    assert put(guard, "telescope/0/tracking", Tracking=True)["ErrorNumber"] == 0
    assert m.Tracking
    assert put(guard, "telescope/0/park")["ErrorNumber"] == 0
    assert m.parks == 0 and not m._parked and not m.Tracking  # never sent Park
    assert get(guard, "telescope/0/atpark")["Value"] is True
    lst = lst_hours(NIGHT, MINERVA.longitude)
    r = put(guard, "telescope/0/slewtocoordinatesasync", RightAscension=lst, Declination=10)
    assert r["ErrorNumber"] == 0x408 and "parked" in r["ErrorMessage"]
    assert put(guard, "telescope/0/unpark")["ErrorNumber"] == 0
    assert get(guard, "telescope/0/atpark")["Value"] is False
    assert [e[2] for e in guard.events] == ["park", "unpark"]


async def test_a_restarted_mount_server_is_connected_again(mount, guard):
    """Night 2026-11-09/10: T2's PWI server restarted mid-night (Connected
    false). The supervisor's AtPark read through the guard, and the guard's
    own park, connect it again instead of failing with NotConnected for the
    rest of the night (which held every lease: T1 sat idle)."""
    from robofast.alpaca_client import AlpacaDevice
    from robofast.owner import DeviceOwner
    from robofast.timeline import Timeline

    m, _, _ = mount
    tl = Timeline()
    owner = DeviceOwner("t2.telescope", lambda: AlpacaDevice(guard.address, "telescope", 0), tl)
    g = owner.grant("safety")
    assert put(guard, "telescope/0/park")["ErrorNumber"] == 0
    assert await g.get("AtPark") is True
    m.restart()
    assert get(guard, "telescope/0/atpark")["ErrorNumber"] == 0x407
    assert await g.get("AtPark") is True  # reconnected, through the guard
    assert m.Connected
    assert [e.detail["member"] for e in tl.events("reconnected")] == ["AtPark"]
    m.Tracking = True
    m.restart()
    assert put(guard, "telescope/0/park")["ErrorNumber"] == 0  # the guard's own requests
    assert m.Connected and not m.Tracking
    owner.close()


def test_the_guard_refuses_slews_outside_the_limits(mount, guard):
    m, _, _ = mount
    lst = lst_hours(NIGHT, MINERVA.longitude)
    put(guard, "telescope/0/tracking", Tracking=True)
    for ra, dec in (((lst + 12) % 24, 10.0), (lst, 31.68), (lst + 4.5, -30.0)):
        r = put(guard, "telescope/0/slewtocoordinatesasync", RightAscension=ra, Declination=dec)
        assert r["ErrorNumber"] == 0x401, r
    assert (m.ra, m.dec) == (0.0, 0.0)  # nothing forwarded
    assert len(guard.refused) == 3 and not guard.slews
    r = put(guard, "telescope/0/slewtocoordinatesasync", RightAscension=lst, Declination=10.0)
    assert r["ErrorNumber"] == 0 and (m.ra, m.dec) == (lst, 10.0)
    while get(guard, "telescope/0/slewing")["Value"]:
        pass
    [slew] = guard.slews
    assert slew["error_arcsec"] == 0.0 and 60 < slew["alt"] < 75 and "seconds" in slew


class LateMount(Mount):
    """A PWI2 that reads Slewing False for a moment after the goto, before
    it starts moving (night1108: the guard recorded t1's slew as ended
    0.24 s after the goto, 25 deg from its target)."""

    def __init__(self, start_delay: float) -> None:
        super().__init__()
        self._slew_time = 0.5
        self.start_delay = start_delay
        self._start_at = 0.0
        self._goal: tuple[float, float] | None = None

    def SlewToCoordinatesAsync(self, ra: float, dec: float) -> None:
        self.slews.append((ra, dec))
        self._goal = (ra, dec)
        self._start_at = self._now() + self.start_delay
        self._slew_until = self._start_at + self._slew_time

    def _sync(self) -> None:
        if self._goal is not None and self._now() >= self._slew_until:
            self.ra, self.dec = self._goal
            self._goal = None

    @property
    def Slewing(self) -> bool:
        self._sync()
        return self._start_at <= self._now() < self._slew_until

    @property
    def RightAscension(self) -> float:
        self._sync()
        return self.ra

    @property
    def Declination(self) -> float:
        self._sync()
        return self.dec


def test_a_slew_ends_when_the_mount_has_slewn_not_before_it_starts():
    """A Slewing False before the mount ever read Slewing True ends the
    slew's record only if the mount is already on the target."""
    import time

    m = LateMount(start_delay=0.4)
    srv = FakeAlpacaServer({"telescope": m, "focuser": FakeFocuser()}).start()
    ep = Endpoint.parse(f"{srv.address}/0")
    g = MountGuard(
        {"t1": {"telescope": ep, "focuser": ep, "rotator": None}},
        limits=(MIN_ALT, MAX_ALT),
        site=(MINERVA.latitude, MINERVA.longitude),
        clock=lambda: NIGHT,
    ).start()
    try:
        lst = lst_hours(NIGHT, MINERVA.longitude)
        put(g, "telescope/0/tracking", Tracking=True)
        assert (
            put(g, "telescope/0/slewtocoordinatesasync", RightAscension=lst, Declination=10.0)[
                "ErrorNumber"
            ]
            == 0
        )
        assert get(g, "telescope/0/slewing")["Value"] is False  # not started yet
        assert "seconds" not in g.slews[0]
        seen = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            v = get(g, "telescope/0/slewing")["Value"]
            seen = seen or v
            if seen and not v:
                break
            time.sleep(0.01)
        [slew] = g.slews
        assert seen and slew["seconds"] >= 0.9 and slew["error_arcsec"] == 0.0
        # A slew to where the mount already is ends at the first False.
        put(g, "telescope/0/slewtocoordinatesasync", RightAscension=lst, Declination=10.0)
        m._start_at = m._slew_until = 0.0  # PWI: nothing to do
        assert get(g, "telescope/0/slewing")["Value"] is False
        assert g.slews[1]["error_arcsec"] == 0.0 and g.slews[1]["seconds"] < 0.4
    finally:
        g.stop()
        srv.stop()


def test_the_guard_refuses_slews_outside_the_azimuth_window(mount):
    m, _, srv = mount
    ep = Endpoint.parse(f"{srv.address}/0")
    g = MountGuard(
        {"t1": {"telescope": ep, "focuser": ep, "rotator": None}},
        limits=(MIN_ALT, MAX_ALT),
        site=(MINERVA.latitude, MINERVA.longitude),
        clock=lambda: NIGHT,
        az_windows={"t1": (117.0, 264.0)},
    ).start()
    try:
        lst = lst_hours(NIGHT, MINERVA.longitude)
        assert round(azimuth(lst, 0.0, lst, MINERVA.latitude)) == 180
        put(g, "telescope/0/tracking", Tracking=True)
        # Up but in the north (Dec 60 on the meridian, az 0) and low in the east-northeast.
        for ra, dec in ((lst, 60.0), (lst + 4.0, 30.0)):
            r = put(g, "telescope/0/slewtocoordinatesasync", RightAscension=ra, Declination=dec)
            assert r["ErrorNumber"] == 0x401 and "window" in r["ErrorMessage"], r
        assert (m.ra, m.dec) == (0.0, 0.0) and len(g.refused) == 2
        r = put(g, "telescope/0/slewtocoordinatesasync", RightAscension=lst, Declination=10.0)
        assert r["ErrorNumber"] == 0 and (m.ra, m.dec) == (lst, 10.0)
    finally:
        g.stop()


def test_the_guard_refuses_everything_else(mount, guard):
    m, foc, _ = mount
    m.port_action = "SelectPort"
    for path, form in (
        ("telescope/0/action", {"Action": "SelectPort", "Parameters": "1"}),
        ("telescope/0/slewtoaltazasync", {"Azimuth": 10, "Altitude": 50}),
        ("telescope/0/moveaxis", {"Axis": 0, "Rate": 1}),
        ("focuser/0/move", {"Position": 1}),
    ):
        assert put(guard, path, **form)["ErrorNumber"] == 0x40B, path
    assert m.port is None and foc.Position == 12345
    assert get(guard, "focuser/0/position")["Value"] == 12345  # reads pass
    r = httpx.get(f"http://{guard.address}/api/v1/rotator/0/position")
    assert r.status_code == 400  # no rotator given: not served
    guard.lock()
    assert put(guard, "telescope/0/tracking", Tracking=True)["ErrorNumber"] == 0x40B
    assert not m.Tracking
    assert len(guard.refused) == 5


def args(**kw) -> argparse.Namespace:
    base = dict(
        i_know_the_enclosure_is_closed=True,
        telescope=["t1=127.0.0.1:1/0"],
        camera=["t1=127.0.0.1:2/0"],
        focuser=[],
        rotator=[],
        end_action="leave",
        duration=3600.0,
        exptime=60.0,
        images=2,
        image_exptime=10.0,
        spectra=None,
        ignore_twilight=False,
        console_port=-1,
        az_window=[],
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"i_know_the_enclosure_is_closed": False}, "enclosure-is-closed"),
        ({"telescope": [], "camera": []}, "no real telescope"),
        ({"camera": []}, "needs both"),
        ({"camera": ["t1=127.0.0.1:2/0", "t2=127.0.0.1:3/0"]}, "needs both"),
        ({"telescope": ["t1=127.0.0.1:1"]}, "HOST:PORT/N"),
        ({"telescope": ["t1"]}, "NAME=HOST:PORT/N"),
        ({"rotator": ["t2=none"]}, "not a real telescope"),
        ({"end_action": "park"}, "leave"),
        ({"end_action": "altaz:10,180"}, "outside"),
        (
            {
                "telescope": ["t1=127.0.0.1:1/0", "t2=127.0.0.1:5/0"],
                "camera": ["t1=127.0.0.1:2/0", "t2=127.0.0.1:2/0"],
            },
            "also t1",
        ),
    ],
)
def test_refused_before_anything_starts(kw, why):
    with pytest.raises(RehearsalRefused, match=why):
        settings_from_args(args(**kw))


def test_settings_from_args():
    s = settings_from_args(args(rotator=["t1=none"], end_action="altaz:60,180"))
    r = s.real["t1"]
    assert str(r.telescope) == "127.0.0.1:1/0" and r.focuser == r.telescope and r.rotator is None
    assert (s.end_action.alt, s.end_action.az) == (60.0, 180.0)
    assert parse_duration("2h") == 7200 and parse_duration("1h30m") == 5400
    assert parse_duration("90") == 90
    s = settings_from_args(args(az_window=["t1=117,264"]))
    assert s.az_windows == {"t1": (117.0, 264.0)}
    for bad in (["t9=1,2"], ["t1=264,117"], ["t1=x"]):
        with pytest.raises(RehearsalRefused):
            settings_from_args(args(az_window=bad))


def test_preflight_refuses_a_parked_mount_and_connects_the_camera(mount):
    m, _, srv = mount
    cam = FakeCamera()
    cam.Connected = False
    csrv = FakeAlpacaServer({"camera": cam}).start()
    try:
        ep = Endpoint.parse(f"{srv.address}/0")
        s = RehearsalSettings(
            real={"t1": RealTelescope(ep, Endpoint.parse(f"{csrv.address}/0"), ep, None)},
            end_action=EndAction.parse("altaz:60,0"),
        )
        m._parked = True
        with pytest.raises(RehearsalRefused, match=r"parked.*canslewaltazasync"):
            preflight(s)
        assert cam.Connected is True
        m._parked = False
        m.CanSlewAltAzAsync = True
        info = preflight(s)["t1"]
        assert info["telescope_simulator"] is True and info["camera_state"] == 0
        cam._state, cam._ends = 2, float("inf")  # exposing
        with pytest.raises(RehearsalRefused, match="not idle"):
            preflight(s)
    finally:
        csrv.stop()
    with pytest.raises(RehearsalRefused, match=r"camera.*ConnectError"):
        preflight(s)


def test_the_plan_is_what_is_up():
    end = NIGHT + timedelta(hours=2)
    targets = observable(catalog(), NIGHT, end)
    assert targets and all(t["up"] > 0 for t in targets)
    ep = Endpoint("127.0.0.1", 1, 0)
    s = RehearsalSettings(real={"t1": RealTelescope(ep, Endpoint("127.0.0.1", 2, 0), ep, ep)})
    groups = make_requests(s, NIGHT, end, targets)
    spectra = [g for g in groups if g["id"].startswith("REH")]
    assert len(spectra) == 2 * 3600 // 120 + 2
    first = spectra[0]["requests"][0]["configurations"][0]
    assert spectra[0]["name"].startswith("B star") and first["instrument_configs"][0][
        "extra_params"
    ] == {"iodine": True}
    tmpl = spectra[2]["requests"][0]["configurations"][0]["instrument_configs"][0]
    assert tmpl["extra_params"] == {"iodine": False}
    images = [g for g in groups if g["id"].startswith("IMG-")]
    # Only the real telescope's: the night is the telescopes given (2026-11-27).
    assert sorted(g["requests"][0]["location"]["telescope"] for g in images) == ["t1", "t1"]
    with pytest.raises(RehearsalRefused, match="nothing in the catalog"):
        make_requests(s, NIGHT, end, [])
    # In daylight, only with --ignore-twilight, and away from the Sun.
    noon = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    day = observable(catalog(), noon, noon + timedelta(hours=1), ignore_twilight=True)
    assert day and len(day) < len(observable(catalog(), noon, noon + timedelta(hours=1)))


def test_the_rehearsal_has_an_observable_request_all_night_in_a_southern_window():
    """Night 2026-11-21 (night1121): ~390 spectrum requests were handed out
    cyclically over the RV targets, ~50 each whatever their windows; the
    targets up early spent theirs, and from 06:41 UTC all 357 left were
    "outside their windows": the night idled with the roof closed. With the
    T2 azimuth window 117-264 and a 13 h night from 01:20 UTC on 2026-11-22,
    there must be an observable request at every 10 min step from dusk to
    dawn, and each target's request count must follow its window length."""
    import numpy as np

    from robofast.testing.rehearsal import _az_intersection, morning_twilight

    start = datetime(2026, 11, 22, 1, 20, tzinfo=UTC)
    ep = Endpoint("127.0.0.1", 1, 0)
    s = RehearsalSettings(
        real={"t2": RealTelescope(ep, Endpoint("127.0.0.1", 2, 0), ep, None)},
        az_windows={"t2": (117.0, 264.0)},
        duration=13 * 3600.0,
    )
    end = min(start + timedelta(seconds=s.duration), morning_twilight(start))
    targets = observable(catalog(), start, end, az_window=_az_intersection(s))
    notes: list[str] = []
    groups = make_requests(s, start, end, targets, note=notes.append)
    assert notes == [], notes  # no span without an observable target
    spectra = [g for g in groups if g["id"].startswith("REH")]
    windows = [
        (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]), g)
        for g in spectra
        for w in g["requests"][0]["windows"]
    ]
    t = start + timedelta(minutes=5)
    while t < end - timedelta(minutes=5):
        assert any(a <= t <= b for a, b, _ in windows), f"no request open at {t:%H:%M} UTC"
        t += timedelta(minutes=10)
    # Every request's target is up (limits and window) throughout its window.
    by_name = {x["name"]: x for x in targets}
    for a, b, g in windows:
        name = g["requests"][0]["configurations"][0]["target"]["name"]
        assert any(w0 <= a and b <= w1 for w0, w1 in by_name[name]["windows"]), g["id"]
    # Counts follow window length.
    count: dict[str, int] = {}
    for g in spectra:
        name = g["requests"][0]["configurations"][0]["target"]["name"]
        count[name] = count.get(name, 0) + 1
    names = sorted(count)
    length = [sum((b - a).total_seconds() for a, b in by_name[n]["windows"]) for n in names]
    # Not exactly proportional: a target that is alone in the window for a
    # while takes every slot there (0.78 measured; the cyclic allotment that
    # idled night1121 gave ~50 each whatever their windows).
    assert np.corrcoef(length, [count[n] for n in names])[0, 1] > 0.7
    assert max(count.values()) < 0.25 * len(spectra)  # no target dominates
    # And taken one spectrum every 2 min (the earliest-open first, as a
    # scheduler ahead of the slots would), they last the night: master's
    # cyclic allotment ran dry from about 08:14 in this model.
    per_request = [
        [(datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])) for w in ws]
        for ws in (g["requests"][0]["windows"] for g in spectra)
    ]
    left, idle, t = list(range(len(spectra))), [], start
    while t < end:
        ready = [i for i in left if any(a <= t <= b for a, b in per_request[i])]
        if ready:
            left.remove(ready[0])
        else:
            idle.append(t)
        t += timedelta(minutes=2)
    assert len(idle) <= 3, [f"{x:%H:%M}" for x in idle[:10]]


def test_imaging_requests_wait_for_the_azimuth_window():
    # The second T2 rehearsal: HD 50692 was picked for the second half (it is in
    # the window at the middle) but is still rising below az 117 at the start.
    start = datetime(2026, 11, 3, 9, 25, tzinfo=UTC)
    end = start + timedelta(hours=2)
    ep = Endpoint("127.0.0.1", 1, 0)
    s = RehearsalSettings(
        real={"t2": RealTelescope(ep, Endpoint("127.0.0.1", 2, 0), ep, ep)},
        az_windows={"t2": (117.0, 264.0)},
    )
    targets = observable(catalog(), start, end, az_window=(117.0, 264.0))
    images = [g for g in make_requests(s, start, end, targets) if g["id"].startswith("IMG-")]
    assert images
    for g in images:
        req = g["requests"][0]
        tgt = req["configurations"][0]["target"]
        for w in req["windows"]:
            for edge in (w["start"], w["end"]):
                when = datetime.fromisoformat(edge)
                az = azimuth(
                    tgt["ra"] / 15, tgt["dec"], lst_hours(when, MINERVA.longitude), MINERVA.latitude
                )
                assert 117 <= az <= 264, (g["id"], edge, az)


def test_a_slew_cut_short_by_a_cancel_is_not_a_hung_slew() -> None:
    """night1121, 03:27 UTC: both mounts started a slew 0.6 s before an
    unsafe cancel; the cleanup aborted them, so they never reported an end,
    and the verdict called them hung. A cancel before the telescope's next
    slew excuses it; a slew with no cancel or error after it still counts."""
    tl = Timeline()
    t = tl.t0
    slews = [
        {"telescope": "t1", "alt": 45.0, "t": t + 1, "utc": "a", "seconds": 20.0},
        {"telescope": "t1", "alt": 45.0, "t": t + 30, "utc": "cut"},
        {"telescope": "t1", "alt": 45.0, "t": t + 60, "utc": "hung"},
        {"telescope": "t2", "alt": 45.0, "t": t + 70, "utc": "hung-t2"},
    ]
    assert slew_problems(slews, tl) == [
        "t1: a slew never reported its end (cut)",
        "t1: a slew never reported its end (hung)",
        "t2: a slew never reported its end (hung-t2)",
    ]
    tl._add(Entry(t + 31, "event", SEQUENCER, None, "cancel_requested", "", {"reason": "unsafe"}))
    assert slew_problems(slews, tl) == [
        "t1: a slew never reported its end (hung)",
        "t2: a slew never reported its end (hung-t2)",
    ]
    # Out of the altitude limits is still reported, cancel or not.
    low = [{"telescope": "t1", "alt": MIN_ALT - 1, "t": t + 30, "utc": "low", "seconds": 5.0}]
    assert slew_problems(low, tl) == [f"t1: slew to altitude {MIN_ALT - 1:.1f}"]


def _sockets_to(port: int) -> int:
    """This process's established TCP sockets whose peer is ``port``."""
    inodes = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in open(name).read().splitlines()[1:]:
            f = line.split()
            if f[3] == "01" and int(f[2].rsplit(":", 1)[1], 16) == port:
                inodes.add(f[9])
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            n += 1
    return n


@pytest.mark.skipif(not os.path.exists("/proc/net/tcp"), reason="needs Linux /proc")
def test_regression_20261125_reconnecting_clients_do_not_leak_upstream_sockets(mount, guard):
    """night1124 ran out of file descriptors after 11.5 h: robofast's clients
    reconnect to the guard after each idle gap, every connection got its own
    thread and upstream client, and none was closed until the guard stopped.
    Now a connection's upstream client closes when the connection does."""
    _, _, srv = mount
    port = int(srv.address.rsplit(":", 1)[1])
    for _ in range(40):  # 40 connections, one after another (httpx.get: a new client each)
        assert get(guard, "telescope/0/tracking")["ErrorNumber"] == 0
    deadline = time.monotonic() + 5
    while (_sockets_to(port) > 2 or len(guard._clients) > 1) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(guard._clients) <= 1, len(guard._clients)
    assert _sockets_to(port) <= 2, _sockets_to(port)
