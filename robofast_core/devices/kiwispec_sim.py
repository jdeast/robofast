"""A TCP simulator of the KiwiSpec spectrograph PC, for tests.

It serves the two protocols :mod:`robofast_core.devices.kiwispec_client`
speaks, on loopback only:

* the legacy ``spectrograph_server.py`` (ASCII, one command per connection),
  reproduced with its quirks: commands are handled **one at a time** (the
  real server is single-threaded), each connection is read once (1024 bytes,
  3 s timeout), the text is parsed from ``repr()`` of the bytes (so a quote
  or backslash mangles it), a command must have exactly two tokens, and a
  command routed to a method the server does not define, or
  ``time_tracker_check`` on a lamp that is off, **kills the listener**
  (``crash_on_undefined``) until :meth:`KiwiSpecSim.restart`;
* the SI Image camera server (binary): ack then ``Done`` for each setting,
  ``Acquire`` acknowledged at once and ``Done`` after the exposure and the
  readout, ``TerminateAcquisition`` ends the exposure early (the frame is
  still read out) and gets its own ``Done``, ``GetStatusFromCamera`` answers
  ``name,value`` lines (the real format is not known; see docs/kiwispec.md).

The hardware model: the iodine stage travels at ``stage_speed`` mm/s (moves
return at once, as the real server starts them on a thread); homing and
``i2stage_connect`` (a power cycle) block the server for their duration; the
iodine cell heater relaxes toward its set point (or ambient when off) with
time constant ``heater_tau``; the exposure meter adds up counts once per
``expmeter_period`` from the light on the fiber (:meth:`set_sky`, plus the
slit-flat LED's and the backlight's rates) while it is logging, and trips
(stops counting for good, as the legacy loop does) above
``expmeter_max_safe`` counts in one reading; a readout takes ``readout``
seconds; the raw frame lands in the camera's buffer (``C:/IMAGES/I`` on the
PC) and ``save_image`` moves it to the file named by ``set_file_name``.

Faults (:meth:`set_fault`): ``hang`` (the spectrograph server reads a command
and never answers, blocking every later one, as the real single-threaded
server would), ``garble`` (replies are noise), ``drop`` (connections close
without a reply), ``camera_hang`` (acquisitions and terminations never
finish), ``ccd_warm`` (the CCD reads ``ccd_warm_temp``), ``stage_stuck``
(moves are accepted but the stage does not move). :meth:`crash` stops the
spectrograph server's listener; :meth:`restart` brings down ``spec``,
``camera`` or ``both`` for ``downtime`` seconds and back with fresh state
(an acquisition in flight is lost, the exposure-meter total starts at 0).

Timings default to the legacy values where the code has them (readout 22 s:
``control.specCalib``'s ``ro_time``; exposure meter 1 reading/s:
``MEASUREMENTSPERSEC``; the stage power cycle 10 s + 10 s) and are guesses
otherwise (stage speed, homing time); tests shrink them.

Run it standalone::

    python -m robofast_core.devices.kiwispec_sim --spec-port 44445 --camera-port 2055
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from robofast_core.devices.kiwispec_client import (
    ACK_FMT,
    COMMAND_FMT,
    DATA_DONE,
    DATA_FMT,
    DATA_STATUS,
    DONE_FMT,
    FN_ACQUIRE,
    FN_GET_STATUS,
    FN_SET_ACQ_MODE,
    FN_SET_ACQ_TYPE,
    FN_SET_CCD_FORMAT,
    FN_SET_COOLER,
    FN_SET_EXPTIME,
    FN_SET_READOUT_MODE,
    FN_TERMINATE,
    PKT_ACK,
    PKT_DATA,
)

log = logging.getLogger(__name__)

#: Commands spectrograph_server.py routes to methods it does not define.
UNDEFINED = frozenset(
    {
        "thar_turn_on",
        "thar_turn_off",
        "flat_turn_on",
        "flat_turn_off",
        "expose",
        "set_camera_param",
        "set_data_path",
        "get_status",
        "get_fits_header",
        "get_filter_name",
        "set_binning",
        "set_size",
    }
)
FAULTS = frozenset({"hang", "garble", "drop", "camera_hang", "ccd_warm", "stage_stuck"})


@dataclass
class SimConfig:
    readout: float = 22.0
    stage_speed: float = 3.0  # mm/s (guess)
    stage_home_time: float = 5.0  # s at the home switch, after travelling to 0 (guess)
    stage_power_cycle: float = 20.0  # i2stage_connect: 10 s off + 10 s on
    #: The server's i2stage_movef range, and its named positions (mm).
    stage_positions: dict[str, float] = field(
        default_factory=lambda: {"in": 11.0, "flat": 68.0, "out": 102.0}
    )
    stage_start: float = 11.0
    expmeter_period: float = 1.0
    #: The legacy server ships with the exposure-meter thread disabled; then
    #: get_expmeter_total answers 'fail'.
    expmeter_enabled: bool = True
    expmeter_max_safe: float = 30e6  # counts per reading (legacy MAXSAFECOUNT)
    led_rate: float = 2.0e4  # counts/s on the exposure meter with the LED on
    backlight_rate: float = 5.0e8  # enough to trip the meter (it must be paused)
    ccd_setpoint: float = -90.0
    ccd_temp: float = -90.0
    ccd_warm_temp: float = -40.0
    backplate_temp: float = 15.0
    heater_setpoint: float = 55.0
    heater_start: float = 55.0
    heater_ambient: float = 20.0
    heater_tau: float = 300.0
    spec_pressure: float = 0.002  # mbar
    pump_pressure: float = 0.01
    #: Every command's processing time on the spectrograph server.
    latency: float = 0.0
    #: The legacy server's per-connection read timeout.
    read_timeout: float = 3.0
    crash_on_undefined: bool = True
    backlight_failsafe: float = 60.0


class _Clock:
    def __call__(self) -> float:
        return time.monotonic()


class KiwiSpecSim:
    """Both servers and the hardware model, on one asyncio loop."""

    def __init__(
        self,
        config: SimConfig | None = None,
        *,
        host: str = "127.0.0.1",
        spec_port: int = 0,
        camera_port: int = 0,
    ) -> None:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the simulator only binds loopback addresses")
        self.config = config or SimConfig()
        self.host = host
        self.spec_port = spec_port
        self.camera_port = camera_port
        self.clock = _Clock()
        self.faults: set[str] = set()
        self.commands: list[str] = []  # every spectrograph-server command seen
        self.camera_commands: list[int] = []  # every SI function number seen
        self.crashed = False
        self._spec_server: asyncio.Server | None = None
        self._camera_server: asyncio.Server | None = None
        self._spec_lock = asyncio.Lock()
        self._conns: set[asyncio.StreamWriter] = set()
        self._camera_conns: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._expmeter_task: asyncio.Task | None = None
        self._unhang = asyncio.Event()
        self._sky: Callable[[float], float] = lambda t: 0.0
        self._sky_t0 = 0.0
        self._reset_state()

    # -- state ------------------------------------------------------------------

    def _reset_state(self) -> None:
        c = self.config
        now = self.clock()
        # iodine stage
        self.stage_connected = True
        self._stage_from = c.stage_start
        self._stage_to = c.stage_start
        self._stage_t0 = now
        # heater
        self.heater_on = True
        self.heater_setpoint = c.heater_setpoint
        self._heater_temp = c.heater_start
        self._heater_t = now
        # lamps
        self.led_on = False
        self.led_on_since: float | None = None
        self.backlight_on = False
        self._backlight_request = 0.0
        # exposure meter
        self.expmeter_logging = c.expmeter_enabled
        self.expmeter_total: float | None = 0.0 if c.expmeter_enabled else None
        self.expmeter_tripped = False
        self.expmeter_readings: list[tuple[float, float]] = []
        self._expmeter_t = now
        # files
        self.file_name = ""
        self.header_buffer = ""
        self.buffer_image: dict[str, Any] | None = None  # C:/IMAGES/I
        self.files: dict[str, dict[str, Any]] = {}
        # camera
        self.cooler_on = True
        self.acquisition: dict[str, Any] | None = None
        self.acq_settings: dict[str, Any] = {"mode": 0, "dark": False, "exptime": 0.0}

    def set_sky(self, rate: Callable[[float], float] | float) -> None:
        """Light on the fiber, in exposure-meter counts/s, as a function of
        the seconds since this call (or a constant)."""
        self._sky = rate if callable(rate) else (lambda t, r=float(rate): r)
        self._sky_t0 = self.clock()

    def light_rate(self, t: float) -> float:
        r = max(0.0, float(self._sky(t - self._sky_t0)))
        if self.led_on:
            r += self.config.led_rate
        if self.backlight_on:
            r += self.config.backlight_rate
        return r

    def stage_position(self) -> float:
        c = self.config
        if self._stage_to == self._stage_from or c.stage_speed <= 0:
            return self._stage_to
        dist = self._stage_to - self._stage_from
        travelled = (self.clock() - self._stage_t0) * c.stage_speed
        if travelled >= abs(dist):
            self._stage_from = self._stage_to
            return self._stage_to
        return self._stage_from + math.copysign(travelled, dist)

    def stage_moving(self) -> bool:
        return self.stage_position() != self._stage_to

    def _stage_go(self, target: float) -> None:
        pos = self.stage_position()
        if "stage_stuck" in self.faults:
            return
        self._stage_from, self._stage_to, self._stage_t0 = pos, target, self.clock()

    def heater_temp(self) -> float:
        c = self.config
        now = self.clock()
        goal = self.heater_setpoint if self.heater_on else c.heater_ambient
        dt = now - self._heater_t
        self._heater_temp = goal + (self._heater_temp - goal) * math.exp(-dt / c.heater_tau)
        self._heater_t = now
        return self._heater_temp

    def ccd_temp(self) -> float:
        if "ccd_warm" in self.faults or not self.cooler_on:
            return self.config.ccd_warm_temp
        return self.config.ccd_temp

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> KiwiSpecSim:
        await self._start_spec()
        await self._start_camera()
        self._expmeter_task = asyncio.get_running_loop().create_task(self._expmeter_loop())
        return self

    async def _start_spec(self) -> None:
        self._spec_server = await asyncio.start_server(
            self._spec_connection, self.host, self.spec_port, backlog=1
        )
        self.spec_port = self._spec_server.sockets[0].getsockname()[1]
        self.crashed = False

    async def _start_camera(self) -> None:
        self._camera_server = await asyncio.start_server(
            self._camera_connection, self.host, self.camera_port
        )
        self.camera_port = self._camera_server.sockets[0].getsockname()[1]

    async def _stop_spec(self) -> None:
        server, self._spec_server = self._spec_server, None
        if server is not None:
            server.close()
        for w in list(self._conns):
            w.close()
        self._unhang.set()
        if server is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await server.wait_closed()
        self._unhang = asyncio.Event()

    async def _stop_camera(self) -> None:
        server, self._camera_server = self._camera_server, None
        if server is not None:
            server.close()
        for w in list(self._camera_conns):
            w.close()
        acq, self.acquisition = self.acquisition, None
        if acq and acq.get("task"):
            acq["task"].cancel()
        if server is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await server.wait_closed()

    async def stop(self) -> None:
        if self._expmeter_task is not None:
            self._expmeter_task.cancel()
        await self._stop_spec()
        await self._stop_camera()
        for t in list(self._tasks):
            t.cancel()

    async def crash(self) -> None:
        """The spectrograph server's listener dies (as on an uncaught error)."""
        log.warning("sim: spectrograph server listener crashed")
        self.crashed = True
        await self._stop_spec()

    async def restart(self, which: str = "spec", downtime: float = 0.5) -> None:
        """Bring ``spec``, ``camera`` or ``both`` down for ``downtime`` s and
        back on the same ports with fresh state."""
        if which not in ("spec", "camera", "both"):
            raise ValueError(which)
        if which in ("spec", "both"):
            await self._stop_spec()
        if which in ("camera", "both"):
            await self._stop_camera()
        await asyncio.sleep(downtime)
        if which in ("spec", "both"):
            files, buffer_image = self.files, self.buffer_image
            acq = self.acquisition
            self._reset_state()
            # The disk and the camera's buffer outlive a server restart.
            self.files, self.buffer_image, self.acquisition = files, buffer_image, acq
            await self._start_spec()
        if which in ("camera", "both"):
            self.acquisition = None
            await self._start_camera()

    def set_fault(self, name: str, on: bool = True) -> None:
        if name not in FAULTS:
            raise ValueError(f"unknown fault {name!r}")
        if on:
            self.faults.add(name)
        else:
            self.faults.discard(name)
            if name == "hang":
                self._unhang.set()
                self._unhang = asyncio.Event()
            if name == "camera_hang" and self.acquisition and self.acquisition.get("hung"):
                self._spawn(self._finish_acquisition(self.acquisition))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- exposure meter -----------------------------------------------------------

    async def _expmeter_loop(self) -> None:
        c = self.config
        while True:
            await asyncio.sleep(c.expmeter_period)
            now = self.clock()
            t0, self._expmeter_t = self._expmeter_t, now
            if not self.expmeter_logging or self.expmeter_tripped or self.expmeter_total is None:
                continue
            n = 8
            dt = (now - t0) / n
            counts = sum(self.light_rate(t0 + (i + 0.5) * dt) * dt for i in range(n))
            if counts > c.expmeter_max_safe:
                log.error("sim: exposure meter reading %.0f > max safe; shutting it down", counts)
                self.expmeter_tripped = True
                self.expmeter_logging = False
                continue
            self.expmeter_total += counts
            self.expmeter_readings.append((now, counts))

    # -- the spectrograph server ------------------------------------------------------

    async def _spec_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._conns.add(writer)
        try:
            async with self._spec_lock:  # single-threaded, like the real one
                if self._spec_server is None:
                    return
                try:
                    async with asyncio.timeout(self.config.read_timeout):
                        data = await reader.read(1024)
                except TimeoutError:
                    return
                if not data:
                    return
                text = repr(data.decode("latin-1")).strip("'")
                tokens = text.split(None, 1)
                if tokens:
                    self.commands.append(tokens[0])
                if "hang" in self.faults:
                    await self._unhang.wait()
                    return
                if "drop" in self.faults:
                    return
                if self.config.latency:
                    await asyncio.sleep(self.config.latency)
                try:
                    reply = await self._process(tokens)
                except _Crash as exc:
                    log.error("sim: %s; the listener dies", exc)
                    self._spawn(self.crash())
                    return
                if "garble" in self.faults:
                    reply = "\x15\x80succ\x00ss 4"
                writer.write(reply.encode("latin-1"))
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            self._conns.discard(writer)
            writer.close()

    async def _process(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "fail"
        cmd, arg = tokens
        if cmd in UNDEFINED and self.config.crash_on_undefined:
            raise _Crash(f"AttributeError: server instance has no attribute {cmd!r}")
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return "fail"
        return await handler(arg)

    async def _cmd_cell_heater_on(self, arg: str) -> str:
        self.heater_temp()
        self.heater_on = True
        return "success"

    async def _cmd_cell_heater_off(self, arg: str) -> str:
        self.heater_temp()
        self.heater_on = False
        return "success"

    async def _cmd_cell_heater_temp(self, arg: str) -> str:
        return f"success {self.heater_temp():.1f} C"

    async def _cmd_cell_heater_set_temp(self, arg: str) -> str:
        try:
            value = float(arg)
        except ValueError:
            raise _Crash("IndexError parsing the heater's reply") from None
        self.heater_temp()
        self.heater_setpoint = value
        return f"success {value:.1f} C"

    async def _cmd_cell_heater_get_set_temp(self, arg: str) -> str:
        return f"success {self.heater_setpoint:.1f} C"

    async def _cmd_get_spec_pressure(self, arg: str) -> str:
        return f"success {self.config.spec_pressure}"

    async def _cmd_get_pump_pressure(self, arg: str) -> str:
        return f"success {self.config.pump_pressure}"

    async def _cmd_i2stage_get_pos(self, arg: str) -> str:
        if not self.stage_connected:
            return "fail"
        pos = self.stage_position()
        name = "unknown"
        for key, value in self.config.stage_positions.items():
            if round(pos, 1) == value:
                name = key
        return f"success {pos} {name}"

    async def _cmd_i2stage_move(self, arg: str) -> str:
        if not self.stage_connected:
            raise _Crash("AttributeError: no motorI2 (stage not connected)")
        target = self.config.stage_positions.get(arg.lower())
        if target is None:
            return "fail"
        self._stage_go(target)
        return "success"

    async def _cmd_i2stage_movef(self, arg: str) -> str:
        try:
            value = float(arg)
        except ValueError:
            raise _Crash("ValueError: float() in process_command") from None
        if value < 0.0 or value > 150.0:
            return "fail"
        if not self.stage_connected:
            raise _Crash("AttributeError: no motorI2 (stage not connected)")
        self._stage_go(value)
        return "success"

    async def _travel(self, target: float) -> None:
        self._stage_go(target)
        if "stage_stuck" in self.faults:
            return
        while self.stage_moving():
            await asyncio.sleep(0.01)

    async def _cmd_i2stage_home(self, arg: str) -> str:
        if not self.stage_connected:
            return "fail"
        if "stage_stuck" in self.faults:
            await asyncio.sleep(self.config.stage_home_time)
            return "fail"
        await self._travel(0.0)
        await asyncio.sleep(self.config.stage_home_time)
        return "success"

    async def _cmd_i2stage_connect(self, arg: str) -> str:
        await asyncio.sleep(self.config.stage_power_cycle)
        self.stage_connected = True
        await self._cmd_i2stage_home("None")
        self._stage_go(self.config.stage_positions["out"])
        self._stage_go(self.config.stage_positions["in"])
        return "success"

    async def _cmd_i2stage_disconnect(self, arg: str) -> str:
        self.stage_connected = False
        return "success"

    async def _cmd_led_turn_on(self, arg: str) -> str:
        if not self.led_on:
            self.led_on_since = self.clock()
        self.led_on = True
        return "success"

    async def _cmd_led_turn_off(self, arg: str) -> str:
        self.led_on = False
        self.led_on_since = None
        return "success"

    async def _cmd_time_tracker_check(self, arg: str) -> str:
        if not self.led_on or self.led_on_since is None:
            raise _Crash("UnboundLocalError: start (lamp log ends with a full line)")
        return f"success {self.clock() - self.led_on_since}"

    async def _cmd_backlight_on(self, arg: str) -> str:
        self.backlight_on = True
        self._backlight_request = self.clock()
        self._spawn(self._backlight_failsafe())
        return "success"

    async def _backlight_failsafe(self) -> None:
        request = self._backlight_request
        await asyncio.sleep(self.config.backlight_failsafe)
        if self.backlight_on and self._backlight_request == request:
            log.warning("sim: backlight failsafe turned the backlight off")
            self.backlight_on = False

    async def _cmd_backlight_off(self, arg: str) -> str:
        self.backlight_on = False
        return "success"

    async def _cmd_get_expmeter_total(self, arg: str) -> str:
        if self.expmeter_total is None:
            return "fail"
        return f"success {self.expmeter_total}"

    async def _cmd_reset_expmeter_total(self, arg: str) -> str:
        if self.expmeter_total is None and not self.config.expmeter_enabled:
            self.expmeter_total = 0.0  # legacy: the attribute now exists
            return "success"
        self.expmeter_total = 0.0
        return "success"

    async def _cmd_stop_log_expmeter(self, arg: str) -> str:
        if not self.config.expmeter_enabled:
            return "fail"  # exp_highvoltage does not exist
        self.expmeter_logging = False
        return "success"

    async def _cmd_start_log_expmeter(self, arg: str) -> str:
        if self.config.expmeter_enabled and not self.expmeter_tripped:
            self.expmeter_logging = True
        return "success"

    async def _cmd_get_index(self, arg: str) -> str:
        return f"success {len(self.files) + 1}"

    async def _cmd_set_file_name(self, arg: str) -> str:
        if len(arg.split()) != 1:
            return "fail"
        self.file_name = arg
        return "success"

    async def _cmd_save_image(self, arg: str) -> str:
        if self.buffer_image is None or not self.file_name:
            return "fail"
        self.files[self.file_name] = {**self.buffer_image, "header": {}}
        self.buffer_image = None
        return "success"

    async def _cmd_write_header(self, arg: str) -> str:
        self.header_buffer += arg
        return "success"

    async def _cmd_write_header_done(self, arg: str) -> str:
        text, self.header_buffer = self.header_buffer + arg, ""
        f = self.files.get(self.file_name)
        if f is None:
            return "fail"
        try:
            f["header"].update(json.loads(text))
        except ValueError:
            return "fail"
        return "success"

    # -- the SI camera server ---------------------------------------------------------

    async def _camera_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._camera_conns.add(writer)
        head = struct.calcsize(COMMAND_FMT)
        try:
            while True:
                first = await reader.readexactly(head)
                length, _pid, _cam, func, plen = struct.unpack(COMMAND_FMT, first)
                params = await reader.readexactly(plen) if plen else b""
                if length != head + plen:
                    log.error("sim: bad SI command length")
                    return
                self.camera_commands.append(func)
                await self._camera_command(func, params, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._camera_conns.discard(writer)
            writer.close()

    @staticmethod
    def _ack(accept: bool = True) -> bytes:
        return struct.pack(ACK_FMT, struct.calcsize(ACK_FMT), PKT_ACK, 1, 1 if accept else 0)

    @staticmethod
    def _done(func: int, err: int = 0) -> bytes:
        n = struct.calcsize(DONE_FMT)
        return struct.pack(DONE_FMT, n, PKT_DATA, 1, err, DATA_DONE, 2, func)

    @staticmethod
    def _data(dtype: int, payload: bytes, err: int = 0) -> bytes:
        n = struct.calcsize(DATA_FMT) + len(payload)
        return struct.pack(DATA_FMT, n, PKT_DATA, 1, err, dtype, len(payload)) + payload

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        if writer.is_closing():
            return
        writer.write(data)
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    async def _camera_command(self, func: int, params: bytes, w: asyncio.StreamWriter) -> None:
        s = self.acq_settings
        if func == FN_TERMINATE:  # no ack
            acq = self.acquisition
            if "camera_hang" in self.faults:
                return
            if acq is not None and acq["phase"] == "exposing":
                acq["terminate"].set()
            await self._send(w, self._done(FN_TERMINATE))
            return
        busy = self.acquisition is not None and func != FN_GET_STATUS
        await self._send(w, self._ack(not busy))
        if busy:
            return
        if func == FN_SET_ACQ_MODE:
            s["mode"] = params[0]
        elif func == FN_SET_ACQ_TYPE:
            s["dark"] = bool(struct.unpack(">HB", params)[1])
        elif func == FN_SET_EXPTIME:
            s["exptime"] = struct.unpack(">I", params)[0] / 1000.0
        elif func == FN_SET_COOLER:
            self.cooler_on = bool(params[0])
        elif func in (FN_SET_READOUT_MODE, FN_SET_CCD_FORMAT):
            pass
        elif func == FN_GET_STATUS:
            text = (
                f"CCD Temperature,{self.ccd_temp():.1f}\n"
                f"Backplate Temperature,{self.config.backplate_temp:.1f}\n"
                f"CCD Temperature Setpoint,{self.config.ccd_setpoint:.1f}\n"
                f"Cooler,{'On' if self.cooler_on else 'Off'}\n"
            )
            await self._send(w, self._data(DATA_STATUS, text.encode() + b"\x00"))
            return
        elif func == FN_ACQUIRE:
            acq = {
                "phase": "exposing",
                "terminate": asyncio.Event(),
                "writer": w,
                "settings": dict(s),
                "hung": False,
                "ccd_temp": self.ccd_temp(),
            }
            self.acquisition = acq
            acq["task"] = self._spawn(self._run_acquisition(acq))
            return
        else:
            await self._send(w, self._done(func, err=1))
            return
        await self._send(w, self._done(func))

    async def _run_acquisition(self, acq: dict[str, Any]) -> None:
        exptime = acq["settings"]["exptime"]
        acq["start"] = self.clock()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(exptime):
                await acq["terminate"].wait()
        acq["end"] = self.clock()
        if "camera_hang" in self.faults:
            acq["hung"] = True
            return
        await self._finish_acquisition(acq)

    async def _finish_acquisition(self, acq: dict[str, Any]) -> None:
        acq["phase"] = "reading"
        acq["hung"] = False
        await asyncio.sleep(self.config.readout)
        if self.acquisition is not acq:
            return  # the camera restarted under it
        settings = acq["settings"]
        self.buffer_image = {
            "exptime_requested": settings["exptime"],
            "exptime_actual": acq["end"] - acq["start"],
            "dark": settings["dark"],
            "terminated": acq["terminate"].is_set(),
            "ccd_temp": acq["ccd_temp"],
            "stage_mm": self.stage_position(),
        }
        self.acquisition = None
        await self._send(acq["writer"], self._done(FN_ACQUIRE))


class _Crash(Exception):
    """An uncaught exception in the real server's accept loop."""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--spec-port", type=int, default=44445)
    ap.add_argument("--camera-port", type=int, default=2055)
    ap.add_argument("--sky-rate", type=float, default=5000.0, help="expmeter counts/s")
    args = ap.parse_args(argv)
    logging.basicConfig(level="INFO")

    async def run() -> None:
        sim = KiwiSpecSim(host=args.host, spec_port=args.spec_port, camera_port=args.camera_port)
        await sim.start()
        sim.set_sky(args.sky_rate)
        print(f"kiwispec simulator on {args.host}:{sim.spec_port} (camera {sim.camera_port})")
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
