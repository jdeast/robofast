"""Alpaca Telescope, Focuser and Rotator server for a PlaneWave CDK700 under PWI.

PWI is reached through :mod:`robofast.devices.pwi_client`, either PWI4
(``"backend": "pwi4"``, HTTP port 8220) or the legacy PWI2 (``"pwi2"``,
44444). The command mapping, what is confirmed and what is assumed are in
``docs/pwi.md``.

Devices
-------
* ``telescope/0`` (ITelescopeV4, alt-az). ``SlewToCoordinatesAsync`` (J2000
  RA/Dec; needs tracking on, as ASCOM requires) and ``SlewToAltAzAsync``
  (needs tracking off) are **refused** unless the target's altitude is
  within [``min_alt``, ``max_alt``] (computed here from PWI's sidereal time
  and the configured latitude, J2000 precessed to date), the mount is
  connected in PWI and both axis motors are enabled. ``AbortSlew`` stops the
  mount (PWI ``stop``, which also stops tracking). ``Park`` slews to
  (``park_alt``, ``park_az``) with tracking off; ``Unpark`` only clears the
  parked state. ``FindHome`` runs PWI's home search, **only** with
  ``"allow_find_home": true`` in the config (else NotImplemented and
  ``CanFindHome`` false); ``AtHome`` is true after it until the mount moves.
  Every slew, park, tracking-on, offset and pulse is **refused while the
  mount is not homed** (PWI2 ``encoders_have_been_set``; PWI4
  ``axisN.is_homed`` where reported, else a home robofast commanded that
  ended at ``home_alt``/``home_az``: :mod:`~robofast.devices.pwi_homed`),
  unless ``"allow_unhomed_moves": true`` **and** ``--allow-unhomed``
  (engineering; every such move is logged as a warning). ``PulseGuide`` is a PWI RA/Dec offset of
  ``guide_rate`` (RA coordinate or Dec) arcsec/s times the duration (needs tracking). No sync, no
  MoveAxis.
* ``focuser/0`` (IFocuserV4, absolute, microns): the focuser **on the current
  M3 port** (PWI2 ``focuserN``; PWI4 the GUI-selected focuser, which follows
  the M3 with "Select from M3"). ``Move`` refused outside
  [``focus_min``, ``focus_max``] or while the M3 is between ports.
  ``Halt`` stops it. Off with ``"focuser": false``.
* ``rotator/0`` (IRotatorV4): the rotator on the current M3 port, in
  mechanical degrees (``Sync`` sets an in-memory offset for ``Position``).
  PWI4 reports only Rotator index 0's angle, so with PWI4 the rotator
  reads only while the M3 is on ``rotator_status_port``. Off with
  ``"rotator": false``.

Actions (telescope)
-------------------
* ``SelectPort`` (:data:`PORT_ACTION`, the sequencer's ``port_action``):
  parameter ``"1"``/``"2"`` or a name from ``ports`` (e.g. ``"imager"``).
  Moves the M3 and waits until PWI reports the port (``m3_timeout``;
  on a timeout it sends ``m3 stop`` and fails). A goto PWI ignored (the
  port unchanged after ``m3_resend_after`` s) is sent again, at most
  ``m3_retries`` times.
* ``Offset``: ``{"east": E, "north": N}`` arcsec on the sky; RA offset
  ``E / cos(Dec)``. Needs tracking; refused beyond ``max_offset``.
* ``MountConnect``: PWI connects to the mount controller (moves nothing).
* ``MotorsEnable`` / ``MotorsDisable``: enable / disable both axes.
  **Enabling is never implicit**: nothing here enables motors, homes or
  moves on its own, and connecting (Alpaca or PWI) never does.
* ``FindHome``: ``"both"`` (default), ``"0"`` (azimuth) or ``"1"``
  (altitude; one axis needs PWI4 4.1.7+), with ``allow_find_home`` only.
* ``Stop``: mount stop, accepted even while not connected.
* ``Status``: JSON of PWI's status (typed) and the server's state.

Pointing models (PWI2)
----------------------
``pointing_models`` (``{"<m3 port>": "<file>"}``, PWI2 only; a PWI4 config
with it is refused): whenever PWI comes back (the first read, a read after
PWI did not answer, the mount connecting) or the M3 port changes, and PWI
reports another ``pointing_model`` than the current port's, the server sends
``setmodel`` for it (legacy ``cdk700.startPWI``). It moves nothing. PWI2
may take seconds to report the new model (night1108: over 3 s on T2), so
the server then re-reads it in the background every second, up to
``model_verify_timeout`` (15 s), and logs the outcome once: loaded (info)
or, only after the whole wait, an error.

Safety
------
* The hardware gate (:mod:`robofast.hwgate`): PWI must be a verified
  simulator unless the config says ``"allow_hardware": true`` **and** the
  command line passes ``--hardware``.
* A limit watch reads PWI every ``watch_interval`` s and sends ``stop`` if
  the mount is moving further outside [``min_alt`` - ``limit_margin``,
  ``max_alt`` + ``limit_margin``] (a tracked target setting, a slew the
  limits did not foresee), or is heading out fast enough that it would
  coast past them after a stop (``stop_distance``: 6-8 deg from full slew
  speed on a CDK700). It only ever stops. Speeds span two read intervals;
  with nothing of ours moving the mount, a fast read is read again before
  it counts (``track_speed_max``); a slew of ours moving toward a target
  inside the limits is predicted to stop within ``goto_margin`` of it.
* A slew this server sent that PWI reports slewing but that does not move
  (``stall_time``, ``stall_move``) has failed -- PWI4 accepts a goto beyond
  its own axis limits, stops at the limit and reports slewing forever: the
  server stops the mount and ``Slewing`` raises the reason until the next
  slew or ``AbortSlew``. So does an axis PWI disables during a slew (a
  drive fault), and a park not done within ``park_timeout``. A mount on its
  target (``slew_tolerance``) and still has finished its slew whatever PWI
  says (PWI2's ``moving`` can stick True). PWI2 judges ``moving`` by the
  mount's servo residuals, so a noisy mount (windshake) can flicker it
  (legacy ``cdk700.inPosition``: "Telescope moving after it said it was
  done"): a PWI2 slew is done only on its target and still **and** with
  ``moving`` False (or ``on_target`` True) on reads ``slew_settle`` s apart;
  a mount on its target whose flag keeps flickering is done after
  ``slew_noisy_time`` s, never declared stalled. A PWI2 that says the slew is
  done while the mount is still and off its target for ``slew_arrive_timeout``
  s has failed, and so has a slew during which PWI reports a motor (drive)
  error code: the mount is stopped and ``Slewing`` raises the reason.
* With ``az_wrap_min``/``az_wrap_max`` (mechanical azimuth, PWI4), every
  goto picks the mechanical angle of its azimuth inside that range nearest
  the current one and sets PWI4's axis-0 wrap range so PWI goes there; it
  is refused when PWI4 cannot report or take the range. The limit watch
  covers the mechanical azimuth range too.
* Every request to PWI has a timeout; every Alpaca request is bounded by
  ``request_timeout``.

Running::

    python -m robofast.devices.pwi_server --config t1_pwi.json [--hardware]

prints one JSON line, ``{"url": ..., "pwi": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import math
import ntpath
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from robofast import hwgate
from robofast.config import load_device_config, require_keys
from robofast.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
)
from robofast.devices.pwi_client import (
    BACKENDS,
    PER_AXIS_HOME_VERSION,
    PWI2_PORT,
    PWI4_PORT,
    PWIClient,
    PWIError,
    PWIStatus,
    PWITimeout,
    make_client,
    version_at_least,
)
from robofast.devices.pwi_homed import HomeRecord, not_at_home
from robofast.devices.pwi_homed import default_path as default_home_path
from robofast.devices.tcsgo_server import (
    COMMON_NAMES,
    _float_param,
    _Handler,
    _int_param,
    _now_stamp,
)
from robofast.devices.tcsgo_sim import hadec_to_altaz

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
PARKED = 0x408
PWI_TIMEOUT = 0x502
#: The Alpaca Action that selects an M3 port (the sequencer's ``port_action``).
PORT_ACTION = "SelectPort"
TELESCOPE_ACTIONS = (
    PORT_ACTION,
    "Offset",
    "MountConnect",
    "MotorsEnable",
    "MotorsDisable",
    "FindHome",
    "Stop",
    "Status",
)


# -- configuration ----------------------------------------------------------------


@dataclass
class PWIEndpoint:
    host: str = "127.0.0.1"
    #: Default: 8220 (PWI4) or 44444 (PWI2).
    port: int | None = None
    #: Per request (connect + reply), s.
    timeout: float = 5.0
    connect_timeout: float = 2.0


#: Site and telescope values a PWI config must give (no defaults;
#: docs/design.md 3.10). ``focus_min``/``focus_max`` too while ``focuser``.
REQUIRED = (
    "name",
    "backend",
    "latitude",
    "longitude",
    "elevation",
    "min_alt",
    "max_alt",
    "park_alt",
    "park_az",
)


@dataclass
class PWIConfig:
    name: str
    #: "pwi4" or "pwi2".
    backend: str
    latitude: float
    longitude: float
    elevation: float
    #: Slew targets outside [min_alt, max_alt] (deg) are refused here.
    min_alt: float
    max_alt: float
    #: Park position (alt, az deg); must be inside the limits.
    park_alt: float
    park_az: float
    pwi: PWIEndpoint = field(default_factory=PWIEndpoint)
    #: Must be true (and ``--hardware`` given) for anything but a simulator.
    allow_hardware: bool = False
    listen: str = "127.0.0.1:11120"
    focuser: bool = True
    #: The focuser's travel, microns (required while ``focuser``).
    focus_min: float = 0.0
    focus_max: float = 0.0
    rotator: bool = True
    #: PWI4 reports only Rotator index 0's angle: the port it is on.
    rotator_status_port: int = 1
    #: Port names for SelectPort (``{"imager": 1, "fau": 2}``); "1"/"2" always work.
    ports: dict[str, int] = field(default_factory=dict)
    #: SelectPort: give up (``m3 stop``) after this long in all (s). A
    #: CDK700's M3 took 6-41 s from port 2 to 1 under PWI4 4.1.6. Keep it
    #: below the sequencer's ``port_timeout`` (60 s).
    m3_timeout: float = 50.0
    #: PWI4 can accept an M3 goto and never move: if the reported port has
    #: not changed this long (s) after a goto, send it again, at most
    #: ``m3_retries`` times.
    m3_resend_after: float = 10.0
    m3_retries: int = 2
    #: Offset action bound, arcsec per axis.
    max_offset: float = 600.0
    #: PulseGuide: arcsec/s applied as a PWI offset, and the longest pulse.
    guide_rate: float = 7.5
    max_pulse_ms: int = 10000
    #: The limit watch: margin (deg) and interval (s); interval 0 turns it off.
    limit_margin: float = 1.0
    watch_interval: float = 0.5
    #: How far the mount coasts after a stop from ``slew_speed`` (deg/s): a
    #: CDK700 under PWI4 (15 deg/s, 15 deg/s^2) coasted 6-8 deg. The limit
    #: watch scales it with the square of the speed (docs/pwi.md, "Safety").
    stop_distance: float = 8.0
    slew_speed: float = 15.0
    #: A read implying more than this (deg/s; default twice ``slew_speed``)
    #: is a bad sample, not motion: the limit watch skips it (MINERVA T1,
    #: 2026-11-05: PWI2 reported 61.4 deg altitude mid-slew from near 40, a
    #: "32 deg/s" climb, and the watch stopped a good slew 7 arcmin short).
    glitch_speed: float | None = None
    #: With no slew in progress (tracking, or still) a read implying more
    #: than this (deg/s, either axis) is suspect: the watch reads PWI again
    #: at once and acts only if that read confirms the motion (MINERVA T1,
    #: 2026-11-07: PWI2 read 77.9 deg altitude for one sample while the
    #: mount tracked at 63.7, "22 deg/s", and the watch stopped it).
    track_speed_max: float = 1.0
    #: A slew of ours to a target inside the limits, moving toward it, is
    #: predicted to stop no further than this past the target (deg): the
    #: watch is for runaways, not for a goto decelerating onto its target.
    goto_margin: float = 1.0
    #: A slew of ours is complete once the mount is this close to its target
    #: (deg; RA/Dec, or alt/az and mechanical azimuth) and not moving, what
    #: ever PWI's own flag says (PWI2's ``moving`` can stick True).
    slew_tolerance: float = 0.1
    #: A slew robofast sent that PWI reports slewing but that has moved less
    #: than ``stall_move`` deg in ``stall_time`` s has failed (PWI4 stops at
    #: its own axis limits and reports slewing forever): stop, report.
    stall_time: float = 3.0
    stall_move: float = 0.001
    #: PWI2 cannot say whether an untracked mount moves: faster than this
    #: (deg/s, between two reads) counts as slewing.
    moving_speed: float = 0.02
    #: Slewing is reported true this long after a motion command (s).
    slew_report_min: float = 1.0
    #: PWI2: a slew on its target is done once ``moving`` reads False (or
    #: ``on_target`` True) on reads this far apart (s) with no ``moving``
    #: True between (legacy cdk700.inPosition re-read after 1 s).
    slew_settle: float = 1.0
    #: PWI2: on its target and still, but ``moving`` keeps flickering
    #: (servo residuals above PWI2's "MAX RMS Encoder Error for Goto"):
    #: done after this long (s), with a warning (docs/pwi.md).
    slew_noisy_time: float = 10.0
    #: PWI2: says the slew is done (or cannot say: untracked) while the mount
    #: is still and off its target: failed after this long (s) from the goto
    #: (PWI2 may start moving seconds after a goto).
    slew_arrive_timeout: float = 30.0
    park_tolerance: float = 0.5
    #: Park fails (mount stopped, Slewing raises) if not parked after this (s).
    park_timeout: float = 180.0
    #: The mechanical azimuth range (deg, PWI4 ``mount.axis0.position_degs``)
    #: a goto may end in. Set, every goto (slew, park) picks the mechanical
    #: angle of its azimuth inside it nearest the current one, sets PWI4's
    #: wrap range so PWI goes there (``/mount/set_axis0_wrap_range_min``),
    #: and is refused if PWI4 cannot say or take it. The limit watch stops
    #: a mount moving out of it. None: PWI chooses (PWI4 4.1.6's default
    #: wrap range is [0, 360), which took a CDK700 the long way to az 0).
    az_wrap_min: float | None = None
    az_wrap_max: float | None = None
    #: Reads reuse a PWI reply this recent (s).
    status_max_age: float = 0.3
    #: Bound on each Alpaca request (s).
    request_timeout: float = 10.0
    aperture_diameter: float | None = None
    focal_length: float | None = None
    #: FindHome (Alpaca) and the FindHome Action are refused unless true:
    #: homing drives the axes to their home sensors, which on a mount whose
    #: home sensor or its direction setting changed can mean the wrong way.
    allow_find_home: bool = False
    #: Where PWI4 reports the mount (deg) at the end of a home. A home robofast
    #: commanded that ends within ``home_tolerance`` of it is recorded in
    #: ``home_record`` (default ``$ROBOFAST_DATA_DIR/pwi-homed/<name>.json``),
    #: which is how robofast knows a PWI4 without a homed flag is homed
    #: (:mod:`robofast.devices.pwi_homed`).
    home_alt: float | None = None
    home_az: float | None = None
    home_tolerance: float = 1.0
    home_record: str | None = None
    #: Moves while PWI does not report the mount homed are refused unless
    #: this is true AND the command line says --allow-unhomed (engineering).
    allow_unhomed_moves: bool = False
    #: robofast-pwi-jog home: give up (stop) after this long (s).
    home_timeout: float = 300.0
    #: PWI2 only: the pointing-model file for each M3 port (``{"1": "a.PXP",
    #: "2": "b.PXP"}``; a ``ports`` name works too). Whenever PWI comes back
    #: (the server's first read, a read after PWI did not answer, the mount
    #: connecting) or the M3 port changes, and PWI reports another model,
    #: the server sends ``setmodel`` for the current port (legacy
    #: ``cdk700.startPWI``: a restarted PWI2 loads ``Default_Mount_Model.PXP``).
    pointing_models: dict[str, str] = field(default_factory=dict)
    #: After a ``setmodel``: how long PWI may take to report the new model
    #: (s), re-read every second in the background, before it is an error.
    model_verify_timeout: float = 15.0
    #: robofast-pwi-jog: largest relative move per axis (deg) and its timeout.
    max_jog_deg: float = 1.0
    jog_timeout: float = 60.0
    #: UDP port for Alpaca discovery (32227 is standard); None: off.
    discovery_port: int | None = None

    @property
    def pwi_port(self) -> int:
        if self.pwi.port is not None:
            return int(self.pwi.port)
        return PWI4_PORT if self.backend == "pwi4" else PWI2_PORT

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PWIConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        require_keys(d, REQUIRED)
        if d.get("focuser", True):
            require_keys(d, ("focus_min", "focus_max"), "config (focuser: true)")
        pwi = {k: v for k, v in d.pop("pwi", {}).items() if not k.startswith("_")}
        pnames = {f.name for f in dataclasses.fields(PWIEndpoint)}
        if set(pwi) - pnames:
            raise ValueError(f"unknown pwi keys: {sorted(set(pwi) - pnames)}")
        cfg = cls(pwi=PWIEndpoint(**pwi), **d)
        if cfg.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}")
        if not -5.0 <= cfg.min_alt < cfg.max_alt <= 90.0:
            raise ValueError("need -5 <= min_alt < max_alt <= 90")
        if not cfg.min_alt <= cfg.park_alt <= cfg.max_alt:
            raise ValueError("park_alt must be within [min_alt, max_alt]")
        if cfg.focuser and cfg.focus_max <= cfg.focus_min:
            raise ValueError("focus_max must exceed focus_min")
        if cfg.focuser and cfg.focus_min < 0:
            raise ValueError("focus_min must be >= 0 (Alpaca positions are not negative)")
        for name, port in cfg.ports.items():
            if port not in (1, 2):
                raise ValueError(f"ports.{name}: {port} is not 1 or 2")
        if not 0 < cfg.max_jog_deg <= 10.0:
            raise ValueError("max_jog_deg must be in (0, 10]")
        if cfg.stop_distance < 0 or cfg.slew_speed <= 0:
            raise ValueError("need stop_distance >= 0 and slew_speed > 0")
        if cfg.glitch_speed is not None and cfg.glitch_speed <= cfg.slew_speed:
            raise ValueError("glitch_speed must be above slew_speed")
        if cfg.stall_time <= 0 or cfg.stall_move <= 0:
            raise ValueError("need stall_time > 0 and stall_move > 0")
        if cfg.slew_settle < 0 or cfg.slew_noisy_time <= 0 or cfg.slew_arrive_timeout <= 0:
            raise ValueError("need slew_settle >= 0, slew_noisy_time > 0, slew_arrive_timeout > 0")
        if cfg.track_speed_max <= 0 or cfg.goto_margin < 0 or cfg.slew_tolerance <= 0:
            raise ValueError("need track_speed_max > 0, goto_margin >= 0, slew_tolerance > 0")
        if cfg.m3_retries < 0 or cfg.m3_resend_after <= 0:
            raise ValueError("need m3_retries >= 0 and m3_resend_after > 0")
        if cfg.home_tolerance <= 0:
            raise ValueError("home_tolerance must be > 0")
        if (cfg.az_wrap_min is None) != (cfg.az_wrap_max is None):
            raise ValueError("az_wrap_min and az_wrap_max go together")
        if cfg.az_wrap_min is not None:
            assert cfg.az_wrap_max is not None
            if cfg.backend != "pwi4":
                raise ValueError("az_wrap_min/az_wrap_max need the pwi4 backend")
            if cfg.az_wrap_max - cfg.az_wrap_min < 360.0:
                raise ValueError("az_wrap_max - az_wrap_min must be at least 360")
        if cfg.park_timeout <= 0:
            raise ValueError("park_timeout must be > 0")
        if cfg.model_verify_timeout <= 0:
            raise ValueError("model_verify_timeout must be > 0")
        if cfg.pointing_models:
            if cfg.backend != "pwi2":
                raise ValueError(
                    "pointing_models is PWI2 only: robofast sends PWI4 no model request (PWI4"
                    " keeps its own model; docs/pwi.md)"
                )
            cfg.model_files()  # validates
        return cfg

    def model_files(self) -> dict[int, str]:
        """``pointing_models`` by M3 port number (1, 2)."""
        out: dict[int, str] = {}
        for key, name in self.pointing_models.items():
            port = self.ports.get(key) or (int(key) if str(key) in ("1", "2") else None)
            if port is None:
                raise ValueError(f"pointing_models: {key!r} is not port 1, 2 or a ports name")
            if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n"):
                raise ValueError(f"pointing_models.{key}: a file name, not {name!r}")
            if port in out and out[port] != name:
                raise ValueError(f"pointing_models: two models for port {port}")
            out[port] = name
        return out

    def home_record_path(self) -> Path:
        return Path(self.home_record) if self.home_record else default_home_path(self.name)


def same_model(a: str | None, b: str | None) -> bool:
    """Whether two pointing-model file names name the same file (PWI may
    report a full Windows path; names compare without case, as on Windows)."""
    if not a or not b:
        return False
    return ntpath.basename(a.strip()).lower() == ntpath.basename(b.strip()).lower()


def coast_deg(config: PWIConfig, speed: float) -> float:
    """How far (deg) a mount moving at ``speed`` (deg/s) goes before a stop
    sent now takes hold: ``stop_distance`` scaled as the square of the
    speed (constant deceleration), plus one ``watch_interval`` of travel.
    The mount cannot go faster than ``slew_speed``: a measured speed above
    it (and below ``glitch_speed``, which is a bad read) is poll-timing
    jitter, and counts as ``slew_speed``."""
    s = min(abs(speed), config.slew_speed)
    return config.stop_distance * (s / config.slew_speed) ** 2 + s * config.watch_interval


def _sep_deg(alt1: float, az1: float, alt2: float, az2: float) -> float:
    a1, a2 = math.radians(alt1), math.radians(alt2)
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(
        math.radians(az1 - az2)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def j2000_to_date(ra_h: float, dec: float) -> tuple[float, float]:
    """Mean J2000 RA/Dec precessed to now (FK5), for altitude checks."""
    from astropy import units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    c = SkyCoord(ra_h * 15 * u.deg, dec * u.deg, frame=FK5(equinox="J2000"))
    d = c.transform_to(FK5(equinox=Time.now()))
    return d.ra.deg / 15.0, d.dec.deg


def target_altaz(ra_h: float, dec: float, lst_h: float, lat: float) -> tuple[float, float]:
    """Alt/az (deg) of a J2000 RA/Dec at local sidereal time ``lst_h``."""
    ra_d, dec_d = j2000_to_date(ra_h, dec)
    return hadec_to_altaz(lst_h - ra_d, dec_d, lat)


# -- the mount logic -------------------------------------------------------------------


class PWIMount:
    """Telescope, focuser and rotator logic over a :class:`PWIClient` (one loop)."""

    def __init__(self, client: PWIClient, config: PWIConfig) -> None:
        self.pwi = client
        self.config = config
        self._st: PWIStatus | None = None
        self._st_at = -1e9
        self._prev: tuple[float, float, float] | None = None  # (t, alt, az)
        self.speed = 0.0  # deg/s between the last two fresh reads
        self._lock = asyncio.Lock()
        self._moving_since = -1e9
        self._homing_since: float | None = None
        self._homing_axes: tuple[int, ...] = ()
        self._home_at: tuple[float, float] | None = None
        #: Axes this process saw homed (for a PWI that reports no homed flag).
        self.homed_here: set[int] = set()
        #: The engineering override (config AND --allow-unhomed).
        self.unhomed_ok = False
        self.version: str | None = None
        #: "unknown", "parking", "parked", "unparked".
        self.park_state = "unknown"
        self.target_ra: float | None = None
        self.target_dec: float | None = None
        #: Rotator Sync offset and Reverse (in memory, Position only).
        self.rotator_offset = 0.0
        self.rotator_reverse = False
        self.rotator_target: float | None = None
        self.focus_target: float | None = None
        self.watch_stops = 0
        self.watch_glitches = 0
        self.pulse_until = 0.0
        self._watch_latched = False
        self._park_task: asyncio.Task | None = None
        #: The record of homes robofast commanded (a PWI4 without a homed flag).
        self.home_record = HomeRecord(config.home_record_path())
        #: A slew this server sent is under way: its target altitude (deg).
        self._slew_active = False
        self._goto_alt: float | None = None
        #: Its alt/az target (None for RA/Dec) and mechanical azimuth target.
        self._goto_altaz: tuple[float, float] | None = None
        self._goto_mech: float | None = None
        self._stall_ref: tuple[float, float, float] | None = None  # (t, alt, az)
        #: An RA/Dec slew's target, apparent (precessed once, at the slew).
        self._goto_app: tuple[float, float] | None = None
        #: The last slew that ended (not failed): (when, alt, mech az target).
        self._done_goto: tuple[float, float | None, float | None] | None = None
        #: A stalled slew is being stopped (Slewing stays true meanwhile).
        self._failing = False
        #: PWI2 settle (:meth:`_pwi2_settled`): since when the slew of ours
        #: has been on its target and still, and reading done ("quiet").
        self._arrived_since: float | None = None
        self._quiet_since: float | None = None
        #: PWI2 read ``moving`` True again on the target after it read done.
        self.moving_flickers = 0
        #: Tracking off was asked during an alt/az slew of ours: sent once
        #: the mount is on the target (on PWI2 it halts a slew midway).
        self._untrack_on_arrival = False
        #: Why the last slew failed (Slewing raises it until the next command).
        self.slew_error: str | None = None
        self.m3_resends = 0
        #: ``pointing_models`` (PWI2): a model check is due (PWI came back,
        #: the mount connected or the port changed); what was seen last.
        self.models = config.model_files()
        self._model_due = True
        self._pwi_up = False
        self._was_connected: bool | None = None
        self._was_port: int | None = None
        #: Every setmodel sent: (port, file, error or None), once its outcome
        #: is known (after the verify wait, if PWI was slow to report it).
        self.model_loads: list[tuple[int, str, str | None]] = []
        self._model_verify: asyncio.Task | None = None

    # -- reads ----------------------------------------------------------------------

    async def status(self, max_age: float | None = None) -> PWIStatus:
        max_age = self.config.status_max_age if max_age is None else max_age
        async with self._lock:
            now = time.monotonic()
            if self._st is None or now - self._st_at > max_age:
                try:
                    st = await self.pwi.status()
                except PWIError:
                    self._pwi_up = False
                    raise
                # The read's time: halfway between asking and the answer
                # (through an SSH tunnel the answer can take 0.3-0.7 s, and
                # its arrival time alone made speeds jitter by ~40%).
                self._take(st, 0.5 * (now + time.monotonic()))
            assert self._st is not None
            return self._st

    def _take(self, st: PWIStatus, t: float) -> None:
        if self._prev is not None and t - self._prev[0] > 0.05:
            dt = t - self._prev[0]
            self.speed = _sep_deg(st.alt_deg, st.az_deg, self._prev[1], self._prev[2]) / dt
            self._prev = (t, st.alt_deg, st.az_deg)
        elif self._prev is None:
            self._prev = (t, st.alt_deg, st.az_deg)
        self._st, self._st_at = st, t
        if self.models:
            port = st.m3_port if st.m3_port in (1, 2) else None
            if (
                not self._pwi_up
                or (st.connected and self._was_connected is False)
                or (port is not None and port != self._was_port)
            ):
                self._model_due = True
            self._was_connected = st.connected
            if port is not None:
                self._was_port = port
        self._pwi_up = True
        if st.version != self.version:
            if self.version is not None and self.homed_here:
                self.homed_here.clear()
                log.warning("PWI version changed: homes seen earlier no longer count")
            self.version = st.version
            log.warning("PWI (%s) version %s", st.backend, st.version or "not reported")
        if st.homed is None:
            why = self.home_record.observe(st)
            if why is not None and self.homed_here:
                self.homed_here.clear()
                log.warning("homes seen earlier no longer count: %s", why)

    def _invalidate(self) -> None:
        self._st_at = -1e9

    async def _send(self, fn: Callable[[], Awaitable[PWIStatus]], what: str) -> PWIStatus:
        log.info("PWI %s", what)
        try:
            st = await fn()
        except PWITimeout:
            self._pwi_up = False
            raise
        finally:
            self._invalidate()
        return st

    async def check_model(self) -> None:
        """``pointing_models``: if a check is due (:meth:`_take`), the mount
        is connected and on a port with a model, and PWI reports another
        model, send ``setmodel`` for that port (once per check: a failure is
        logged, and the next reconnect or port change tries again)."""
        if not self.models or not self._model_due:
            return
        st = await self.status()
        if not st.connected or st.m3_port not in (1, 2):
            return  # still due: once the mount is connected and on a port
        self._model_due = False
        want = self.models.get(st.m3_port)
        if want is None or same_model(st.pointing_model, want):
            return
        log.warning(
            "PWI reports pointing model %r, not %r for port %d: setmodel",
            st.pointing_model,
            want,
            st.m3_port,
        )
        try:
            after = await self._send(lambda: self.pwi.set_model(want), f"setmodel {want}")
        except PWIError as exc:
            log.error("setmodel %s failed: %s", want, exc)
            self.model_loads.append((st.m3_port, want, str(exc)))
            return
        if after.pointing_model is None or same_model(after.pointing_model, want):
            self.model_loads.append((st.m3_port, want, None))
            return
        # PWI2 can be slow to report the new model: re-read it in the
        # background (this runs in the limit watch and in Alpaca requests).
        log.info(
            "setmodel %s: PWI reports %r so far; checking for up to %g s",
            want,
            after.pointing_model,
            self.config.model_verify_timeout,
        )
        if self._model_verify is not None and not self._model_verify.done():
            self._model_verify.cancel()
        self._model_verify = asyncio.get_running_loop().create_task(
            self._verify_model(st.m3_port, want)
        )

    async def _verify_model(self, port: int, want: str) -> None:
        """Re-read PWI every second until it reports ``want`` or
        ``model_verify_timeout`` has passed; record and log the outcome."""
        t0 = time.monotonic()
        timeout = self.config.model_verify_timeout
        seen: str | None = None
        while True:
            await asyncio.sleep(min(1.0, max(0.05, timeout / 10)))
            waited = time.monotonic() - t0
            try:
                st = await self.status(0)
            except PWIError as exc:
                seen = f"no answer ({exc})"
            else:
                seen = repr(st.pointing_model)
                if st.pointing_model is None or same_model(st.pointing_model, want):
                    log.info("setmodel %s: PWI reports it after %.1f s", want, waited)
                    self.model_loads.append((port, want, None))
                    return
            if waited >= timeout:
                err = f"PWI still reports {seen} after {waited:.1f} s"
                log.error("setmodel %s: %s", want, err)
                self.model_loads.append((port, want, err))
                return

    def _arm_slew(
        self, target_alt: float, altaz: tuple[float, float] | None, mech: float | None
    ) -> None:
        """A slew this server sends: watch it for a stall (:meth:`_check_stall`)."""
        self._slew_active = True
        self._goto_alt = target_alt
        self._goto_altaz = altaz
        self._goto_mech = mech
        self._goto_app = None
        self._done_goto = None
        self._failing = False
        self._untrack_on_arrival = False
        self._stall_ref = None
        self._arrived_since = None
        self._quiet_since = None
        self.slew_error = None

    def _disarm_slew(self, completed: bool = False) -> None:
        if completed and self._slew_active:
            # The limit watch's speeds still hold this slew's last reads for
            # a moment: they stay bounded by its target (watch()).
            self._done_goto = (time.monotonic(), self._goto_alt, self._goto_mech)
        self._slew_active = False
        self._goto_alt = None
        self._goto_altaz = None
        self._goto_mech = None
        self._goto_app = None
        self._stall_ref = None
        self._arrived_since = None
        self._quiet_since = None

    def _fail_slew(self, why: str) -> None:
        self._disarm_slew()
        self._failing = False
        if self.park_state == "parking":
            self.park_state = "unknown"
        self.slew_error = why
        log.error("%s", why)

    async def _place_azimuth(self, st: PWIStatus, az: float, what: str) -> float | None:
        """With ``az_wrap_min``/``az_wrap_max``: the mechanical angle a goto
        to azimuth ``az`` must end at (inside that range, nearest the current
        one), with PWI4's wrap range set so that PWI goes there. None when no
        range is configured. Refuses (nothing moves) when PWI4 cannot say its
        mechanical azimuth or wrap range, or does not take the new range."""
        cfg = self.config
        if cfg.az_wrap_min is None or cfg.az_wrap_max is None:
            return None
        if st.az_mech_deg is None or st.az_wrap_min is None:
            raise AlpacaException(
                INVALID_OPERATION,
                f"{what}: PWI reports no mechanical azimuth or wrap range"
                " (mount.axis0.position_degs, mount.axis0_wrap_range_min_degs), so it cannot be"
                " told which way to go round; refusing (az_wrap_min/az_wrap_max are configured)",
            )
        m0 = st.az_mech_deg
        k0 = math.floor((cfg.az_wrap_min - az) / 360.0)
        cands = [
            az + 360.0 * k
            for k in range(k0, k0 + 4 + int((cfg.az_wrap_max - cfg.az_wrap_min) // 360))
            if cfg.az_wrap_min <= az + 360.0 * k <= cfg.az_wrap_max
        ]
        if not cands:
            raise AlpacaException(
                INVALID_OPERATION,
                f"{what}: azimuth {az:.3f} has no mechanical angle in"
                f" [{cfg.az_wrap_min}, {cfg.az_wrap_max}]",
            )
        mech = min(cands, key=lambda c: abs(c - m0))
        if not st.az_wrap_min <= mech < st.az_wrap_min + 360.0 - 1e-6:
            wrap = mech - 180.0
            await self._send(
                lambda: self.pwi.set_az_wrap_min(wrap), f"axis-0 wrap range min {wrap:.3f}"
            )
            now = await self.status(0)
            if now.az_wrap_min is None or abs(now.az_wrap_min - wrap) > 0.01:
                raise AlpacaException(
                    DRIVER_ERROR,
                    f"{what}: PWI did not take axis-0 wrap range min {wrap:.3f} (reads"
                    f" {now.az_wrap_min}); refusing the goto",
                )
        log.info("%s: azimuth %.3f -> mechanical %.3f (from %.3f)", what, az, mech, m0)
        return mech

    @staticmethod
    def _axis_errors(st: PWIStatus) -> str:
        """PWI's error fields for the axes that say something, as text."""
        quiet = ("", "0", "false", "no error", "none")
        errs = {
            k: v
            for k, v in st.raw.items()
            if k.startswith("mount.axis")
            and "error" in k
            and "arcsec" not in k
            and v.strip().lower() not in quiet
        }
        return "; ".join(f"{k}={v}" for k, v in sorted(errs.items()))

    def _at_target(self, st: PWIStatus) -> bool:
        """The mount is within ``slew_tolerance`` of the target of the slew
        in progress (its alt/az and mechanical azimuth, or its RA/Dec)."""
        tol = self.config.slew_tolerance
        if self._goto_altaz is not None:
            alt, az = self._goto_altaz
            if _sep_deg(st.alt_deg, st.az_deg, alt, az) > tol:
                return False
            mech = self._goto_mech
            return mech is None or st.az_mech_deg is None or abs(st.az_mech_deg - mech) <= tol
        if self.target_ra is None or self.target_dec is None:
            return False
        if st.ra2000_hours is not None and st.dec2000_deg is not None:
            here, there = (st.ra2000_hours, st.dec2000_deg), (self.target_ra, self.target_dec)
        elif self._goto_app is not None:
            here, there = (st.ra_hours, st.dec_deg), self._goto_app
        else:
            return False
        return _sep_deg(here[1], here[0] * 15.0, there[1], there[0] * 15.0) <= tol

    def _target_known(self, st: PWIStatus) -> bool:
        """:meth:`_at_target` can say (the slew in progress has a target it
        can compare the mount's position with)."""
        if self._goto_altaz is not None:
            return True
        if self.target_ra is None or self.target_dec is None:
            return False
        return (
            st.ra2000_hours is not None and st.dec2000_deg is not None
        ) or self._goto_app is not None

    def _target_sep(self, st: PWIStatus) -> float | None:
        """How far (deg) the mount is from the slew's target, if known."""
        if self._goto_altaz is not None:
            return _sep_deg(st.alt_deg, st.az_deg, *self._goto_altaz)
        if self.target_ra is None or self.target_dec is None:
            return None
        if st.ra2000_hours is not None and st.dec2000_deg is not None:
            here, there = (st.ra2000_hours, st.dec2000_deg), (self.target_ra, self.target_dec)
        elif self._goto_app is not None:
            here, there = (st.ra_hours, st.dec_deg), self._goto_app
        else:
            return None
        return _sep_deg(here[1], here[0] * 15.0, there[1], there[0] * 15.0)

    def _pwi2_settled(self, st: PWIStatus, now: float) -> bool:
        """A PWI2 slew of ours on its target and still: done once PWI2 reads
        done (``moving`` not True, or ``on_target``) on reads ``slew_settle``
        s apart with no ``moving`` True between; or, if its ``moving`` keeps
        flickering (servo residuals: a noisy mount, windshake), once it has
        been on the target and still for ``slew_noisy_time`` s."""
        cfg = self.config
        if self._arrived_since is None:
            self._arrived_since = now
        quiet = st.slewing is not True or _truthy(st.raw.get("mount.on_target"))
        if quiet:
            if self._quiet_since is None:
                self._quiet_since = now
            return now - self._quiet_since >= cfg.slew_settle
        if self._quiet_since is not None:
            self.moving_flickers += 1
            log.warning(
                "PWI2 reports the mount moving again %.1f s after it read done, on the target"
                " (rms alt %s, az %s arcsec): servo residuals above PWI2's 'MAX RMS Encoder Error"
                " for Goto' (windshake or a noisy mount; docs/pwi.md); waiting for it to settle",
                now - self._quiet_since,
                st.raw.get("mount.alt_rms_error_arcsec"),
                st.raw.get("mount.azm_rms_error_arcsec"),
            )
            self._quiet_since = None
        if now - self._arrived_since >= cfg.slew_noisy_time:
            log.warning(
                "slew complete: on the target and still for %.1f s although PWI2 keeps reporting"
                " moving (rms alt %s, az %s arcsec; tune PWI2's 'MAX RMS Encoder Error for Goto',"
                " docs/pwi.md)",
                now - self._arrived_since,
                st.raw.get("mount.alt_rms_error_arcsec"),
                st.raw.get("mount.azm_rms_error_arcsec"),
            )
            return True
        return False

    def _arrived(self, st: PWIStatus) -> bool:
        """A slew of ours has ended on its target: there, and not moving."""
        return self._at_target(st) and self.speed < self.config.moving_speed

    async def _slew_done(self, st: PWIStatus) -> None:
        """A slew of ours has ended: disarm it, and send a tracking off
        asked for while it moved (:meth:`set_tracking`)."""
        untrack, self._untrack_on_arrival = self._untrack_on_arrival, False
        self._disarm_slew(completed=True)
        if untrack and st.tracking:
            with contextlib.suppress(PWIError):
                await self._send(lambda: self.pwi.tracking(False), "tracking off (slew done)")

    async def _check_stall(self, st: PWIStatus) -> None:
        """PWI4 accepts a goto beyond its own axis limits, stops at the limit
        and reports slewing (and tracking) forever. A slew of ours that PWI
        reports slewing but that has not moved ``stall_move`` deg in
        ``stall_time`` s has failed: stop the mount and keep the reason in
        :attr:`slew_error` (raised by Slewing)."""
        if not self._slew_active:
            return
        cfg = self.config
        now = time.monotonic()
        if not st.motors_enabled:
            off = [
                n for n, on in (("azimuth", st.az_enabled), ("altitude", st.alt_enabled)) if not on
            ]
            errs = self._axis_errors(st)
            self._fail_slew(
                f"slew failed: PWI disabled the {' and '.join(off)} axis during the slew (a drive"
                f" fault, e.g. motor current overload: see PWI's log){'; ' + errs if errs else ''}"
            )
            with contextlib.suppress(PWIError):
                await self._send(self.pwi.stop, "stop (axis disabled)")
            return
        bad = [
            f"{axis} drive error {err[0]}{' (' + err[1] + ')' if err[1] else ''}"
            for axis, err in (("altitude", st.alt_motor_error), ("azimuth", st.az_motor_error))
            if err is not None and err[0] != 0
        ]
        if bad:
            # Legacy cdk700.inPosition failed a slew on any motor error code.
            self._failing = True
            self._disarm_slew()
            with contextlib.suppress(PWIError):
                await self._send(self.pwi.stop, "stop (drive error)")
            self._fail_slew(f"slew failed: PWI reports {' and '.join(bad)}; mount stopped")
            return
        pwi2 = st.backend == "pwi2"
        parking = self.park_state in ("parking", "parked")
        settled = now - self._moving_since > cfg.slew_report_min
        if settled and self._arrived(st):
            if pwi2 and not parking:
                # PWI2's moving is residual-based: done once it has read done
                # across slew_settle (or flickered for slew_noisy_time).
                if self._pwi2_settled(st, now):
                    await self._slew_done(st)
                return
            # On the target and still: done, whatever PWI's own flag says
            # (PWI2's ``moving`` stuck True after a PWI2 restart, night 1107).
            if st.slewing is True:
                log.info(
                    "slew complete: on the target (alt %.3f, az %.3f) although PWI reports slewing",
                    st.alt_deg,
                    st.az_deg,
                )
            await self._slew_done(st)
            return
        self._arrived_since = None
        self._quiet_since = None
        if pwi2 and not parking and self._target_known(st) and st.slewing is not True:
            # PWI2 reads done (or cannot say: untracked) but the mount is not
            # on its target: it may not have started yet (seconds after a
            # goto, night 1107/1108); still and off target for
            # slew_arrive_timeout, the slew has failed.
            if now - self._moving_since > cfg.slew_arrive_timeout and self.speed < cfg.moving_speed:
                sep = self._target_sep(st)
                self._failing = True
                self._disarm_slew()
                with contextlib.suppress(PWIError):
                    await self._send(self.pwi.stop, "stop (slew not on its target)")
                self._fail_slew(
                    f"slew failed: PWI2 reports the slew done but the mount has stood"
                    f" {sep if sep is not None else float('nan'):.3f} deg from its target"
                    f" (beyond slew_tolerance {cfg.slew_tolerance:g}) {cfg.slew_arrive_timeout:g}"
                    f" s after the goto (alt {st.alt_deg:.3f}, az {st.az_deg:.3f})"
                )
            self._stall_ref = None
            return
        if st.slewing is not True:
            # Not on the target: PWI's word that it is done counts only once
            # the mount had time to start (PWI2 can report a tracked mount
            # not moving for a second or two after a goto; disarming then
            # left the limit watch without the target, night 1107).
            if (
                now - self._moving_since > max(cfg.slew_report_min, cfg.stall_time)
                and self.speed < cfg.moving_speed
            ):
                await self._slew_done(st)
            self._stall_ref = None
            return
        if self._at_target(st):
            self._stall_ref = None  # settling on the target (moving: servo residuals)
            return
        # Progress: toward an alt/az target whose mechanical azimuth is known
        # (a mount pushing against an obstruction may still creep), else any
        # movement (RA/Dec targets move; a sky separation can grow on the way).
        ref = self._stall_ref
        if (
            self._goto_altaz is not None
            and self._goto_mech is not None
            and st.az_mech_deg is not None
        ):
            togo = max(abs(st.alt_deg - self._goto_altaz[0]), abs(st.az_mech_deg - self._goto_mech))
            if togo < 0.01:
                self._stall_ref = None  # settling on the target
                return
            progressed = ref is None or ref[1] - togo > cfg.stall_move
            sample = (now, togo, 0.0)
        else:
            progressed = ref is None or (
                _sep_deg(st.alt_deg, st.az_deg, ref[1], ref[2]) > cfg.stall_move
            )
            sample = (now, st.alt_deg, st.az_deg)
        if progressed:
            self._stall_ref = sample
            return
        assert ref is not None
        if now - ref[0] < cfg.stall_time:
            return
        if st.backend == "pwi2" and st.tracking and _truthy(st.raw.get("mount.on_target")):
            # PWI2 says it is on the target (what the legacy code waited
            # for) and the mount has stopped: a finished slew, not a stall.
            log.info(
                "slew complete: PWI2 on_target (alt %.3f, az %.3f) although it reports moving",
                st.alt_deg,
                st.az_deg,
            )
            await self._slew_done(st)
            return
        why = (
            f"slew failed: PWI reports slewing but the mount has made no progress for"
            f" {now - ref[0]:.1f} s (alt {st.alt_deg:.3f}, az {st.az_deg:.3f}"
            + (f", mechanical az {st.az_mech_deg:.3f}" if st.az_mech_deg is not None else "")
            + "); the target is probably beyond PWI's own axis limits or the axis is blocked"
            " (PWI4 accepts such a goto, stops short and reports slewing forever). Mount stopped."
        )
        # Stop first, then publish the error: a client polling Slewing (the
        # watcher task also runs this check) must not see the failure while the
        # mount is still being driven. Disarming first keeps a concurrent check
        # from stopping twice; ``_failing`` keeps Slewing true meanwhile.
        self._failing = True
        self._disarm_slew()
        with contextlib.suppress(PWIError):
            await self._send(self.pwi.stop, "stop (slew stalled)")
        self._fail_slew(why)

    async def slewing(self) -> bool:
        if self.slew_error is not None:
            raise AlpacaException(DRIVER_ERROR, self.slew_error)
        if time.monotonic() - self._moving_since < self.config.slew_report_min:
            return True
        st = await self.status()
        await self._check_stall(st)
        if self.slew_error is not None:
            raise AlpacaException(DRIVER_ERROR, self.slew_error)
        if self._slew_active or self._failing:
            # Ours, not yet on its target (_check_stall ends it there, or
            # when PWI says done and nothing has moved for stall_time): a
            # PWI2 that has not started moving yet is not done (night 1107).
            return True
        if await self.homing(st):
            return True
        if st.slewing:
            if st.backend != "pwi2" or self._slew_active or self.speed >= self.config.moving_speed:
                return True
            # PWI2's ``moving`` can stick True (night 1107, after a PWI2
            # restart): with no slew of ours under way (one that ended on its
            # target, see _check_stall) and the mount not moving, it is not
            # slewing.
        if self.park_state == "parking":
            return True
        if self.speed > self.config.moving_speed and not st.tracking:
            return True
        raw = st.raw
        for axis in ("0", "1"):
            v = raw.get(f"mount.axis{axis}.setpoint_velocity_degs_per_sec")
            with contextlib.suppress(ValueError, TypeError):
                if v is not None and abs(float(v)) > 1e-4 and not st.tracking:
                    return True
        return False

    async def homing(self, st: PWIStatus | None = None) -> bool:
        if self._homing_since is None:
            return False
        st = st or await self.status()
        if st.finding_home:
            return True
        if time.monotonic() - self._homing_since < 2.0:
            return True
        if st.backend == "pwi4" and st.slewing:
            return True
        self._homing_since = None
        self._home_at = (st.alt_deg, st.az_deg)
        log.info("homing done (axes %s); PWI homed: %s", self._homing_axes, st.homed)
        if st.tracking:
            # PWI2's home leaves the mount tracking (MINERVA T1, 2026-11-09);
            # a homed mount stands still, as after an alt/az slew.
            with contextlib.suppress(PWIError):
                await self._send(lambda: self.pwi.tracking(False), "tracking off (home done)")
        if st.homed is None:
            self._homed_without_flag(st, self._homing_axes, "robofast-pwi-server FindHome")
        return False

    def _homed_without_flag(self, st: PWIStatus, axes: tuple[int, ...], how: str) -> None:
        """A home this server commanded ended and PWI has no homed flag: count
        it (and record it) only if the mount ended at the configured home."""
        cfg = self.config
        if cfg.home_alt is None and cfg.home_az is None:
            # No home position to check against: counts for this process only.
            self.homed_here |= set(axes)
            log.warning(
                "home not recorded (no home_alt/home_az in the config): counts until this"
                " server stops; it ended at alt %.3f az %.3f",
                st.alt_deg,
                st.az_deg,
            )
            return
        why = not_at_home(st, axes, cfg.home_alt, cfg.home_az, cfg.home_tolerance)
        if why is not None:
            log.error("home NOT counted: %s", why)
            return
        self.homed_here |= set(axes)
        self.home_record.record(axes, st, how)

    def is_homed(self, st: PWIStatus) -> bool | None:
        """PWI's homed flag; without one, whether robofast homed both axes
        (this process, or the home record, :mod:`~robofast.devices.pwi_homed`);
        None: unknown (treated as not homed)."""
        if st.homed is not None:
            return st.homed
        if self.homed_here >= {0, 1}:
            return True
        if self.homed_here | self.home_record.axes(st) >= {0, 1}:
            return True
        return None

    async def at_home(self) -> bool:
        """True after a completed FindHome until the mount moves away."""
        st = await self.status()
        if await self.homing(st) or self._home_at is None:
            return False
        if _sep_deg(st.alt_deg, st.az_deg, *self._home_at) > 0.1 or st.tracking:
            self._home_at = None
            return False
        return True

    async def at_park(self) -> bool:
        if self.park_state != "parked":
            return False
        st = await self.status()
        cfg = self.config
        if st.tracking or _sep_deg(st.alt_deg, st.az_deg, cfg.park_alt, cfg.park_az) > (
            cfg.park_tolerance
        ):
            self.park_state = "unknown"
            return False
        return True

    # -- checks -------------------------------------------------------------------------

    async def _ready(self, what: str, *, motion: bool = True) -> PWIStatus:
        st = await self.status(0)
        if not st.connected:
            raise AlpacaException(
                INVALID_OPERATION,
                f"{what}: PWI is not connected to the mount (Action MountConnect)",
            )
        if not st.motors_enabled:
            off = [
                n for n, on in (("azimuth", st.az_enabled), ("altitude", st.alt_enabled)) if not on
            ]
            errs = self._axis_errors(st)
            raise AlpacaException(
                INVALID_OPERATION,
                f"{what}: the {' and '.join(off)} axis motor is disabled. PWI disables an axis"
                " after a drive fault (e.g. motor current overload, I2t: see PWI's log); find the"
                f" cause, then Action MotorsEnable{'. ' + errs if errs else ''}",
            )
        for axis, err in (("alt", st.alt_motor_error), ("az", st.az_motor_error)):
            if err is not None and err[0] != 0:
                raise AlpacaException(INVALID_OPERATION, f"{what}: {axis} drive error {err}")
        if motion and not self.is_homed(st):
            state = "not homed" if st.homed is False else "homed state unknown"
            if not self.unhomed_ok:
                raise AlpacaException(
                    INVALID_OPERATION,
                    f"{what}: mount {state}; refusing (engineering override: allow_unhomed_moves"
                    " in the config AND --allow-unhomed)",
                )
            log.warning("UNHOMED MOVE (%s): mount %s; override in effect", what, state)
        return st

    def _check_alt(self, alt: float, what: str) -> None:
        cfg = self.config
        if not cfg.min_alt <= alt <= cfg.max_alt:
            # InvalidValue (ASCOM: a target the mount cannot reach), which
            # ConformU expects; InvalidOperation made it abandon its run.
            raise AlpacaException(
                INVALID_VALUE,
                f"{what}: target altitude {alt:.2f} deg outside [{cfg.min_alt}, {cfg.max_alt}]",
            )

    async def _not_parked(self, what: str) -> None:
        if await self.at_park():
            raise AlpacaException(PARKED, f"{what}: the telescope is parked")

    # -- telescope ------------------------------------------------------------------------

    async def slew_radec(self, ra: float, dec: float) -> None:
        if not (0.0 <= ra < 24.0) or not (-90.0 <= dec <= 90.0):
            raise AlpacaException(INVALID_VALUE, f"RA {ra} h / Dec {dec} deg out of range")
        await self._not_parked("slew")
        st = await self._ready("slew")
        if not st.tracking:
            raise AlpacaException(INVALID_OPERATION, "slew: tracking is off")
        if st.lst_hours is None:
            raise AlpacaException(DRIVER_ERROR, "slew: PWI reports no sidereal time")
        app = j2000_to_date(ra, dec)
        alt, az = hadec_to_altaz(st.lst_hours - app[0], app[1], self.config.latitude)
        self._check_alt(alt, "slew")
        mech = await self._place_azimuth(st, az % 360.0, "slew")
        self.target_ra, self.target_dec = ra, dec
        self.park_state = "unparked"
        self._watch_latched = False
        self._arm_slew(alt, None, mech)
        self._goto_app = app
        self._moving_since = time.monotonic()
        await self._send(
            lambda: self.pwi.goto_radec_j2000(ra, dec), f"goto J2000 {ra:.5f} h {dec:.4f}"
        )

    async def slew_altaz(self, alt: float, az: float, *, what: str = "SlewToAltAz") -> None:
        if not -90.0 <= alt <= 90.0 or not 0.0 <= az < 360.0:
            raise AlpacaException(INVALID_VALUE, f"alt {alt} / az {az} out of range")
        await self._not_parked(what)
        st = await self._ready(what)
        if st.tracking:
            raise AlpacaException(INVALID_OPERATION, f"{what}: tracking is on")
        self._check_alt(alt, what)
        mech = await self._place_azimuth(st, az, what)
        self._watch_latched = False
        self._arm_slew(alt, (alt, az), mech)
        # ASCOM: the mount does not track after SlewToAltAz. PWI2's alt/az
        # move turns tracking on (MINERVA T1, 2026-11-09): tracking off is
        # sent once the slew is on its target (not before: it halts the move).
        self._untrack_on_arrival = True
        self._moving_since = time.monotonic()
        await self._send(lambda: self.pwi.goto_altaz(alt, az), f"goto alt {alt:.4f} az {az:.4f}")

    async def abort(self) -> None:
        self._untrack_on_arrival = False
        self._moving_since = -1e9
        self._disarm_slew()
        self.slew_error = None
        if self.park_state == "parking":
            self.park_state = "unknown"
        if self._park_task is not None:
            self._park_task.cancel()
        self._homing_since = None
        await self._send(self.pwi.stop, "stop")

    async def tracking(self) -> bool:
        return (await self.status()).tracking

    async def set_tracking(self, on: bool) -> None:
        if on:
            await self._not_parked("Tracking")
            st = await self._ready("Tracking")
            if st.tracking:
                return
            self.park_state = "unparked"
            self._watch_latched = False
        else:
            if self._slew_active and self._goto_altaz is not None:
                # An alt/az slew of ours (sent with tracking off) is under
                # way: PWI2 can report tracking during it, and tracking off
                # then halts the slew midway (night 1107, the rehearsal's
                # end action). Sent once the mount is on the target.
                self._untrack_on_arrival = True
                log.info("tracking off: once the alt/az slew is on its target")
                return
            if not (await self.status(0)).tracking:
                return
        await self._send(lambda: self.pwi.tracking(on), f"tracking {'on' if on else 'off'}")

    async def park(self) -> None:
        if self.park_state == "parked" and await self.at_park():
            return
        st = await self._ready("Park")
        cfg = self.config
        mech = await self._place_azimuth(st, cfg.park_az % 360.0, "Park")
        if st.tracking:
            await self._send(lambda: self.pwi.tracking(False), "tracking off (park)")
        self.park_state = "parking"
        self._watch_latched = False
        self._arm_slew(cfg.park_alt, (cfg.park_alt, cfg.park_az % 360.0), mech)
        self._moving_since = time.monotonic()
        await self._send(lambda: self.pwi.goto_altaz(cfg.park_alt, cfg.park_az), "goto park")
        self._park_task = asyncio.get_running_loop().create_task(self._finish_park())

    async def _finish_park(self) -> None:
        cfg = self.config
        deadline = time.monotonic() + cfg.park_timeout
        while self.park_state == "parking" and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                st = await self.status(0)
            except PWIError:
                continue
            near = _sep_deg(st.alt_deg, st.az_deg, cfg.park_alt, cfg.park_az) <= cfg.park_tolerance
            if near and not st.slewing and self.speed < cfg.moving_speed:
                if st.tracking:
                    await self._send(lambda: self.pwi.tracking(False), "tracking off (parked)")
                self.park_state = "parked"
                log.info("parked")
                return
        if self.park_state == "parking":
            self._fail_slew(
                f"Park did not complete within park_timeout {cfg.park_timeout:g} s; mount stopped"
            )
            with contextlib.suppress(PWIError):
                await self._send(self.pwi.stop, "stop (park timeout)")

    async def unpark(self) -> None:
        if self.park_state == "parking":
            raise AlpacaException(INVALID_OPERATION, "Unpark: still parking")
        self.park_state = "unparked"

    async def find_home(self, axis: int | None = None) -> None:
        """Home both axes, or one (0 azimuth, 1 altitude; PWI4 4.1.7+).
        Never called implicitly: only FindHome / the FindHome Action, and
        only with ``allow_find_home``."""
        if not self.config.allow_find_home:
            raise AlpacaException(
                NOT_IMPLEMENTED, "FindHome is disabled (allow_find_home is false in the config)"
            )
        await self._not_parked("FindHome")
        st = await self._ready("FindHome", motion=False)
        if axis is not None:
            if st.backend != "pwi4" or not version_at_least(st.version, PER_AXIS_HOME_VERSION):
                raise AlpacaException(
                    INVALID_OPERATION,
                    f"FindHome axis {axis}: needs PWI4 4.1.7 or later (this is"
                    f" {st.backend} {st.version or 'version unknown'})",
                )
        log.warning("FIND HOME: axes %s", "both" if axis is None else axis)
        self._homing_axes = (0, 1) if axis is None else (axis,)
        self.homed_here -= set(self._homing_axes)
        self._homing_since = time.monotonic()
        self._home_at = None
        self._disarm_slew()
        self.slew_error = None
        self._moving_since = time.monotonic()
        self._watch_latched = True  # homing may cross the limits
        await self._send(lambda: self.pwi.find_home(axis), f"find home {axis}")

    async def mount_connect(self) -> None:
        await self._send(self.pwi.mount_connect, "mount connect")
        with contextlib.suppress(PWIError):
            await self.check_model()

    async def motors(self, on: bool) -> None:
        if on:
            st = await self.status(0)
            if not st.connected:
                raise AlpacaException(INVALID_OPERATION, "MotorsEnable: PWI is not connected")
            await self._send(self.pwi.enable_motors, "motors enable")
        else:
            await self._send(self.pwi.disable_motors, "motors disable")

    def _port(self, parameters: str) -> int:
        p = (parameters or "").strip()
        if p in self.config.ports:
            return self.config.ports[p]
        if p in ("1", "2"):
            return int(p)
        names = ", ".join(["1", "2", *self.config.ports])
        raise AlpacaException(INVALID_VALUE, f"{PORT_ACTION}: no port {p!r} (one of {names})")

    async def select_port(self, parameters: str) -> str:
        out = await self._select_port(parameters)
        with contextlib.suppress(PWIError):
            await self.check_model()
        return out

    async def _select_port(self, parameters: str) -> str:
        port = self._port(parameters)
        st = await self.status(0)
        if st.m3_port == port:
            return json.dumps({"port": port, "moved": False})
        cfg = self.config
        # PWI4 sometimes answers an M3 goto and never moves (seen ~3 s after
        # the M3 arrived at the other port). A goto counts as taken once the
        # reported port changes (PWI4 reads -1 while moving); one not taken
        # after m3_resend_after s is sent again, at most m3_retries times.
        deadline = time.monotonic() + cfg.m3_timeout
        sends = 0
        while True:
            await self._send(lambda: self.pwi.m3_select(port), f"m3 port {port}")
            sends += 1
            sent_at, before, taken = time.monotonic(), st.m3_port, False
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                st = await self.status(0)
                if st.m3_port == port and not st.m3_moving:
                    return json.dumps({"port": port, "moved": True})
                taken = taken or st.m3_port != before
                if not taken and time.monotonic() - sent_at >= cfg.m3_resend_after:
                    break
            if time.monotonic() >= deadline or sends > cfg.m3_retries:
                break
            self.m3_resends += 1
            log.warning(
                "M3: goto port %d not taken after %.0f s (port reads %s): sending it again",
                port,
                cfg.m3_resend_after,
                st.m3_port or "moving/unknown",
            )
        if not taken:
            # The last goto was not taken either: let it run out the clock.
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                st = await self.status(0)
                if st.m3_port == port and not st.m3_moving:
                    return json.dumps({"port": port, "moved": True})
        with contextlib.suppress(PWIError):
            await self._send(self.pwi.m3_stop, "m3 stop (timeout)")
        raise AlpacaException(
            DRIVER_ERROR,
            f"{PORT_ACTION}: M3 not at port {port} after {cfg.m3_timeout} s ({sends} goto(s))",
        )

    async def offset(self, parameters: str) -> str:
        try:
            p = json.loads(parameters or "{}")
            east, north = float(p.get("east", 0.0)), float(p.get("north", 0.0))
        except (ValueError, TypeError, AttributeError):
            raise AlpacaException(
                INVALID_VALUE, 'Offset parameters: JSON {"east": arcsec, "north": arcsec}'
            ) from None
        cfg = self.config
        if not (math.isfinite(east) and math.isfinite(north)):
            raise AlpacaException(INVALID_VALUE, "Offset: not finite")
        if abs(east) > cfg.max_offset or abs(north) > cfg.max_offset:
            raise AlpacaException(INVALID_VALUE, f'Offset exceeds max_offset {cfg.max_offset}"')
        await self._not_parked("Offset")
        st = await self._ready("Offset")
        if not st.tracking:
            raise AlpacaException(INVALID_OPERATION, "Offset: tracking is off")
        cosd = math.cos(math.radians(st.dec_deg))
        if cosd < 0.01:
            raise AlpacaException(INVALID_OPERATION, "Offset: too close to the pole")
        ra_arcsec = east / cosd
        self._moving_since = time.monotonic()
        await self._send(
            lambda: self.pwi.offset_radec(ra_arcsec, north), f'offset E {east}" N {north}"'
        )
        return json.dumps({"east": east, "north": north, "ra_arcsec": ra_arcsec})

    async def pulse_guide(self, direction: int, ms: int) -> None:
        """A guide pulse as a PWI offset: ``guide_rate`` arcsec/s for ``ms``."""
        cfg = self.config
        if direction not in (0, 1, 2, 3):
            raise AlpacaException(INVALID_VALUE, f"PulseGuide direction {direction}")
        if not 0 <= ms <= cfg.max_pulse_ms:
            raise AlpacaException(INVALID_VALUE, f"PulseGuide duration {ms} ms")
        arcsec = cfg.guide_rate * ms / 1000.0
        # ASCOM guideNorth 0, South 1, East 2, West 3; east = +RA. The RA
        # guide rate moves the RA axis (coordinate arcsec), as ConformU expects.
        ra = {2: arcsec, 3: -arcsec}.get(direction, 0.0)
        north = {0: arcsec, 1: -arcsec}.get(direction, 0.0)
        await self._not_parked("PulseGuide")
        st = await self._ready("PulseGuide")
        if not st.tracking:
            raise AlpacaException(INVALID_OPERATION, "PulseGuide: tracking is off")
        await self._send(lambda: self.pwi.offset_radec(ra, north), f'pulse RA {ra}" Dec {north}"')
        self.pulse_until = max(self.pulse_until, time.monotonic() + ms / 1000.0)

    async def report(self) -> dict[str, Any]:
        st = await self.status(0)
        return {
            "pwi": st.as_dict(),
            "park_state": self.park_state,
            "speed_deg_s": self.speed,
            "limits": [self.config.min_alt, self.config.max_alt],
            "watch_stops": self.watch_stops,
            "watch_glitches": self.watch_glitches,
            "homed": self.is_homed(st),
            "homed_here": sorted(self.homed_here),
            "home_record": sorted(self.home_record.axes(st)),
            "unhomed_override": self.unhomed_ok,
            "slew_error": self.slew_error,
            # PWI2 judges "moving" by the servo residuals against its "MAX RMS
            # Encoder Error for Goto" (not readable through its HTTP API;
            # docs/pwi.md): the residuals, and how often moving flickered.
            "rms_error_arcsec": {
                "alt": _num(st.raw.get("mount.alt_rms_error_arcsec")),
                "az": _num(st.raw.get("mount.azm_rms_error_arcsec")),
            }
            if st.backend == "pwi2"
            else None,
            "moving_flickers": self.moving_flickers,
            "m3_resends": self.m3_resends,
            "pointing_model": st.pointing_model,
            "pointing_model_wanted": self.models.get(st.m3_port),
            "model_loads": [list(x) for x in self.model_loads],
        }

    # -- the limit watch -----------------------------------------------------------------

    def _limit_check(
        self, pos: float, v: float, lo: float, hi: float, goto: float | None
    ) -> tuple[bool, bool, float]:
        """One axis against [lo, hi]: (outside it, should stop, rest point).
        Stop if outside and moving further out, or heading out fast enough
        that a stop now would coast past. A slew of ours to a target inside
        moving toward it (not yet ``goto_margin`` past it) comes to rest
        no further than ``goto_margin`` past that target: PWI decelerates
        onto it. Past the target, or moving away, the full coast counts."""
        cfg = self.config
        rest = pos + math.copysign(coast_deg(cfg, v), v)
        if goto is not None and lo <= goto <= hi:
            gm = cfg.goto_margin
            if v > 1e-4 and pos <= goto + gm:
                rest = min(rest, goto + gm)
            elif v < -1e-4 and pos >= goto - gm:
                rest = max(rest, goto - gm)
        outside = not lo <= pos <= hi
        out_now = (pos < lo and v < -1e-4) or (pos > hi and v > 1e-4)
        heading = (v < -1e-4 and rest < lo) or (v > 1e-4 and rest > hi)
        if self._homing_since is not None:
            heading = False  # homing may cross the limits
        return outside or heading, out_now or heading, rest

    def _commanded_motion(self) -> bool:
        """Motion of ours is under way (a slew, a park, a home)."""
        return (
            self._slew_active
            or self._homing_since is not None
            or self.park_state == "parking"
            or time.monotonic() - self._moving_since < self.config.slew_report_min
        )

    async def watch(self) -> None:
        cfg = self.config
        glitch = cfg.glitch_speed or 2.0 * cfg.slew_speed
        # The last good reads (t, alt, mech az), newest last: speeds come
        # from the newest read and the oldest kept (two intervals), so one
        # read's timing jitter does not make a speed (night 1107: a
        # 16 deg/s slew measured at 21.6 deg/s, "would coast to 95").
        hist: collections.deque[tuple[float, float, float | None]] = collections.deque(maxlen=3)
        confirmed = False  # fast motion of no slew of ours, confirmed by a re-read

        def rates(t: float, alt: float, mech: float | None, ref) -> tuple[float, float]:
            dt = t - ref[0]
            if dt <= 1e-3:
                return 0.0, 0.0
            vm = (mech - ref[2]) / dt if mech is not None and ref[2] is not None else 0.0
            return (alt - ref[1]) / dt, vm

        while True:
            await asyncio.sleep(cfg.watch_interval)
            if self._model_due and self.models:
                with contextlib.suppress(PWIError):
                    await self.check_model()
            try:
                st = await self.status(cfg.watch_interval / 2)
            except PWIError:
                hist.clear()
                confirmed = False
                continue
            await self._check_stall(st)
            t, alt, mech = self._st_at, st.alt_deg, st.az_mech_deg
            if hist and t - hist[-1][0] <= 1e-3:
                continue  # the same read again
            v1, vm1 = rates(t, alt, mech, hist[-1]) if hist else (0.0, 0.0)
            if abs(v1) > glitch or abs(vm1) > glitch:
                # Measure the next read from scratch: this one is not motion.
                log.warning(
                    "limit watch: skipped a bad PWI read (altitude %.2f deg at %.1f deg/s,"
                    " mechanical azimuth at %.1f deg/s)",
                    alt,
                    v1,
                    vm1,
                )
                self.watch_glitches += 1
                hist.clear()
                confirmed = False
                continue
            fast = max(abs(v1), abs(vm1)) > cfg.track_speed_max
            confirmed = confirmed and fast
            if hist and fast and not confirmed and not self._commanded_motion():
                # Nothing of ours is moving it, yet it reads fast: read again
                # now, and believe it only if that read agrees.
                try:
                    st2 = await self.status(0)
                except PWIError:
                    hist.clear()
                    continue
                if self._st_at - t <= 1e-3:
                    continue  # no fresh read to be had
                t2, alt2, mech2 = self._st_at, st2.alt_deg, st2.az_mech_deg
                v2, vm2 = rates(t2, alt2, mech2, hist[-1])
                if max(abs(v2), abs(vm2)) <= cfg.track_speed_max:
                    log.warning(
                        "limit watch: skipped a bad PWI read (altitude %.2f deg, %.2f deg/s;"
                        " read again: %.2f deg, %.3f deg/s)",
                        alt,
                        v1,
                        alt2,
                        v2,
                    )
                    self.watch_glitches += 1
                    hist.append((t2, alt2, mech2))
                    continue
                if abs(v2) > glitch or abs(vm2) > glitch:
                    self.watch_glitches += 1
                    hist.clear()
                    continue
                # Confirmed: the re-read is this sample (the first stays
                # out of the speeds; it may be the one that was wrong), and
                # this motion is not read twice again while it lasts.
                confirmed = True
                st, t, alt, mech = st2, t2, alt2, mech2
            ref = hist[0] if hist else None
            hist.append((t, alt, mech))
            if ref is None:
                continue  # one read is no speed
            v, vm = rates(t, alt, mech, ref)
            # A mount that has stopped (the last interval) coasts nowhere,
            # whatever the interval before it saw (a slew just ended on its
            # target, and no longer bounds the coast).
            v1, vm1 = rates(t, alt, mech, hist[-2])
            if abs(v1) < cfg.moving_speed:
                v = v1
            if abs(vm1) < cfg.moving_speed:
                vm = vm1
            goto_alt, goto_mech = self._goto_alt, self._goto_mech
            done = self._done_goto
            if not self._slew_active and done is not None and done[0] >= ref[0]:
                # A slew just ended on its target: these speeds still hold
                # its last reads (moving toward that target).
                goto_alt, goto_mech = done[1], done[2]
            m = cfg.limit_margin
            near, stop, rest = self._limit_check(alt, v, cfg.min_alt - m, cfg.max_alt + m, goto_alt)
            what = f"altitude {alt:.2f} deg at {v:.2f} deg/s (would coast to {rest:.2f})"
            what += f" against [{cfg.min_alt}, {cfg.max_alt}]"
            if cfg.az_wrap_min is not None and cfg.az_wrap_max is not None and mech is not None:
                near_m, stop_m, rest_m = self._limit_check(
                    mech, vm, cfg.az_wrap_min - m, cfg.az_wrap_max + m, goto_mech
                )
                if stop_m and not stop:
                    what = (
                        f"mechanical azimuth {mech:.2f} deg at {vm:.2f} deg/s (would coast to"
                        f" {rest_m:.2f}) against [{cfg.az_wrap_min}, {cfg.az_wrap_max}]"
                    )
                near, stop = near or near_m, stop or stop_m
            if not near:
                self._watch_latched = False
            elif stop and not self._watch_latched:
                self._watch_latched = True
                self.watch_stops += 1
                if self._slew_active:
                    # A slew of ours, stopped short: Slewing reports it failed.
                    self._fail_slew(f"LIMIT: {what}: slew stopped")
                else:
                    self._disarm_slew()
                    log.error("LIMIT: %s: stop", what)
                with contextlib.suppress(PWIError):
                    await self._send(self.pwi.stop, "stop (limit watch)")

    # -- focuser ----------------------------------------------------------------------

    async def _port_now(self, what: str) -> int:
        st = await self.status()
        if st.m3_port not in (1, 2):
            raise AlpacaException(INVALID_OPERATION, f"{what}: the M3 is between ports")
        return st.m3_port

    async def focus_position(self) -> int:
        st = await self.status()
        if st.focuser_position is None:
            raise AlpacaException(DRIVER_ERROR, "focuser position not reported")
        return max(0, round(st.focuser_position))

    async def focus_moving(self) -> bool:
        return bool((await self.status()).focuser_moving)

    async def focus_move(self, position: int) -> None:
        cfg = self.config
        if not cfg.focus_min <= position <= cfg.focus_max:
            raise AlpacaException(
                INVALID_VALUE, f"Move: {position} outside [{cfg.focus_min}, {cfg.focus_max}]"
            )
        port = await self._port_now("Move")
        self.focus_target = position
        await self._send(lambda: self.pwi.focuser_goto(port, position), f"focuser{port} {position}")

    async def focus_halt(self) -> None:
        port = await self._port_now("Halt")
        await self._send(lambda: self.pwi.focuser_stop(port), f"focuser{port} stop")

    # -- rotator ------------------------------------------------------------------------

    async def rotator_mech(self) -> float:
        st = await self.status()
        port = st.m3_port
        if port not in (1, 2):
            raise AlpacaException(DRIVER_ERROR, "rotator: the M3 is between ports")
        if st.backend == "pwi4" and port != self.config.rotator_status_port:
            raise AlpacaException(
                DRIVER_ERROR,
                f"rotator: PWI4 reports only port {self.config.rotator_status_port}'s rotator",
            )
        if st.rotator_mech_deg is None:
            raise AlpacaException(DRIVER_ERROR, "rotator position not reported")
        return st.rotator_mech_deg % 360.0

    async def rotator_moving(self) -> bool:
        st = await self.status()
        return bool(st.rotator_moving)

    async def rotator_goto(self, mech: float) -> None:
        port = await self._port_now("rotator")
        await self.rotator_mech()  # refuse what we cannot read back
        await self._send(
            lambda: self.pwi.rotator_goto_mech(port, mech), f"rotator{port} {mech:.3f}"
        )

    async def rotator_halt(self) -> None:
        port = await self._port_now("Halt")
        self.rotator_target = None
        await self._send(lambda: self.pwi.rotator_stop(port), f"rotator{port} stop")


# -- Alpaca over HTTP ------------------------------------------------------------------

TELESCOPE_CAN = {
    "canpark": True,
    "canpulseguide": True,
    "cansetdeclinationrate": False,
    "cansetguiderates": False,
    "cansetpark": False,
    "cansetpierside": False,
    "cansetrightascensionrate": False,
    "cansettracking": True,
    "canslew": False,
    "canslewaltaz": False,
    "canslewaltazasync": True,
    "canslewasync": True,
    "cansync": False,
    "cansyncaltaz": False,
    "canunpark": True,
}
TELESCOPE_NOT_IMPLEMENTED_PUT = frozenset(
    {
        "declinationrate",
        "rightascensionrate",
        "guideratedeclination",
        "guideraterightascension",
        "doesrefraction",
        "sideofpier",
        "siteelevation",
        "sitelatitude",
        "sitelongitude",
        "utcdate",
        "moveaxis",
        "slewtocoordinates",
        "slewtotarget",
        "slewtoaltaz",
        "synctocoordinates",
        "synctotarget",
        "synctoaltaz",
        "setpark",
        "commandblind",
        "commandbool",
        "commandstring",
    }
)
INTERFACE = {"telescope": 4, "focuser": 4, "rotator": 4}


class PWIServer(ThreadingHTTPServer):
    """The Alpaca server. The PWI client and logic live on a private event
    loop thread; HTTP requests (threads) submit to it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, config: PWIConfig, *, hardware: bool = False, allow_unhomed: bool = False
    ) -> None:
        allow = hwgate.gate_open(config.allow_hardware, hardware, what="PWI hardware")
        what = f"{config.backend} PWI"
        self.pwi_ip = hwgate.check_endpoint(
            config.pwi.host, config.pwi_port, allow_hardware=allow, what=what, allow_absent=True
        )
        # Re-verified before every request to PWI (a no-op with the gate open).
        self._guard = hwgate.SimulatorGuard(self.pwi_ip, config.pwi_port, hardware=allow, what=what)
        if allow:
            log.warning("HARDWARE MODE: %s at %s:%d", config.backend, self.pwi_ip, config.pwi_port)
        host, _, port = config.listen.rpartition(":")
        super().__init__((host, int(port)), _Handler)
        self.config = config
        self.devices: tuple[str, ...] = ("telescope",)
        self.devices += ("focuser",) if config.focuser else ()
        self.devices += ("rotator",) if config.rotator else ()
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="pwi-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self._watch: concurrent.futures.Future | None = None
        self.mount: PWIMount = self._run_sync(self._build())
        self.mount.unhomed_ok = config.allow_unhomed_moves and allow_unhomed
        if self.mount.unhomed_ok:
            log.warning("ENGINEERING OVERRIDE: moves allowed while the mount is not homed")
        elif config.allow_unhomed_moves != allow_unhomed:
            log.info("unhomed moves need allow_unhomed_moves in the config AND --allow-unhomed")

    async def _build(self) -> PWIMount:
        p = self.config.pwi
        client = make_client(
            self.config.backend,
            self.pwi_ip,
            self.config.pwi_port,
            timeout=p.timeout,
            connect_timeout=p.connect_timeout,
            guard=self._guard,
        )
        return PWIMount(client, self.config)

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_thread.is_alive():
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)  # type: ignore[arg-type]

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def _call(self, fn: Callable[[], Awaitable[Any]], what: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AlpacaException(
                PWI_TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except PWITimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(PWI_TIMEOUT, str(exc)) from None
        except PWIError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management -----------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast PWI ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{name} {d}",
                    "DeviceType": d.capitalize(),
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-pwi-{name}-{d}-0",
                }
                for d in self.devices
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>PWI</title></head>"
            f"<body><h1>robofast PWI: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members --------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        if value:
            if not self.connected(device, client):
                # Reading only: connecting never commands PWI.
                self._call(lambda: self.mount.status(0), "Connect")
                with self._clients_lock:
                    first = not any(d == "telescope" for d, _ in self._clients)
                    self._clients.add((device, client))
                if first and device == "telescope":
                    # A new session: targets set by earlier clients do not
                    # carry over (TargetRightAscension/Declination are unset
                    # until written or slewed to, as ASCOM expects).
                    self.mount.target_ra = self.mount.target_dec = None
        else:
            with self._clients_lock:
                self._clients.discard((device, client))

    def _common(self, device: str, member: str, client: int) -> Any:
        cfg = self.config
        pwi_version = self.mount.version or "(version not read)"
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": f"{cfg.name} {device} via PlaneWave {cfg.backend.upper()}",
            # PWI's own version first: it goes in the FITS header (MNTSWVER).
            "driverinfo": f"PlaneWave {cfg.backend.upper()} {pwi_version};"
            f" robofast pwi_server {DRIVER_VERSION}",
            "driverversion": DRIVER_VERSION,
            "interfaceversion": INTERFACE[device],
            "name": f"robofast PWI {cfg.name} {device}",
            "supportedactions": list(TELESCOPE_ACTIONS) if device == "telescope" else [],
        }[member]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        if verb == "PUT":
            if member == "connected":
                self._set_connected(device, client, _bool_param(params, "Connected"))
                return None
            if member in ("connect", "disconnect"):
                self._set_connected(device, client, member == "connect")
                return None
        elif member in COMMON_NAMES and member not in ("connect", "disconnect"):
            return self._common(device, member, client)
        if member in COMMON_NAMES:
            raise _BadRequest(f"{verb} {member} is not allowed")
        if member in ("connect", "disconnect", "action") and verb == "GET":
            raise _BadRequest(f"{member} is a PUT method")
        if device == "telescope":
            return self._telescope(verb, member, params, client)
        if device == "focuser":
            return self._focuser(verb, member, params, client)
        return self._rotator(verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    @staticmethod
    def _unknown(verb: str, member: str) -> Any:
        raise _BadRequest(f"{verb} {member}: no such member")

    def _telescope(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        t = self.mount
        if verb == "PUT" and member == "action" and params.get("Action", "").lower() == "stop":
            # Stop works whether or not this client is connected.
            self._call(t.abort, "Stop")
            return ""
        if verb == "GET":
            if member == "canfindhome":
                return cfg.allow_find_home
            if member in TELESCOPE_CAN:
                return TELESCOPE_CAN[member]
            if member in ("canmoveaxis", "axisrates"):
                axis = _int_param(params, "axis")
                if axis not in (0, 1, 2):
                    raise AlpacaException(INVALID_VALUE, f"axis {axis}")
                return False if member == "canmoveaxis" else []
            static = {
                "alignmentmode": 0,  # algAltAz
                "equatorialsystem": 2,  # equJ2000
                "declinationrate": 0.0,
                "rightascensionrate": 0.0,
                "sitelatitude": cfg.latitude,
                "sitelongitude": cfg.longitude,
                "siteelevation": cfg.elevation,
                "slewsettletime": 0,
                "trackingrate": 0,
                "trackingrates": [0],
                "guideratedeclination": cfg.guide_rate / 3600.0,
                "guideraterightascension": cfg.guide_rate / 3600.0,
                "utcdate": _now_stamp() + "Z",
            }
            if member in static:
                return static[member]
            if member in ("aperturediameter", "aperturearea", "focallength"):
                d = cfg.aperture_diameter
                v = {
                    "aperturediameter": d,
                    "aperturearea": None if d is None else math.pi * d * d / 4,
                    "focallength": cfg.focal_length,
                }[member]
                if v is None:
                    raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not configured")
                return v
            if member in ("doesrefraction", "sideofpier", "destinationsideofpier"):
                raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        self._need("telescope", member, client)
        if verb == "GET":
            if member in (
                "rightascension",
                "declination",
                "altitude",
                "azimuth",
                "siderealtime",
            ):
                st = self._call(t.status, member)
                if member == "siderealtime":
                    if st.lst_hours is None:
                        raise AlpacaException(DRIVER_ERROR, "PWI reports no sidereal time")
                    return st.lst_hours
                if member == "rightascension":
                    return st.ra2000_hours if st.ra2000_hours is not None else st.ra_hours
                if member == "declination":
                    return st.dec2000_deg if st.dec2000_deg is not None else st.dec_deg
                return st.alt_deg if member == "altitude" else st.az_deg
            if member == "atpark":
                return self._call(t.at_park, member)
            if member == "athome":
                return self._call(t.at_home, member)
            if member == "slewing":
                return self._call(t.slewing, member)
            if member == "tracking":
                return self._call(t.tracking, member)
            if member == "ispulseguiding":
                return time.monotonic() < t.pulse_until
            if member in ("targetrightascension", "targetdeclination"):
                v = t.target_ra if member == "targetrightascension" else t.target_dec
                if v is None:
                    raise AlpacaException(INVALID_OPERATION, f"{member} has not been set")
                return v
            if member == "devicestate":
                return self._telescope_state()
            return self._unknown(verb, member)
        # PUT
        if member == "slewtocoordinatesasync":
            ra = _float_param(params, "RightAscension")
            dec = _float_param(params, "Declination")
            return self._call(lambda: t.slew_radec(ra, dec), "SlewToCoordinatesAsync")
        if member == "slewtotargetasync":
            if t.target_ra is None or t.target_dec is None:
                raise AlpacaException(INVALID_OPERATION, "SlewToTargetAsync: no target set")
            ra, dec = t.target_ra, t.target_dec
            return self._call(lambda: t.slew_radec(ra, dec), "SlewToTargetAsync")
        if member == "slewtoaltazasync":
            alt = _float_param(params, "Altitude")
            az = _float_param(params, "Azimuth")
            return self._call(lambda: t.slew_altaz(alt, az), "SlewToAltAzAsync")
        if member == "targetrightascension":
            v = _float_param(params, "TargetRightAscension")
            if not 0.0 <= v < 24.0:
                raise AlpacaException(INVALID_VALUE, f"TargetRightAscension {v}")
            t.target_ra = v
            return None
        if member == "targetdeclination":
            v = _float_param(params, "TargetDeclination")
            if not -90.0 <= v <= 90.0:
                raise AlpacaException(INVALID_VALUE, f"TargetDeclination {v}")
            t.target_dec = v
            return None
        if member == "abortslew":
            if self._call(t.at_park, member):
                raise AlpacaException(PARKED, "AbortSlew: the telescope is parked")
            return self._call(t.abort, "AbortSlew")
        if member == "park":
            return self._call(t.park, "Park")
        if member == "unpark":
            return self._call(t.unpark, "Unpark")
        if member == "findhome":
            return self._call(t.find_home, "FindHome")
        if member == "tracking":
            on = _bool_param(params, "Tracking")
            return self._call(lambda: t.set_tracking(on), "Tracking")
        if member == "trackingrate":
            rate = _int_param(params, "TrackingRate")
            if rate != 0:
                raise AlpacaException(INVALID_VALUE, f"TrackingRate {rate}: sidereal only")
            return None
        if member == "slewsettletime":
            if _float_param(params, "SlewSettleTime") < 0:
                raise AlpacaException(INVALID_VALUE, "SlewSettleTime must be >= 0")
            return None
        if member == "pulseguide":
            direction = _int_param(params, "Direction")
            ms = _int_param(params, "Duration")
            return self._call(lambda: t.pulse_guide(direction, ms), "PulseGuide")
        if member == "action":
            return self._action(params.get("Action", ""), params.get("Parameters", ""))
        if member in TELESCOPE_NOT_IMPLEMENTED_PUT:
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    def _action(self, name: str, parameters: str) -> str:
        t = self.mount
        key = name.lower()
        if key == PORT_ACTION.lower():
            timeout = self.config.m3_timeout + 10.0
            fut = asyncio.run_coroutine_threadsafe(t.select_port(parameters), self._loop)
            try:
                return fut.result(timeout)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                raise AlpacaException(PWI_TIMEOUT, f"{PORT_ACTION}: timed out") from None
            except PWIError as exc:
                raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        if key == "offset":
            return self._call(lambda: t.offset(parameters), "Offset")
        if key == "mountconnect":
            self._call(t.mount_connect, "MountConnect")
            return ""
        if key in ("motorsenable", "motorsdisable"):
            on = key == "motorsenable"
            self._call(lambda: t.motors(on), name)
            return ""
        if key == "findhome":
            p = (parameters or "both").strip().lower()
            if p not in ("both", "0", "1"):
                raise AlpacaException(INVALID_VALUE, 'FindHome: "both", "0" (az) or "1" (alt)')
            axis = None if p == "both" else int(p)
            self._call(lambda: t.find_home(axis), "FindHome")
            return ""
        if key == "status":
            return json.dumps(self._call(t.report, "Status"))
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _telescope_state(self) -> list[dict[str, Any]]:
        t = self.mount
        state: list[dict[str, Any]] = []
        for name, fn in (("AtPark", t.at_park), ("Slewing", t.slewing), ("Tracking", t.tracking)):
            try:
                state.append({"Name": name, "Value": self._call(fn, name)})
            except AlpacaException as exc:
                log.warning("DeviceState without %s: %s", name, exc)
        try:
            st = self._call(t.status, "DeviceState")
            ra = st.ra2000_hours if st.ra2000_hours is not None else st.ra_hours
            dec = st.dec2000_deg if st.dec2000_deg is not None else st.dec_deg
            state += [
                {"Name": "Altitude", "Value": st.alt_deg},
                {"Name": "Azimuth", "Value": st.az_deg},
                {"Name": "RightAscension", "Value": ra},
                {"Name": "Declination", "Value": dec},
                {"Name": "UTCDate", "Value": _now_stamp() + "Z"},
            ]
            if st.lst_hours is not None:
                state.append({"Name": "SiderealTime", "Value": st.lst_hours})
        except AlpacaException as exc:
            log.warning("DeviceState without coordinates: %s", exc)
        with contextlib.suppress(AlpacaException):
            state.append({"Name": "AtHome", "Value": self._call(t.at_home, "AtHome")})
        state.append({"Name": "IsPulseGuiding", "Value": time.monotonic() < t.pulse_until})
        state.append({"Name": "TimeStamp", "Value": _now_stamp()})
        return state

    def _focuser(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        t = self.mount
        if verb == "GET":
            static = {
                "absolute": True,
                "maxincrement": round(cfg.focus_max),
                "maxstep": round(cfg.focus_max),
                "stepsize": 1.0,  # microns
                "tempcomp": False,
                "tempcompavailable": False,
            }
            if member in static:
                return static[member]
            if member == "temperature":
                raise AlpacaException(NOT_IMPLEMENTED, "Temperature is not implemented")
        self._need("focuser", member, client)
        if verb == "GET":
            if member == "position":
                return self._call(t.focus_position, "Position")
            if member == "ismoving":
                return self._call(t.focus_moving, "IsMoving")
            if member == "devicestate":
                state: list[dict[str, Any]] = []
                for name, fn in (("IsMoving", t.focus_moving), ("Position", t.focus_position)):
                    with contextlib.suppress(AlpacaException):
                        state.append({"Name": name, "Value": self._call(fn, name)})
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)
        if member == "move":
            pos = _int_param(params, "Position")
            return self._call(lambda: t.focus_move(pos), "Move")
        if member == "halt":
            return self._call(t.focus_halt, "Halt")
        if member == "tempcomp":
            if _bool_param(params, "TempComp"):
                raise AlpacaException(NOT_IMPLEMENTED, "no temperature compensation")
            return None
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no focuser actions")
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    def _rotator(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        t = self.mount
        if verb == "GET" and member == "canreverse":
            return True
        self._need("rotator", member, client)

        def sky(x: float) -> float:
            return ((-x if t.rotator_reverse else x) + t.rotator_offset) % 360.0

        def to_mech(p: float) -> float:
            x = (p - t.rotator_offset) % 360.0
            return (-x if t.rotator_reverse else x) % 360.0

        if verb == "GET":
            if member == "ismoving":
                return self._call(t.rotator_moving, "IsMoving")
            if member == "mechanicalposition":
                return round(self._call(t.rotator_mech, member), 4)
            if member == "position":
                return round(sky(self._call(t.rotator_mech, member)), 4)
            if member == "targetposition":
                if t.rotator_target is not None:
                    return round(t.rotator_target, 4)
                return round(sky(self._call(t.rotator_mech, member)), 4)
            if member == "reverse":
                return t.rotator_reverse
            if member == "stepsize":
                raise AlpacaException(NOT_IMPLEMENTED, "StepSize is not known")
            if member == "devicestate":
                state: list[dict[str, Any]] = []
                with contextlib.suppress(AlpacaException):
                    state.append(
                        {"Name": "IsMoving", "Value": self._call(t.rotator_moving, "IsMoving")}
                    )
                with contextlib.suppress(AlpacaException):
                    x = self._call(t.rotator_mech, "DeviceState")
                    state += [
                        {"Name": "MechanicalPosition", "Value": round(x, 4)},
                        {"Name": "Position", "Value": round(sky(x), 4)},
                    ]
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)

        def angle(name: str) -> float:
            v = _float_param(params, name)
            if not 0.0 <= v < 360.0:
                raise AlpacaException(INVALID_VALUE, f"{name} {v} not in [0, 360)")
            return v

        if member == "reverse":
            t.rotator_reverse = _bool_param(params, "Reverse")
            return None
        if member == "movemechanical":
            x = angle("Position")
            t.rotator_target = sky(x)
            return self._call(lambda: t.rotator_goto(x), "MoveMechanical")
        if member == "moveabsolute":
            p = angle("Position")
            t.rotator_target = p
            return self._call(lambda: t.rotator_goto(to_mech(p)), member)
        if member == "move":
            d = _float_param(params, "Position")
            if not -360.0 < d < 360.0:
                raise AlpacaException(INVALID_VALUE, f"Position {d} not in (-360, 360)")
            x = self._call(t.rotator_mech, member)
            p = (sky(x) + d) % 360.0
            t.rotator_target = p
            return self._call(lambda: t.rotator_goto(to_mech(p)), "Move")
        if member == "sync":
            p = angle("Position")
            x = self._call(t.rotator_mech, member)
            t.rotator_offset = (p - (-x if t.rotator_reverse else x)) % 360.0
            t.rotator_target = None
            return None
        if member == "halt":
            return self._call(t.rotator_halt, "Halt")
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no rotator actions")
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> PWIServer:
        # One status read, to log PWI's version (reading only; never fatal).
        try:
            self._call(lambda: self.mount.status(0), "startup status")
        except AlpacaException as exc:
            log.warning("startup status read failed: %s", exc)
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="pwi-http", daemon=True
        )
        self._http_thread.start()
        if self.config.watch_interval > 0:
            self._watch = asyncio.run_coroutine_threadsafe(self.mount.watch(), self._loop)
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
        if self._watch is not None:
            self._watch.cancel()
        if self._http_thread is not None:
            self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.mount.pwi.aclose(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real PWI (the config must also say allow_hardware)",
    )
    ap.add_argument(
        "--allow-unhomed",
        action="store_true",
        help="engineering: allow moves while not homed (the config must say allow_unhomed_moves)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_device_config(args.config, PWIConfig.from_dict)
    if args.listen:
        config.listen = args.listen
    try:
        server = PWIServer(config, hardware=args.hardware, allow_unhomed=args.allow_unhomed)
    except hwgate.HardwareNotAllowed as exc:
        log.error("%s", exc)
        sys.exit(2)
    server.start()
    print(
        json.dumps(
            {"url": f"http://{server.address}", "pwi": f"{server.pwi_ip}:{config.pwi_port}"}
        ),
        flush=True,
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
