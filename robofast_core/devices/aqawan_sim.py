"""A TCP simulator of the Aqawan PAC controller, for tests and ConformU.

It speaks the wire protocol :mod:`robofast_core.devices.aqawan_pac` describes,
including the quirks the legacy driver implies, each switchable:

* ``negotiate``: offers a telnet option (``IAC DO TERMINAL-TYPE``) on connect;
* the first line of a session is the terminal type (``vt100``) and is
  consumed; ``settle``: command lines arriving within this many seconds of it
  are silently dropped (the *assumed* reason the legacy client re-sent every
  command, see that module);
* ``prompt``: replies end ``\\r\\n\\r\\n#>`` (and a bare ``#>`` follows the
  terminal type); without it, replies end at ``\\r\\n`` only;
* ``echo``: command lines are echoed back.

The enclosure: two shutters that take ``open_time``/``close_time`` to move;
``CLOSE_SEQUENTIAL`` closes them one after the other (``close_order``).
The heartbeat watchdog: ``HEARTBEAT`` restarts a ``heartbeat_timeout``
countdown that is already expired at power-on. While it is expired the
shutters may not open (``Success=FALSE, Heartbeat timer expired``), and if it
runs out with a shutter not closed, the PAC closes sequentially by itself.
``heartbeat_timeout`` of ``None`` disables the watchdog (for ConformU). Its
default, 60 s, is the real PAC's timeout (confirmed by JDE, 2026-10-31); the
supervisor must ping at least four times per timeout
(``SafetyConfig.heartbeat_interval`` <= ``heartbeat_timeout``/4, as legacy
did: "ensure 4 heartbeats before timeout").

Fault injection (thread-safe methods, or ``SIM ...`` lines on the same port
when it runs as a process): :meth:`set_fault` (an E-stop-like fault: motion
stops and is refused; ``clearable=False`` survives ``CLEAR_FAULTS``;
``blocks_motion=False`` is a fault that only refuses opening, so a close
still works),
:meth:`set_error`, :meth:`set_door`, :meth:`set_op_mode` (``MANUAL`` refuses
motion), :meth:`set_mode`: ``ok``, ``hang`` (read, never answer), ``silent``
(close every connection at once), ``garble`` (truncate ``STATUS``), ``slow``
(answer after ``slow_delay``).

Status values the legacy code never shows (door states, the meaning of
``Heartbeat``, a stopped shutter's state) are guesses: doors ``OPEN``/
``CLOSED``, ``Heartbeat`` = seconds left, a stopped shutter ``STOPPED``. The
server does not depend on the last one: any shutter string it does not know
is ``shutterError``, and it logs the raw string so the real one is learned.
An E-stop-like fault stops motion here, so a shutter is "closed and faulted" or "stopped
part-way and faulted"; the server keeps the two apart.

Run it standalone::

    python -m robofast_core.devices.aqawan_sim --port 22004

It prints ``aqawan simulator on HOST:PORT`` once listening. It only binds
loopback addresses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import threading
import time
from dataclasses import dataclass

from robofast_core.devices.aqawan_pac import COMMANDS, TelnetFilter

log = logging.getLogger(__name__)

_IAC_DO_TTYPE = bytes([255, 253, 24])
MODES = ("ok", "hang", "silent", "garble", "slow")


@dataclass
class _Shutter:
    state: str = "CLOSED"
    target: str | None = None  # "OPEN"/"CLOSED" while moving
    done_at: float = 0.0


class AqawanSim:
    """The simulated PAC. :meth:`start` serves it on a background thread."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        open_time: float = 1.0,
        close_time: float = 1.0,
        heartbeat_timeout: float | None = 60.0,
        settle: float = 0.3,
        prompt: bool = True,
        negotiate: bool = True,
        echo: bool = False,
        close_order: tuple[int, int] = (1, 2),
        slow_delay: float = 2.0,
    ) -> None:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the simulator binds loopback addresses only")
        self.host, self.port = host, port
        self.open_time, self.close_time = open_time, close_time
        self.heartbeat_timeout = heartbeat_timeout
        self.settle, self.prompt, self.negotiate, self.echo = settle, prompt, negotiate, echo
        self.close_order = close_order
        self.slow_delay = slow_delay
        self._lock = threading.RLock()
        self._mode_changed = threading.Condition(self._lock)
        self.mode = "ok"
        self._booted = time.monotonic()
        self._shutters = {1: _Shutter(), 2: _Shutter()}
        self._then_close: int | None = None  # CLOSE_SEQUENTIAL's second shutter
        self._hb_deadline: float | None = None  # expired at power-on
        self.fault: str | None = None
        self.fault_clearable = True
        self.fault_blocks_motion = True
        self.error: str | None = None
        self.doors = {"EntryDoor1": "CLOSED", "EntryDoor2": "CLOSED", "PanelDoor": "CLOSED"}
        self.op_mode = "AUTO"
        self.lights_on = True
        #: Commands executed, as (monotonic time, command).
        self.log: list[tuple[float, str]] = []
        self.dropped = 0
        self.watchdog_trips = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None

    # -- the enclosure --------------------------------------------------------

    def _tick(self, now: float) -> None:
        """Advance the model to ``now`` (under the lock)."""
        for n, sh in self._shutters.items():
            if sh.target is not None and now >= sh.done_at:
                sh.state, sh.target = sh.target, None
                if sh.state == "CLOSED" and self._then_close is not None and n != self._then_close:
                    second, self._then_close = self._then_close, None
                    self._move(second, "CLOSED", sh.done_at)
        if self._hb_expired(now) and self.heartbeat_timeout is not None:
            moving_closed = all(
                s.state == "CLOSED" or s.target == "CLOSED" or n == self._then_close
                for n, s in self._shutters.items()
            )
            if not moving_closed and not (self.fault and self.fault_blocks_motion):
                self.watchdog_trips += 1
                log.warning("sim: heartbeat expired; closing")
                self._close_sequential(now)

    def _hb_expired(self, now: float) -> bool:
        if self.heartbeat_timeout is None:
            return False
        return self._hb_deadline is None or now > self._hb_deadline

    def _move(self, n: int, target: str, now: float) -> None:
        sh = self._shutters[n]
        if sh.state == target and sh.target is None:
            return
        if sh.target == target:
            return
        travel = self.open_time if target == "OPEN" else self.close_time
        sh.state = "OPENING" if target == "OPEN" else "CLOSING"
        sh.target, sh.done_at = target, now + travel

    def _close_sequential(self, now: float) -> None:
        first, second = self.close_order
        if self._shutters[first].state == "CLOSED" and self._shutters[first].target is None:
            self._move(second, "CLOSED", now)
        else:
            self._move(first, "CLOSED", now)
            if not (
                self._shutters[second].state == "CLOSED" and self._shutters[second].target is None
            ):
                # The second waits for the first, unless it is already closing.
                if self._shutters[second].target != "CLOSED":
                    self._then_close = second

    def _stop_all(self) -> None:
        self._then_close = None
        for sh in self._shutters.values():
            if sh.target is not None:
                sh.state, sh.target = "STOPPED", None

    def shutter_states(self) -> tuple[str, str]:
        with self._lock:
            self._tick(time.monotonic())
            return self._shutters[1].state, self._shutters[2].state

    def closed(self) -> bool:
        return self.shutter_states() == ("CLOSED", "CLOSED")

    def open(self) -> bool:
        return self.shutter_states() == ("OPEN", "OPEN")

    def commands(self) -> list[str]:
        with self._lock:
            return [c for _, c in self.log]

    # -- fault injection --------------------------------------------------------

    def set_fault(
        self, text: str | None, *, clearable: bool = True, blocks_motion: bool = True
    ) -> None:
        with self._lock:
            self._tick(time.monotonic())
            self.fault, self.fault_clearable = text, clearable
            self.fault_blocks_motion = blocks_motion
            if text is not None and blocks_motion:
                self._stop_all()

    def set_error(self, text: str | None) -> None:
        with self._lock:
            self.error = text

    def set_door(self, door: str, state: str) -> None:
        with self._lock:
            if door not in self.doors:
                raise ValueError(door)
            self.doors[door] = state.upper()

    def set_op_mode(self, mode: str) -> None:
        with self._lock:
            self.op_mode = mode.upper()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        with self._mode_changed:
            self.mode = mode
            self._mode_changed.notify_all()

    def heartbeat(self) -> None:
        """A HEARTBEAT, as if sent over the wire."""
        with self._lock:
            now = time.monotonic()
            self._tick(now)
            if self.heartbeat_timeout is not None:
                self._hb_deadline = now + self.heartbeat_timeout

    # -- commands ----------------------------------------------------------------

    def status_text(self, now: float) -> str:
        hb = 0.0
        if self.heartbeat_timeout is None:
            hb = 0.0
        elif self._hb_deadline is not None:
            hb = max(0.0, self._hb_deadline - now)
        pairs = [
            ("Shutter1", self._shutters[1].state),
            ("Shutter2", self._shutters[2].state),
            ("SWVersion", "SIM-1.0"),
            ("EnclHumidity", "35.0"),
            *self.doors.items(),
            ("Heartbeat", f"{int(hb)}"),
            ("SystemUpTime", f"{int(now - self._booted)}"),
            ("Fault", "TRUE" if self.fault else "FALSE"),
            ("Error", "TRUE" if self.error else "FALSE"),
            ("PanelExhaustTemp", "21.5"),
            ("EnclTemp", "12.0"),
            ("EnclExhaustTemp", "12.5"),
            ("EnclIntakeTemp", "11.5"),
            ("LightsOn", "TRUE" if self.lights_on else "FALSE"),
            ("EnclOpMode", self.op_mode),
        ]
        return ", ".join(f"{k}={v}" for k, v in pairs)

    def _motion_refusal(self, now: float, opening: bool) -> str | None:
        if self.fault and (opening or self.fault_blocks_motion):
            return self.fault
        if self.op_mode != "AUTO":
            return "Enclosure not in AUTO"
        if opening and self._hb_expired(now):
            return "Heartbeat timer expired"
        return None

    def execute(self, command: str) -> str:
        """Run one PAC command; return the reply text (no framing)."""
        with self._lock:
            now = time.monotonic()
            self._tick(now)
            self.log.append((now, command))
            if command not in COMMANDS:
                return "Success=FALSE, Unknown command"
            if command == "STATUS":
                return self.status_text(now)
            if command == "HEARTBEAT":
                if self.heartbeat_timeout is not None:
                    self._hb_deadline = now + self.heartbeat_timeout
                return "Success=TRUE"
            if command == "GET_FAULTS":
                return f"Faults={self.fault or 'None'}"
            if command == "GET_ERRORS":
                return f"Errors={self.error or 'None'}"
            if command == "CLEAR_FAULTS":
                if self.fault and not self.fault_clearable:
                    return f"Success=FALSE, {self.fault}"
                self.fault = None
                return "Success=TRUE"
            if command == "CLEAR_ERRORS":
                self.error = None
                return "Success=TRUE"
            if command in ("LIGHTS_ON", "LIGHTS_OFF"):
                self.lights_on = command == "LIGHTS_ON"
                return "Success=TRUE"
            if command == "STOP":
                self._stop_all()
                return "Success=TRUE"
            if command == "RESET_PAC":
                return "Success=TRUE"
            opening = command.startswith("OPEN")
            if command.startswith(("OPEN", "CLOSE")):
                refusal = self._motion_refusal(now, opening)
                if refusal:
                    return f"Success=FALSE, {refusal}"
                if command == "OPEN_SHUTTERS":
                    self._then_close = None
                    self._move(1, "OPEN", now)
                    self._move(2, "OPEN", now)
                elif command in ("OPEN_SHUTTER_1", "OPEN_SHUTTER_2"):
                    n = int(command[-1])
                    if self._then_close == n:
                        self._then_close = None
                    self._move(n, "OPEN", now)
                elif command == "CLOSE_SHUTTERS":
                    self._then_close = None
                    self._move(1, "CLOSED", now)
                    self._move(2, "CLOSED", now)
                elif command == "CLOSE_SEQUENTIAL":
                    self._close_sequential(now)
                else:  # CLOSE_SHUTTER_n
                    self._move(int(command[-1]), "CLOSED", now)
                return "Success=TRUE"
            return "Success=TRUE"  # fans, LEDs, alerts, MCBs: accepted, not modelled

    def _control(self, args: list[str]) -> str:
        """``SIM ...`` lines: fault injection for a standalone simulator."""
        what = args[0].upper() if args else ""
        rest = " ".join(args[1:])
        if what == "FAULT":
            self.set_fault(rest or None)
        elif what == "LATCHFAULT":
            self.set_fault(rest or "Estop active", clearable=False)
        elif what == "SOFTFAULT":
            self.set_fault(rest or "Motor overload", blocks_motion=False)
        elif what == "ERROR":
            self.set_error(rest or None)
        elif what == "DOOR" and len(args) == 3:
            self.set_door(args[1], args[2])
        elif what == "OPMODE" and len(args) == 2:
            self.set_op_mode(args[1])
        elif what == "MODE" and len(args) == 2:
            self.set_mode(args[1])
        elif what == "STATE":
            with self._lock:
                s1, s2 = self.shutter_states()
                return json.dumps(
                    {"shutters": [s1, s2], "trips": self.watchdog_trips, "dropped": self.dropped}
                )
        else:
            return "SIM ERROR unknown control"
        return "SIM OK"

    # -- the wire -------------------------------------------------------------------

    def _frame(self, text: str) -> bytes:
        return (text + ("\r\n\r\n#>" if self.prompt else "\r\n")).encode()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.mode == "silent":
                return
            if self.negotiate:
                writer.write(_IAC_DO_TTYPE)
            telnet = TelnetFilter()
            buf = b""
            ttype_at: float | None = None
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buf += telnet.feed(chunk)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("ascii", "replace").strip()
                    if not line:
                        continue
                    if ttype_at is None:
                        ttype_at = time.monotonic()
                        if self.prompt:
                            writer.write(b"\r\n#>")
                        continue
                    if line.startswith("SIM "):
                        writer.write(self._frame(self._control(line.split()[1:])))
                        continue
                    if time.monotonic() - ttype_at < self.settle:
                        with self._lock:
                            self.dropped += 1
                        continue
                    if self.echo:
                        writer.write(line.encode() + b"\r\n")
                    mode = self.mode
                    if mode == "hang":
                        await self._wait_mode_change()
                        return
                    if mode == "silent":
                        return
                    if mode == "slow":
                        await asyncio.sleep(self.slow_delay)
                    reply = self.execute(line)
                    if mode == "garble" and line == "STATUS":
                        reply = reply[: len(reply) * 2 // 5]
                    writer.write(self._frame(reply))
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def _wait_mode_change(self) -> None:
        assert self._stopping is not None
        while self.mode == "hang" and not self._stopping.is_set():
            await asyncio.sleep(0.05)

    async def _ticker(self) -> None:
        while True:
            with self._lock:
                self._tick(time.monotonic())
            await asyncio.sleep(0.02)

    async def _serve(self) -> None:
        self._stopping = asyncio.Event()
        self._server = await asyncio.start_server(self._session, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        ticker = asyncio.create_task(self._ticker())
        self._ready.set()
        try:
            await self._stopping.wait()
        finally:
            ticker.cancel()
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> AqawanSim:
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            try:
                self._loop.run_until_complete(self._serve())
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, name="aqawan-sim", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("simulator did not start")
        return self

    def stop(self) -> None:
        self.set_mode("ok")
        if self._loop is not None and self._stopping is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--open-time", type=float, default=3.0)
    ap.add_argument("--close-time", type=float, default=3.0)
    ap.add_argument(
        "--heartbeat-timeout", type=float, default=60.0, help="seconds; 0 disables the watchdog"
    )
    ap.add_argument("--settle", type=float, default=0.3)
    ap.add_argument("--no-prompt", action="store_true")
    ap.add_argument("--no-negotiate", action="store_true")
    ap.add_argument("--echo", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sim = AqawanSim(
        args.host,
        args.port,
        open_time=args.open_time,
        close_time=args.close_time,
        heartbeat_timeout=args.heartbeat_timeout or None,
        settle=args.settle,
        prompt=not args.no_prompt,
        negotiate=not args.no_negotiate,
        echo=args.echo,
    ).start()
    print(f"aqawan simulator on {sim.address}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
