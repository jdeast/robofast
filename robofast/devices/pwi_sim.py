"""Simulators of PlaneWave PWI's HTTP APIs (PWI4 and PWI2) for a CDK700.

One mount model (:class:`CDK700Model`) behind either front end:

* ``api="pwi4"``: ``GET /status``, ``/mount/connect``, ``/mount/enable?axis=``,
  ``/mount/goto_alt_az?alt_degs=&az_degs=``, ... answering ``key=value`` lines
  with the keys PWI4 4.1.4 reports (the subset robofast reads, plus
  ``pwi4.version``). Unknown paths are 404, bad parameters 400 (as PWI4).
* ``api="pwi2"``: ``GET /?device=mount&cmd=move&alt=..&azm=..`` and
  ``/?cmd=getsystem``, answering the ``<system>`` XML status PWI2 sends
  (fields as in the legacy ``telstateunknown.xml``). PWI2's quirk is kept:
  ``mount.moving`` is ``True`` whenever tracking is off; ``stuck_moving``
  makes it ``True`` always (as MINERVA T1's PWI2 after a restart);
  ``altaz_tracks`` reports tracking on during an alt/az move (and a home),
  leaves it on after, and tracking off halts that move (as MINERVA T1's
  PWI2, 2026-11-08/09).

The model, from the legacy driver's use of PWI (``docs/pwi.md``):

* motion needs the mount connected and both axes enabled; a goto otherwise
  does nothing (*assumed*: what PWI does then is not known). Nothing is
  connected or enabled at start unless asked (``--ready``), and not homed
  unless asked (``--homed``): moves work unhomed here, as on a real mount;
  refusing them is robofast's job.
* each axis slews at ``slew_rate`` deg/s. Azimuth is mechanical
  (``mech``, unbounded; reported azimuth is ``mech`` mod 360). A goto takes
  azimuth to the mechanical angle in [``wrap_min``, ``wrap_min`` + 360), as
  PWI4 does with its axis-0 wrap range (PWI4 ``/status``
  ``mount.axis0_wrap_range_min_degs``, set with
  ``/mount/set_axis0_wrap_range_min?degs=``; 0 by default, so a goto from
  az 359 to 1 goes the long way round, as PWI4 did on a CDK700). Tracking
  stays continuous. Below ``az_mech_min`` the mount cannot go (an
  obstruction, or a cable at its end): it stops there and reports slewing.
* ``goto_ra_dec_j2000`` slews and then tracks (J2000 = apparent here);
  ``goto_alt_az`` slews and stops tracking (PWI4; PWI2 keeps its tracking
  state and holds the alt/az); ``stop`` and ``disable`` stop the axes and
  tracking; ``find_home`` drives the axes to (``home_alt``, ``home_az``)
  and sets "homed" (after at least ``home_time`` s); PWI4's
  ``/mount/findhome?axis=0|1`` (4.1.7 beta 3+) homes one axis. PWI4 reports
  ``mount.axisN.is_homed`` only with ``report_homed`` (PWI4 4.1.4 did not);
  PWI2 reports ``encoders_have_been_set``. With ``home_search`` (deg, 0 by
  default) a home of an **unhomed** altitude axis first drives the reported
  altitude down that far at ``slew_rate`` (the search for the home sensor, in
  the arbitrary frame the encoders power up in), then the encoders are set
  and the reported position jumps to the home: as MINERVA T2's PWI2 after a
  controller power cycle (2026-11-09: from ~0-18 deg, downward at ~2-3 deg/s,
  then 45.85 deg).
* PWI4's own altitude limit (``pwi_min_alt``, off by default): a goto
  below it is accepted, the mount stops at the limit and then reports
  slewing *and* tracking until stopped -- what PWI4 4.1.6 does on a CDK700.
* :meth:`CDK700Model.power_cycle`: the controller loses power (disconnected,
  disabled, not homed, both axes reading 0 deg, as a CDK700's encoders do);
  :meth:`CDK700Model.pwi_restart`: PWI itself restarts (disconnected,
  disabled, not homed, ``Default_Mount_Model.PXP`` loaded; ``--restarted``
  starts a simulator in that state).
* PWI2 ``device=mount&cmd=setmodel&filename=F`` loads pointing model ``F``
  (reported as ``mount.pointing_model``; PWI4's ``mount.model.filename``),
  after ``model_delay`` s (default 0; MINERVA's T2 took over 3 s; None:
  never, a PWI that does not load it).
* the M3 takes ``m3_time`` s between ports (PWI4 ``m3.port`` -1, PWI2 0
  meanwhile); ``m3_drops`` gotos are answered but ignored, as PWI4
  sometimes does; one
  focuser (microns) and one rotator (mechanical deg) per port. PWI4
  commands act on the current port's; PWI4's ``rotator.mech_position_degs``
  always reports port 1's rotator (PWI4 4.1.4 reports Rotator index 0).

Fault injection (methods, or ``GET /robofast-sim/mode?mode=...``): ``ok``,
``hang`` (never answer), ``slow`` (answer after ``slow_delay``), ``garble``
(a non-status reply), ``http500``. ``GET /robofast-sim/restart`` is a PWI restart
(:meth:`CDK700Model.pwi_restart`, mode back to ``ok``). ``GET /robofast-sim/state`` is the
model as JSON (tests only; robofast's client never sends either).

It binds loopback only and registers itself with :mod:`robofast.hwgate`
while serving, so servers accept it without the hardware gate::

    python -m robofast.devices.pwi_sim --api pwi4 --port 0 [--ready]

prints ``pwi4 simulator on HOST:PORT`` once listening.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import logging
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from robofast.devices.tcsgo_sim import altaz_to_hadec, hadec_to_altaz, lst_hours
from robofast.hwgate import SimulatorRegistration

log = logging.getLogger(__name__)

MODES = ("ok", "hang", "slow", "garble", "http500")
#: Mt. Hopkins (public), the default site of the simulated mount.
SIM_LATITUDE = 31.680407
SIM_LONGITUDE = -110.878977


class _Axis:
    """A focuser or rotator: position moving toward a target at ``rate``/s."""

    def __init__(self, pos: float, rate: float) -> None:
        self.pos = pos
        self.target = pos
        self.rate = rate

    def tick(self, dt: float) -> None:
        d = self.target - self.pos
        step = self.rate * dt
        self.pos = self.target if abs(d) <= step else self.pos + math.copysign(step, d)

    @property
    def moving(self) -> bool:
        return abs(self.target - self.pos) > 1e-9


class CDK700Model:
    """The simulated mount, M3, focusers and rotators. Thread-safe."""

    def __init__(
        self,
        *,
        latitude: float = SIM_LATITUDE,
        longitude: float = SIM_LONGITUDE,
        slew_rate: float = 4.0,
        home_time: float = 2.0,
        m3_time: float = 1.0,
        focus_rate: float = 2000.0,
        rotator_rate: float = 20.0,
        alt: float = 30.0,
        az: float = 90.0,
        ready: bool = False,
        homed: bool = False,
        report_homed: bool = False,
        home_alt: float = 60.0,
        home_az: float = 180.0,
        version: str = "4.1.8.0",
        pwi_min_alt: float | None = None,
        wrap_min: float = 0.0,
        az_mech_min: float | None = None,
        model_file: str = "",
        stuck_moving: bool = False,
        altaz_tracks: bool = False,
        home_search: float = 0.0,
    ) -> None:
        self.lat, self.lon = latitude, longitude
        #: PWI2: ``mount.moving`` stuck True (MINERVA T1 after a PWI2
        #: restart, 2026-11-07), whatever the mount does.
        self.stuck_moving = stuck_moving
        #: PWI2: report tracking on during an alt/az move, and halt the move
        #: on tracking off (MINERVA T1, 2026-11-08: the rehearsal's end
        #: action sent tracking off mid-slew and the mount stopped short).
        self.altaz_tracks = altaz_tracks
        self.slew_rate = slew_rate
        self.home_time = home_time
        #: An unhomed altitude axis's home first searches this far down (deg).
        self.home_search = home_search
        #: A home search is under way: the axes it homes (encoders set at its end).
        self._searching: tuple[int, ...] = ()
        self.m3_time = m3_time
        self.lock = threading.RLock()
        self.connected = ready
        self.enabled = [ready, ready]
        self.axes_homed = [homed, homed]
        self.report_homed = report_homed
        self.home_pos = (home_alt, home_az)
        self.version = version
        self.pwi_min_alt = pwi_min_alt
        self.wrap_min = wrap_min
        self.az_mech_min = az_mech_min
        #: PWI4: report the mechanical azimuth and wrap range in /status.
        self.report_wrap = True
        #: Mechanical azimuth target of an alt/az goto (deg), or None.
        self.mech_target: float | None = None
        #: The next this many M3 gotos are answered and ignored.
        self.m3_drops = 0
        #: PWI2: a noisy mount (servo residuals above PWI2's "MAX RMS
        #: Encoder Error for Goto", windshake): while tracking at rest,
        #: ``mount.moving`` follows this pattern, one entry per status read,
        #: cyclically (``on_target`` the opposite). None: steady.
        self.moving_flicker: list[bool] | None = None
        self._flicker_i = 0
        #: PWI2 motor (drive) error per axis ("alt", "azm"): (code, message).
        self.motor_errors: dict[str, tuple[int, str]] = {}
        self.homing_axes: tuple[int, ...] = ()
        self.alt, self.mech = alt, az
        self.tracking = False
        #: Tracked/slew target in RA (h) / Dec (deg), or None.
        self.radec: tuple[float, float] | None = None
        #: Alt/az slew target (deg), or None.
        self.altaz: tuple[float, float] | None = None
        self.slewing = False
        self.homing_until = 0.0
        self.m3_port = 1
        self.m3_target = 1
        self.m3_until = 0.0
        self.focusers = {1: _Axis(25000.0, focus_rate), 2: _Axis(25000.0, focus_rate)}
        self.rotators = {1: _Axis(0.0, rotator_rate), 2: _Axis(0.0, rotator_rate)}
        #: The loaded pointing-model file (PWI2 ``setmodel``; PWI4 reports it).
        self.model_file = model_file
        #: setmodel takes effect this long after it is answered (None: never).
        self.model_delay: float | None = 0.0
        self._model_pending: tuple[str, float] | None = None
        self.ignored: list[str] = []
        self.commands: list[str] = []
        self._t = time.monotonic()

    # -- geometry ------------------------------------------------------------------

    @property
    def az(self) -> float:
        return self.mech % 360.0

    @az.setter
    def az(self, value: float) -> None:
        self.mech = float(value)

    def wrap_to_mech(self, az: float) -> float:
        """The mechanical angle a goto to azimuth ``az`` ends at (PWI4's wrap)."""
        return self.wrap_min + (az - self.wrap_min) % 360.0

    def nearest_mech(self, az: float) -> float:
        """The mechanical angle of ``az`` nearest the current one (tracking)."""
        return self.mech + (az - self.mech + 180.0) % 360.0 - 180.0

    def lst(self) -> float:
        return lst_hours(self.lon)

    def radec_now(self) -> tuple[float, float]:
        ha, dec = altaz_to_hadec(self.alt, self.az, self.lat)
        return (self.lst() - ha) % 24.0, dec

    def altaz_of(self, ra: float, dec: float) -> tuple[float, float]:
        return hadec_to_altaz(self.lst() - ra, dec, self.lat)

    # -- time ---------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        with self.lock:
            now = time.monotonic() if now is None else now
            dt, self._t = max(0.0, now - self._t), now
            if self._model_pending is not None and now >= self._model_pending[1]:
                self.model_file, self._model_pending = self._model_pending[0], None
            if self.homing_until and now >= self.homing_until and not self.slewing:
                self.homing_until = 0.0
                for i in self.homing_axes:
                    self.axes_homed[i] = True
                self.homing_axes = ()
            if self.m3_until and now >= self.m3_until:
                self.m3_until = 0.0
                self.m3_port = self.m3_target
            for a in (*self.focusers.values(), *self.rotators.values()):
                a.tick(dt)
            if self.slewing:
                if self.radec is not None:
                    t_alt, t_az = self.altaz_of(*self.radec)
                    t_mech = self.wrap_to_mech(t_az)
                else:
                    assert self.altaz is not None
                    t_alt, t_az = self.altaz
                    if self.mech_target is None:
                        self.mech_target = self.wrap_to_mech(t_az)
                    t_mech = self.mech_target
                stuck = self.pwi_min_alt is not None and t_alt < self.pwi_min_alt
                if stuck:
                    t_alt = self.pwi_min_alt
                if self.az_mech_min is not None and t_mech < self.az_mech_min:
                    t_mech, stuck = self.az_mech_min, True
                step = self.slew_rate * dt
                d_alt, d_az = t_alt - self.alt, t_mech - self.mech
                self.alt += max(-step, min(step, d_alt))
                self.mech += max(-step, min(step, d_az))
                if abs(d_alt) <= step and abs(d_az) <= step:
                    if stuck:
                        # At PWI's limit: still "slewing", and "tracking", forever.
                        self.tracking = True
                    elif self._searching:
                        # The home sensor found: the encoders are set, and
                        # the reported position is the home's.
                        axes, self._searching = self._searching, ()
                        self.alt = float(self.home_pos[0])
                        if 0 in axes:
                            self.mech = float(self.home_pos[1])
                        self.altaz, self.mech_target = (self.alt, self.az), self.mech
                        self.slewing = False
                    else:
                        self.slewing = False
            elif self.tracking:
                if self.radec is None:
                    self.radec = self.radec_now()
                alt, az = self.altaz_of(*self.radec)
                self.alt, self.mech = alt, self.nearest_mech(az)

    # -- commands ----------------------------------------------------------------------

    def _can_move(self, what: str) -> bool:
        if self.connected and all(self.enabled):
            return True
        self.ignored.append(what)
        return False

    def connect(self) -> None:
        self.connected = True

    def enable(self, axis: int | None = None) -> None:
        if not self.connected:
            self.ignored.append("enable")
            return
        for i in (0, 1) if axis is None else (axis,):
            self.enabled[i] = True

    def disable(self, axis: int | None = None) -> None:
        for i in (0, 1) if axis is None else (axis,):
            self.enabled[i] = False
        self.stop()

    def stop(self) -> None:
        self._searching = ()
        self.homing_until = 0.0
        self.homing_axes = ()
        self.slewing = False
        self.tracking = False
        self.radec = self.altaz = None
        self.mech_target = None

    @property
    def homed(self) -> bool:
        return all(self.axes_homed)

    def find_home(self, axis: int | None = None) -> None:
        if self._can_move("find_home"):
            self.stop()
            axes = (0, 1) if axis is None else (axis,)
            alt = self.home_pos[0] if 1 in axes else self.alt
            az = self.home_pos[1] if 0 in axes else self.az
            self.radec, self.altaz, self.slewing = None, (alt, az), True
            # The home is a mechanical position (no wrap choice).
            self.mech_target = float(self.home_pos[1]) if 0 in axes else self.mech
            if self.home_search > 0 and 1 in axes and not self.axes_homed[1]:
                # Search for the home sensor first (in the unhomed frame).
                self.altaz = (self.alt - self.home_search, self.az)
                self.mech_target = self.mech
                self._searching = axes
            self.homing_axes = axes
            self.homing_until = time.monotonic() + self.home_time
            if self.altaz_tracks:
                self.tracking = True  # PWI2 on MINERVA T1: a home leaves it tracking

    def goto_radec(self, ra: float, dec: float) -> None:
        if self._can_move("goto_radec"):
            self.radec, self.altaz = (ra % 24.0, dec), None
            self.slewing = self.tracking = True

    def goto_altaz(self, alt: float, az: float, *, keep_tracking: bool = False) -> None:
        if self._can_move("goto_altaz"):
            self.radec, self.altaz = None, (alt, az % 360.0)
            self.mech_target = None
            self.slewing = True
            if not keep_tracking:
                self.tracking = False

    def offset_radec(self, ra_arcsec: float, dec_arcsec: float) -> None:
        if self.radec is None or not self.tracking or not self._can_move("offset"):
            self.ignored.append("offset")
            return
        ra, dec = self.radec
        self.radec = ((ra + ra_arcsec / 3600.0 / 15.0) % 24.0, dec + dec_arcsec / 3600.0)
        self.slewing = True

    def set_tracking(self, on: bool) -> None:
        if on and not self._can_move("tracking_on"):
            return
        self.tracking = on
        if on:
            self.radec = self.radec_now()
            self.altaz = None
        else:
            self.radec = None
            if self.slewing and (self.altaz is None or self.altaz_tracks):
                self.slewing = False

    def power_cycle(self) -> None:
        """The mount controller loses power and comes back."""
        self.stop()
        self.connected = False
        self.enabled = [False, False]
        self.axes_homed = [False, False]
        self.alt = self.az = 0.0

    def pwi_restart(self, model_file: str = "Default_Mount_Model.PXP") -> None:
        """PWI (the program) is restarted while the controller keeps power:
        not connected, motors disabled, not homed, and the default pointing
        model loaded (as PWI2 did on MINERVA T1, 2026-11-06)."""
        self.stop()
        self.connected = False
        self.enabled = [False, False]
        self.axes_homed = [False, False]
        self.model_file = model_file

    def m3_goto(self, port: int) -> None:
        if self.m3_drops > 0:
            self.m3_drops -= 1
            self.ignored.append("m3_goto")
            return
        if port != self.m3_port or self.m3_until:
            self.m3_target = port
            self.m3_until = time.monotonic() + self.m3_time

    def m3_stop(self) -> None:
        self.m3_until = 0.0

    @property
    def current_port(self) -> int:
        return 0 if self.m3_until else self.m3_port

    @property
    def homing(self) -> bool:
        return bool(self.homing_until)

    def state(self) -> dict[str, Any]:
        with self.lock:
            self.tick()
            return {
                "connected": self.connected,
                "enabled": list(self.enabled),
                "homed": self.homed,
                "axes_homed": list(self.axes_homed),
                "homing": self.homing,
                "alt": self.alt,
                "az": self.az,
                "mech": self.mech,
                "wrap_min": self.wrap_min,
                "tracking": self.tracking,
                "slewing": self.slewing,
                "m3_port": self.current_port,
                "focusers": {p: a.pos for p, a in self.focusers.items()},
                "rotators": {p: a.pos for p, a in self.rotators.items()},
                "ignored": list(self.ignored),
                "commands": list(self.commands),
                "model_file": self.model_file,
            }


# -- PWI4 front end -----------------------------------------------------------------------


def _b4(v: bool) -> str:
    return "true" if v else "false"


def pwi4_status(m: CDK700Model) -> str:
    m.tick()
    ra, dec = m.radec_now()
    t_ra, t_dec = m.radec if m.radec is not None else (ra, dec)
    port = m.current_port
    sel = port or m.m3_port
    foc, rot = m.focusers[sel], m.rotators[sel]
    moving = m.slewing or m.homing
    lines = [
        ("pwi4.version", m.version),
        ("response.timestamp_utc", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())),
        ("site.latitude_degs", repr(m.lat)),
        ("site.longitude_degs", repr(m.lon)),
        ("site.lmst_hours", repr(m.lst())),
        ("mount.is_connected", _b4(m.connected)),
        ("mount.julian_date", repr(time.time() / 86400.0 + 2440587.5)),
        ("mount.ra_apparent_hours", repr(ra)),
        ("mount.dec_apparent_degs", repr(dec)),
        ("mount.ra_j2000_hours", repr(ra)),
        ("mount.dec_j2000_degs", repr(dec)),
        ("mount.target_ra_apparent_hours", repr(t_ra)),
        ("mount.target_dec_apparent_degs", repr(t_dec)),
        ("mount.azimuth_degs", repr(m.az)),
        ("mount.altitude_degs", repr(m.alt)),
        ("mount.is_slewing", _b4(moving)),
        ("mount.is_tracking", _b4(m.tracking)),
        ("mount.axis0.is_enabled", _b4(m.enabled[0])),
        *(
            [
                ("mount.axis0.position_degs", repr(m.mech)),
                ("mount.axis1.position_degs", repr(m.alt)),
                ("mount.axis0_wrap_range_min_degs", repr(m.wrap_min)),
            ]
            if m.report_wrap
            else []
        ),
        ("mount.axis1.is_enabled", _b4(m.enabled[1])),
        *(
            [(f"mount.axis{i}.is_homed", _b4(m.axes_homed[i])) for i in (0, 1)]
            if m.report_homed
            else []
        ),
        ("mount.axis0.setpoint_velocity_degs_per_sec", repr(m.slew_rate if moving else 0.0)),
        ("mount.axis1.setpoint_velocity_degs_per_sec", repr(m.slew_rate if moving else 0.0)),
        ("mount.model.filename", m.model_file),
        ("focuser.exists", "true"),
        ("focuser.is_connected", "true"),
        ("focuser.is_enabled", "true"),
        ("focuser.position", repr(foc.pos)),
        ("focuser.is_moving", _b4(foc.moving)),
        ("rotator.exists", "true"),
        ("rotator.is_connected", "true"),
        ("rotator.is_enabled", "true"),
        ("rotator.mech_position_degs", repr(m.rotators[1].pos)),
        ("rotator.is_moving", _b4(rot.moving)),
        ("rotator.is_slewing", _b4(rot.moving)),
        ("m3.exists", "1"),
        ("m3.port", str(port or -1)),  # PWI4 reads -1 while the M3 moves
    ]
    return "".join(f"{k}={v}\n" for k, v in lines)


def _param(q: dict[str, list[str]], name: str) -> float:
    try:
        v = float(q[name][0])
    except (KeyError, IndexError, ValueError):
        raise _HTTPError(400, f"Invalid or missing parameter {name}") from None
    if not math.isfinite(v):
        raise _HTTPError(400, f"Invalid value for {name}")
    return v


class _HTTPError(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code = code


def pwi4_handle(m: CDK700Model, path: str, q: dict[str, list[str]]) -> str:
    with m.lock:
        m.tick()
        if path != "/status":
            m.commands.append(
                path + ("?" + "&".join(f"{k}={v[0]}" for k, v in q.items()) if q else "")
            )
        if path == "/status":
            pass
        elif path == "/mount/connect":
            m.connect()
        elif path == "/mount/disconnect":
            m.stop()
            m.connected = False
        elif path in ("/mount/enable", "/mount/disable"):
            axis = q.get("axis", [""])[0]
            if axis not in ("0", "1"):
                raise _HTTPError(400, f"Invalid value for axis: {axis}. Must be 0 or 1")
            (m.enable if path.endswith("enable") else m.disable)(int(axis))
        elif path == "/mount/find_home":
            m.find_home()  # both axes; an axis parameter is not read here
        elif path == "/mount/findhome":
            if tuple(int(x) for x in m.version.split(".")[:3]) < (4, 1, 7):
                raise _HTTPError(404, "Command not found")
            axis = q.get("axis", [""])[0]
            if axis not in ("0", "1"):
                raise _HTTPError(400, f"Invalid value for axis: {axis}")
            m.find_home(int(axis))
        elif path == "/mount/stop":
            m.stop()
        elif path == "/mount/set_axis0_wrap_range_min":
            m.wrap_min = _param(q, "degs")
        elif path == "/mount/goto_ra_dec_j2000":
            m.goto_radec(_param(q, "ra_hours"), _param(q, "dec_degs"))
        elif path == "/mount/goto_alt_az":
            m.goto_altaz(_param(q, "alt_degs"), _param(q, "az_degs"))
        elif path in ("/mount/tracking_on", "/mount/tracking_off"):
            m.set_tracking(path.endswith("_on"))
        elif path == "/mount/offset":
            ra = dec = 0.0
            for key in q:
                if key == "ra_add_arcsec":
                    ra = _param(q, key)
                elif key == "dec_add_arcsec":
                    dec = _param(q, key)
                else:
                    raise _HTTPError(400, f"Unknown parameter {key}")
            m.offset_radec(ra, dec)
        elif path == "/m3/goto":
            port = q.get("port", [""])[0]
            if port not in ("1", "2"):
                raise _HTTPError(400, "Invalid value for 'port'. Must be 1 or 2")
            m.m3_goto(int(port))
        elif path == "/m3/stop":
            m.m3_stop()
        elif path == "/focuser/goto":
            m.focusers[m.current_port or m.m3_port].target = _param(q, "target")
        elif path == "/focuser/stop":
            f = m.focusers[m.current_port or m.m3_port]
            f.target = f.pos
        elif path == "/rotator/goto_mech":
            m.rotators[m.current_port or m.m3_port].target = _param(q, "degs") % 360.0
        elif path == "/rotator/stop":
            r = m.rotators[m.current_port or m.m3_port]
            r.target = r.pos
        else:
            raise _HTTPError(404, "Command not found")
        return pwi4_status(m)


# -- PWI2 front end -------------------------------------------------------------------------


def _b2(v: bool) -> str:
    return "True" if v else "False"


def _sexa(value: float, *, hours: bool, places: int) -> str:
    sign = "-" if value < 0 else "+"
    total = round(abs(value) * 3600.0, places)
    d, rem = divmod(total, 3600.0)
    mnt, s = divmod(rem, 60.0)
    text = f"{int(d):02d} {int(mnt):02d} {s:0{places + 3}.{places}f}"
    return text if hours else sign + text


def pwi2_status(m: CDK700Model) -> str:
    m.tick()
    ra, dec = m.radec_now()
    t_ra, t_dec = m.radec if m.radec is not None else (ra, dec)
    port = m.current_port
    moving = m.slewing or m.homing or not m.tracking or m.stuck_moving  # PWI2's quirks
    on_target = m.tracking and not m.slewing
    if m.moving_flicker and m.tracking and not (m.slewing or m.homing):
        # A noisy mount at rest: PWI2's residual-based flags flicker.
        moving = m.moving_flicker[m._flicker_i % len(m.moving_flicker)]
        m._flicker_i += 1
        on_target = not moving
    alt_err = m.motor_errors.get("alt", (0, "No error"))
    azm_err = m.motor_errors.get("azm", (0, "No error"))

    def el(tag: str, value: Any) -> str:
        return f"<{tag}>{escape(str(value))}</{tag}>"

    def dev(name: str, a: _Axis, unit: str) -> str:
        return (
            f"<{name}>"
            + el("connected", "True")
            + el("position", round(a.pos) if unit == "Micron" else f"{a.pos:.3f}")
            + el("unit", unit)
            + el("moving", _b2(a.moving))
            + el("goto_complete", _b2(not a.moving))
            + f"</{name}>"
        )

    sel = port or m.m3_port
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?><system>',
        "<status>"
        + el("utc", time.strftime("%H:%M:%S", time.gmtime()))
        + el("lst", _sexa(m.lst(), hours=True, places=3))
        + el("jd", f"{time.time() / 86400.0 + 2440587.5:.7f}")
        + "</status>",
        dev("focuser", m.focusers[sel], "Micron"),
        dev("focuser1", m.focusers[1], "Micron"),
        dev("focuser2", m.focusers[2], "Micron"),
        dev("rotator", m.rotators[sel], "Degree"),
        dev("rotator1", m.rotators[1], "Degree"),
        dev("rotator2", m.rotators[2], "Degree"),
        "<mount>"
        + el("connected", _b2(m.connected))
        + el("on_target", _b2(on_target))
        + el("moving", _b2(moving))
        + el("tracking", _b2(m.tracking))
        + el("ra", _sexa(ra, hours=True, places=3))
        + el("dec", _sexa(dec, hours=False, places=2))
        + el("ra_target", _sexa(t_ra, hours=True, places=3))
        + el("dec_target", _sexa(t_dec, hours=False, places=2))
        + el("ra_2000", _sexa(ra, hours=True, places=3))
        + el("dec_2000", _sexa(dec, hours=False, places=2))
        + el("ra_radian", repr(math.radians(ra * 15.0)))
        + el("dec_radian", repr(math.radians(dec)))
        + el("azm_radian", repr(math.radians(m.az)))
        + el("alt_radian", repr(math.radians(m.alt)))
        + el("azm_rms_error_arcsec", "0.1")
        + el("alt_rms_error_arcsec", "0.1")
        + el("azm_motor_error_code", str(azm_err[0]))
        + el("alt_motor_error_code", str(alt_err[0]))
        + el("azm_motor_error_message", azm_err[1])
        + el("alt_motor_error_message", alt_err[1])
        + el("azm_enabled", _b2(m.enabled[0]))
        + el("alt_enabled", _b2(m.enabled[1]))
        + el("pointing_model", m.model_file)
        + el("is_finding_home", _b2(m.homing))
        + el("encoders_have_been_set", _b2(m.homed))
        + "</mount>",
        "<m3>"
        + el("connected", "True")
        + el("port", port)
        + el("position_rotate", 0)
        + el("position_tilt", 0)
        + el("unit", "Degree")
        + el("moving_rotate", _b2(port == 0))
        + el("moving_tilt", "False")
        + "</m3>",
        "<temperature>"
        + "".join(el(t, "-99.0") for t in ("primary", "ambient", "secondary", "backplate", "m3"))
        + "</temperature>",
        "<fans>" + el("on", "False") + "</fans>",
        "</system>",
    ]
    return "".join(parts)


def pwi2_handle(m: CDK700Model, path: str, q: dict[str, list[str]]) -> str:
    if path not in ("/", ""):
        raise _HTTPError(404, "not found")
    device = q.get("device", [""])[0]
    cmd = q.get("cmd", [""])[0]
    with m.lock:
        m.tick()
        if cmd != "getsystem":
            m.commands.append(
                f"{device}:{cmd}"
                + "".join(f"&{k}={v[0]}" for k, v in q.items() if k not in ("device", "cmd"))
            )
        if cmd == "getsystem" and not device:
            pass
        elif device == "mount" and cmd == "connect":
            m.connect()
        elif device == "mount" and cmd == "disconnect":
            m.stop()
            m.connected = False
        elif device == "mount" and cmd == "enable":
            m.enable()
        elif device == "mount" and cmd == "disable":
            m.disable()
        elif device == "mount" and cmd == "findhome":
            m.find_home()
        elif device == "mount" and cmd == "setmodel":
            name = q.get("filename", [""])[0]
            if not name:
                raise _HTTPError(400, "setmodel: no filename")
            if m.model_delay is None:
                pass  # answered, never loaded
            elif m.model_delay > 0:
                m._model_pending = (name, time.monotonic() + m.model_delay)
            else:
                m.model_file = name
        elif device == "mount" and cmd == "stop":
            m.stop()
        elif device == "mount" and cmd in ("trackingon", "trackingoff"):
            m.set_tracking(cmd == "trackingon")
        elif device == "mount" and cmd == "move":
            if "ra2000" in q:
                m.goto_radec(_param(q, "ra2000"), _param(q, "dec2000"))
            elif "alt" in q:
                m.goto_altaz(_param(q, "alt"), _param(q, "azm"), keep_tracking=True)
                if m.altaz_tracks and m.altaz is not None:
                    m.tracking = True
            elif "incrementra" in q:
                m.offset_radec(_param(q, "incrementra"), _param(q, "incrementdec"))
            else:
                raise _HTTPError(400, "move: unsupported parameters")
        elif device == "m3" and cmd == "select":
            port = q.get("port", [""])[0]
            if port not in ("1", "2"):
                raise _HTTPError(400, "bad port")
            m.m3_goto(int(port))
        elif device == "m3" and cmd == "stop":
            m.m3_stop()
        elif device in ("focuser1", "focuser2", "rotator1", "rotator2") and cmd in ("move", "stop"):
            axes = m.focusers if device.startswith("focuser") else m.rotators
            a = axes[int(device[-1])]
            if cmd == "stop":
                a.target = a.pos
            else:
                v = _param(q, "position")
                a.target = v % 360.0 if device.startswith("rotator") else v
        else:
            raise _HTTPError(400, f"unknown device/cmd {device!r}/{cmd!r}")
        return pwi2_status(m)


# -- the server ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: PWISim
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)

    def _reply(self, code: int, body: str, ctype: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        sim = self.server
        url = urlparse(self.path)
        q = parse_qs(url.query, keep_blank_values=True)
        if url.path == "/robofast-sim/mode":
            sim.set_mode(q.get("mode", ["ok"])[0])
            self._reply(200, sim.mode, "text/plain")
            return
        if url.path == "/robofast-sim/restart":
            # PWI restarted (tests only): answering again, in a fresh PWI's state.
            with sim.model.lock:
                sim.model.pwi_restart(q.get("model", ["Default_Mount_Model.PXP"])[0])
            sim.set_mode("ok")
            self._reply(200, "restarted", "text/plain")
            return
        if url.path == "/robofast-sim/state":
            self._reply(200, json.dumps(sim.model.state()), "application/json")
            return
        mode = sim.mode
        if mode == "hang":
            sim.hung.wait(sim.hang_time)
            self.close_connection = True
            return
        if mode == "slow":
            time.sleep(sim.slow_delay)
        if mode == "garble":
            self._reply(200, "<html>PWI is starting...</html>", "text/plain")
            return
        if mode == "http500":
            self._reply(500, "Internal server error", "text/plain")
            return
        try:
            if sim.api == "pwi4":
                body, ctype = pwi4_handle(sim.model, url.path, q), "text/plain"
            else:
                body, ctype = pwi2_handle(sim.model, url.path, q), "text/xml"
        except _HTTPError as exc:
            self._reply(exc.code, str(exc), "text/plain")
            return
        self._reply(200, body, ctype)


class PWISim(ThreadingHTTPServer):
    """One simulated PWI (``api`` "pwi4" or "pwi2") on loopback."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        api: str = "pwi4",
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        model: CDK700Model | None = None,
        slow_delay: float = 2.0,
        hang_time: float = 600.0,
        **model_kw: Any,
    ) -> None:
        if api not in ("pwi4", "pwi2"):
            raise ValueError("api must be pwi4 or pwi2")
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the simulator binds loopback addresses only")
        super().__init__((host, port), _Handler)
        self.api = api
        self.model = model or CDK700Model(**model_kw)
        self.mode = "ok"
        self.slow_delay = slow_delay
        self.hang_time = hang_time
        self.hung = threading.Event()
        self._thread: threading.Thread | None = None
        self._registration: SimulatorRegistration | None = None

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        if mode == "hang":
            self.hung.clear()
        else:
            self.hung.set()

    def start(self) -> PWISim:
        self._registration = SimulatorRegistration(self.port)
        self._thread = threading.Thread(
            target=self.serve_forever, name=f"{self.api}-sim", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.set_mode("ok")
        self.shutdown()
        self.server_close()
        if self._registration is not None:
            self._registration.close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", choices=["pwi4", "pwi2"], default="pwi4")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--slew-rate", type=float, default=4.0, help="deg/s per axis")
    ap.add_argument("--home-time", type=float, default=2.0)
    ap.add_argument("--m3-time", type=float, default=1.0)
    ap.add_argument("--alt", type=float, default=30.0, help="starting altitude")
    ap.add_argument("--az", type=float, default=90.0, help="starting azimuth")
    ap.add_argument("--ready", action="store_true", help="start connected and enabled (ConformU)")
    ap.add_argument("--homed", action="store_true", help="start homed")
    ap.add_argument("--report-homed", action="store_true", help="PWI4: report mount.axisN.is_homed")
    ap.add_argument("--version", default="4.1.8.0", help="PWI4 version to report")
    ap.add_argument("--pwi-min-alt", type=float, help="PWI's own altitude limit (deg)")
    ap.add_argument("--stuck-moving", action="store_true", help="PWI2: mount.moving always True")
    ap.add_argument("--wrap-min", type=float, default=0.0, help="PWI4 axis-0 wrap range min")
    ap.add_argument("--model-file", default="", help="the pointing-model file loaded at start")
    ap.add_argument(
        "--restarted",
        action="store_true",
        help="start as PWI does after a restart: not connected, not homed, default model",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sim = PWISim(
        args.api,
        args.host,
        args.port,
        slew_rate=args.slew_rate,
        home_time=args.home_time,
        m3_time=args.m3_time,
        alt=args.alt,
        az=args.az,
        ready=args.ready,
        homed=args.homed,
        report_homed=args.report_homed,
        version=args.version,
        pwi_min_alt=args.pwi_min_alt,
        wrap_min=args.wrap_min,
        model_file=args.model_file,
        stuck_moving=args.stuck_moving,
    )
    if args.restarted:
        sim.model.pwi_restart(args.model_file or "Default_Mount_Model.PXP")
    sim.start()
    print(f"{args.api} simulator on {sim.address}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            sim.stop()


if __name__ == "__main__":
    main()
