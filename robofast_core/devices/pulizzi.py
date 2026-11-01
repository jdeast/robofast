"""Pulizzi (later Eaton) IPC remote power controller: the serial ASCII protocol.

The FLWO 60-inch's TRES calibration lamps (ThAr, incandescent) are on a
Pulizzi Engineering "Intelligent Power Controller", switched today by a
small program the legacy TCS window starts (FLWO's legacy 60-inch software,
with thanks to FLWO). This module speaks the same protocol itself, so the
simulator (:mod:`robofast_core.devices.pulizzi_sim`) exercises this exact
code. Protocol, sources and open questions: ``docs/pulizzi.md``.

Protocol (Pulizzi IPC34xx manual, "Programmers Guide"; what the legacy
program was seen to send is marked *legacy*):

* 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control; DTR must
  be high. ASCII, **upper case only**; a command ends with CR (an LF is
  ignored). The unit does not echo.
* **Log in** by sending the unit name (factory ``@@@@``; *legacy* sends
  ``@@@@``); the unit answers a line starting ``IPC`` (``IPC ONLINE!`` or
  ``IPC@@@@`` depending on the firmware). A character that is not a
  command answers ``INVALID`` and **ends the session** (log in again).
* ``DX0``: the status of unit 0, one line per outlet, ``OUTLET 1 ON (
  UNIT#0 J1 )`` (*legacy* polls it).
* ``Nxx`` / ``Fxx``: outlet ``xx`` (two digits, 01-08) on / off; the unit
  answers ``DONE`` (*legacy* sends ``N02``, ``F04``, ...). With "auto
  event" enabled, a status listing follows the ``DONE``.
* Give the unit at least 0.5 s to answer; an unanswered command is retried
  once after logging in again (the manual's advice).

Only :data:`ALLOWED` commands are ever sent: the login name, ``DX0``, and
``Nxx``/``Fxx`` for an outlet the caller passes. The commands that switch
every outlet (``A0n``/``A1n``, ``S0n``/``S1n``), reboot an outlet
(``SR0``), arm the watchdog (``WE0``, which reboots the unit when
communication stops), or change the unit's name, password or power-on
sequence (``ADn``, ``PWn``, ``ISn``) are never sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

#: Outlets on one unit (the IPC34xx; a stack numbers on to 80).
OUTLETS = 8
#: The status request for unit 0 (the only unit robofast addresses).
STATUS = "DX0"
_SWITCH = re.compile(r"^[NF](0[1-8])$")
_OUTLET_LINE = re.compile(r"OUTLET\s+(\d+)\s+(ON|OFF)\b")
_NAME = re.compile(r"^[A-Z0-9@]{4}$")


def allowed(command: str, name: str) -> bool:
    """Whether ``command`` is one this client may send (module doc)."""
    return command in (name, STATUS) or bool(_SWITCH.match(command))


#: For the tests and the docs: the forms of every allowed command.
ALLOWED = ("<unit name>", STATUS, "Nxx", "Fxx")


class PulizziError(Exception):
    """A protocol error, or the unit did not do what it was told."""


class PulizziTimeout(PulizziError):
    """No (complete) answer within the timeout."""


class Transport(Protocol):
    """A byte stream to the unit (a serial port or the simulator)."""

    async def write(self, data: bytes) -> None: ...

    async def readline(self, timeout: float) -> bytes:
        """One line (ending in LF or CR); :class:`PulizziTimeout` if none."""
        ...

    def discard(self) -> None:
        """Drop any unread input."""
        ...

    def close(self) -> None: ...


class SerialTransport:
    """A real serial port (pyserial, an optional dependency), 9600 8N1 with
    DTR held high. Constructed only by a server that passed the hardware
    gate."""

    def __init__(self, device: str, baud: int = 9600) -> None:
        try:
            import serial  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - only on the site computer
            raise PulizziError("pyserial is not installed (pip install pyserial)") from exc
        self.device = device
        self._ser = serial.Serial(device, baud, timeout=0.1, write_timeout=1.0)
        self._ser.dtr = True  # the manual: DTR must be high
        self._buf = b""

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._ser.write, data)

    async def readline(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            for sep in (b"\n", b"\r"):
                i = self._buf.find(sep)
                if i >= 0:
                    line, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
                    return line
            if time.monotonic() > deadline:
                raise PulizziTimeout(f"no answer from {self.device} within {timeout:g} s")
            # pyserial's own timeout (0.1 s) bounds each blocking read.
            n = max(1, self._ser.in_waiting)
            self._buf += await asyncio.to_thread(self._ser.read, n)

    def discard(self) -> None:
        self._buf = b""
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.close()


@dataclass
class Status:
    """One ``DX0`` answer."""

    #: Outlet number (1-8) -> on.
    outlets: dict[int, bool]
    #: The other lines (watchdog and remote status; their format varies
    #: with the firmware), kept for ``Action("Status")``.
    other: list[str] = field(default_factory=list)
    at: float = 0.0


def parse_status(lines: list[str]) -> Status:
    outlets: dict[int, bool] = {}
    other = []
    for ln in lines:
        m = _OUTLET_LINE.search(ln)
        if m:
            outlets[int(m.group(1))] = m.group(2) == "ON"
        elif ln:
            other.append(ln)
    return Status(outlets, other, time.monotonic())


def _clean(raw: bytes) -> str:
    # The manual: some firmware sends stray NULs; ignore them.
    return raw.replace(b"\x00", b"").decode("ascii", "replace").strip()


class PulizziIPC:
    """One Pulizzi unit, one exchange at a time (a lock)."""

    def __init__(
        self,
        transport: Transport,
        *,
        name: str = "@@@@",
        timeout: float = 1.0,
        settle: float = 0.3,
    ) -> None:
        if not _NAME.match(name):
            raise ValueError(f"unit name {name!r}: four upper-case letters, digits or @")
        self.transport = transport
        self.name = name
        #: Bound on one answer (the manual: at least 0.5 s).
        self.timeout = timeout
        #: After the last line of a listing, how long to wait for more.
        self.settle = settle
        self.logged_in = False
        self.sent: list[str] = []
        self._lock = asyncio.Lock()

    async def _send(self, command: str) -> None:
        if not allowed(command, self.name):
            raise PulizziError(f"refusing to send {command!r} (not in the allowed set)")
        self.transport.discard()
        self.sent.append(command)
        await self.transport.write(command.encode("ascii") + b"\r")

    async def _line(self, timeout: float) -> str:
        while True:
            ln = _clean(await self.transport.readline(timeout))
            if ln:
                if ln.startswith("INVALID"):
                    self.logged_in = False
                    raise PulizziError("the unit answered INVALID (session ended)")
                return ln

    async def _login(self) -> None:
        for attempt in (1, 2):  # the manual: retry once (stale input)
            await self._send(self.name)
            try:
                deadline = time.monotonic() + self.timeout
                while True:
                    ln = await self._line(max(0.01, deadline - time.monotonic()))
                    if ln.startswith("IPC"):
                        self.logged_in = True
                        log.debug("pulizzi: logged in (%s)", ln)
                        return
            except PulizziTimeout:
                if attempt == 2:
                    raise PulizziTimeout("the unit did not answer the login") from None
            except PulizziError:
                if attempt == 2:
                    raise

    async def _listing(self, first_timeout: float) -> list[str]:
        """Lines until ``OUTLETS`` outlet lines, or a quiet ``settle``."""
        lines = [await self._line(first_timeout)]
        n = int(bool(_OUTLET_LINE.search(lines[0])))
        while n < OUTLETS:
            try:
                ln = await self._line(self.settle if n else self.timeout)
            except PulizziTimeout:
                break
            lines.append(ln)
            n += int(bool(_OUTLET_LINE.search(ln)))
        return lines

    async def _exchange(self, command: str, expect_done: bool) -> list[str]:
        """Send ``command`` (logging in first if needed); on no answer, log
        in again and retry once."""
        last: Exception | None = None
        for _ in (1, 2):
            if not self.logged_in:
                await self._login()
            await self._send(command)
            try:
                if expect_done:
                    ln = await self._line(self.timeout)
                    if ln != "DONE":
                        raise PulizziError(f"{command}: expected DONE, got {ln!r}")
                    # With auto-event on, a listing follows; drain it.
                    with contextlib.suppress(PulizziTimeout):
                        while True:
                            await self._line(self.settle)
                    return [ln]
                return await self._listing(self.timeout)
            except PulizziError as exc:  # includes timeouts and INVALID
                last = exc
                self.logged_in = False
        assert last is not None
        raise last

    # -- the API -------------------------------------------------------------------

    async def status(self) -> Status:
        async with self._lock:
            st = parse_status(await self._exchange(STATUS, expect_done=False))
        if not st.outlets:
            raise PulizziError("DX0: no outlet lines in the answer")
        return st

    async def switch(self, outlet: int, on: bool) -> Status:
        """Switch one outlet and read the status back; :class:`PulizziError`
        if the outlet is not then in the asked state (a front-panel
        override, the remote-disable switch, or a fault)."""
        if not 1 <= outlet <= OUTLETS:
            raise PulizziError(f"no outlet {outlet}")
        command = f"{'N' if on else 'F'}{outlet:02d}"
        async with self._lock:
            await self._exchange(command, expect_done=True)
            st = parse_status(await self._exchange(STATUS, expect_done=False))
        if st.outlets.get(outlet) is not on:
            raise PulizziError(
                f"{command}: outlet {outlet} reads {'ON' if st.outlets.get(outlet) else 'OFF'}"
                " after the command (front-panel override, remote disabled, or a fault?)"
            )
        return st
