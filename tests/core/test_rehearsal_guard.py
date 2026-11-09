"""The hardware rehearsal's safety pieces (robofast.testing.rehearsal,
robofast.testing.mount_guard; docs/demo.md): the mount guard between
robofast and a real mount, the refusals before anything starts, and the
plan. The whole rehearsal runs in tests/e2e/test_rehearsal.py."""

from __future__ import annotations

import argparse
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
)

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
        ({"telescope": ["t3=127.0.0.1:1/0"], "camera": ["t3=127.0.0.1:2/0"]}, "unknown"),
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
    assert sorted(g["requests"][0]["location"]["telescope"] for g in images) == [
        "t1",
        "t1",
        "t2",
        "t2",
    ]
    with pytest.raises(RehearsalRefused, match="nothing in the catalog"):
        make_requests(s, NIGHT, end, [])
    # In daylight, only with --ignore-twilight, and away from the Sun.
    noon = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    day = observable(catalog(), noon, noon + timedelta(hours=1), ignore_twilight=True)
    assert day and len(day) < len(observable(catalog(), noon, noon + timedelta(hours=1)))


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
