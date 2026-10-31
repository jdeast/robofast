"""Async client for the DFM Engineering TCS ("TCSGO" / TCSGalil) external command port.

The FLWO 60-inch (Tillinghast) telescope is run by a DFM TCSGalil. Its
external command protocol is described in ``docs/tcsgo.md``, from DFM's
"TCSGalil Optimized 2.0 Operations Manual", revision 1.3, Appendix A, and
from the TRES front-end code that drove it. Parts of this module are adapted
from jdeast/tres-guider (J. Eastman, J. Kansky; ``telescope.py``,
``tcsserver.py``), relicensed into robofast (BSD-3) with Jan Kansky's
agreement.

The wire, as far as it is known
-------------------------------
* TCP (or serial) to TCSGalil; the legacy client opened one TCP connection
  per command and closed it after the reply (``telescope.py:send``), and its
  relay (``tcsserver.py``) serialized commands with a lock.
* A command is ``#`` + numbers separated by ``,`` + ``;`` + carriage return,
  e.g. ``#6,18.3457,35.3456,2000.0;\\r``. Every argument is a float; values
  are truncated where needed.
* Only queries (``#25``, ``#26``, ``#32``, ``#62``, ``#63``, ``#64``, ``#28``)
  return data: ``#v1,v2,...;`` (status example: ``#58,0,30,0,12,94,24,0;``).
  Every other command is documented "Data returned: NONE". *Unknown*: whether
  TCSGalil sends any acknowledgement or error for them, and what it does
  with a malformed or unknown command. This client therefore waits a short
  ``ack_window`` after a set command, logs anything that arrives, and treats
  silence as "accepted"; the effect is verified by reading status.
* Legacy notes say rapid-fire commands "tank" the server ("no more than 1
  per second") and that a stuck server needs TCSGalil restarting. So
  commands are spaced at least ``min_interval`` apart, one at a time.

This client
-----------
* Only commands in :data:`ALLOWED` can be sent. Commands that rewrite the
  encoder zero points, the pointing model, the park position or the
  handpaddle setup (``#2``, ``#3``, ``#5``, ``#14``-``#16``, ``#22``-``#24``,
  ``#30``, ``#31``, ``#37``, ``#42``, ``#53``-``#56``, ``#60``, ``#66``-``#70``)
  are documented in :data:`COMMANDS` but never sent by robofast.
* Every exchange has a hard deadline (connect, write, reply). Queries are
  retried at most ``max_attempts`` times in a loop (no recursion); a set
  command is re-sent only if it never reached the wire (the connection
  failed), and the relative ones (``#7`` offset, ``#61`` pulse guide) never.
* Commands run one at a time from a priority queue: "urgent" ones (stop the
  mount, the dome, the shutters; park) go before queued routine ones and
  cut short a query in flight.
* :func:`check_tcs_address` refuses a non-loopback TCS unless hardware use
  is explicitly allowed (the server requires a config flag *and* a CLI flag).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import logging
import re
import socket
import time
from dataclasses import dataclass, fields

from robofast_core.owner import DeviceError, DeviceTimeout

log = logging.getLogger(__name__)


# -- the command set ------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One TCSGalil command (manual Appendix A)."""

    number: int
    name: str
    #: Argument names (empty: the command number alone).
    args: tuple[str, ...] = ()
    #: Number of values a query returns; 0 for "Data returned: NONE".
    returns: int = 0
    #: Starts, stops or redirects motion of mount, dome, shutters, focus or
    #: mirror doors (see docs/tcsgo.md, "Commands that move hardware").
    moves: bool = False
    #: Sending it twice asks for no more than sending it once.
    idempotent: bool = True
    #: Goes ahead of queued routine commands and cuts short a query in flight.
    urgent: bool = False


_C = Command
#: Every command in the manual (v1.3, Appendix A), plus the two undocumented
#: ones the legacy client used (#72, #73). Not all may be sent: see ALLOWED.
COMMANDS: dict[int, Command] = {
    c.number: c
    for c in (
        _C(2, "INITIALIZE DOME POSITION", ("azimuth_deg",)),
        _C(3, "INITIALIZE MOUNT POSITION", ("ra_hours", "dec_deg", "equinox")),
        _C(4, "INITIALIZE INSTRUMENT ROTATOR", ("angle_deg",)),
        _C(5, "INITIALIZE FOCUS POSITION", ("position_um",)),
        _C(6, "SET TARGET OBJECT", ("ra_hours", "dec_deg", "equinox")),
        _C(7, "OFFSET TARGET OBJECT", ("east_arcsec", "north_arcsec"), idempotent=False),
        _C(8, "SET TARGET OBJECT FROM LIBRARY", ("object",)),
        _C(9, "SET TARGET OBJECT FROM TABLE", ("line",)),
        _C(10, "SET TARGET OBJECT TO ZENITH"),
        _C(12, "SET MOUNT MODE", ("mode",), moves=True),
        _C(13, "STOP MOUNT", moves=True, urgent=True),
        _C(14, "SET TRACK RATES", ("ha_rate", "dec_rate", "aux_ha_rate", "aux_dec_rate")),
        _C(15, "CHANGE GUIDE RATE", ("arcsec_per_s",)),
        _C(16, "CHANGE SET RATE", ("arcsec_per_s",)),
        _C(18, "USE COSINE OF DECLINATION", ("on",)),
        _C(19, "USE RATE CORRECTIONS", ("on",)),
        _C(20, "SET DOME MODE", ("mode",), moves=True),
        _C(22, "SET DISPLAY EQUINOX", ("equinox",)),
        _C(23, "SET TABLE OBJECT", ("line", "ra_hours", "dec_deg", "equinox")),
        _C(24, "CHANGE POINTING COEFFICIENTS", tuple(f"c{i}" for i in range(17))),
        _C(25, "READ MOUNT COORDINATES", returns=10),
        _C(26, "READ TCS STATUS", returns=8),
        _C(27, "MOVE FOCUS", ("position_um",), moves=True),
        _C(28, "READ POINT", returns=6),
        _C(29, "ACTUATE MIRROR DOORS", ("open",), moves=True),
        _C(30, "SET ENCODER OFFSETS FOR ZENITH"),
        _C(31, "SET ENCODER OFFSETS TO DEFAULTS"),
        _C(32, "READ TARGET OBJECT COORDINATES", returns=10),
        _C(35, "DOME STOP ROTATION", moves=True, urgent=True),
        _C(36, "ACTUATE DOME UPPER SHUTTER", ("open",), moves=True),
        _C(37, "INITIALIZE FOCUS ENCODER", moves=True),
        _C(
            38,
            "SET CELESTIAL TRAJECTORY",
            ("ra", "dec", "equinox", "ra_rate", "dec_rate", "ra_acc", "dec_acc", "start"),
        ),
        _C(39, "SET MOUNT TRAJECTORY", ("ha", "dec", "ha_rate", "dec_rate", "ha_acc", "dec_acc")),
        _C(40, "DOME STOP UPPER SHUTTER", moves=True, urgent=True),
        _C(41, "DOME STOP LOWER SHUTTER", moves=True, urgent=True),
        _C(42, "SET PIER SIDE", ("east",)),
        _C(43, "SET DOME AZIMUTH TARGET", ("azimuth_deg",)),
        _C(44, "ACTUATE DOME LOWER SHUTTER", ("open",), moves=True),
        _C(50, "START TRACKING", moves=True),
        _C(51, "SET TRACKING MODE", ("normal",)),
        _C(53, "SET SLEW RATE", ("arcsec_per_s",)),
        _C(54, "SET SLOW FOCUS RATE", ("percent",)),
        _C(55, "SET FAST FOCUS RATE", ("percent",)),
        _C(56, "INITIALIZE TELESCOPE ALT AZ", ("alt_deg", "az_deg")),
        _C(57, "SET TARGET OBJECT ALT AZ", ("alt_deg", "az_deg")),
        _C(58, "PARK MOUNT", moves=True, urgent=True),
        _C(59, "UNPARK MOUNT"),
        _C(60, "SET MOUNT PARKING POSITION"),
        _C(61, "PULSE GUIDE", ("direction", "duration_ms"), moves=True, idempotent=False),
        _C(62, "READ DOME AZIMUTH", returns=1),
        _C(63, "READ INSTRUMENT ROTATOR AZIMUTH", returns=1),
        _C(64, "READ FOCUSER POSITION", returns=1),
        _C(66, "APPLY MOUNT CORRECTIONS", ("on",)),
        _C(67, "APPLY MOUNT RATE CORRECTIONS", ("on",)),
        _C(68, "APPLY REFRACTION CORRECTIONS", ("on",)),
        _C(69, "APPLY REFRACTION RATE CORRECTIONS", ("on",)),
        _C(70, "APPLY PERIODIC ERROR CORRECTIONS", ("on",)),
        # Undocumented in manual v1.3; used by the legacy client.
        _C(72, "STOP FOCUS (undocumented)", moves=True, urgent=True),
        _C(73, "DOME CONTINUOUS ROTATION (undocumented)", ("direction",), moves=True),
    )
}

#: What robofast may send. Reads; target setting; mount mode, stop, tracking,
#: park/unpark; offsets and pulse guiding; focus moves; the dome and its
#: shutters. Nothing that rewrites encoders, the pointing model, the park
#: position, rates or other TCS configuration.
ALLOWED = frozenset(
    {6, 7, 10, 12, 13, 20, 25, 26, 27, 32, 35, 36, 40, 41, 43, 44, 50, 58, 59, 61, 62, 64}
)

MOUNT_MODES = {0: "hold", 1: "follow", 2: "slew", 3: "chase"}
DOME_MODES = {0: "manual", 1: "track", 2: "goto", 3: "home"}
#: #61 directions.
NORTH, SOUTH, EAST, WEST = 0, 1, 2, 3


# -- errors -------------------------------------------------------------------


class TCSError(DeviceError):
    """Talking to the TCS failed."""


class TCSTimeout(TCSError, DeviceTimeout):
    """No complete reply within the command's deadline."""


class TCSProtocolError(TCSError):
    """A reply that cannot be understood (garbled, truncated, wrong length)."""


class HardwareNotAllowed(Exception):
    """A non-loopback TCS address without explicit permission."""


def check_tcs_address(host: str, *, allow_hardware: bool) -> str:
    """Resolve ``host`` once and return the IP literal to connect to.

    Unless ``allow_hardware``, every address ``host`` resolves to must be
    loopback; otherwise :class:`HardwareNotAllowed`.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HardwareNotAllowed(f"cannot resolve TCS host {host!r}: {exc}") from exc
    addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if not allow_hardware and not all(a.is_loopback for a in addrs):
        raise HardwareNotAllowed(
            f"TCS host {host!r} ({', '.join(map(str, addrs))}) is not loopback: refusing to"
            " talk to real hardware without allow_hardware in the config AND --hardware"
        )
    return str(addrs[0])


# -- encoding and parsing ----------------------------------------------------------


def _num(x: float) -> str:
    x = float(x)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"not a finite number: {x}")
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.7f}".rstrip("0").rstrip(".")


def encode(number: int, *args: float) -> bytes:
    """``#number,arg,...;\\r`` for a known command with the right arity."""
    spec = COMMANDS.get(number)
    if spec is None:
        raise ValueError(f"#{number} is not a TCSGalil command")
    if len(args) != len(spec.args):
        raise TypeError(f"#{number} {spec.name} takes {spec.args}, got {len(args)} arguments")
    return ("#" + ",".join([str(number), *map(_num, args)]) + ";\r").encode("ascii")


_REPLY = re.compile(rb"^\s*#?\s*([^;#]*?)\s*;\s*$")


def parse_reply(number: int, raw: bytes) -> tuple[float, ...]:
    """The values of a query reply (``#v1,...,vn;``); :class:`TCSProtocolError`
    unless it has exactly the documented number of numeric fields."""
    spec = COMMANDS[number]
    m = _REPLY.match(raw)
    if m is None:
        raise TCSProtocolError(f"#{number}: malformed reply {raw[:120]!r}")
    parts = m.group(1).split(b",") if m.group(1) else []
    if len(parts) != spec.returns:
        raise TCSProtocolError(
            f"#{number}: expected {spec.returns} values, got {len(parts)}: {raw[:120]!r}"
        )
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        raise TCSProtocolError(f"#{number}: non-numeric reply {raw[:120]!r}") from None


# -- typed replies ------------------------------------------------------------------

#: (byte, bit) -> TCSStatus field, from the manual's #26 table.
STATUS_BITS: dict[tuple[int, int], str] = {
    (0, 0): "drives_switch_up",
    (0, 1): "track_switch_up",
    (0, 2): "aux_track_switch_up",
    (0, 3): "dome_track_switch_up",
    (0, 4): "auto_dome_switch_up",
    (0, 5): "amplifiers_switch_up",
    (1, 0): "paddle_north",
    (1, 1): "paddle_south",
    (1, 2): "paddle_east",
    (1, 3): "paddle_west",
    (1, 4): "paddle_set",
    (1, 5): "paddle_slew",
    (1, 6): "pulse_guiding",
    (2, 0): "horizon_limit",
    (2, 1): "east_limit",
    (2, 2): "west_limit",
    (2, 3): "north_limit",
    (2, 4): "south_limit",
    (2, 5): "target_out_of_range",
    (2, 6): "approaching_soft_limit",
    (2, 7): "soft_limit",
    (3, 0): "dome_find_home_mode",
    (3, 1): "dome_goto_mode",
    (3, 2): "dome_track_mode",
    (3, 3): "dome_rotating_left",
    (3, 4): "dome_rotating_right",
    (3, 5): "dome_at_home",
    (3, 6): "dome_manual_mode",
    (4, 0): "focus_moving_in",
    (4, 1): "focus_moving_out",
    (4, 2): "focus_outer_limit",
    (4, 3): "focus_inner_limit",
    (4, 4): "rotator_ccw",
    (4, 5): "rotator_cw",
    (5, 0): "serial_ready",
    (5, 1): "tcp_connected",
    (5, 2): "use_cos_dec",
    (5, 3): "use_rate_corrections",
    (5, 4): "pier_east",
    (5, 5): "pier_west",
    (5, 6): "galil_stale",
    (6, 0): "mode_chase",
    (6, 1): "mode_slew",
    (6, 2): "mode_follow",
    (6, 3): "mode_hold",
    (6, 4): "target_captured",
    (7, 0): "upper_shutter_open",
    (7, 1): "upper_shutter_closed",
    (7, 2): "lower_shutter_open",
    (7, 3): "lower_shutter_closed",
    (7, 4): "mirror_doors_open",
    (7, 5): "mirror_doors_closed",
}
_BY_NAME = {name: key for key, name in STATUS_BITS.items()}
#: Limit flags: any of these set means the mount is at (or past) a limit.
LIMIT_FLAGS = (
    "horizon_limit",
    "east_limit",
    "west_limit",
    "north_limit",
    "south_limit",
    "soft_limit",
)


@dataclass(frozen=True)
class TCSStatus:
    """A parsed ``#26`` reply: eight bytes of flags (see :data:`STATUS_BITS`)."""

    raw: tuple[int, ...]
    drives_switch_up: bool = False
    track_switch_up: bool = False
    aux_track_switch_up: bool = False
    dome_track_switch_up: bool = False
    auto_dome_switch_up: bool = False
    amplifiers_switch_up: bool = False
    paddle_north: bool = False
    paddle_south: bool = False
    paddle_east: bool = False
    paddle_west: bool = False
    paddle_set: bool = False
    paddle_slew: bool = False
    pulse_guiding: bool = False
    horizon_limit: bool = False
    east_limit: bool = False
    west_limit: bool = False
    north_limit: bool = False
    south_limit: bool = False
    target_out_of_range: bool = False
    approaching_soft_limit: bool = False
    soft_limit: bool = False
    dome_find_home_mode: bool = False
    dome_goto_mode: bool = False
    dome_track_mode: bool = False
    dome_rotating_left: bool = False
    dome_rotating_right: bool = False
    dome_at_home: bool = False
    dome_manual_mode: bool = False
    focus_moving_in: bool = False
    focus_moving_out: bool = False
    focus_outer_limit: bool = False
    focus_inner_limit: bool = False
    rotator_ccw: bool = False
    rotator_cw: bool = False
    serial_ready: bool = False
    tcp_connected: bool = False
    use_cos_dec: bool = False
    use_rate_corrections: bool = False
    pier_east: bool = False
    pier_west: bool = False
    galil_stale: bool = False
    mode_chase: bool = False
    mode_slew: bool = False
    mode_follow: bool = False
    mode_hold: bool = False
    target_captured: bool = False
    upper_shutter_open: bool = False
    upper_shutter_closed: bool = False
    lower_shutter_open: bool = False
    lower_shutter_closed: bool = False
    mirror_doors_open: bool = False
    mirror_doors_closed: bool = False

    @classmethod
    def from_values(cls, values: tuple[float, ...]) -> TCSStatus:
        if len(values) != 8:
            raise TCSProtocolError(f"#26: expected 8 bytes, got {len(values)}")
        raw: list[int] = []
        for v in values:
            if not float(v).is_integer() or not 0 <= v <= 255:
                raise TCSProtocolError(f"#26: {v!r} is not a byte")
            raw.append(int(v))
        flags = {name: bool(raw[b] >> bit & 1) for (b, bit), name in STATUS_BITS.items()}
        return cls(raw=tuple(raw), **flags)

    @staticmethod
    def encode(**flags: bool) -> tuple[int, ...]:
        """The eight bytes for the named flags (for the simulator and tests)."""
        out = [0] * 8
        for name, on in flags.items():
            b, bit = _BY_NAME[name]
            if on:
                out[b] |= 1 << bit
        return tuple(out)

    @property
    def mode(self) -> str:
        """``chase``, ``slew``, ``follow``, ``hold``, or ``unknown`` (no bit,
        or more than one)."""
        on = [m for m in ("chase", "slew", "follow", "hold") if getattr(self, f"mode_{m}")]
        return on[0] if len(on) == 1 else "unknown"

    @property
    def limits(self) -> list[str]:
        return [f for f in LIMIT_FLAGS if getattr(self, f)]

    @property
    def focus_moving(self) -> bool:
        return self.focus_moving_in or self.focus_moving_out

    @property
    def dome_rotating(self) -> bool:
        return self.dome_rotating_left or self.dome_rotating_right

    def as_dict(self) -> dict[str, object]:
        d: dict[str, object] = {f.name: getattr(self, f.name) for f in fields(self)}
        d["raw"] = list(self.raw)
        d["mode"] = self.mode
        d["limits"] = self.limits
        return d


@dataclass(frozen=True)
class MountCoords:
    """A parsed ``#25`` reply (mean RA/Dec in ``equinox``)."""

    ha_hours: float
    ra_hours: float
    dec_deg: float
    equinox: float
    airmass: float
    zenith_distance_deg: float
    azimuth_deg: float
    lst_hours: float
    ut_hours: float
    date_years: float

    @classmethod
    def from_values(cls, v: tuple[float, ...]) -> MountCoords:
        return cls(*v)

    @property
    def altitude_deg(self) -> float:
        return 90.0 - self.zenith_distance_deg

    def as_dict(self) -> dict[str, float]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["altitude_deg"] = self.altitude_deg
        return d


@dataclass(frozen=True)
class TargetCoords:
    """A parsed ``#32`` reply."""

    elapsed_s: float
    ha_hours: float
    ra_hours: float
    dec_deg: float
    equinox: float
    ra_rate_arcsec_s: float
    dec_rate_arcsec_s: float
    airmass: float
    zenith_distance_deg: float
    azimuth_deg: float

    @classmethod
    def from_values(cls, v: tuple[float, ...]) -> TargetCoords:
        return cls(*v)

    def as_dict(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# -- client ---------------------------------------------------------------------


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    number: int = 0
    args: tuple[float, ...] = ()
    future: asyncio.Future | None = None


class TCSClient:
    """One TCSGalil at ``host:port``. Use from one event loop.

    ``host`` must be an IP literal that passed :func:`check_tcs_address`;
    the constructor re-checks loopback unless ``allow_hardware``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        allow_hardware: bool = False,
        connect_timeout: float = 2.0,
        command_timeout: float = 3.0,
        ack_window: float = 0.2,
        min_interval: float = 0.5,
        max_attempts: int = 2,
    ) -> None:
        addr = ipaddress.ip_address(host)  # ValueError for a hostname
        if not allow_hardware and not addr.is_loopback:
            raise HardwareNotAllowed(f"TCS {host} is not loopback")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.host, self.port = host, port
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.ack_window = ack_window
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self._queue: asyncio.PriorityQueue[_Job] | None = None
        self._worker: asyncio.Task | None = None
        self._seq = itertools.count()
        self._last_end = -1e9
        self._inflight: tuple[int, asyncio.Task] | None = None
        #: Diagnostics: exchanges made, retries, unexpected acknowledgements.
        self.exchanges = 0
        self.retries = 0
        self.acks: list[bytes] = []

    # -- public ------------------------------------------------------------

    async def command(
        self, number: int, *args: float, timeout: float | None = None
    ) -> tuple[float, ...]:
        """Send command ``number``; return a query's values (``()`` otherwise).

        ``timeout`` bounds the whole call, queueing included (default:
        ``command_timeout`` times ``max_attempts``, plus the spacing).
        """
        if number not in ALLOWED:
            raise ValueError(f"#{number} is not an allowed TCS command")
        encode(number, *args)  # arity and finiteness, before queueing
        spec = COMMANDS[number]
        if timeout is None:
            timeout = (self.command_timeout + self.min_interval) * self.max_attempts + 1.0
        self._ensure_worker()
        assert self._queue is not None
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        job = _Job(0 if spec.urgent else 1, next(self._seq), number, tuple(args), fut)
        if spec.urgent and self._inflight is not None:
            other, task = self._inflight
            if COMMANDS[other].returns and not task.done():
                log.info("#%d pre-empts query #%d", number, other)
                task.cancel()
        self._queue.put_nowait(job)
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(fut)
        except TimeoutError:
            fut.cancel()  # skipped if not yet started; runs to its own bound if it has
            raise TCSTimeout(
                f"#{number} {spec.name}: no completion within {timeout:.1f} s"
            ) from None

    async def status(self) -> TCSStatus:
        return TCSStatus.from_values(await self.command(26))

    async def mount(self) -> MountCoords:
        return MountCoords.from_values(await self.command(25))

    async def target(self) -> TargetCoords:
        return TargetCoords.from_values(await self.command(32))

    async def focus_position(self) -> float:
        return (await self.command(64))[0]

    async def dome_azimuth(self) -> float:
        return (await self.command(62))[0]

    async def aclose(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
            self._worker = None

    # -- the worker ----------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._queue = asyncio.PriorityQueue()
            self._worker = asyncio.ensure_future(self._work())

    async def _work(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            fut = job.future
            assert fut is not None
            if fut.done():  # its caller gave up before it started: never sent
                continue
            wait = self._last_end + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if fut.done():
                continue
            task = asyncio.ensure_future(self._run(job.number, job.args))
            self._inflight = (job.number, task)
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():  # the worker itself is being cancelled
                    task.cancel()
                    raise
                # Pre-empted by an urgent command (only queries are): queue it
                # again, behind the urgent one; its caller's deadline still holds.
                self._queue.put_nowait(job)
            except Exception as exc:
                if not fut.done():
                    fut.set_exception(exc)
            else:
                if not fut.done():
                    fut.set_result(result)
            finally:
                self._inflight = None
                self._last_end = time.monotonic()

    async def _run(self, number: int, args: tuple[float, ...]) -> tuple[float, ...]:
        """One command, with bounded retries (a loop, never recursion)."""
        spec = COMMANDS[number]
        line = encode(number, *args)
        attempts = self.max_attempts if spec.returns or spec.idempotent else 1
        last: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                self.retries += 1
                log.warning("#%d %s: retry %d after %s", number, spec.name, attempt, last)
                await asyncio.sleep(self.min_interval)
            try:
                return await self._exchange(spec, line)
            except _NotSent as exc:
                last = exc.cause
            except TCSError as exc:
                if not spec.returns:
                    raise  # it reached the wire: never re-send a set command
                last = exc
        assert last is not None
        raise last

    async def _exchange(self, spec: Command, line: bytes) -> tuple[float, ...]:
        what = f"#{spec.number} {spec.name}"
        try:
            async with asyncio.timeout(self.connect_timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError:
            raise _NotSent(TCSTimeout(f"{what}: connect timed out")) from None
        except OSError as exc:
            raise _NotSent(TCSError(f"{what}: cannot connect: {exc}")) from exc
        self.exchanges += 1
        try:
            try:
                writer.write(line)
                await writer.drain()
            except OSError as exc:
                raise TCSError(f"{what}: write failed: {exc!r}") from exc
            if not spec.returns:
                await self._drain_ack(reader, what)
                return ()
            try:
                async with asyncio.timeout(self.command_timeout):
                    raw = await reader.readuntil(b";")
            except TimeoutError:
                raise TCSTimeout(f"{what}: no reply within {self.command_timeout} s") from None
            except asyncio.IncompleteReadError as exc:
                raise TCSProtocolError(
                    f"{what}: connection closed mid-reply {exc.partial[:120]!r}"
                ) from None
            except asyncio.LimitOverrunError:
                raise TCSProtocolError(f"{what}: reply too long") from None
            except OSError as exc:
                raise TCSError(f"{what}: connection lost: {exc!r}") from exc
            return parse_reply(spec.number, raw)
        finally:
            writer.close()
            with contextlib.suppress(TimeoutError, OSError):
                async with asyncio.timeout(0.5):
                    await writer.wait_closed()

    async def _drain_ack(self, reader: asyncio.StreamReader, what: str) -> None:
        """Wait ``ack_window`` for an (undocumented) acknowledgement; log it."""
        data = b""
        deadline = time.monotonic() + self.ack_window
        while (left := deadline - time.monotonic()) > 0:
            try:
                async with asyncio.timeout(left):
                    chunk = await reader.read(256)
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            data += chunk
            if data.rstrip().endswith(b";"):
                break
        if data:
            self.acks.append(data)
            log.info("%s: unexpected reply %r (documented: none)", what, data[:120])


class _NotSent(Exception):
    """The command never reached the wire (safe to retry)."""

    def __init__(self, cause: TCSError) -> None:
        super().__init__(str(cause))
        self.cause = cause
