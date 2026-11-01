"""The MEarth South roll-off roof's computer interface: a parallel port.

Adapted from the MEarth observing system (J. Irwin, used with permission):
its roof daemon's ``south`` roof type and its power-fail close script. What
follows is what that code does; anything it does not show is marked
*unconfirmed* and listed in ``docs/mearth_roof.md`` for checking on site.

The hardware
------------
The roof is one roll-off section on rails, moved by a chain drive from a
motor on a variable frequency drive (VFD) with acceleration and deceleration
ramps at the ends of travel and an over-current trip. A local/computer
switch on the roof controls selects who drives it. Between the control PC's
parallel port and the drive sits a small "roof interface box" (the legacy
docs call it the roof decoder). Limit switches at both ends of travel stop
the drive by themselves, and a second set of contacts reports them to the
computer; those contacts are powered from the weather station's 15 V supply,
so without it the roof still moves and stops in the right places but the
computer cannot tell where it is.

The protocol
------------
Commands are single bytes, latched by the box on a strobe pulse:

1. write the byte to the data register (``PPWDATA``);
2. wait about 1 ms, assert ``STROBE`` (control bit 0; the pin is inverted,
   so setting the bit drives the line low) with ``PPFCONTROL``;
3. wait about 1 ms, release it; wait about 1 ms.

The bytes are ``o`` (0x6F, drive towards open), ``c`` (0x63, drive towards
closed) and 0x00 (stop). Nothing else is ever sent. What the box does with
any other byte is *unconfirmed*.

Status is read from the status register (``PPRSTATUS``):

* open limit made: ``BUSY`` (0x80) **set** in the register;
* closed limit made: ``PAPEROUT`` (0x20) **clear** in the register.

(``BUSY`` is inverted by the port hardware, so both switches pull their pin
low when made; an unpowered or unplugged input floats high and reads as "not
made" on both, *unconfirmed*.) Both made at once is a wiring or switch fault.

There is no heartbeat or watchdog: the box holds the last latched byte for
as long as it has power, whatever the computer does. A command in progress
therefore finishes (the drive stops at the limit) even if the software
dies, but nothing closes an open roof when the software stops. A robofast
enclosure on this roof is attended only (``docs/design.md`` section 3.3).

This module
-----------
* :class:`RoofPort` is the register-level interface; :class:`PPDevPort`
  implements it on Linux ``/dev/parportN`` (``ppdev`` ioctls), the simulator
  (:mod:`robofast_core.devices.mearth_roof_sim`) in memory.
* :class:`RoofInterface` is the asyncio client: it sends only the three
  command bytes, with the strobe sequence above, and reads the limit
  switches. All port I/O runs on one worker thread, so commands never
  interleave, and every call is bounded.
* A real port needs ``allow_hardware`` (the server requires it in the config
  **and** ``--hardware`` on the command line).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

from robofast_core.owner import DeviceError, DeviceTimeout

log = logging.getLogger(__name__)

#: Command bytes (the only ones ever written).
CMD_OPEN = ord("o")
CMD_CLOSE = ord("c")
CMD_STOP = 0x00
COMMAND_NAMES = {CMD_OPEN: "open", CMD_CLOSE: "close", CMD_STOP: "stop"}

#: Status register bits (Linux ``parport.h``).
STATUS_BUSY = 0x80
STATUS_PAPEROUT = 0x20
#: Control register bit (Linux ``parport.h``).
CONTROL_STROBE = 0x01

# Linux ppdev ioctl numbers (``linux/ppdev.h``): _IOC(dir, 'p', nr, size).
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("p") << 8) | nr


PPRSTATUS = _ioc(_IOC_READ, 0x81, 1)
PPWDATA = _ioc(_IOC_WRITE, 0x86, 1)
PPFCONTROL = _ioc(_IOC_WRITE, 0x8E, 2)  # struct ppdev_frob_struct {mask, val}
PPCLAIM = _ioc(0, 0x8B, 0)
PPRELEASE = _ioc(0, 0x8C, 0)


class RoofPortError(DeviceError):
    """The parallel port failed (or is not open)."""


class RoofPortTimeout(RoofPortError, DeviceTimeout):
    """A port operation did not finish in time."""


class HardwareNotAllowed(Exception):
    """A real port without explicit permission."""


@dataclass(frozen=True)
class Limits:
    """The limit switches as the computer sees them. ``raw`` is the status
    register."""

    open: bool
    closed: bool
    raw: int

    @property
    def both(self) -> bool:
        return self.open and self.closed

    @property
    def neither(self) -> bool:
        return not (self.open or self.closed)

    def describe(self) -> str:
        if self.both:
            return "both limits made"
        if self.open:
            return "open limit made"
        if self.closed:
            return "closed limit made"
        return "neither limit made"


def decode_limits(status: int) -> Limits:
    """The limit switches from a status register value."""
    return Limits(
        open=bool(status & STATUS_BUSY), closed=not (status & STATUS_PAPEROUT), raw=status
    )


class RoofPort(Protocol):
    """Register-level access to the port (blocking; called on one thread)."""

    def write_data(self, value: int) -> None: ...

    def frob_control(self, mask: int, value: int) -> None: ...

    def read_status(self) -> int: ...

    def close(self) -> None: ...


class PPDevPort:
    """A Linux ``ppdev`` parallel port (``/dev/parportN``), claimed on open."""

    def __init__(self, device: str, *, allow_hardware: bool = False) -> None:
        if not allow_hardware:
            raise HardwareNotAllowed(
                f"parallel port {device!r} is real hardware: refusing without allow_hardware"
                " in the config AND --hardware"
            )
        import fcntl  # Linux only; imported here so the module loads anywhere

        self._ioctl = fcntl.ioctl
        self.device = device
        try:
            self._fd: int | None = os.open(device, os.O_RDWR)
        except OSError as exc:
            raise RoofPortError(f"cannot open {device}: {exc}") from exc
        try:
            self._ioctl(self._fd, PPCLAIM)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise RoofPortError(f"cannot claim {device}: {exc}") from exc
        log.warning("HARDWARE MODE: claimed parallel port %s", device)

    def _fdcheck(self) -> int:
        if self._fd is None:
            raise RoofPortError(f"{self.device} is closed")
        return self._fd

    def write_data(self, value: int) -> None:
        try:
            self._ioctl(self._fdcheck(), PPWDATA, bytes([value & 0xFF]))
        except OSError as exc:
            raise RoofPortError(f"{self.device}: write data: {exc}") from exc

    def frob_control(self, mask: int, value: int) -> None:
        try:
            self._ioctl(self._fdcheck(), PPFCONTROL, bytes([mask & 0xFF, value & 0xFF]))
        except OSError as exc:
            raise RoofPortError(f"{self.device}: frob control: {exc}") from exc

    def read_status(self) -> int:
        try:
            return self._ioctl(self._fdcheck(), PPRSTATUS, b"\x00")[0]
        except OSError as exc:
            raise RoofPortError(f"{self.device}: read status: {exc}") from exc

    def close(self) -> None:
        # Releasing the port leaves the data lines as they are, so the box
        # keeps whatever it latched (the latch is in the box, not the port).
        if self._fd is not None:
            try:
                self._ioctl(self._fd, PPRELEASE)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None


@dataclass
class StrobeTiming:
    """Seconds: data to strobe, strobe width, after the strobe (legacy 1 ms each)."""

    setup: float = 0.001
    width: float = 0.001
    hold: float = 0.001


class RoofInterface:
    """Asyncio access to the roof interface box through a :class:`RoofPort`.

    One worker thread does all port I/O, so a strobe sequence is never
    interleaved with another; ``timeout`` bounds every call (a call that
    times out leaves its thread to finish; the next one waits behind it).
    """

    def __init__(
        self,
        port: RoofPort,
        *,
        timing: StrobeTiming | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.port = port
        self.timing = timing or StrobeTiming()
        self.timeout = timeout
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="roof-port")
        #: The last byte this process latched (None: none since start).
        self.last_command: int | None = None
        self.commands_sent = 0
        self._closed = False

    async def _run(self, fn, what: str):  # type: ignore[no-untyped-def]
        if self._closed:
            raise RoofPortError(f"roof port {what}: interface closed")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._pool, fn)
        try:
            async with asyncio.timeout(self.timeout):
                return await asyncio.shield(fut)
        except TimeoutError:
            raise RoofPortTimeout(f"roof port {what}: no answer within {self.timeout} s") from None

    def _strobe(self, value: int) -> None:
        t = self.timing
        self.port.write_data(value)
        time.sleep(t.setup)
        self.port.frob_control(CONTROL_STROBE, CONTROL_STROBE)  # line low
        time.sleep(t.width)
        self.port.frob_control(CONTROL_STROBE, 0)  # line high: latched
        time.sleep(t.hold)

    async def send(self, value: int) -> None:
        """Latch one command byte (only :data:`CMD_OPEN`, :data:`CMD_CLOSE`,
        :data:`CMD_STOP`)."""
        if value not in COMMAND_NAMES:
            raise ValueError(f"0x{value:02x} is not a roof command")
        await self._run(lambda: self._strobe(value), COMMAND_NAMES[value])
        self.last_command = value
        self.commands_sent += 1
        log.info("roof command latched: %s", COMMAND_NAMES[value])

    async def limits(self) -> Limits:
        return decode_limits(await self._run(self.port.read_status, "read status"))

    def close(self) -> None:
        """Release the port (sends nothing: the box keeps its latch)."""
        if self._closed:
            return
        self._closed = True
        self._pool.submit(self.port.close)
        self._pool.shutdown(wait=True, cancel_futures=False)
