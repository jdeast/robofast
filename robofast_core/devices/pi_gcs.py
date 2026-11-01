"""PI GCS (General Command Set) over a byte stream, for one stage axis.

Just enough of PI's ASCII command language to drive the TRES front end's
calibration stage: a PI L-505 linear stage on a PI C-663 Mercury Step
controller, reached as a serial port (the controller's USB port, or its
RS-232 port, at 115200 baud). The legacy front end drives it with PI's
``pipython`` library (jdeast/tres-guider ``calstage.py``, J. Eastman and
J. Kansky; ``docs/tres_frontend.md``); this module speaks the same commands
itself, so robofast needs no vendor library and the simulator
(:mod:`robofast_core.devices.tres_frontend_sim`) exercises this exact code.

Protocol (GCS 2.0; ``docs/tres_frontend.md`` lists what to confirm on the
real controller):

* A command is one line, ``MNEMONIC [axis [value]]``, ended by LF. Setting
  commands get **no reply**; whether one worked is read back with ``ERR?``
  (the error code of the last failure, cleared by the read; 0 is none).
* A query (``POS? 1``) answers one line per axis, ``1=20.000000``; lines
  of a multi-line answer end in a space before the LF, the last one not.
* Single-byte commands: ``\\x05`` ("#5") answers the motion state as a hex
  bit mask, bit 0 set while axis 1 moves.

Every exchange is bounded by ``timeout``, one exchange at a time (a lock),
and stale input is discarded before each. Only :data:`ALLOWED` commands are
ever sent; :data:`MOVING` ones move the stage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import Protocol

log = logging.getLogger(__name__)

#: Every command this client sends. Queries end in ``?``.
ALLOWED = frozenset(
    {"*IDN?", "POS?", "ONT?", "FRF?", "SVO?", "TMN?", "TMX?", "ERR?", "\x05"}
    | {"MOV", "SVO", "FNL", "HLT"}
)
#: The ones that move the stage: an absolute move and the reference move to
#: the negative limit (legacy ``pitools.startup(refmodes='FNL')``). ``SVO``
#: (servo on) only energises the axis; ``HLT`` stops it.
MOVING = frozenset({"MOV", "FNL"})

#: The GCS error codes this module names (PI's GCS error table); others are
#: reported by number.
ERRORS = {
    0: "no error",
    1: "parameter syntax error",
    2: "unknown command",
    5: "move refused: axis not referenced, or servo off",
    7: "position out of limits",
    10: "controller was stopped by command",
    15: "invalid axis identifier",
    17: "parameter out of range",
}


class GCSError(Exception):
    """A protocol error, a refused command, or a controller error code."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class GCSTimeout(GCSError):
    """No (complete) answer within the timeout."""


class Transport(Protocol):
    """A byte stream to the controller (a serial port or the simulator)."""

    async def write(self, data: bytes) -> None: ...

    async def readline(self, timeout: float) -> bytes:
        """One line including its LF; :class:`GCSTimeout` if none arrives."""
        ...

    def discard(self) -> None:
        """Drop any unread input."""
        ...

    def close(self) -> None: ...


class SerialTransport:
    """A real serial port (pyserial, an optional dependency). Constructed
    only by a server that passed the hardware gate."""

    def __init__(self, device: str, baud: int = 115200) -> None:
        try:
            import serial  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - only on the front-end computer
            raise GCSError("pyserial is not installed (pip install pyserial)") from exc
        self.device = device
        self._ser = serial.Serial(device, baud, timeout=0.1, write_timeout=1.0)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._ser.write, data)

    async def readline(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            # pyserial's own timeout (0.1 s) bounds each blocking read.
            chunk = await asyncio.to_thread(self._ser.readline)
            buf += chunk
            if buf.endswith(b"\n"):
                return buf
            if time.monotonic() > deadline:
                raise GCSTimeout(f"no answer from {self.device} within {timeout:g} s")

    def discard(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.close()


def _number(x: float) -> str:
    if not math.isfinite(x):
        raise GCSError(f"not a finite number: {x}")
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


class GCSAxis:
    """One axis of a GCS controller, one exchange at a time."""

    def __init__(self, transport: Transport, *, axis: str = "1", timeout: float = 2.0) -> None:
        if not axis or not axis.isalnum():
            raise ValueError(f"bad axis identifier {axis!r}")
        self.transport = transport
        self.axis = axis
        self.timeout = timeout
        self._lock = asyncio.Lock()
        #: The last command that moves the stage, for status reports.
        self.last_motion: str | None = None

    # -- the wire ---------------------------------------------------------------

    @staticmethod
    def _check(line: str) -> None:
        word = line.split(" ", 1)[0]
        if word not in ALLOWED:
            raise GCSError(f"refusing to send {word!r}: not in this client's allowlist")

    async def _send(self, line: str) -> None:
        self._check(line)
        self.transport.discard()
        data = line.encode() if line == "\x05" else (line + "\n").encode()
        await self.transport.write(data)

    async def _read_answer(self) -> list[str]:
        lines = []
        while True:
            raw = await self.transport.readline(self.timeout)
            text = raw.decode("ascii", "replace")
            if not text.endswith("\n"):
                raise GCSTimeout("answer cut short")
            more = text.endswith(" \n")
            lines.append(text.rstrip(" \n").rstrip("\r"))
            if not more:
                return lines
            if len(lines) > 64:
                raise GCSError("answer too long")

    async def query(self, line: str) -> list[str]:
        """Send a query (or ``\\x05``) and return its answer lines."""
        async with self._lock:
            await self._send(line)
            return await self._read_answer()

    async def command(self, line: str) -> None:
        """Send a setting command, then ``ERR?``; a nonzero code raises."""
        async with self._lock:
            await self._send(line)
            if line.split(" ", 1)[0] in MOVING:
                self.last_motion = line
            await self._send("ERR?")
            code = _int(await self._read_answer())
        if code:
            raise GCSError(
                f"{line!r}: error {code} ({ERRORS.get(code, 'see the GCS manual')})", code
            )

    def _value(self, lines: list[str], what: str) -> str:
        for ln in lines:
            key, sep, value = ln.partition("=")
            if sep and key.strip() == self.axis:
                return value.strip()
        raise GCSError(f"{what}: no value for axis {self.axis} in {lines!r}")

    async def _axis_query(self, mnemonic: str) -> str:
        return self._value(await self.query(f"{mnemonic} {self.axis}"), mnemonic)

    # -- queries ----------------------------------------------------------------

    async def identify(self) -> str:
        return " ".join(await self.query("*IDN?"))

    async def error(self) -> int:
        """Read (and so clear) the controller's last error code."""
        return _int(await self.query("ERR?"))

    async def position(self) -> float:
        return _float(await self._axis_query("POS?"), "POS?")

    async def on_target(self) -> bool:
        return _flag(await self._axis_query("ONT?"), "ONT?")

    async def referenced(self) -> bool:
        return _flag(await self._axis_query("FRF?"), "FRF?")

    async def servo(self) -> bool:
        return _flag(await self._axis_query("SVO?"), "SVO?")

    async def limits(self) -> tuple[float, float]:
        lo = _float(await self._axis_query("TMN?"), "TMN?")
        hi = _float(await self._axis_query("TMX?"), "TMX?")
        if not lo < hi:
            raise GCSError(f"travel range [{lo}, {hi}] is empty")
        return lo, hi

    async def moving(self) -> bool:
        """``#5``: bit 0 of the hex mask is axis 1."""
        lines = await self.query("\x05")
        try:
            mask = int(lines[0].strip(), 16)
        except (ValueError, IndexError):
            raise GCSError(f"#5: unexpected answer {lines!r}") from None
        return bool(mask & (1 << (int(self.axis) - 1 if self.axis.isdigit() else 0)))

    # -- commands ---------------------------------------------------------------

    async def servo_on(self) -> None:
        await self.command(f"SVO {self.axis} 1")

    async def move(self, position: float) -> None:
        """``MOV``: start an absolute move (mm) and return."""
        await self.command(f"MOV {self.axis} {_number(position)}")

    async def reference_negative(self) -> None:
        """``FNL``: start a reference move to the negative limit and return."""
        await self.command(f"FNL {self.axis}")

    async def halt(self) -> None:
        """``HLT``: stop the axis (smoothly). The controller then reports
        error 10 ("stopped by command"), which is read and cleared here."""
        async with self._lock:
            await self._send(f"HLT {self.axis}")
            await self._send("ERR?")
            code = _int(await self._read_answer())
        if code not in (0, 10):
            raise GCSError(f"HLT: error {code} ({ERRORS.get(code, 'see the GCS manual')})", code)


def _int(lines: list[str]) -> int:
    try:
        return int(lines[0].strip())
    except (ValueError, IndexError):
        raise GCSError(f"expected an error code, got {lines!r}") from None


def _float(text: str, what: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise GCSError(f"{what}: not a number: {text!r}") from None
    if not math.isfinite(v):
        raise GCSError(f"{what}: not finite: {text!r}")
    return v


def _flag(text: str, what: str) -> bool:
    if text not in ("0", "1"):
        raise GCSError(f"{what}: expected 0 or 1, got {text!r}")
    return text == "1"
