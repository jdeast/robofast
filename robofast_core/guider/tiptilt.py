"""Tip/tilt devices: what moves the star on the guide camera.

Every device is a :class:`~robofast_core.guider.base.TipTilt`: a position
``(a, b)`` in its own units, optional range limits, ``move_by`` and
``home``. Three real backends and their simulators:

Starlight Xpress AO (MINERVA T2; never commissioned)
----------------------------------------------------
:class:`SXAO`. RS-232, 9600 8N1 (the SX AO-L handbook, p. 12; legacy
``minerva_library/ao.py``). Commands are ASCII; the unit answers one
character:

* ``K`` -- centre the mirror; answers ``K`` when done.
* ``G`` + direction + five digits, e.g. ``GN00012`` -- move that many steps;
  directions ``N``/``S`` (axis a) and ``T``/``W`` (axis b), as the legacy
  driver used them. Answers ``G`` when done, or ``L`` if the mirror reached
  its mechanical limit (then it stops there).

Only these two are used, because they are the only ones the legacy driver
exercised; the handbook lists more (not confirmed here). Whether the unit
wants a line terminator is not confirmed either: the legacy driver sent
``\\r\\n`` after each command, which the handbook does not show;
``terminator`` is configurable and defaults to none.

Fixed from the legacy driver: every read is bounded (it opened the port
with ``timeout=None``, so a silent unit hung the guider forever); the
position tallies are updated only for moves the unit acknowledged, after
the limit check (it added the move to the tallies first, so a refused or
limited move left them wrong); a move that would leave the configured
range is clipped to it and reported (``at_limit``) instead of silently
skipped; an ``L`` reply marks the tallies uncertain until the next ``K``.

PI S-340 with an E-518 controller (FLWO 60" TRES front end)
----------------------------------------------------------
:class:`PIGCS`, through PI's ``pipython`` (GCS). Adapted from
jdeast/tres-guider (J. Eastman, J. Kansky) ``tiptilt.py``: axes ``A`` and
``B``, absolute ``MOV``, range from ``qTMN``/``qTMX``, centre the middle of
the range. ``pipython`` is an optional import; :class:`SimGCS` is a
simulator of the subset of ``GCSDevice`` used (same method names, GCS error
on an out-of-range move, servo state), so the driver is tested without it.
tres-guider noted that ``qPOS`` did not seem to work: positions are read
back with ``qPOS`` once at connect and otherwise tallied from the commanded
moves (the stage is closed-loop).

Mount offsets (T1, and offloading)
----------------------------------
:class:`MountOffset`, through the robofast Alpaca telescope: ``Action
("Offset", '{"east": E, "north": N}')`` (the TCSGO server's guider offset,
arcseconds on the sky) or, for any Alpaca mount, ``PulseGuide`` at a known
guide rate. Units: arcseconds (east, north); no limits.

Safety: every backend that reaches real hardware -- a serial port, PI's
USB/TCP, a non-loopback Alpaca address -- is refused unless hardware use is
allowed (``allow_hardware`` in the config AND ``--hardware``; see
:func:`~robofast_core.guider.base.check_hardware`). The simulators never
are.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any, Protocol

from robofast_core.guider.base import (
    Clock,
    MoveResult,
    RealClock,
    TipTiltError,
    check_address,
    check_hardware,
)

log = logging.getLogger(__name__)

Limits = tuple[tuple[float, float], tuple[float, float]]


class _Tally:
    """Position, limits and clipping shared by the backends."""

    name = "tiptilt"
    units = ""

    def __init__(self, limits: Limits | None, centre: tuple[float, float] = (0.0, 0.0)) -> None:
        self._limits = limits
        self._centre = centre
        self._pos = centre
        #: The tallies may not match the device (a limit or a timeout).
        self.uncertain = False
        self.clock: Clock = RealClock()

    @property
    def position(self) -> tuple[float, float]:
        return self._pos

    @property
    def centre(self) -> tuple[float, float]:
        return self._centre

    @property
    def limits(self) -> Limits | None:
        return self._limits

    def _clip(self, a: float, b: float) -> tuple[float, float, bool]:
        if self._limits is None:
            return a, b, False
        (alo, ahi), (blo, bhi) = self._limits
        ca, cb = min(max(a, alo), ahi), min(max(b, blo), bhi)
        return ca, cb, (ca != a or cb != b)


# -- Starlight Xpress AO --------------------------------------------------------------


class Transport(Protocol):
    """A byte stream with bounded reads (a serial port or a simulator)."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def read(self, n: int, timeout: float) -> bytes:
        """Up to ``n`` bytes; fewer (possibly none) if ``timeout`` passes."""
        ...

    async def flush_input(self) -> None: ...


class SerialTransport:
    """A real serial port through pyserial (optional import), 9600 8N1 by
    default. Hardware: refused unless allowed."""

    def __init__(self, port: str, *, baudrate: int = 9600, allow_hardware: bool = False) -> None:
        check_hardware(f"serial port {port}", allowed=allow_hardware)
        self.port = port
        self.baudrate = baudrate
        self._ser: Any = None

    async def open(self) -> None:
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TipTiltError("pyserial is not installed") from exc

        def _open() -> Any:
            return serial.Serial(
                self.port,
                self.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.05,  # never None (the legacy bug)
                write_timeout=1.0,
            )

        self._ser = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._ser is not None:
            await asyncio.to_thread(self._ser.close)
            self._ser = None

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._ser.write, data)

    async def read(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        out = b""
        while len(out) < n and time.monotonic() < deadline:
            out += await asyncio.to_thread(self._ser.read, n - len(out))
        return out

    async def flush_input(self) -> None:
        await asyncio.to_thread(self._ser.reset_input_buffer)


class SXAOSim:
    """A simulated SX AO unit behind the :class:`Transport` interface: the
    ``K`` and ``G`` commands (module doc), a mechanical range of
    ``+/- travel`` steps per axis, ``step_time`` seconds per step.
    ``position`` is the true mirror position (steps); ``silent`` makes it
    stop answering (a dead unit); ``commands`` records what it received."""

    def __init__(
        self,
        *,
        travel: int = 1000,
        step_time: float = 2e-5,
        terminator: bytes = b"",
        clock: Clock | None = None,
    ) -> None:
        self.travel = travel
        self.step_time = step_time
        self.terminator = terminator
        self.clock = clock or RealClock()
        self.position = [0, 0]
        self.silent = False
        self.commands: list[str] = []
        self._buf = b""
        self._out = b""
        self.on_move: Callable[[tuple[int, int]], None] | None = None

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def flush_input(self) -> None:
        self._out = b""

    async def write(self, data: bytes) -> None:
        self._buf += data
        while self._buf:
            if self.terminator and self._buf.startswith(self.terminator):
                self._buf = self._buf[len(self.terminator) :]
                continue
            c = self._buf[:1]
            if c == b"K":
                self._buf = self._buf[1:]
                self.commands.append("K")
                self.position = [0, 0]
                self._reply(b"K")
            elif c == b"G":
                if len(self._buf) < 7:
                    return  # wait for the rest
                cmd = self._buf[:7].decode("ascii", "replace")
                self._buf = self._buf[7:]
                self.commands.append(cmd)
                d, digits = cmd[1], cmd[2:]
                if d not in "NSTW" or not digits.isdigit():
                    continue  # the unit ignores garbage (assumed)
                n = int(digits)
                axis = 0 if d in "NS" else 1
                sign = 1 if d in "NT" else -1
                want = self.position[axis] + sign * n
                hit = abs(want) > self.travel
                self.position[axis] = max(-self.travel, min(self.travel, want))
                await self.clock.sleep(n * self.step_time)
                if self.on_move is not None:
                    self.on_move((self.position[0], self.position[1]))
                self._reply(b"L" if hit else b"G")
            else:
                self._buf = self._buf[1:]  # unknown byte: ignored (assumed)

    def _reply(self, b: bytes) -> None:
        if not self.silent:
            self._out += b

    async def read(self, n: int, timeout: float) -> bytes:
        if not self._out:
            await self.clock.sleep(timeout if self.silent else 0)
        out, self._out = self._out[:n], self._out[n:]
        return out


class SXAO(_Tally):
    """The Starlight Xpress AO driver (module doc). Positions in steps;
    ``limits`` is the software range (keep it inside the unit's travel)."""

    units = "steps"

    def __init__(
        self,
        transport: Transport,
        *,
        limits: Limits = ((-500.0, 500.0), (-500.0, 500.0)),
        terminator: bytes = b"",
        timeout: float = 0.5,
        step_time: float = 1e-3,
        name: str = "sxao",
    ) -> None:
        super().__init__(limits)
        self.transport = transport
        self.terminator = terminator
        self.timeout = timeout
        #: Allowance per step on top of ``timeout`` for a move's reply.
        self.step_time = step_time
        self.name = name
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        await self.transport.open()
        await self.home()

    async def close(self) -> None:
        await self.transport.close()

    async def _command(self, cmd: str, timeout: float) -> str:
        await self.transport.flush_input()
        await self.transport.write(cmd.encode("ascii") + self.terminator)
        reply = await self.transport.read(1, timeout)
        if not reply:
            self.uncertain = True
            raise TipTiltError(f"{self.name}: no reply to {cmd!r} within {timeout:.2f} s")
        return reply.decode("ascii", "replace")

    async def home(self) -> MoveResult:
        async with self._lock:
            r = await self._command("K", self.timeout + 5.0)
            if r != "K":
                self.uncertain = True
                raise TipTiltError(f"{self.name}: centre answered {r!r}, not 'K'")
            self._pos = (0.0, 0.0)
            self.uncertain = False
            return MoveResult(self._pos, t_sent=self.clock.now())

    async def move_by(self, da: float, db: float) -> MoveResult:
        async with self._lock:
            a0, b0 = self._pos
            # Whole steps, clipped to the range *before* anything is sent.
            ta, tb, clipped = self._clip(a0 + round(da), b0 + round(db))
            sa, sb = round(ta - a0), round(tb - b0)
            t_sent = self.clock.now()
            at_limit = clipped
            for axis, steps, pos_dir, neg_dir in ((0, sa, "N", "S"), (1, sb, "T", "W")):
                if steps == 0:
                    continue
                if abs(steps) > 99999:
                    raise TipTiltError(f"{self.name}: move of {steps} steps")
                cmd = f"G{pos_dir if steps > 0 else neg_dir}{abs(steps):05d}"
                r = await self._command(cmd, self.timeout + abs(steps) * self.step_time)
                if r == "G":
                    pos = list(self._pos)
                    pos[axis] += steps
                    self._pos = (pos[0], pos[1])
                elif r == "L":
                    # Stopped somewhere on the way, at its mechanical limit.
                    self.uncertain = True
                    at_limit = True
                    log.warning(
                        "%s: limit reached on %s; position uncertain until K", self.name, cmd
                    )
                else:
                    self.uncertain = True
                    raise TipTiltError(f"{self.name}: {cmd} answered {r!r}")
            return MoveResult(
                self._pos,
                at_limit=at_limit,
                clipped=(a0 + da - self._pos[0], b0 + db - self._pos[1]) if at_limit else (0, 0),
                t_sent=t_sent,
            )


# -- PI GCS ---------------------------------------------------------------------------


class GCSError(Exception):
    """As ``pipython.GCSError``: a GCS error with its number."""

    def __init__(self, value: int, message: str = "") -> None:
        super().__init__(f"GCS error {value}: {message}")
        self.val = value


#: PI_CNTR_POS_OUT_OF_LIMITS (GCS error 7).
POS_OUT_OF_LIMITS = 7
#: PI_CNTR_SERVO_OFF (GCS error 5): "Cannot set target position while servo off".
SERVO_OFF = 5


class SimGCS:
    """A simulator of the ``pipython.GCSDevice`` subset :class:`PIGCS` uses
    (same names and return shapes: dicts keyed by axis). The S-340's range
    is 0..2 (mrad; tres-guider used 0..2 with 1.0 the centre); a target
    outside ``qTMN``/``qTMX`` raises GCS error 7 and the axis stays put; a
    move with the servo off raises error 5. ``qPOS`` follows the target with
    first-order response ``tau`` seconds on ``clock``."""

    def __init__(
        self,
        *,
        axes: tuple[str, str] = ("A", "B"),
        low: float = 0.0,
        high: float = 2.0,
        tau: float = 1e-3,
        clock: Clock | None = None,
    ) -> None:
        self.axes = axes
        self.low = {a: low for a in axes}
        self.high = {a: high for a in axes}
        self.tau = tau
        self.clock = clock or RealClock()
        self._connected = False
        self.servo = {a: False for a in axes}
        self.target = {a: (low + high) / 2 for a in axes}
        self._from = dict(self.target)
        self._t_cmd = {a: 0.0 for a in axes}
        self.moves: list[dict[str, float]] = []
        self.on_move: Callable[[dict[str, float], float], None] | None = None

    # connection
    def EnumerateUSB(self, mask: str = "") -> list[str]:
        return ["PI E-518 SN 0000000000 (simulated)"]

    def ConnectUSB(self, serialnum: str) -> None:
        self._connected = True

    def ConnectTCPIP(self, ipaddress: str, ipport: int = 50000) -> None:
        self._connected = True

    def IsConnected(self) -> bool:
        return self._connected

    def CloseConnection(self) -> None:
        self._connected = False

    def qIDN(self) -> str:
        return "(c)2026 Physik Instrumente (PI), E-518 (simulated), 0, 0.0.0"

    def _need(self) -> None:
        if not self._connected:
            raise GCSError(-9, "no connection")

    # servo and range
    def SVO(self, axes: Any, values: Any = None) -> None:
        self._need()
        if isinstance(axes, dict):
            items = axes.items()
        else:
            axes = [axes] if isinstance(axes, str) else list(axes)
            values = [values] if not isinstance(values, (list, tuple)) else list(values)
            items = zip(axes, values, strict=True)
        for a, v in items:
            self.servo[a] = bool(v)

    def qSVO(self, axes: Any = None) -> dict[str, bool]:
        self._need()
        return dict(self.servo)

    def qTMN(self, axes: Any = None) -> dict[str, float]:
        self._need()
        return dict(self.low)

    def qTMX(self, axes: Any = None) -> dict[str, float]:
        self._need()
        return dict(self.high)

    # motion
    def position_at(self, t: float) -> tuple[float, float]:
        """The stage's position at time ``t`` relative to the centre of its
        range (since the last command; for the simulator's world)."""
        a, b = self.axes
        return (
            self._actual(a, t) - (self.low[a] + self.high[a]) / 2,
            self._actual(b, t) - (self.low[b] + self.high[b]) / 2,
        )

    def _actual(self, a: str, t: float | None = None) -> float:
        t = self.clock.now() if t is None else t
        if t < self._t_cmd[a]:
            return self._from[a]
        dt = t - self._t_cmd[a]
        k = math.exp(-dt / self.tau) if self.tau > 0 else 0.0
        return self.target[a] + (self._from[a] - self.target[a]) * k

    def MOV(self, axes: Any, values: Any = None) -> None:
        self._need()
        if isinstance(axes, dict):
            req = dict(axes)
        else:
            axes = [axes] if isinstance(axes, str) else list(axes)
            values = [values] if not isinstance(values, (list, tuple)) else list(values)
            req = dict(zip(axes, (float(v) for v in values), strict=True))
        for a, v in req.items():
            if not self.servo[a]:
                raise GCSError(SERVO_OFF, f"servo off on {a}")
            if not self.low[a] <= v <= self.high[a]:
                raise GCSError(POS_OUT_OF_LIMITS, f"{a} target {v} out of limits")
        now = self.clock.now()
        for a, v in req.items():
            self._from[a] = self._actual(a)
            self._t_cmd[a] = now
            self.target[a] = v
        self.moves.append(req)
        if self.on_move is not None:
            self.on_move(dict(self.target), now)

    def qMOV(self, axes: Any = None) -> dict[str, float]:
        self._need()
        return dict(self.target)

    def qPOS(self, axes: Any = None) -> dict[str, float]:
        self._need()
        return {a: self._actual(a) for a in self.axes}

    def qONT(self, axes: Any = None) -> dict[str, bool]:
        self._need()
        return {a: abs(self._actual(a) - self.target[a]) < 1e-4 for a in self.axes}


def open_pi_gcs(
    *,
    usb_serial: str | None = None,
    host: str | None = None,
    port: int = 50000,
    allow_hardware: bool = False,
) -> Any:
    """A connected ``pipython.GCSDevice`` (real hardware: gated)."""
    check_hardware("PI E-518 controller", allowed=allow_hardware)
    try:
        from pipython import GCSDevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TipTiltError("pipython is not installed (pip install pipython)") from exc
    dev = GCSDevice()
    if usb_serial:
        found = dev.EnumerateUSB()
        if not any(usb_serial in d for d in found):
            raise TipTiltError(f"no PI controller with serial {usb_serial} on USB: {found}")
        dev.ConnectUSB(serialnum=usb_serial)
    elif host:
        dev.ConnectTCPIP(ipaddress=host, ipport=port)
    else:
        raise TipTiltError("PI: give usb_serial or host")
    return dev


class PIGCS(_Tally):
    """The PI tip/tilt driver (module doc). ``gcs`` is a connected
    ``GCSDevice`` or a :class:`SimGCS`. ``in_thread`` runs each GCS call in
    a worker thread (pipython blocks); the simulator does not need it."""

    units = "mrad"

    def __init__(
        self,
        gcs: Any,
        *,
        axes: tuple[str, str] = ("A", "B"),
        margin: float = 0.0,
        in_thread: bool = False,
        timeout: float = 1.0,
        name: str = "pi",
    ) -> None:
        super().__init__(None)
        self.gcs = gcs
        self.axes = axes
        self.margin = margin
        self.in_thread = in_thread
        self.timeout = timeout
        self.name = name
        self._lock = asyncio.Lock()

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            if self.in_thread:
                async with asyncio.timeout(self.timeout):
                    return await asyncio.to_thread(fn, *args)
            return fn(*args)
        except TimeoutError as exc:
            self.uncertain = True
            raise TipTiltError(f"{self.name}: {fn.__name__} timed out") from exc
        except Exception as exc:
            if type(exc).__name__ == "GCSError":
                raise TipTiltError(f"{self.name}: {fn.__name__}: {exc}") from exc
            raise

    async def open(self) -> None:
        g = self.gcs
        if not await self._call(g.IsConnected):
            raise TipTiltError(f"{self.name}: the GCS device is not connected")
        await self._call(g.SVO, list(self.axes), [True, True])
        lo = await self._call(g.qTMN)
        hi = await self._call(g.qTMX)
        a, b = self.axes
        m = self.margin
        self._limits = ((lo[a] + m, hi[a] - m), (lo[b] + m, hi[b] - m))
        self._centre = ((lo[a] + hi[a]) / 2, (lo[b] + hi[b]) / 2)
        try:
            pos = await self._call(g.qPOS)
            self._pos = (float(pos[a]), float(pos[b]))
        except TipTiltError:
            self._pos = self._centre
        await self.home()

    async def close(self) -> None:
        pass  # the stage holds its position; the service owns the connection

    async def _move_to(self, a: float, b: float) -> MoveResult:
        ca, cb, clipped = self._clip(a, b)
        t_sent = self.clock.now()
        await self._call(self.gcs.MOV, list(self.axes), [ca, cb])
        self._pos = (ca, cb)
        return MoveResult(self._pos, at_limit=clipped, clipped=(a - ca, b - cb), t_sent=t_sent)

    async def move_by(self, da: float, db: float) -> MoveResult:
        async with self._lock:
            return await self._move_to(self._pos[0] + da, self._pos[1] + db)

    async def home(self) -> MoveResult:
        async with self._lock:
            r = await self._move_to(*self._centre)
            self.uncertain = False
            return r


# -- mount offsets ---------------------------------------------------------------------


#: ASCOM GuideDirections.
GUIDE_NORTH, GUIDE_SOUTH, GUIDE_EAST, GUIDE_WEST = 0, 1, 2, 3


class MountOffset(_Tally):
    """Guide (or offload) by moving the mount, through an Alpaca telescope
    (module doc). ``driver`` has ``async call(member, *args, timeout)``: an
    :class:`~robofast_core.alpaca_client.AlpacaDevice`, a
    :class:`~robofast_core.owner.Grant`, or a fake. Units: arcseconds
    (east, north); the position is the running total sent.

    ``method`` ``"offset"`` sends ``Action(action, {"east", "north"})``;
    ``"pulseguide"`` sends ``PulseGuide`` for ``|arcsec| / guide_rate``
    seconds per axis (``guide_rate`` in arcsec/s on the sky; east pulses are
    lengthened by 1/cos(dec), from :attr:`dec_deg`). A move larger than
    ``max_offset`` on an axis is clipped and reported."""

    units = "arcsec"

    def __init__(
        self,
        driver: Any,
        *,
        method: str = "offset",
        action: str = "Offset",
        guide_rate: float = 7.5,
        max_offset: float = 30.0,
        min_offset: float = 0.0,
        timeout: float = 5.0,
        name: str = "mount",
    ) -> None:
        super().__init__(None)
        if method not in ("offset", "pulseguide"):
            raise ValueError(f"mount offset method {method!r}")
        self.driver = driver
        self.method = method
        self.action = action
        self.guide_rate = guide_rate
        self.max_offset = max_offset
        self.min_offset = min_offset
        self.timeout = timeout
        self.name = name
        self.dec_deg = 0.0
        self.sent: list[tuple[float, float]] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        aclose = getattr(self.driver, "aclose", None)
        if aclose is not None:
            await aclose()

    async def move_by(self, da: float, db: float) -> MoveResult:
        east = max(-self.max_offset, min(self.max_offset, da))
        north = max(-self.max_offset, min(self.max_offset, db))
        clipped = (da - east, db - north)
        if abs(east) < self.min_offset and abs(north) < self.min_offset:
            return MoveResult(self._pos, t_sent=self.clock.now())
        t_sent = self.clock.now()
        try:
            async with asyncio.timeout(self.timeout):
                if self.method == "offset":
                    params = json.dumps({"east": round(east, 4), "north": round(north, 4)})
                    await self.driver.call("Action", self.action, params, timeout=self.timeout)
                else:
                    await self._pulse(east, north)
        except TipTiltError:
            raise
        except Exception as exc:
            raise TipTiltError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        self.sent.append((east, north))
        self._pos = (self._pos[0] + east, self._pos[1] + north)
        return MoveResult(self._pos, at_limit=clipped != (0.0, 0.0), clipped=clipped, t_sent=t_sent)

    async def _pulse(self, east: float, north: float) -> None:
        cosd = max(math.cos(math.radians(self.dec_deg)), 0.05)
        for arcsec, pos_dir, neg_dir, scale in (
            (north, GUIDE_NORTH, GUIDE_SOUTH, 1.0),
            (east, GUIDE_EAST, GUIDE_WEST, 1.0 / cosd),
        ):
            ms = round(abs(arcsec) / self.guide_rate * scale * 1000)
            if ms <= 0:
                continue
            d = pos_dir if arcsec > 0 else neg_dir
            await self.driver.call("PulseGuide", d, ms, timeout=self.timeout)

    async def home(self) -> MoveResult:
        # A mount has no centre to return to: the total is just re-zeroed.
        self._pos = (0.0, 0.0)
        return MoveResult(self._pos, t_sent=self.clock.now())


def alpaca_telescope(address: str, number: int = 0, *, allow_hardware: bool = False) -> Any:
    """An Alpaca telescope driver for :class:`MountOffset` (non-loopback:
    gated)."""
    from robofast_core.alpaca_client import AlpacaDevice

    check_address(address, "Alpaca telescope", allowed=allow_hardware)
    return AlpacaDevice(address, "telescope", number)
