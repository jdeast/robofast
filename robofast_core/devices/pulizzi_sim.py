"""A simulated Pulizzi IPC power controller, speaking the protocol of
:mod:`robofast_core.devices.pulizzi` as a
:class:`~robofast_core.devices.pulizzi.Transport`.

Behaviour from the Pulizzi IPC34xx manual (``docs/pulizzi.md``):

* nothing is answered until the unit name is sent; then ``IPC ONLINE!``;
* ``DX0`` lists the eight outlets (``OUTLET   2   OFF ( UNIT#0 J2 )``)
  followed by the watchdog and remote status lines;
* ``Nxx`` / ``Fxx`` switch one outlet and answer ``DONE`` (followed by a
  listing when ``auto_event`` is on);
* any other command answers ``INVALID`` and ends the session;
* stray NULs in answers (``nulls``), as some firmware sends.

Test hooks: ``commands`` (every line received), ``silent`` (answer nothing:
a dead unit or cable), ``remote_disabled`` (the front-panel remote-disable
switch: commands answer ``DONE`` but switch nothing), ``panel(outlet, on)``
(a front-panel manual switch), ``drop_session()`` (the unit forgets the
login, e.g. after its own power cycle).
"""

from __future__ import annotations

import asyncio
import re

from robofast_core.devices.pulizzi import OUTLETS, PulizziTimeout

_SWITCH = re.compile(r"^([NF])(\d\d)$")


class PulizziSim:
    """In-process transport; for one event loop."""

    def __init__(
        self,
        *,
        name: str = "@@@@",
        on: list[int] | tuple[int, ...] = (),
        auto_event: bool = False,
        nulls: bool = False,
    ) -> None:
        self.name = name
        self.state = {k: k in on for k in range(1, OUTLETS + 1)}
        self.auto_event = auto_event
        self.nulls = nulls
        self.logged_in = False
        self.commands: list[str] = []
        self.silent = False
        self.remote_disabled = False
        self.closed = False
        self._replies: list[bytes] = []
        self._event = asyncio.Event()
        self._partial = b""

    # -- test hooks ----------------------------------------------------------------

    def panel(self, outlet: int, on: bool) -> None:
        self.state[outlet] = on

    def drop_session(self) -> None:
        self.logged_in = False

    def lit(self) -> list[int]:
        return [k for k, v in self.state.items() if v]

    # -- the unit ------------------------------------------------------------------

    def _say(self, *lines: str) -> None:
        for ln in lines:
            raw = ln.encode("ascii") + b"\r\n"
            if self.nulls:
                raw = b"\x00" + raw
            self._replies.append(raw)
        self._event.set()

    def _listing(self) -> list[str]:
        out = [
            f"OUTLET   {k}   {'ON' if v else 'OFF'} ( UNIT#0 J{k} )" for k, v in self.state.items()
        ]
        return [*out, "WATCHDOG DISABLED", "REMOTE ENABLED"]

    def _handle(self, cmd: str) -> None:
        self.commands.append(cmd)
        if self.silent:
            return
        if cmd == self.name:
            self.logged_in = True
            self._say("IPC ONLINE!")
            return
        if not self.logged_in:
            return  # an unaddressed unit ignores everything
        m = _SWITCH.match(cmd)
        if cmd == "DX0":
            self._say(*self._listing())
        elif m and 1 <= int(m.group(2)) <= OUTLETS:
            if not self.remote_disabled:
                self.state[int(m.group(2))] = m.group(1) == "N"
            self._say("DONE")
            if self.auto_event:
                self._say(*self._listing())
        else:
            self.logged_in = False
            self._say("INVALID")

    # -- Transport -----------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("simulator closed")
        self._partial += data.replace(b"\n", b"")
        while b"\r" in self._partial:
            line, self._partial = self._partial.split(b"\r", 1)
            self._handle(line.decode("ascii", "replace"))

    async def readline(self, timeout: float) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._replies:
            self._event.clear()
            left = deadline - loop.time()
            if left <= 0:
                raise PulizziTimeout(f"simulated unit: no answer within {timeout:g} s")
            try:
                await asyncio.wait_for(self._event.wait(), left)
            except TimeoutError:
                pass
        return self._replies.pop(0)

    def discard(self) -> None:
        self._replies.clear()

    def close(self) -> None:
        self.closed = True
