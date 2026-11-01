"""A simulated PI C-663 controller with an L-505 stage: the TRES front end's
calibration stage, speaking the GCS subset of
:mod:`robofast_core.devices.pi_gcs` as a :class:`~robofast_core.devices.pi_gcs.Transport`.

Behaviour modelled on the GCS manual and on how the legacy front end uses
the stage (jdeast/tres-guider ``calstage.py``, J. Eastman and J. Kansky):

* travel ``travel`` mm (``TMN?`` 0, ``TMX?`` 26: the legacy simulator's
  range), moves at ``velocity`` mm/s, position interpolated in time;
* after power-up the axis is unreferenced with the servo off; ``MOV`` is
  then refused with error 5, as is ``MOV`` outside the travel (error 7);
* ``FNL`` drives to the negative limit and marks the axis referenced;
* ``HLT`` stops where it is and sets error 10;
* ``ERR?`` returns and clears the last error.

Test hooks: ``commands`` (every line received), ``silent`` (answer
nothing: a dead controller), ``stuck`` (moves never progress: a jammed
stage), ``stop_short`` (a move ends this many mm before its target).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from robofast_core.devices.pi_gcs import GCSTimeout

TRAVEL = 26.0


@dataclass
class _Motion:
    start: float
    origin: float
    target: float
    reference: bool = False


class C663Sim:
    """In-process transport; thread-safe enough for one event loop."""

    def __init__(
        self,
        *,
        position: float = 0.0,
        referenced: bool = True,
        servo: bool = True,
        velocity: float = 10.0,
        travel: float = TRAVEL,
        idn: str = "(c)2015 Physik Instrumente (PI) GmbH & Co. KG, C-663, 0000000000, sim",
    ) -> None:
        if velocity <= 0 or travel <= 0:
            raise ValueError("velocity and travel must be positive")
        self.tmn, self.tmx = 0.0, float(travel)
        if not self.tmn <= position <= self.tmx:
            raise ValueError("position outside the travel")
        self._pos = float(position)
        self._referenced = referenced
        self.servo = servo
        self.velocity = float(velocity)
        self.idn = idn
        self.err = 0
        self._motion: _Motion | None = None
        self._replies: list[bytes] = []
        self._event = asyncio.Event()
        self.commands: list[str] = []
        self.silent = False
        self.stuck = False
        self.stop_short = 0.0
        self.closed = False

    # -- the mechanism ------------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic()

    def _settle(self) -> None:
        m = self._motion
        if m is None:
            return
        if self.stuck:
            m.start = self._now()  # never progresses
            return
        dist = m.target - m.origin
        t = self._now() - m.start
        travelled = self.velocity * t
        if travelled >= abs(dist):
            self._pos = m.target
            self._motion = None
            if m.reference:
                self._referenced = True
        else:
            self._pos = m.origin + travelled * (1 if dist > 0 else -1)

    @property
    def position(self) -> float:
        self._settle()
        return self._pos

    @property
    def referenced(self) -> bool:
        self._settle()
        return self._referenced

    @property
    def moving(self) -> bool:
        self._settle()
        return self._motion is not None

    def _start(self, target: float, *, reference: bool = False) -> None:
        self._settle()
        if not reference and target > self._pos:
            target = max(self._pos, target - self.stop_short)
        elif not reference and target < self._pos:
            target = min(self._pos, target + self.stop_short)
        self._motion = _Motion(self._now(), self._pos, target, reference)

    # -- GCS ------------------------------------------------------------------------

    def _answer(self, *lines: str) -> None:
        for i, ln in enumerate(lines):
            end = " \n" if i < len(lines) - 1 else "\n"
            self._replies.append((ln + end).encode())
        self._event.set()

    def _fail(self, code: int) -> None:
        if not self.err:
            self.err = code

    def handle(self, line: str) -> None:
        self.commands.append(line)
        if self.silent:
            return
        if line == "\x05":
            self._answer("1" if self.moving else "0")
            return
        word, _, rest = line.partition(" ")
        args = rest.split()
        if word == "*IDN?":
            self._answer(self.idn)
            return
        if word == "ERR?":
            code, self.err = self.err, 0
            self._answer(str(code))
            return
        if word.endswith("?"):
            if args != ["1"]:
                self._fail(15)
                return
            value = {
                "POS?": lambda: f"{self.position:.6f}",
                "ONT?": lambda: "0" if self.moving else "1",
                "FRF?": lambda: "1" if self.referenced else "0",
                "SVO?": lambda: "1" if self.servo else "0",
                "TMN?": lambda: f"{self.tmn:.6f}",
                "TMX?": lambda: f"{self.tmx:.6f}",
            }.get(word)
            if value is None:
                self._fail(2)
                return
            self._answer(f"1={value()}")
            return
        if not args or args[0] != "1":
            self._fail(15 if args else 1)
            return
        if word == "SVO" and len(args) == 2 and args[1] in ("0", "1"):
            self._settle()
            if args[1] == "0":
                self._motion = None
            self.servo = args[1] == "1"
        elif word == "MOV" and len(args) == 2:
            try:
                target = float(args[1])
            except ValueError:
                self._fail(1)
                return
            if not self.referenced or not self.servo:
                self._fail(5)
            elif not self.tmn <= target <= self.tmx:
                self._fail(7)
            else:
                self._start(target)
        elif word == "FNL" and len(args) == 1:
            if not self.servo:
                self._fail(5)
            else:
                self._referenced = False
                self._start(self.tmn, reference=True)
        elif word == "HLT" and len(args) == 1:
            self._settle()
            self._motion = None
            self._fail(10)
        else:
            self._fail(2)

    # -- Transport --------------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        text = data.decode("ascii", "replace")
        if text == "\x05":
            self.handle(text)
            return
        for line in text.split("\n"):
            if line:
                self.handle(line)

    async def readline(self, timeout: float) -> bytes:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while not self._replies:
                    self._event.clear()
                    await self._event.wait()
        if not self._replies:
            raise GCSTimeout(f"no answer from the simulated C-663 within {timeout:g} s")
        return self._replies.pop(0)

    def discard(self) -> None:
        self._replies.clear()

    def close(self) -> None:
        self.closed = True
