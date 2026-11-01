"""Simulated ADC prism motors: two motors, ``A`` and ``B``, on one line,
speaking the command subset of :mod:`robofast_core.devices.adc_mdrive` as
a :class:`~robofast_core.devices.adc_mdrive.Transport`.

Modelled on how FLWO's legacy ADC daemon uses them (``docs/adc.md``):

* ``<m>ma <n>`` starts a move to absolute step ``n`` at ``speed`` steps/s
  (the shorter way is not taken: an absolute move goes through the steps
  in between, as a stepper's counter does);
* ``<m>pr p`` answers the position as a bare integer line;
* ``<m>cl HO`` drives the motor to its home and sets the counter to 0;
* every command is echoed, then a ``>`` prompt; an unknown command gets a
  ``?`` line instead.

Test hooks: ``commands`` (every line received), ``silent`` (answer
nothing: a dead line), ``stuck`` (a set of motors whose moves never
progress), ``stop_short`` (a move ends this many steps early),
``position`` (where each motor starts) and ``speed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from robofast_core.devices.adc_mdrive import STEPS_PER_TURN, MotorTimeout


@dataclass
class _Motion:
    start: float
    origin: float
    target: int


class _Motor:
    def __init__(self, sim: ADCMotorsSim, name: str, position: int) -> None:
        self.sim = sim
        self.name = name
        self._pos = float(position)
        self._motion: _Motion | None = None

    def settle(self) -> None:
        m = self._motion
        if m is None:
            return
        if self.name in self.sim.stuck:
            m.start = time.monotonic()
            return
        dist = m.target - m.origin
        travelled = self.sim.speed * (time.monotonic() - m.start)
        if travelled >= abs(dist):
            self._pos, self._motion = float(m.target), None
        else:
            self._pos = m.origin + travelled * (1 if dist > 0 else -1)

    @property
    def position(self) -> int:
        self.settle()
        return round(self._pos)

    @property
    def moving(self) -> bool:
        self.settle()
        return self._motion is not None

    def start(self, target: int) -> None:
        self.settle()
        if target > self._pos:
            target = max(round(self._pos), target - self.sim.stop_short)
        elif target < self._pos:
            target = min(round(self._pos), target + self.sim.stop_short)
        self._motion = _Motion(time.monotonic(), self._pos, target)


class ADCMotorsSim:
    """In-process transport for both motors."""

    def __init__(
        self,
        *,
        position: tuple[int, int] | list[int] = (0, 0),
        speed: float = 2000.0,
        names: tuple[str, str] = ("A", "B"),
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        if len(position) != 2 or not all(0 <= p < STEPS_PER_TURN for p in position):
            raise ValueError("position: two step counts in 0..3999")
        self.speed = float(speed)
        self.motors = {n: _Motor(self, n, p) for n, p in zip(names, position, strict=True)}
        self._replies: list[bytes] = []
        self._event = asyncio.Event()
        self.commands: list[str] = []
        self.silent = False
        self.stuck: set[str] = set()
        self.stop_short = 0
        self.closed = False

    def position(self, name: str) -> int:
        return self.motors[name].position

    def moving(self, name: str | None = None) -> bool:
        names = self.motors if name is None else [name]
        return any(self.motors[n].moving for n in names)

    def _answer(self, *lines: str) -> None:
        for ln in lines:
            self._replies.append((ln + "\r\n").encode())
        self._event.set()

    def handle(self, line: str) -> None:
        self.commands.append(line)
        if self.silent:
            return
        motor = self.motors.get(line[:1])
        command = line[1:]
        if motor is None:
            return  # another device's name: not ours to answer
        if command == "pr p":
            self._answer(line, str(motor.position), ">")
        elif command == "cl HO":
            self._answer(line, ">")
            motor.start(0)
        elif command.startswith("ma "):
            try:
                target = int(command[3:])
            except ValueError:
                self._answer(line, "?")
                return
            if not 0 <= target < STEPS_PER_TURN:
                self._answer(line, "?")
                return
            self._answer(line, ">")
            motor.start(target)
        else:
            self._answer(line, "?")

    # -- Transport ---------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        for line in data.decode("ascii", "replace").split("\n"):
            if line:
                self.handle(line)

    async def readline(self, timeout: float) -> bytes:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while not self._replies:
                    self._event.clear()
                    await self._event.wait()
        if not self._replies:
            raise MotorTimeout(f"no answer from the simulated ADC motors within {timeout:g} s")
        return self._replies.pop(0)

    def discard(self) -> None:
        self._replies.clear()

    def close(self) -> None:
        self.closed = True
