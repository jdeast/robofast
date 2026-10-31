"""A TCP simulator of a DFM TCSGalil ("TCSGO"), for tests and ConformU.

It speaks the external command protocol of :mod:`robofast_core.devices.tcsgo_client`
(``docs/tcsgo.md``): packets ``#n,a,b,...;`` terminated by ``\\r``; queries
answer ``#v1,...,vn;``; every other command answers nothing (``ack="echo"``
echoes the packet instead, as the legacy test relay did, to exercise a
client against a TCS that does acknowledge). Unknown or malformed packets are
ignored and counted (``ignored``): what the real TCS does with them is not
documented.

The mount (equatorial, HA/Dec axes) follows the manual's model:

* a *target* is either celestial (RA/Dec, moving at the sidereal rate in HA)
  or fixed in HA/Dec (``#10`` zenith, ``#58`` park);
* mount modes ``#12``: 0 hold position (axes stop), 1 follow target (move at
  the target's velocity, no position feedback), 2 slew to target (both axes
  at ``slew_rate`` deg/s until within ``capture_arcsec``, then follow with
  "target captured"), 3 chase (like slewing, but stays in chase);
* ``#13`` stops (hold); ``#50`` starts tracking at the current position;
  ``#7`` offsets the target (east = +RA, divided by cos Dec), and in follow
  mode the mount does not move until a ``#12`` (manual); ``#61`` pulse guides
  at ``guide_rate`` arcsec/s, setting the "ASCOM is pulseguiding" bit;
* ``#58`` parks at (``park_ha``, ``park_dec``): a slew, then hold; ``#59``
  clears the internal parked flag. (The TCS reports no parked bit in
  ``#26``; this simulator tracks one only for :meth:`parked`.)
* limits: a slew target below ``min_alt`` or outside the HA/Dec travel sets
  "target out of range" and does not move (the manual says the mount
  "switches to Follow Target mode"); tracking into ``min_alt`` sets "reached
  soft limit" and holds; within ``approach_deg`` of it sets "approaching".

The focuser (``#27``, ``#64``) moves at ``focus_rate`` microns/s between
``focus_min`` and ``focus_max``. *Assumed*: "moving in" is decreasing
microns. The dome (``#20``, ``#43``, ``#35``, ``#62``) rotates at
``dome_rate`` deg/s; ``#20`` works only with the front-panel AUTO DOME switch
up (manual); two shutters (``#36`` upper, ``#44`` lower; ``#40``/``#41``
stop) take ``shutter_time`` s each.

Fault injection (thread-safe methods, or ``SIM ...;`` packets on the same
port when run as a process): :meth:`set_mode` ``ok``/``hang`` (read, never
answer or act)/``silent`` (close every connection)/``garble`` (truncate query
replies)/``slow`` (answer after ``slow_delay``); :meth:`set_drives` (drives
switch down: nothing moves, as with the amplifiers off); :meth:`hit_limit`
(a hard travel limit trips: the mount holds and the bit stays until
:meth:`clear_limit`); :meth:`set_stale` (Galil data stale);
:meth:`set_switch` (any front-panel switch).

Run it standalone::

    python -m robofast_core.devices.tcsgo_sim --port 0

It prints ``tcsgo simulator on HOST:PORT`` once listening. It binds loopback
addresses only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import ipaddress
import json
import logging
import math
import threading
import time
from dataclasses import dataclass

from robofast_core.devices.tcsgo_client import COMMANDS, TCSStatus

log = logging.getLogger(__name__)

MODES = ("ok", "hang", "silent", "garble", "slow")
SIDEREAL = 1.00273790935  # sidereal hours per solar hour
FLWO_LATITUDE = 31.6809  # deg N; the 60-inch, approximate (public)
FLWO_LONGITUDE = -110.8781  # deg E


def lst_hours(longitude_deg: float, unix: float | None = None) -> float:
    """Local mean sidereal time (hours), to about 0.1 s: plenty for a simulator."""
    unix = time.time() if unix is None else unix
    jd = unix / 86400.0 + 2440587.5
    d = jd - 2451545.0
    gmst = 18.697374558 + 24.06570982441908 * d
    return (gmst + longitude_deg / 15.0) % 24.0


def hadec_to_altaz(ha_h: float, dec: float, lat: float) -> tuple[float, float]:
    """(alt, az) in degrees, azimuth north = 0, east = 90."""
    h, d, p = math.radians(ha_h * 15.0), math.radians(dec), math.radians(lat)
    sin_alt = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    y = -math.cos(d) * math.sin(h)
    x = math.sin(d) * math.cos(p) - math.cos(d) * math.sin(p) * math.cos(h)
    az = math.degrees(math.atan2(y, x)) % 360.0
    return math.degrees(alt), az


def altaz_to_hadec(alt: float, az: float, lat: float) -> tuple[float, float]:
    """(ha hours, dec deg) for an altitude and azimuth (north = 0, east = 90)."""
    a, z, p = math.radians(alt), math.radians(az), math.radians(lat)
    sin_dec = math.sin(a) * math.sin(p) + math.cos(a) * math.cos(p) * math.cos(z)
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    y = -math.cos(a) * math.sin(z)
    x = math.sin(a) * math.cos(p) - math.cos(a) * math.sin(p) * math.cos(z)
    ha = math.degrees(math.atan2(y, x)) / 15.0
    return ha, math.degrees(dec)


def _wrap_h(h: float) -> float:
    """Hours into [-12, 12)."""
    return (h + 12.0) % 24.0 - 12.0


@dataclass
class _Target:
    kind: str  # "radec" or "fixed"
    a: float  # RA hours (radec) or HA hours (fixed)
    dec: float
    equinox: float = 2000.0
    set_at: float = 0.0

    def ha(self, lst: float) -> float:
        return _wrap_h(lst - self.a) if self.kind == "radec" else self.a


@dataclass
class _Shutter:
    state: str = "closed"  # open, closed, opening, closing, stopped
    done_at: float = 0.0


class TCSGOSim:
    """The simulated TCSGalil. :meth:`start` serves it on a background thread."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        latitude: float = FLWO_LATITUDE,
        longitude: float = FLWO_LONGITUDE,
        slew_rate: float = 2.0,
        capture_arcsec: float = 10.0,
        min_alt: float = 15.0,
        approach_deg: float = 2.0,
        ha_limit: float = 6.0,
        dec_limits: tuple[float, float] = (-40.0, 89.5),
        park_ha: float = 0.0,
        park_dec: float | None = None,
        guide_rate: float = 5.0,
        focus_rate: float = 200.0,
        focus_min: float = -5000.0,
        focus_max: float = 5000.0,
        dome_rate: float = 3.0,
        dome_home: float = 180.0,
        shutter_time: float = 3.0,
        display_equinox: float = 2000.0,
        ack: str | None = None,
        slow_delay: float = 3.0,
    ) -> None:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the simulator binds loopback addresses only")
        self.host, self.port = host, port
        self.latitude, self.longitude = latitude, longitude
        self.slew_rate, self.capture_arcsec = slew_rate, capture_arcsec
        self.min_alt, self.approach_deg = min_alt, approach_deg
        self.ha_limit, self.dec_limits = ha_limit, dec_limits
        self.park = (park_ha, latitude if park_dec is None else park_dec)
        self.guide_rate = guide_rate
        self.focus_rate, self.focus_min, self.focus_max = focus_rate, focus_min, focus_max
        self.dome_rate, self.dome_home = dome_rate, dome_home
        self.shutter_time = shutter_time
        self.display_equinox = display_equinox
        if ack not in (None, "echo"):
            raise ValueError("ack must be None or 'echo'")
        self.ack, self.slow_delay = ack, slow_delay
        self._lock = threading.RLock()
        self.mode = "ok"
        # The mount starts parked, holding.
        self.ha, self.dec = self.park
        self.mount_mode = 0
        self.target: _Target | None = None
        self.captured = False
        self.parked_flag = True
        self._parking = False
        self.out_of_range = False
        self.soft_limit = False
        self.hard_limits: set[str] = set()
        self._pulses: dict[str, tuple[int, float]] = {}  # axis -> (direction, until)
        self.switches = {
            "drives_switch_up": True,
            "track_switch_up": True,
            "aux_track_switch_up": False,
            "dome_track_switch_up": False,
            "auto_dome_switch_up": True,
            "amplifiers_switch_up": True,
        }
        self.stale = False
        #: #18 "use cosine of declination" for the paddle (and pulse guiding).
        self.use_cos_dec = False
        self.focus = 0.0
        self.focus_target = 0.0
        self.dome_az = dome_home
        self.dome_mode = 0
        self._dome_dir = 0
        self.dome_target: float | None = None
        self.shutters = {"upper": _Shutter(), "lower": _Shutter()}
        self.mirror_doors_open = True
        #: Executed commands: (monotonic time, number, args).
        self.log: list[tuple[float, int, tuple[float, ...]]] = []
        self.ignored: list[bytes] = []
        self._last_tick = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None

    # -- the model --------------------------------------------------------------

    def lst(self) -> float:
        return lst_hours(self.longitude)

    def altaz(self) -> tuple[float, float]:
        return hadec_to_altaz(self.ha, self.dec, self.latitude)

    @property
    def drives_on(self) -> bool:
        return self.switches["drives_switch_up"] and self.switches["amplifiers_switch_up"]

    def _in_range(self, ha: float, dec: float) -> bool:
        alt, _ = hadec_to_altaz(ha, dec, self.latitude)
        lo, hi = self.dec_limits
        return alt >= self.min_alt and abs(ha) <= self.ha_limit and lo <= dec <= hi

    def _step(self, cur: float, goal: float, step: float) -> float:
        d = goal - cur
        return goal if abs(d) <= step else cur + math.copysign(step, d)

    def _tick(self, now: float) -> None:
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        lst = self.lst()
        moving = self.drives_on and not self.hard_limits
        # Mount.
        if moving and self.mount_mode in (2, 3) and self.target is not None:
            tha = self.target.ha(lst)
            step = self.slew_rate * dt
            self.ha = self._step(self.ha * 15.0, tha * 15.0, step) / 15.0
            self.dec = self._step(self.dec, self.target.dec, step)
            sep = (
                math.hypot(
                    (self.ha - tha) * 15.0 * math.cos(math.radians(self.dec)),
                    self.dec - self.target.dec,
                )
                * 3600.0
            )
            if sep <= self.capture_arcsec:
                self.ha, self.dec = tha, self.target.dec
                self.captured = True
                if self.mount_mode == 2 and self._parking:
                    self.mount_mode, self.parked_flag, self._parking = 0, True, False
                elif self.mount_mode == 2:
                    self.mount_mode = 1  # captured: follow the target
            elif self.mount_mode == 3:
                self.captured = False
        elif moving and self.mount_mode == 1 and self.target is not None:
            if self.target.kind == "radec":
                self.ha = _wrap_h(self.ha + dt * SIDEREAL / 3600.0)
        # Pulse guiding (follow or hold): guide_rate arcsec/s for the pulse.
        if moving:
            for axis, (direction, until) in list(self._pulses.items()):
                step = self.guide_rate * max(0.0, min(now, until) - (now - dt)) / 3600.0
                if direction == 0:
                    self.dec += step
                elif direction == 1:
                    self.dec -= step
                else:  # east is +RA, so -HA; RA rates are on the sky only with cos Dec on
                    cosd = max(0.05, math.cos(math.radians(self.dec))) if self.use_cos_dec else 1
                    self.ha += (-1 if direction == 2 else 1) * step / 15.0 / cosd
                if now >= until:
                    del self._pulses[axis]
        # Limits.
        alt, _ = self.altaz()
        if self.mount_mode != 0 and alt < self.min_alt and not self._parking:
            log.warning("sim: reached soft limit (alt %.1f)", alt)
            self.soft_limit = True
            self.mount_mode = 0
        elif alt >= self.min_alt:
            self.soft_limit = False
        # Focus.
        if self.drives_on and self.focus != self.focus_target:
            self.focus = self._step(self.focus, self.focus_target, self.focus_rate * dt)
        # Dome.
        if self.switches["auto_dome_switch_up"]:
            goal = None
            if self.dome_mode == 1:
                goal = self.altaz()[1]
            elif self.dome_mode == 2:
                goal = self.dome_target
            elif self.dome_mode == 3:
                goal = self.dome_home
            if goal is not None:
                d = (goal - self.dome_az + 180.0) % 360.0 - 180.0
                step = self.dome_rate * dt
                self._dome_dir = 0 if abs(d) < 0.01 else (1 if d > 0 else -1)
                self.dome_az = goal if abs(d) <= step else self.dome_az + math.copysign(step, d)
                self.dome_az %= 360.0
                if self.dome_mode == 3 and abs(d) <= step:
                    self.dome_az = self.dome_home
                    self.dome_mode = 0
                    self._dome_dir = 0
            else:
                self._dome_dir = 0
        for sh in self.shutters.values():
            if sh.state in ("opening", "closing") and now >= sh.done_at:
                sh.state = "open" if sh.state == "opening" else "closed"

    def status_values(self) -> tuple[int, ...]:
        alt, _ = self.altaz()
        up, lo = self.shutters["upper"].state, self.shutters["lower"].state
        flags = dict(self.switches)
        flags.update(
            pulse_guiding=bool(self._pulses),
            horizon_limit="horizon" in self.hard_limits,
            east_limit="east" in self.hard_limits,
            west_limit="west" in self.hard_limits,
            north_limit="north" in self.hard_limits,
            south_limit="south" in self.hard_limits,
            target_out_of_range=self.out_of_range,
            approaching_soft_limit=alt < self.min_alt + self.approach_deg,
            soft_limit=self.soft_limit,
            dome_find_home_mode=self.dome_mode == 3,
            dome_goto_mode=self.dome_mode == 2,
            dome_track_mode=self.dome_mode == 1,
            dome_manual_mode=self.dome_mode == 0,
            dome_rotating_left=self._dome_dir < 0,
            dome_rotating_right=self._dome_dir > 0,
            dome_at_home=abs((self.dome_az - self.dome_home + 180) % 360 - 180) < 1.0,
            focus_moving_in=self.focus_target < self.focus and self.drives_on,
            focus_moving_out=self.focus_target > self.focus and self.drives_on,
            focus_outer_limit=self.focus >= self.focus_max,
            focus_inner_limit=self.focus <= self.focus_min,
            tcp_connected=True,
            galil_stale=self.stale,
            use_cos_dec=self.use_cos_dec,
            mode_hold=self.mount_mode == 0,
            mode_follow=self.mount_mode == 1,
            mode_slew=self.mount_mode == 2,
            mode_chase=self.mount_mode == 3,
            target_captured=self.captured and self.mount_mode != 2,
            upper_shutter_open=up == "open",
            upper_shutter_closed=up == "closed",
            lower_shutter_open=lo == "open",
            lower_shutter_closed=lo == "closed",
            mirror_doors_open=self.mirror_doors_open,
            mirror_doors_closed=not self.mirror_doors_open,
        )
        return TCSStatus.encode(**flags)

    def mount_values(self) -> tuple[float, ...]:
        lst = self.lst()
        alt, az = self.altaz()
        zd = 90.0 - alt
        airmass = 1.0 / max(0.01, math.cos(math.radians(min(zd, 89.0))))
        now = datetime.datetime.now(datetime.UTC)
        ut = now.hour + now.minute / 60 + (now.second + now.microsecond / 1e6) / 3600
        year0 = datetime.datetime(now.year, 1, 1, tzinfo=datetime.UTC)
        frac = (now - year0).total_seconds() / (365.25 * 86400.0)
        ra = (lst - self.ha) % 24.0
        return (
            self.ha,
            ra,
            self.dec,
            self.display_equinox,
            airmass,
            zd,
            az,
            lst,
            ut,
            now.year + frac,
        )

    def target_values(self, now: float) -> tuple[float, ...]:
        t = self.target
        if t is None:
            return (0.0,) * 10
        lst = self.lst()
        ha = t.ha(lst)
        alt, az = hadec_to_altaz(ha, t.dec, self.latitude)
        ra = t.a if t.kind == "radec" else (lst - t.a) % 24.0
        airmass = 1.0 / max(0.01, math.cos(math.radians(min(90 - alt, 89.0))))
        return (now - t.set_at, ha, ra, t.dec, t.equinox, 0.0, 0.0, airmass, 90 - alt, az)

    # -- commands --------------------------------------------------------------------

    def _set_target(self, t: _Target, now: float) -> None:
        t.set_at = now
        ok = self._in_range(t.ha(self.lst()), t.dec)
        self.out_of_range = not ok
        if not ok:
            log.info("sim: target out of range")
            if self.mount_mode in (2, 3):
                self.mount_mode = 1  # the manual: it switches to Follow Target
            return
        self.target = t
        self.captured = False

    def execute(self, number: int, args: tuple[float, ...]) -> tuple[float, ...] | None:
        """Run one command; return a query's values, else None."""
        with self._lock:
            now = time.monotonic()
            self._tick(now)
            spec = COMMANDS.get(number)
            if spec is None or len(args) != len(spec.args):
                self.ignored.append(f"#{number}{args}".encode())
                return None
            self.log.append((now, number, args))
            if number == 26:
                return self.status_values()
            if number == 25:
                return self.mount_values()
            if number == 32:
                return self.target_values(now)
            if number == 64:
                return (round(self.focus, 1),)
            if number == 62:
                return (round(self.dome_az, 2),)
            if number == 63:
                return (0.0,)
            if number == 28:
                lst = self.lst()
                t = self.target
                return (
                    time.time() / 86400 + 2440587.5 - 2450000,
                    lst,
                    self.ha,
                    self.dec,
                    t.a if t else 0.0,
                    t.dec if t else 0.0,
                )
            self._command(number, args, now)
            return None

    def _command(self, n: int, a: tuple[float, ...], now: float) -> None:
        if n == 6:
            ra, dec, eq = a
            if not (0 <= ra < 24 and -90 <= dec <= 90):
                self.ignored.append(f"#6 out of range {a}".encode())
                return
            self._parking = False
            self._set_target(_Target("radec", ra, dec, eq or self.display_equinox), now)
        elif n == 7:
            if self.target is not None:
                east, north = a
                t = self.target
                cosd = max(0.05, math.cos(math.radians(t.dec)))
                if t.kind == "radec":
                    t.a = (t.a + east / cosd / 15.0 / 3600.0) % 24.0
                else:
                    t.a -= east / cosd / 15.0 / 3600.0
                t.dec += north / 3600.0
                self.captured = False
        elif n == 10:
            self._parking = False
            self._set_target(_Target("fixed", 0.0, self.latitude), now)
        elif n == 12:
            mode = int(a[0])
            if mode in (0, 1, 2, 3):
                if mode in (2, 3) and (self.target is None or self.out_of_range):
                    mode = 1 if self.target is not None else 0
                if mode != 0:
                    self.parked_flag = False
                self.mount_mode = mode
                if mode in (2, 3):
                    self.captured = False
        elif n == 13:
            self.mount_mode = 0
            self._parking = False
            self._pulses.clear()
        elif n == 50:
            lst = self.lst()
            self._parking = False
            self.parked_flag = False
            self.target = _Target(
                "radec", (lst - self.ha) % 24.0, self.dec, self.display_equinox, now
            )
            self.out_of_range = False
            self.mount_mode = 1
            self.captured = True
        elif n == 58:
            self.target = _Target("fixed", *self.park, set_at=now)
            self.out_of_range = False
            self._parking = True
            self.captured = False
            self.mount_mode = 2
        elif n == 59:
            self.parked_flag = False
        elif n == 61:
            direction, ms = int(a[0]), a[1]
            if direction in (0, 1, 2, 3) and ms > 0:
                axis = "dec" if direction in (0, 1) else "ra"
                self._pulses[axis] = (direction, now + ms / 1000.0)
        elif n == 27:
            self.focus_target = min(self.focus_max, max(self.focus_min, a[0]))
        elif n == 20:
            if self.switches["auto_dome_switch_up"] and int(a[0]) in (0, 1, 2, 3):
                self.dome_mode = int(a[0])
        elif n == 43:
            self.dome_target = a[0] % 360.0
        elif n == 35:
            self.dome_mode = 0
            self._dome_dir = 0
        elif n in (36, 44):
            sh = self.shutters["upper" if n == 36 else "lower"]
            want = "open" if int(a[0]) == 1 else "closed"
            if sh.state != want and sh.state != ("opening" if want == "open" else "closing"):
                sh.state = "opening" if want == "open" else "closing"
                sh.done_at = now + self.shutter_time
        elif n in (40, 41):
            sh = self.shutters["upper" if n == 40 else "lower"]
            if sh.state in ("opening", "closing"):
                sh.state = "stopped"
        elif n == 18:
            self.use_cos_dec = int(a[0]) == 1
        elif n == 29:
            self.mirror_doors_open = int(a[0]) == 1
        # Everything else (rates, corrections, encoder and model changes) is
        # accepted and not modelled.

    # -- fault injection / inspection ---------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode

    def set_drives(self, on: bool) -> None:
        with self._lock:
            self._tick(time.monotonic())
            self.switches["drives_switch_up"] = on

    def set_switch(self, name: str, on: bool) -> None:
        with self._lock:
            if name not in self.switches:
                raise ValueError(name)
            self._tick(time.monotonic())
            self.switches[name] = on

    def hit_limit(self, which: str = "horizon") -> None:
        if which not in ("horizon", "east", "west", "north", "south"):
            raise ValueError(which)
        with self._lock:
            self._tick(time.monotonic())
            self.hard_limits.add(which)
            self.mount_mode = 0

    def clear_limit(self, which: str | None = None) -> None:
        with self._lock:
            if which is None:
                self.hard_limits.clear()
            else:
                self.hard_limits.discard(which)

    def set_stale(self, stale: bool) -> None:
        with self._lock:
            self.stale = stale

    def numbers(self) -> list[int]:
        with self._lock:
            return [n for _, n, _ in self.log]

    def state(self) -> dict[str, object]:
        with self._lock:
            self._tick(time.monotonic())
            alt, az = self.altaz()
            return {
                "ha": self.ha,
                "dec": self.dec,
                "alt": alt,
                "az": az,
                "mode": self.mount_mode,
                "parked": self.parked_flag,
                "focus": self.focus,
                "dome_az": self.dome_az,
                "shutters": {k: v.state for k, v in self.shutters.items()},
            }

    def parked(self) -> bool:
        with self._lock:
            self._tick(time.monotonic())
            return self.parked_flag and self.mount_mode == 0

    def tracking(self) -> bool:
        with self._lock:
            self._tick(time.monotonic())
            return self.mount_mode in (1, 3) and self.drives_on

    def _control(self, words: list[str]) -> str:
        what = words[0].upper() if words else ""
        arg = words[1].upper() if len(words) > 1 else ""
        if what == "MODE" and arg:
            self.set_mode(arg.lower())
        elif what == "DRIVES":
            self.set_drives(arg == "ON")
        elif what == "LIMIT":
            self.hit_limit(arg.lower() or "horizon")
        elif what == "CLEARLIMIT":
            self.clear_limit()
        elif what == "STALE":
            self.set_stale(arg == "ON")
        elif what == "STATE":
            return json.dumps(self.state())
        else:
            return "SIM ERROR unknown control"
        return "SIM OK"

    # -- the wire --------------------------------------------------------------------

    def _parse(self, packet: bytes) -> tuple[int, tuple[float, ...]] | None:
        text = packet.decode("ascii", "replace").strip()
        if not text.startswith("#"):
            return None
        try:
            nums = [float(x) for x in text[1:].split(",")]
        except ValueError:
            return None
        if not nums or not nums[0].is_integer():
            return None
        return int(nums[0]), tuple(nums[1:])

    @staticmethod
    def _fmt(values: tuple[float, ...]) -> bytes:
        out = []
        for v in values:
            out.append(str(int(v)) if float(v).is_integer() else f"{v:.6f}")
        return ("#" + ",".join(out) + ";").encode()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.mode == "silent":
                return
            buf = b""
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buf += chunk
                while b";" in buf:
                    packet, buf = buf.split(b";", 1)
                    text = packet.strip()
                    if not text:
                        continue
                    if text.startswith(b"SIM"):
                        reply = self._control(text.decode().split()[1:])
                        writer.write(reply.encode() + b";")
                        await writer.drain()
                        continue
                    mode = self.mode
                    if mode == "silent":
                        return
                    if mode == "hang":
                        while self.mode == "hang" and not (
                            self._stopping is not None and self._stopping.is_set()
                        ):
                            await asyncio.sleep(0.05)
                        return
                    parsed = self._parse(text)
                    if parsed is None:
                        with self._lock:
                            self.ignored.append(text)
                        continue
                    if mode == "slow":
                        await asyncio.sleep(self.slow_delay)
                    values = self.execute(*parsed)
                    if values is not None:
                        out = self._fmt(values)
                        if self.mode == "garble":
                            out = out[: max(2, len(out) * 2 // 5)] + b";"
                        writer.write(out)
                    elif self.ack == "echo":
                        writer.write(text + b";")
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def _ticker(self) -> None:
        while True:
            with self._lock:
                self._tick(time.monotonic())
            await asyncio.sleep(0.02)

    async def _serve(self) -> None:
        self._stopping = asyncio.Event()
        server = await asyncio.start_server(self._session, self.host, self.port)
        self.port = server.sockets[0].getsockname()[1]
        ticker = asyncio.create_task(self._ticker())
        self._ready.set()
        try:
            await self._stopping.wait()
        finally:
            ticker.cancel()
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> TCSGOSim:
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            try:
                self._loop.run_until_complete(self._serve())
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, name="tcsgo-sim", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("simulator did not start")
        return self

    def stop(self) -> None:
        self.set_mode("ok")
        if self._loop is not None and self._stopping is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--slew-rate", type=float, default=2.0, help="deg/s per axis")
    ap.add_argument("--min-alt", type=float, default=15.0)
    ap.add_argument("--dec-min", type=float, default=-40.0)
    ap.add_argument("--ha-limit", type=float, default=6.0, help="hours either side")
    ap.add_argument("--shutter-time", type=float, default=3.0)
    ap.add_argument("--focus-rate", type=float, default=200.0)
    ap.add_argument("--dome-rate", type=float, default=3.0)
    ap.add_argument("--ack", choices=["echo"], default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sim = TCSGOSim(
        args.host,
        args.port,
        slew_rate=args.slew_rate,
        min_alt=args.min_alt,
        dec_limits=(args.dec_min, 89.5),
        ha_limit=args.ha_limit,
        shutter_time=args.shutter_time,
        focus_rate=args.focus_rate,
        dome_rate=args.dome_rate,
        ack=args.ack,
    ).start()
    print(f"tcsgo simulator on {sim.address}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
