"""The 60-inch ADC's two prism motors on one serial line.

FLWO's legacy ADC daemon (``adc_d``, part of the 60-inch TCS software
"telshell", T. Groner; with thanks to FLWO) drives two stepper motors,
named ``A`` and ``B``, on one serial port at 9600 baud, 8N1. Every command
is ASCII, prefixed by the motor's name and ended by a line feed. It uses
exactly these (``docs/adc.md``):

* ``<m>ma <steps>``: move to an absolute position, in steps from home
  (11.1111 steps per degree, 0..3999);
* ``<m>pr p``: print the position (the legacy daemon only logs the reply);
* ``<m>cl HO``: run the homing routine (the legacy waits 8 s for A, 12 s
  for B and reads nothing in between);
* a single byte 3 (Ctrl-C), a controller reset, at the daemon's start.

The command set (a device name before each command, ``MA``/``PR``/``CL``,
Ctrl-C reset, LF terminator) matches an IMS/Schneider MDrive in "party"
mode, with ``HO`` a homing subroutine stored in each motor; this is
**inferred, not confirmed**, as is the format of the ``pr p`` reply. This
client therefore takes the first line of the reply that is a bare integer
and treats a line starting with ``?`` as an error (``docs/adc.md`` lists
what to confirm).

This client sends only :data:`ALLOWED` (never the reset, which the legacy
daemon sent on every start), one exchange at a time, each bounded by
``timeout``; a move outside 0..``max_steps`` is refused before anything is
sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Protocol

log = logging.getLogger(__name__)

#: The commands (after the motor's name) this client sends.
ALLOWED = frozenset({"ma", "pr p", "cl HO"})
#: The ones that turn a prism.
MOVING = frozenset({"ma", "cl HO"})
#: Steps in one turn (legacy 11.1111 steps/deg).
STEPS_PER_TURN = 4000
_INT = re.compile(r"^[+-]?\d+$")


class MotorError(Exception):
    """A refused command, an error reply, or an unexpected answer."""


class MotorTimeout(MotorError):
    """No (complete) answer within the timeout."""


class Transport(Protocol):
    """A byte stream to the motors (a serial port or the simulator)."""

    async def write(self, data: bytes) -> None: ...

    async def readline(self, timeout: float) -> bytes:
        """One line including its LF; :class:`MotorTimeout` if none arrives."""
        ...

    def discard(self) -> None: ...

    def close(self) -> None: ...


class SerialTransport:
    """A real serial port, 8N1 (pyserial, an optional dependency).
    Constructed only by a server that passed the hardware gate."""

    def __init__(self, device: str, baud: int = 9600) -> None:
        try:
            import serial  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - only on the TCS host
            raise MotorError("pyserial is not installed (pip install pyserial)") from exc
        self.device = device
        self._ser = serial.Serial(device, baud, timeout=0.1, write_timeout=1.0)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._ser.write, data)

    async def readline(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            buf += await asyncio.to_thread(self._ser.readline)  # bounded by 0.1 s
            if buf.endswith(b"\n"):
                return buf
            if time.monotonic() > deadline:
                raise MotorTimeout(f"no answer from {self.device} within {timeout:g} s")

    def discard(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.close()


class Motors:
    """The two motors on one line, one exchange at a time."""

    def __init__(
        self,
        transport: Transport,
        *,
        names: tuple[str, str] = ("A", "B"),
        timeout: float = 1.0,
        ack_window: float = 0.1,
        max_steps: int = STEPS_PER_TURN,
    ) -> None:
        for n in names:
            if len(n) != 1 or not n.isalnum():
                raise ValueError(f"bad motor name {n!r}")
        self.transport = transport
        self.names = names
        self.timeout = timeout
        self.ack_window = ack_window
        self.max_steps = max_steps
        self._lock = asyncio.Lock()
        #: The last command that turned a prism (status reports).
        self.last_motion: str | None = None

    def _line(self, motor: str, command: str) -> str:
        if motor not in self.names:
            raise MotorError(f"no motor {motor!r}")
        word = command.split(" ", 1)[0] if command.startswith("ma ") else command
        if word not in ALLOWED:
            raise MotorError(f"refusing to send {command!r}: not in this client's allowlist")
        return f"{motor}{command}"

    async def _send(self, line: str) -> None:
        self.transport.discard()
        await self.transport.write((line + "\n").encode("ascii"))

    async def position(self, motor: str) -> int:
        """``pr p``: the position in steps (the first bare-integer line)."""
        line = self._line(motor, "pr p")
        async with self._lock:
            await self._send(line)
            deadline = time.monotonic() + self.timeout
            seen: list[str] = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MotorTimeout(f"{line!r}: no position within {self.timeout:g} s {seen}")
                raw = await self.transport.readline(left)
                text = raw.decode("ascii", "replace").strip().lstrip(">").strip()
                if text.startswith("?"):
                    raise MotorError(f"{line!r}: controller error {text!r}")
                if _INT.match(text):
                    return int(text)
                seen.append(text)
                if len(seen) > 8:
                    raise MotorError(f"{line!r}: no position in {seen!r}")

    async def _command(self, motor: str, command: str) -> None:
        line = self._line(motor, command)
        async with self._lock:
            await self._send(line)
            self.last_motion = line
            # Collect the echo for a moment: an error reply fails the command.
            deadline = time.monotonic() + self.ack_window
            while (left := deadline - time.monotonic()) > 0:
                try:
                    raw = await self.transport.readline(left)
                except MotorTimeout:
                    break
                text = raw.decode("ascii", "replace").strip().lstrip(">").strip()
                if text.startswith("?"):
                    raise MotorError(f"{line!r}: controller error {text!r}")

    async def move(self, motor: str, steps: int) -> None:
        """``ma``: start an absolute move and return."""
        if not isinstance(steps, int) or not 0 <= steps < self.max_steps:
            raise MotorError(f"move to {steps!r} steps refused: outside 0..{self.max_steps - 1}")
        await self._command(motor, f"ma {steps}")

    async def home(self, motor: str) -> None:
        """``cl HO``: start the motor's homing routine and return."""
        await self._command(motor, "cl HO")
