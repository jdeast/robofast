"""Alpaca Telescope, Focuser and (optionally) Dome server for a DFM TCSGalil.

Built for the FLWO 60-inch (Tillinghast) telescope, which a DFM TCSGalil
("TCSGO") controls; the protocol and its unknowns are in ``docs/tcsgo.md``.
The TCS is reached through
:class:`~robofast_core.devices.tcsgo_client.TCSClient` (allowlist, hard
timeouts, bounded retries, one command at a time).

Devices
-------
* ``telescope/0`` (ITelescopeV4, equatorial). ``SlewToCoordinatesAsync`` /
  ``SlewToTargetAsync``: ``#6`` (RA, Dec, equinox ``equinox``) then, unless
  the TCS flags the target out of range, ``#12,2`` (slew to target; the TCS
  switches to Follow once the target is captured). Refused below
  ``min_alt`` (computed here from the TCS's sidereal time), while parked
  (ParkedException), with tracking off (as ASCOM requires), and with the
  drives switch down or the Galil data stale. ``Slewing``: TCS mode "slew",
  or "chase" not yet captured, or a slew or park commanded less than
  ``slew_report_min`` s ago. ``AbortSlew``: ``#12,1`` (follow from where it
  is: stops the slew and keeps tracking, as ASCOM asks) while slewing to a
  target, ``#13`` (stop: hold position) while parking. ``Tracking``: TCS mode
  follow, chase or slew; setting it true sends ``#50`` (start tracking),
  false ``#13`` (hold). ``Park``: ``#58`` (``park_command: "park"``) or
  ``#10`` + ``#12,2`` (``"zenith"``, the manual's "stow at zenith");
  ``Unpark``: ``#59``. The TCS has no "parked" status bit, so ``AtPark`` is
  true when the mount holds position within ``park_tolerance`` of
  (``park_alt``, ``park_az``) and no ``Unpark``/motion has been commanded
  since; a server restart re-derives it from the position. ``PulseGuide``:
  ``#61`` at the TCS's guide rate (``guide_rate`` must say what that is).
  RA/Dec/Alt/Az/SiderealTime come from ``#25``; RA/Dec are converted to
  J2000 (``EquatorialSystem`` 2) if the TCS's display equinox is not 2000.
  No sync (it would rewrite the encoder zero points), no MoveAxis (the TCS
  has none), no alt-az slews.
* ``focuser/0`` (IFocuserV3, absolute): ``Move`` sends ``#27``;
  ``Position`` is the TCS focus in microns minus ``focus_min`` (Alpaca
  positions are non-negative), so ``MaxStep`` is ``focus_max - focus_min``.
  ``IsMoving`` from the status bits (and for ``focus_settle`` s after a
  move). No Halt: the stop command the legacy code used (``#72``) is not in
  the manual. Off with ``"focuser": false``.
* ``dome/0`` (IDomeV3), only with ``"dome": true`` (whether the 60-inch dome
  is wired to the TCS is not confirmed): shutters ``#36``/``#44`` in
  ``shutter_open_order`` / ``shutter_close_order``, ``SlewToAzimuth``
  ``#43`` + ``#20,2``, ``FindHome`` ``#20,3``, ``Slaved`` ``#20,1`` /
  ``#35``, ``AbortSlew`` ``#35``, ``#40``, ``#41``.

Actions (telescope)
-------------------
* ``Action("Offset", '{"east": E, "north": N}')`` -- the guider's offset, in
  arcseconds on the sky (east = +RA; the TCS divides by cos Dec). Sends
  ``#7,E,N`` and then, unless the mount is in chase mode, ``#12,offset_mode``
  (the manual: in follow mode the target moves but the mount does not until
  a mode command). Refused unless tracking, beyond ``max_offset`` per axis,
  or while parked. Returns JSON of what was sent.
* ``Action("Status", "")`` -- JSON: the parsed ``#26`` flags, mount mode,
  limits, and ``#25`` coordinates.
* ``Action("Faults", "")`` -- JSON ``{"fault": bool, "reasons": [...]}``:
  drives switch down, amplifiers off, Galil data stale, any travel or soft
  limit, target out of range.

Safety
------
The TCS address must be loopback (a simulator) unless the config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``.
The address is resolved once at startup; only that literal is contacted.

Running
-------
::

    python -m robofast_core.devices.tcsgo_server --config tcsgo.json [--hardware]

with a JSON config (``sites/flwo60/tcsgo.json``; every key except ``tcs`` is
optional). It prints one JSON line, ``{"url": ..., "tcs": ...}``, once
listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import logging
import math
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _alpaca,
    _BadRequest,
    _bool_param,
    _Discovery,
    _uint32,
)
from robofast_core.devices.tcsgo_client import (
    EAST,
    NORTH,
    SOUTH,
    WEST,
    HardwareNotAllowed,
    MountCoords,
    TCSClient,
    TCSError,
    TCSStatus,
    TCSTimeout,
    check_tcs_address,
)
from robofast_core.devices.tcsgo_sim import FLWO_LATITUDE, FLWO_LONGITUDE, hadec_to_altaz

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
VALUE_NOT_SET = 0x402
PARKED = 0x408
TCS_TIMEOUT = 0x502
OPEN, CLOSED, OPENING, CLOSING, ERROR = 0, 1, 2, 3, 4  # ASCOM ShutterState


# -- configuration ----------------------------------------------------------------


@dataclass
class TCSConfig:
    host: str = "127.0.0.1"
    port: int = 5003
    connect_timeout: float = 2.0
    command_timeout: float = 3.0
    ack_window: float = 0.2
    #: Spacing between commands (legacy note: "no more than 1 per second").
    min_interval: float = 0.5
    max_attempts: int = 2


@dataclass
class TCSGOConfig:
    tcs: TCSConfig = field(default_factory=TCSConfig)
    #: Must be true (and ``--hardware`` given) for a non-loopback TCS.
    allow_hardware: bool = False
    name: str = "flwo60"
    listen: str = "127.0.0.1:11140"
    latitude: float = FLWO_LATITUDE
    longitude: float = FLWO_LONGITUDE
    elevation: float = 2344.0
    #: Equinox sent with #6 (and reported as EquatorialSystem J2000).
    equinox: float = 2000.0
    #: robofast refuses slews below this altitude (deg), before the TCS's own.
    min_alt: float = 20.0
    #: "park" (#58) or "zenith" (#10 + #12,2).
    park_command: str = "park"
    #: Where the mount is when parked (for AtPark) and how close counts.
    park_alt: float = 90.0
    park_az: float = 0.0
    park_tolerance: float = 1.0
    #: The TCS's handpaddle guide rate (#15), arcsec/s; robofast never sets it.
    guide_rate: float = 5.0
    max_pulse_ms: int = 10000
    #: Offset action: per-axis bound (arcsec; legacy MAX_JOG) and the #12
    #: mode sent after #7 unless already chasing (2 slew, 3 chase; None: none).
    max_offset: float = 1000.0
    offset_mode: int | None = 2
    #: Slewing is reported true this long after a slew/park is commanded.
    slew_report_min: float = 0.5
    #: Reads reuse a TCS reply this recent.
    status_max_age: float = 0.5
    #: Bound on each Alpaca request.
    request_timeout: float = 8.0
    aperture_diameter: float | None = 1.524
    focal_length: float | None = None
    focuser: bool = True
    focus_min: float = -5000.0
    focus_max: float = 5000.0
    focus_settle: float = 0.5
    dome: bool = False
    shutter_open_order: tuple[str, ...] = ("upper", "lower")
    shutter_close_order: tuple[str, ...] = ("lower", "upper")
    #: UDP port for Alpaca discovery (32227 is standard); None: off.
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TCSGOConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        tcs = d.pop("tcs", {})
        tcs_names = {f.name for f in dataclasses.fields(TCSConfig)}
        if set(tcs) - tcs_names:
            raise ValueError(f"unknown tcs keys: {sorted(set(tcs) - tcs_names)}")
        for key in ("shutter_open_order", "shutter_close_order"):
            if key in d:
                d[key] = tuple(d[key])
        cfg = cls(tcs=TCSConfig(**tcs), **d)
        if cfg.park_command not in ("park", "zenith"):
            raise ValueError("park_command must be 'park' or 'zenith'")
        if cfg.offset_mode not in (None, 2, 3):
            raise ValueError("offset_mode must be 2, 3 or null")
        for key in ("shutter_open_order", "shutter_close_order"):
            if sorted(getattr(cfg, key)) != ["lower", "upper"]:
                raise ValueError(f"{key} must name 'upper' and 'lower' once each")
        if cfg.focus_max <= cfg.focus_min:
            raise ValueError("focus_max must exceed focus_min")
        return cfg


def _sep_deg(alt1: float, az1: float, alt2: float, az2: float) -> float:
    a1, a2 = math.radians(alt1), math.radians(alt2)
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(
        math.radians(az1 - az2)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def to_j2000(ra_h: float, dec: float, equinox: float) -> tuple[float, float]:
    """Mean RA/Dec of ``equinox`` to J2000 (FK5); identity within 0.01 yr."""
    if abs(equinox - 2000.0) < 0.01:
        return ra_h, dec
    from astropy import units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    c = SkyCoord(ra_h * 15 * u.deg, dec * u.deg, frame=FK5(equinox=Time(equinox, format="jyear")))
    j = c.transform_to(FK5(equinox="J2000"))
    return j.ra.deg / 15.0, j.dec.deg


# -- the TCS logic ---------------------------------------------------------------------


class _Cache:
    def __init__(self) -> None:
        self.value: Any = None
        self.at = -1e9
        self.lock = asyncio.Lock()


class TCSGO:
    """Telescope, focuser and dome logic over a :class:`TCSClient` (one loop)."""

    def __init__(self, tcs: TCSClient, config: TCSGOConfig) -> None:
        self.tcs = tcs
        self.config = config
        self._caches: dict[int, _Cache] = {n: _Cache() for n in (25, 26, 62, 64)}
        self._moving_since = -1e9
        #: "unknown" (derive from position), "parking", "parked", "unparked".
        self._park_state = "unknown"
        self.target_ra: float | None = None
        self.target_dec: float | None = None
        self._pulse_until = 0.0
        self._focus_since = -1e9
        self._shutter_cmd: str | None = None

    # -- reads ------------------------------------------------------------------

    async def _read(self, number: int, max_age: float | None) -> Any:
        max_age = self.config.status_max_age if max_age is None else max_age
        c = self._caches[number]
        async with c.lock:
            if c.value is None or time.monotonic() - c.at > max_age:
                t = time.monotonic()
                values = await self.tcs.command(number)
                c.value, c.at = values, t
            return c.value

    async def status(self, max_age: float | None = None) -> TCSStatus:
        return TCSStatus.from_values(await self._read(26, max_age))

    async def mount(self, max_age: float | None = None) -> MountCoords:
        return MountCoords.from_values(await self._read(25, max_age))

    def _invalidate(self) -> None:
        for c in self._caches.values():
            c.at = -1e9

    async def _send(self, number: int, *args: float) -> None:
        try:
            await self.tcs.command(number, *args)
        finally:
            self._invalidate()

    async def radec_j2000(self) -> tuple[float, float]:
        m = await self.mount()
        return to_j2000(m.ra_hours, m.dec_deg, m.equinox)

    async def faults(self) -> dict[str, Any]:
        st = await self.status(0)  # a fault channel never reports a cached state
        reasons = []
        if not st.drives_switch_up:
            reasons.append("drives switch down")
        if not st.amplifiers_switch_up:
            reasons.append("amplifiers switch down")
        if st.galil_stale:
            reasons.append("Galil data stale")
        reasons += [f"limit: {x}" for x in st.limits]
        if st.target_out_of_range:
            reasons.append("target out of range")
        return {"fault": bool(reasons), "reasons": reasons, "mode": st.mode}

    async def status_report(self) -> dict[str, Any]:
        st = await self.status(0)
        m = await self.mount(0)
        return {"status": st.as_dict(), "mount": m.as_dict(), "atpark": await self.at_park()}

    # -- telescope ------------------------------------------------------------------

    async def at_park(self) -> bool:
        if self._park_state == "unparked":
            return False
        st = await self.status()
        m = await self.mount()
        cfg = self.config
        physical = (
            st.mode == "hold"
            and _sep_deg(m.altitude_deg, m.azimuth_deg, cfg.park_alt, cfg.park_az)
            <= cfg.park_tolerance
        )
        if physical and self._park_state in ("unknown", "parking"):
            self._park_state = "parked"
        elif not physical and self._park_state == "parked":
            self._park_state = "unknown"
        return physical and self._park_state == "parked"

    async def slewing(self) -> bool:
        if time.monotonic() - self._moving_since < self.config.slew_report_min:
            return True
        st = await self.status()
        return st.mode == "slew" or (st.mode == "chase" and not st.target_captured)

    async def tracking(self) -> bool:
        st = await self.status()
        return st.mode in ("follow", "chase") or (st.mode == "slew" and not self._parking)

    @property
    def _parking(self) -> bool:
        return self._park_state == "parking"

    async def _check_motion_allowed(self) -> None:
        st = await self.status(0)
        if not st.drives_switch_up or not st.amplifiers_switch_up:
            raise AlpacaException(INVALID_OPERATION, "drives or amplifiers switch is down")
        if st.galil_stale:
            raise AlpacaException(INVALID_OPERATION, "TCS reports Galil data stale")

    async def set_tracking(self, on: bool) -> None:
        if on:
            if await self.at_park():
                raise AlpacaException(PARKED, "Tracking: the telescope is parked")
            if await self.tracking():
                return
            await self._check_motion_allowed()
            self._park_state = "unparked"
            await self._send(50)
        else:
            if (await self.status(0)).mode == "hold":
                return
            await self._send(13)
            if self._parking:
                self._park_state = "unknown"

    async def slew(self, ra: float, dec: float) -> None:
        if not (0.0 <= ra < 24.0) or not (-90.0 <= dec <= 90.0):
            raise AlpacaException(INVALID_VALUE, f"RA {ra} h / Dec {dec} deg out of range")
        if await self.at_park():
            raise AlpacaException(PARKED, "slew: the telescope is parked")
        if not await self.tracking():
            raise AlpacaException(INVALID_OPERATION, "slew: tracking is off")
        await self._check_motion_allowed()
        m = await self.mount(0)
        alt, _ = hadec_to_altaz(m.lst_hours - ra, dec, self.config.latitude)
        if alt < self.config.min_alt:
            raise AlpacaException(
                INVALID_OPERATION,
                f"slew: target altitude {alt:.1f} deg is below min_alt {self.config.min_alt}",
            )
        self.target_ra, self.target_dec = ra, dec
        await self._send(6, ra, dec, self.config.equinox)
        st = await self.status(0)
        if st.target_out_of_range:
            raise AlpacaException(INVALID_OPERATION, "slew: the TCS says target out of range")
        self._park_state = "unparked"
        self._moving_since = time.monotonic()
        await self._send(12, 2)
        log.info("slewing to RA %.5f h, Dec %.4f deg (J%.1f)", ra, dec, self.config.equinox)

    async def abort(self) -> None:
        self._moving_since = -1e9
        if self._parking:
            await self._send(13)
            self._park_state = "unknown"
            return
        st = await self.status(0)
        if st.mode == "slew" or (st.mode == "chase" and not st.target_captured):
            await self._send(12, 1)  # follow from here: stop the slew, keep tracking

    async def park(self) -> None:
        if self._park_state == "parked" and await self.at_park():
            return
        await self._check_motion_allowed()
        self._park_state = "parking"
        self._moving_since = time.monotonic()
        if self.config.park_command == "park":
            await self._send(58)
        else:
            await self._send(10)
            await self._send(12, 2)
        log.info("parking (%s)", self.config.park_command)

    async def unpark(self) -> None:
        await self._send(59)
        self._park_state = "unparked"

    async def pulse_guide(self, direction: int, ms: int) -> None:
        if direction not in (0, 1, 2, 3):
            raise AlpacaException(INVALID_VALUE, f"PulseGuide direction {direction}")
        if not 0 <= ms <= self.config.max_pulse_ms:
            raise AlpacaException(INVALID_VALUE, f"PulseGuide duration {ms} ms")
        if await self.at_park():
            raise AlpacaException(PARKED, "PulseGuide: the telescope is parked")
        # ASCOM guideNorth=0, South=1, East=2, West=3; the TCS uses the same.
        tcs_dir = {0: NORTH, 1: SOUTH, 2: EAST, 3: WEST}[direction]
        await self._send(61, tcs_dir, ms)
        self._pulse_until = max(self._pulse_until, time.monotonic() + ms / 1000.0)

    async def is_pulse_guiding(self) -> bool:
        if time.monotonic() < self._pulse_until:
            return True
        return (await self.status()).pulse_guiding

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
            raise AlpacaException(
                INVALID_VALUE, f'Offset ({east}, {north})" exceeds max_offset {cfg.max_offset}"'
            )
        if await self.at_park():
            raise AlpacaException(PARKED, "Offset: the telescope is parked")
        st = await self.status(0)
        if st.mode not in ("follow", "chase"):
            raise AlpacaException(INVALID_OPERATION, f"Offset: mount is in {st.mode} mode")
        await self._send(7, east, north)
        sent: list[list[float]] = [[7, east, north]]
        if st.mode != "chase" and cfg.offset_mode is not None:
            self._moving_since = time.monotonic()
            await self._send(12, cfg.offset_mode)
            sent.append([12, cfg.offset_mode])
        log.info('offset east %.2f" north %.2f"', east, north)
        return json.dumps({"east": east, "north": north, "sent": sent})

    # -- focuser ----------------------------------------------------------------------

    async def focus_position(self) -> int:
        um = (await self._read(64, None))[0]
        return round(um - self.config.focus_min)

    async def focus_move(self, position: int) -> None:
        cfg = self.config
        if not 0 <= position <= cfg.focus_max - cfg.focus_min:
            raise AlpacaException(INVALID_VALUE, f"Move: {position} outside 0..MaxStep")
        st = await self.status(0)
        if not st.drives_switch_up:
            raise AlpacaException(INVALID_OPERATION, "focus: drives switch is down")
        self._focus_since = time.monotonic()
        await self._send(27, position + cfg.focus_min)

    async def focus_moving(self) -> bool:
        if time.monotonic() - self._focus_since < self.config.focus_settle:
            return True
        return (await self.status()).focus_moving

    # -- dome ---------------------------------------------------------------------------

    async def shutter_status(self) -> int:
        st = await self.status()
        if st.upper_shutter_open and st.lower_shutter_open:
            if self._shutter_cmd == "open":
                self._shutter_cmd = None
            return OPEN
        if st.upper_shutter_closed and st.lower_shutter_closed:
            if self._shutter_cmd == "close":
                self._shutter_cmd = None
            return CLOSED
        if self._shutter_cmd == "open":
            return OPENING
        if self._shutter_cmd == "close":
            return CLOSING
        return ERROR

    async def open_shutter(self) -> None:
        await self._check_motion_allowed()
        self._shutter_cmd = "open"
        for which in self.config.shutter_open_order:
            await self._send(36 if which == "upper" else 44, 1)

    async def close_shutter(self) -> None:
        self._shutter_cmd = "close"
        for which in self.config.shutter_close_order:
            await self._send(36 if which == "upper" else 44, 0)

    async def dome_abort(self) -> None:
        self._shutter_cmd = None
        for n in (35, 40, 41):
            await self._send(n)

    async def dome_azimuth(self) -> float:
        return (await self._read(62, None))[0] % 360.0

    async def dome_slewing(self) -> bool:
        st = await self.status()
        if st.dome_rotating or st.dome_find_home_mode:
            return True
        return await self.shutter_status() in (OPENING, CLOSING)

    async def dome_slew(self, az: float) -> None:
        if not 0.0 <= az < 360.0:
            raise AlpacaException(INVALID_VALUE, f"azimuth {az} outside 0..360")
        await self._send(43, az)
        await self._send(20, 2)

    async def dome_slaved(self) -> bool:
        return (await self.status()).dome_track_mode

    async def set_dome_slaved(self, on: bool) -> None:
        await self._send(20, 1) if on else await self._send(35)

    async def dome_find_home(self) -> None:
        await self._send(20, 3)


# -- Alpaca over HTTP ------------------------------------------------------------------

COMMON_NAMES = frozenset(
    {
        "connected",
        "connecting",
        "connect",
        "disconnect",
        "description",
        "driverinfo",
        "driverversion",
        "interfaceversion",
        "name",
        "supportedactions",
    }
)
TELESCOPE_CAN = {
    "canfindhome": False,
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
    "canslewaltazasync": False,
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
        "slewtoaltazasync",
        "synctocoordinates",
        "synctotarget",
        "synctoaltaz",
        "findhome",
        "setpark",
        "commandblind",
        "commandbool",
        "commandstring",
    }
)
TELESCOPE_ACTIONS = ("Offset", "Status", "Faults")
DOME_CAN = {
    "canfindhome": True,
    "canpark": False,
    "cansetaltitude": False,
    "cansetazimuth": True,
    "cansetpark": False,
    "cansetshutter": True,
    "canslave": True,
    "cansyncazimuth": False,
}
DOME_NOT_IMPLEMENTED_PUT = frozenset(
    {
        "park",
        "setpark",
        "slewtoaltitude",
        "synctoazimuth",
        "commandblind",
        "commandbool",
        "commandstring",
    }
)
INTERFACE = {"telescope": 4, "focuser": 4, "dome": 3}


def _float_param(params: dict[str, str], name: str) -> float:
    if name not in params:
        raise _BadRequest(f"missing parameter {name}")
    try:
        v = float(params[name])
    except ValueError:
        raise _BadRequest(f"{name}={params[name]!r} is not a number") from None
    if not math.isfinite(v):
        raise _BadRequest(f"{name}={params[name]!r} is not finite")
    return v


def _int_param(params: dict[str, str], name: str) -> int:
    v = _float_param(params, name)
    if not v.is_integer():
        raise _BadRequest(f"{name}={params[name]!r} is not an integer")
    return int(v)


def _now_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


class _Handler(BaseHTTPRequestHandler):
    server: TCSGOServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = (json.dumps(body) if ctype == "application/json" else str(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        if self.command == "GET":
            params = {k.lower(): v[0] for k, v in query.items()}
        else:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            params = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        parts = [p for p in url.path.split("/") if p]
        lower = [p.lower() for p in parts]
        srv = self.server
        if lower[:1] == ["management"] and self.command == "GET":
            ctid = _uint32(params.get("clienttransactionid"))
            value = srv.management(lower[1:])
            if value is None:
                self._send(400, f"unknown management path {url.path}", "text/plain")
            else:
                self._send(200, _alpaca(value, ctid=ctid, stid=srv.next_stid()))
            return
        if lower[:1] == ["setup"]:
            self._send(200, srv.setup_page(), "text/html")
            return
        if parts != lower or len(lower) != 5 or lower[:2] != ["api", "v1"]:
            self._send(400, f"unknown path {url.path}", "text/plain")
            return
        # Device 0 is served under its type's name, any other number N as
        # "type/N" (a server with two devices of one type lists both).
        device = lower[2] if lower[3] == "0" else f"{lower[2]}/{lower[3]}"
        if device not in srv.devices:
            self._send(400, f"no device {parts[2]}/{parts[3]}", "text/plain")
            return
        member = lower[4]
        get = (lambda k: params.get(k.lower())) if self.command == "GET" else params.get
        ctid = _uint32(get("ClientTransactionID"))
        client = _uint32(get("ClientID"))
        stid = srv.next_stid()
        try:
            value = srv.member(device, self.command, member, params, client)
        except _BadRequest as exc:
            self._send(400, str(exc), "text/plain")
            return
        except AlpacaException as exc:
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=exc.number, msg=str(exc)))
            return
        except Exception as exc:  # never a traceback to the client
            log.exception("%s %s %s", device, self.command, member)
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=DRIVER_ERROR, msg=repr(exc)))
            return
        self._send(200, _alpaca(value, ctid=ctid, stid=stid))


class TCSGOServer(ThreadingHTTPServer):
    """The Alpaca server. The TCS client and logic live on a private event
    loop thread; HTTP requests (threads) submit to it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: TCSGOConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.allow_hardware != hardware:
            log.info(
                "hardware use needs allow_hardware in the config AND --hardware;"
                " only a loopback TCS is allowed now"
            )
        self.tcs_ip = check_tcs_address(config.tcs.host, allow_hardware=allow)
        if allow:
            log.warning("HARDWARE MODE: TCS at %s:%d", self.tcs_ip, config.tcs.port)
        host, _, port = config.listen.rpartition(":")
        super().__init__((host, int(port)), _Handler)
        self.config = config
        self._allow = allow
        self.devices = ("telescope",) + (("focuser",) if config.focuser else ())
        self.devices += ("dome",) if config.dome else ()
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="tcsgo-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self.tcsgo: TCSGO = self._run_sync(self._build())

    async def _build(self) -> TCSGO:
        t = self.config.tcs
        client = TCSClient(
            self.tcs_ip,
            t.port,
            allow_hardware=self._allow,
            connect_timeout=t.connect_timeout,
            command_timeout=t.command_timeout,
            ack_window=t.ack_window,
            min_interval=t.min_interval,
            max_attempts=t.max_attempts,
        )
        return TCSGO(client, self.config)

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
                TCS_TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except TCSTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(TCS_TIMEOUT, str(exc)) from None
        except TCSError as exc:
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
                "ServerName": f"robofast TCSGO ({name})",
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
                    "UniqueID": f"robofast-tcsgo-{name}-{d}-0",
                }
                for d in self.devices
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>TCSGO</title></head>"
            f"<body><h1>robofast TCSGO: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members --------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        if value:
            if not self.connected(device, client):
                self._call(lambda: self.tcsgo.status(0), "Connect")
                with self._clients_lock:
                    self._clients.add((device, client))
        else:
            with self._clients_lock:
                self._clients.discard((device, client))

    def _common(self, device: str, member: str, client: int) -> Any:
        cfg = self.config
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": f"{cfg.name} {device} via DFM TCSGalil",
            "driverinfo": f"robofast_core.devices.tcsgo_server: Alpaca {device} for DFM TCSGalil",
            "driverversion": DRIVER_VERSION,
            "interfaceversion": INTERFACE[device],
            "name": f"robofast TCSGO {cfg.name} {device}",
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
        return self._dome(verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    @staticmethod
    def _unknown(verb: str, member: str) -> Any:
        raise _BadRequest(f"{verb} {member}: no such member")

    def _telescope(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        t = self.tcsgo
        if verb == "GET":
            if member in TELESCOPE_CAN:
                return TELESCOPE_CAN[member]
            if member == "canmoveaxis":
                axis = _int_param(params, "axis")
                if axis not in (0, 1, 2):
                    raise AlpacaException(INVALID_VALUE, f"axis {axis}")
                return False
            if member == "axisrates":
                axis = _int_param(params, "axis")
                if axis not in (0, 1, 2):
                    raise AlpacaException(INVALID_VALUE, f"axis {axis}")
                return []
            static = {
                "alignmentmode": 1,  # algPolar (not German: no pier side)
                "equatorialsystem": 2,  # equJ2000
                "declinationrate": 0.0,
                "rightascensionrate": 0.0,
                "sitelatitude": cfg.latitude,
                "sitelongitude": cfg.longitude,
                "siteelevation": cfg.elevation,
                "slewsettletime": 0,
                "trackingrate": 0,  # driveSidereal
                "trackingrates": [0],
                "athome": False,
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
            if member == "rightascension":
                return self._call(lambda: t.radec_j2000(), member)[0]
            if member == "declination":
                return self._call(lambda: t.radec_j2000(), member)[1]
            if member in ("altitude", "azimuth", "siderealtime"):
                m = self._call(t.mount, member)
                return {
                    "altitude": m.altitude_deg,
                    "azimuth": m.azimuth_deg,
                    "siderealtime": m.lst_hours,
                }[member]
            if member == "atpark":
                return self._call(t.at_park, member)
            if member == "slewing":
                return self._call(t.slewing, member)
            if member == "tracking":
                return self._call(t.tracking, member)
            if member == "ispulseguiding":
                return self._call(t.is_pulse_guiding, member)
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
            return self._call(lambda: t.slew(ra, dec), "SlewToCoordinatesAsync")
        if member == "slewtotargetasync":
            if t.target_ra is None or t.target_dec is None:
                raise AlpacaException(INVALID_OPERATION, "SlewToTargetAsync: no target set")
            ra, dec = t.target_ra, t.target_dec
            return self._call(lambda: t.slew(ra, dec), "SlewToTargetAsync")
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
        t = self.tcsgo
        key = name.lower()
        if key == "offset":
            return self._call(lambda: t.offset(parameters), "Offset")
        if key == "status":
            return json.dumps(self._call(t.status_report, "Status"))
        if key == "faults":
            return json.dumps(self._call(t.faults, "Faults"))
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _telescope_state(self) -> list[dict[str, Any]]:
        t = self.tcsgo
        state: list[dict[str, Any]] = []
        for name, fn in (
            ("AtPark", t.at_park),
            ("Slewing", t.slewing),
            ("Tracking", t.tracking),
            ("IsPulseGuiding", t.is_pulse_guiding),
        ):
            try:
                state.append({"Name": name, "Value": self._call(fn, name)})
            except AlpacaException as exc:
                log.warning("DeviceState without %s: %s", name, exc)
        try:
            m = self._call(t.mount, "DeviceState")
            ra, dec = self._call(t.radec_j2000, "DeviceState")
            state += [
                {"Name": "Altitude", "Value": m.altitude_deg},
                {"Name": "Azimuth", "Value": m.azimuth_deg},
                {"Name": "RightAscension", "Value": ra},
                {"Name": "Declination", "Value": dec},
                {"Name": "SiderealTime", "Value": m.lst_hours},
                {"Name": "UTCDate", "Value": _now_stamp() + "Z"},
            ]
        except AlpacaException as exc:
            log.warning("DeviceState without coordinates: %s", exc)
        state.append({"Name": "AtHome", "Value": False})
        state.append({"Name": "TimeStamp", "Value": _now_stamp()})
        return state

    def _focuser(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        t = self.tcsgo
        span = round(cfg.focus_max - cfg.focus_min)
        if verb == "GET":
            static = {
                "absolute": True,
                "maxincrement": span,
                "maxstep": span,
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
        if member == "tempcomp":
            if _bool_param(params, "TempComp"):
                raise AlpacaException(NOT_IMPLEMENTED, "no temperature compensation")
            return None
        if member == "halt":
            raise AlpacaException(NOT_IMPLEMENTED, "Halt: the TCS has no documented focus stop")
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no focuser actions")
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    def _dome(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        t = self.tcsgo
        if verb == "GET" and member in DOME_CAN:
            return DOME_CAN[member]
        if verb == "GET" and member == "altitude":
            raise AlpacaException(NOT_IMPLEMENTED, "Altitude is not implemented")
        self._need("dome", member, client)
        if verb == "GET":
            if member == "shutterstatus":
                return self._call(t.shutter_status, member)
            if member == "azimuth":
                return self._call(t.dome_azimuth, member)
            if member == "athome":
                return self._call(t.status, member).dome_at_home
            if member == "atpark":
                return False
            if member == "slaved":
                return self._call(t.dome_slaved, member)
            if member == "slewing":
                return self._call(t.dome_slewing, member)
            if member == "devicestate":
                state: list[dict[str, Any]] = [{"Name": "AtPark", "Value": False}]
                for name, fn in (
                    ("ShutterStatus", t.shutter_status),
                    ("Azimuth", t.dome_azimuth),
                    ("Slewing", t.dome_slewing),
                ):
                    with contextlib.suppress(AlpacaException):
                        state.append({"Name": name, "Value": self._call(fn, name)})
                with contextlib.suppress(AlpacaException):
                    st = self._call(t.status, "AtHome")
                    state.append({"Name": "AtHome", "Value": st.dome_at_home})
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)
        if member == "openshutter":
            return self._call(t.open_shutter, "OpenShutter")
        if member == "closeshutter":
            return self._call(t.close_shutter, "CloseShutter")
        if member == "abortslew":
            return self._call(t.dome_abort, "AbortSlew")
        if member == "slewtoazimuth":
            az = _float_param(params, "Azimuth")
            return self._call(lambda: t.dome_slew(az), "SlewToAzimuth")
        if member == "findhome":
            return self._call(t.dome_find_home, "FindHome")
        if member == "slaved":
            on = _bool_param(params, "Slaved")
            return self._call(lambda: t.set_dome_slaved(on), "Slaved")
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no dome actions")
        if member in DOME_NOT_IMPLEMENTED_PUT:
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> TCSGOServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="tcsgo-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.tcsgo.tcs.aclose(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a non-loopback TCS (the config must also say allow_hardware)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = TCSGOConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    try:
        server = TCSGOServer(config, hardware=args.hardware)
    except HardwareNotAllowed as exc:
        log.error("%s", exc)
        sys.exit(2)
    server.start()
    print(
        json.dumps(
            {"url": f"http://{server.address}", "tcs": f"{server.tcs_ip}:{config.tcs.port}"}
        ),
        flush=True,
    )
    stop = threading.Event()
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
