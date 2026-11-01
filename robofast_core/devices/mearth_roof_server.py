"""Alpaca Dome server for the MEarth South roll-off roof.

One Alpaca **Dome** (device 0) stands for the roof, driven through its
parallel-port interface box
(:class:`~robofast_core.devices.mearth_roof_port.RoofInterface`; protocol and
hardware in that module, open questions in ``docs/mearth_roof.md``). The
control logic follows the MEarth observing system's roof daemon (adapted from
the MEarth observing system, J. Irwin, used with permission), with the
changes listed under "Differences from legacy".

Members
-------
* ``OpenShutter`` latches ``o`` and returns; a monitor task (every
  ``poll_interval``) watches the limit switches and latches stop once the
  open limit is made. Refused (``InvalidOperationException``) unless the
  closed limit is made (or the roof is already open), while a roof fault is
  latched, while both limits read made, or while an emergency close is
  latched.
* ``CloseShutter`` latches ``c`` (always: the drive ignores it at the closed
  end, and a close must never depend on what the switches say) and is
  watched the same way, ending at the closed limit.
* ``AbortSlew`` latches stop.
* ``ShutterStatus`` (the roof's position; a fault is a separate axis, as for
  the Aqawan): both limits made -> ``shutterError``; a move in progress ->
  ``shutterOpening``/``shutterClosing``; closed limit -> ``shutterClosed``
  (even with a fault latched); a latched fault -> ``shutterError``; open
  limit -> ``shutterOpen``; neither limit and no move -> ``shutterError``
  (position unknown: moved by hand, or the limit contacts' supply is off).
* ``Action("Faults", "")``: the read-only fault channel the safety
  supervisor polls (``SafetyConfig.fault_action``), JSON ``{"fault",
  "error", "faults", "errors", "estop", "shutters", "limits", "stuck"}``.
  ``fault`` is a latched roof fault (a move that timed out or never left its
  starting limit), both limits made, an unknown position, or an emergency
  close latch. ``shutters`` is ``["OPEN"]``, ``["CLOSED"]``, ``["MOVING"]``
  or ``["UNKNOWN"]``.
* ``Action("ClearFaults", note)`` clears the latched fault (and an
  emergency close latch); refused unless the closed limit is made, the open
  limit is not and nothing moves. Privileged at the dome owner
  (``"Action:ClearFaults"`` in :data:`robofast_core.safety.ENCLOSURE_MEMBERS`):
  for an operator, through the supervisor. Legacy cleared its STUCK flag
  when an operator commanded the roof where it already was; here that is an
  explicit, logged clear.
* ``Action("Status", "")``: JSON of the limits, the move, the fault and the
  last latched command.
* There is no ``Heartbeat`` action: the hardware has no watchdog, so a site
  on this roof is ``attended_only``.
* ``PUT /robofast/v1/emergency_close`` as for the Aqawan server
  (:mod:`robofast_core.emergency`), when configured.

Differences from legacy
-----------------------
* Legacy only timed a move out (180 s). Here a move that has not left its
  starting limit within ``departure_timeout`` (30 s, *unconfirmed*) is a
  fault at once: the commonest cause in legacy's operator guide is the
  roof left on local control, and the VFD trip is the next.
* Legacy treated "neither limit" as unknown and carried on; here it is a
  fault that blocks opening (the supervisor then closes).
* Legacy changed an open into a close, and kept a close latched past its
  timeout, during a UPS power failure. robofast has no power-fail input to
  this server yet; a UPS-on-battery signal belongs to the supervisor's
  weather side (``docs/mearth_roof.md``).

Safety
------
``port.type`` ``"ppdev"`` (a real ``/dev/parportN``) needs
``"allow_hardware": true`` in the config **and** ``--hardware`` on the
command line; ``"sim"`` runs the in-process simulator
(:mod:`robofast_core.devices.mearth_roof_sim`). Starting the server never
writes to the port; stopping it sends nothing (the box keeps its latch, so
a move in progress completes).

Running
-------
::

    python -m robofast_core.devices.mearth_roof_server --config roof.json [--hardware]

with a JSON config (``sites/mearth_south/roof.json``). It prints one JSON
line, ``{"url": ..., "port": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any

from robofast_core.cancel import stop_task
from robofast_core.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    CAN,
    CLOSED,
    CLOSING,
    COMMON_NAMES,
    DRIVER_ERROR,
    ERROR,
    INVALID_OPERATION,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    NOT_IMPLEMENTED_GET,
    NOT_IMPLEMENTED_PUT,
    OPEN,
    OPENING,
    PAC_TIMEOUT,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
    _Handler,
)
from robofast_core.devices.mearth_roof_port import (
    CMD_CLOSE,
    CMD_OPEN,
    CMD_STOP,
    COMMAND_NAMES,
    HardwareNotAllowed,
    Limits,
    PPDevPort,
    RoofInterface,
    RoofPort,
    RoofPortError,
    RoofPortTimeout,
    StrobeTiming,
)
from robofast_core.devices.mearth_roof_sim import MEarthSouthRoofSim
from robofast_core.emergency import EmergencyConfig, EmergencyGate, parse_body
from robofast_core.safety import AUTO_CLEAR_PREFIX

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
ACTIONS = ("Status", "Faults", "ClearFaults")
SIM_KEYS = frozenset({"travel_time", "position", "drive_on_ups"})


# -- configuration ----------------------------------------------------------------


@dataclass
class PortConfig:
    #: "ppdev" (a real /dev/parportN; gated) or "sim" (in-process simulator).
    type: str = "ppdev"
    device: str = "/dev/parport0"
    #: Simulator parameters (``MEarthSouthRoofSim``) when type is "sim".
    sim: dict[str, Any] = field(default_factory=dict)
    #: Strobe timing, seconds (legacy 1 ms each).
    strobe_setup: float = 0.001
    strobe_width: float = 0.001
    strobe_hold: float = 0.001
    #: Bound on one port operation.
    timeout: float = 2.0


@dataclass
class MEarthRoofConfig:
    port: PortConfig = field(default_factory=PortConfig)
    #: Must be true (and ``--hardware`` given) for a real port.
    allow_hardware: bool = False
    name: str = "mearth-south"
    listen: str = "127.0.0.1:11150"
    #: Whole-move bounds (legacy South: 180 s each way).
    open_timeout: float = 180.0
    close_timeout: float = 180.0
    #: A move must leave its starting limit within this (unconfirmed).
    departure_timeout: float = 30.0
    #: Limit-switch polling period of the monitor task.
    poll_interval: float = 0.25
    #: ShutterStatus reuses a limit reading this recent.
    status_max_age: float = 0.5
    #: Bound on each Alpaca request (the supervisor's dome_timeout is 5 s).
    request_timeout: float = 4.5
    discovery_port: int | None = None
    emergency_close: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MEarthRoofConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        port = {k: v for k, v in d.pop("port", {}).items() if not k.startswith("_")}
        port_names = {f.name for f in dataclasses.fields(PortConfig)}
        if set(port) - port_names:
            raise ValueError(f"unknown port keys: {sorted(set(port) - port_names)}")
        cfg = cls(port=PortConfig(**port), **d)
        if cfg.port.type not in ("ppdev", "sim"):
            raise ValueError("port.type must be 'ppdev' or 'sim'")
        if set(cfg.port.sim) - SIM_KEYS:
            raise ValueError(f"unknown port.sim keys: {sorted(set(cfg.port.sim) - SIM_KEYS)}")
        for name in ("open_timeout", "close_timeout", "departure_timeout", "poll_interval"):
            if getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if cfg.departure_timeout >= min(cfg.open_timeout, cfg.close_timeout):
            raise ValueError("departure_timeout must be shorter than the move timeouts")
        EmergencyConfig.from_dict(cfg.emergency_close)  # validated here
        return cfg


def make_port(cfg: PortConfig, *, allow_hardware: bool) -> RoofPort:
    if cfg.type == "sim":
        return MEarthSouthRoofSim(**cfg.sim)
    return PPDevPort(cfg.device, allow_hardware=allow_hardware)


# -- the roof ------------------------------------------------------------------------


@dataclass
class Move:
    command: int  # CMD_OPEN or CMD_CLOSE
    started: float
    left_start: bool = False

    @property
    def opening(self) -> bool:
        return self.command == CMD_OPEN


class MEarthSouthRoof:
    """The roof logic over a :class:`RoofInterface`. Runs on one event loop."""

    def __init__(
        self, iface: RoofInterface, config: MEarthRoofConfig, gate: EmergencyGate | None = None
    ) -> None:
        self.iface = iface
        self.config = config
        self.gate = gate
        self._limits: Limits | None = None
        self._limits_at = -1e9
        self._read_lock = asyncio.Lock()
        self._move: Move | None = None
        #: A latched roof fault (a failed move); cleared only by ClearFaults.
        self.stuck: str | None = None
        self._monitor: asyncio.Task | None = None
        self._last_seen: str | None = None

    def start(self) -> None:
        self._monitor = asyncio.ensure_future(self._run_monitor())

    async def shutdown(self) -> None:
        await stop_task(self._monitor, 2.0)

    # -- reading ------------------------------------------------------------------

    async def limits(self, max_age: float | None = None) -> Limits:
        max_age = self.config.status_max_age if max_age is None else max_age
        async with self._read_lock:
            if self._limits is None or time.monotonic() - self._limits_at > max_age:
                t = time.monotonic()
                lim = await self.iface.limits()
                self._limits, self._limits_at = lim, t
                seen = lim.describe()
                if seen != self._last_seen:
                    log.info("roof limits: %s (status 0x%02x)", seen, lim.raw)
                    self._last_seen = seen
            return self._limits

    @property
    def moving(self) -> bool:
        return self._move is not None

    # -- the monitor (legacy's main-loop roof step) -----------------------------------

    async def _run_monitor(self) -> None:
        while True:
            try:
                await self._step()
            except RoofPortError as exc:
                log.error("roof monitor: %s", exc)
            await asyncio.sleep(self.config.poll_interval)

    async def _step(self) -> None:
        move = self._move
        if move is None:
            return
        lim = await self.limits(max_age=0)
        if self._move is not move:
            return  # superseded while reading
        cfg = self.config
        elapsed = time.monotonic() - move.started
        target = lim.open if move.opening else lim.closed
        start_made = lim.closed if move.opening else lim.open
        what = COMMAND_NAMES[move.command]
        if lim.both:
            await self._fail(f"both limits made during {what}")
            return
        if target:
            log.info("roof %s complete after %.1f s", what, elapsed)
            await self._end_move()
            return
        if not start_made:
            move.left_start = True
        if not move.left_start and elapsed > cfg.departure_timeout:
            await self._fail(
                f"{what}: roof did not leave its starting limit within"
                f" {cfg.departure_timeout:g} s (local control? drive tripped? no power?)"
            )
            return
        timeout = cfg.open_timeout if move.opening else cfg.close_timeout
        if elapsed > timeout:
            await self._fail(f"{what}: limit not reached within {timeout:g} s ({lim.describe()})")

    async def _end_move(self) -> None:
        self._move = None
        try:
            await self.iface.send(CMD_STOP)
        except RoofPortError as exc:
            log.error("roof: stop after move failed: %s", exc)

    async def _fail(self, why: str) -> None:
        log.error("ROOF STUCK: %s; stopping", why)
        self.stuck = why
        await self._end_move()

    # -- commands -------------------------------------------------------------------

    def _refusal(self, lim: Limits) -> str | None:
        latched = None if self.gate is None else self.gate.describe()
        if latched is not None:
            return f"{latched} (ClearFaults, with the roof closed, releases it)"
        if self.stuck is not None:
            return f"roof fault latched: {self.stuck}"
        if lim.both:
            return "both limit switches read made"
        if not lim.closed and not lim.open:
            return "roof position unknown (neither limit made)"
        return None

    async def open(self) -> None:
        if self._move is not None and self._move.opening:
            return
        lim = await self.limits(max_age=0)
        refusal = self._refusal(lim)
        if refusal:
            raise AlpacaException(INVALID_OPERATION, f"not opening: {refusal}")
        if lim.open and self._move is None:
            return
        await self._start(CMD_OPEN)

    async def close(self) -> None:
        await self._start(CMD_CLOSE)

    async def _start(self, command: int) -> None:
        self._move = Move(command, time.monotonic())
        try:
            await self.iface.send(command)
        except BaseException:
            # Unknown whether it latched: keep watching a close (the monitor
            # times it out); forget an open.
            if command != CMD_CLOSE:
                self._move = None
            raise
        self._limits_at = -1e9

    async def abort(self) -> None:
        self._move = None
        await self.iface.send(CMD_STOP)

    # -- state ----------------------------------------------------------------------

    async def shutter_status(self) -> int:
        lim = await self.limits()
        move = self._move
        if lim.both:
            return ERROR
        if move is not None:
            if move.opening:
                return OPEN if lim.open else OPENING
            return CLOSED if lim.closed else CLOSING
        if lim.closed:
            return CLOSED
        if self.stuck is not None:
            return ERROR
        if lim.open:
            return OPEN
        return ERROR

    def _position(self, lim: Limits) -> str:
        if self._move is not None:
            return "MOVING"
        if lim.both or lim.neither:
            return "UNKNOWN"
        return "OPEN" if lim.open else "CLOSED"

    async def faults(self) -> dict[str, Any]:
        lim = await self.limits()
        problems = []
        if self.stuck is not None:
            problems.append(self.stuck)
        if lim.both:
            problems.append("both limit switches read made")
        elif lim.neither and self._move is None:
            problems.append("roof position unknown (neither limit made)")
        latched = None if self.gate is None else self.gate.describe()
        if latched is not None:
            problems.append(latched)
        out: dict[str, Any] = {
            "fault": bool(problems),
            "error": False,
            "faults": "; ".join(problems) or None,
            "errors": None,
            "estop": False,
            "shutters": [self._position(lim)],
            "limits": {"open": lim.open, "closed": lim.closed},
            "stuck": self.stuck,
        }
        if self.gate is not None:
            out["emergency_close"] = self.gate.latched
        return out

    async def status(self) -> dict[str, Any]:
        lim = await self.limits(max_age=0)
        move = self._move
        return {
            "limits": {"open": lim.open, "closed": lim.closed, "raw": lim.raw},
            "moving": None if move is None else COMMAND_NAMES[move.command],
            "move_elapsed": None if move is None else round(time.monotonic() - move.started, 1),
            "stuck": self.stuck,
            "last_command": (
                None if self.iface.last_command is None else COMMAND_NAMES[self.iface.last_command]
            ),
        }

    async def clear_faults(self, note: str, *, auto: bool = False) -> str:
        lim = await self.limits(max_age=0)
        if not lim.closed or lim.open or self._move is not None:
            raise AlpacaException(
                INVALID_OPERATION, f"not clearing faults: roof not closed ({lim.describe()})"
            )
        if auto and self.gate is not None and self.gate.latched:
            raise AlpacaException(INVALID_OPERATION, "not auto-clearing an emergency close latch")
        log.warning("ClearFaults (%s); was: %s", note or "no note", self.stuck)
        was, self.stuck = self.stuck, None
        replies = [f"cleared: {was}" if was else "no roof fault latched"]
        if self.gate is not None and self.gate.clear(note or "ClearFaults"):
            replies.append("emergency close latch cleared")
        return "; ".join(replies)


# -- Alpaca over HTTP ----------------------------------------------------------------


class MEarthRoofServer(ThreadingHTTPServer):
    """The Alpaca server (the Aqawan server's HTTP handler); the roof logic
    runs on a private event loop thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: MEarthRoofConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.port.type == "ppdev" and not allow:
            raise HardwareNotAllowed(
                f"port {config.port.device!r} is real hardware: it needs allow_hardware in the"
                " config AND --hardware (port.type 'sim' runs the simulator)"
            )
        self.port: RoofPort = make_port(config.port, allow_hardware=allow)
        host, _, port = config.listen.rpartition(":")
        try:
            super().__init__((host, int(port)), _Handler)
        except BaseException:
            self.port.close()
            raise
        self.config = config
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[int] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="roof-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self.roof: MEarthSouthRoof = self._run_sync(self._build())

    @property
    def sim(self) -> MEarthSouthRoofSim | None:
        return self.port if isinstance(self.port, MEarthSouthRoofSim) else None

    async def _build(self) -> MEarthSouthRoof:
        p = self.config.port
        iface = RoofInterface(
            self.port,
            timing=StrobeTiming(p.strobe_setup, p.strobe_width, p.strobe_hold),
            timeout=p.timeout,
        )
        ecfg = EmergencyConfig.from_dict(self.config.emergency_close)
        roof = MEarthSouthRoof(iface, self.config, None if ecfg is None else EmergencyGate(ecfg))
        roof.start()
        return roof

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_thread.is_alive():
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)  # type: ignore[arg-type]

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def _call(self, fn: Callable[[], Awaitable[Any]], what: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AlpacaException(
                PAC_TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except RoofPortTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(PAC_TIMEOUT, str(exc)) from None
        except RoofPortError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- the emergency close channel ---------------------------------------------------

    def emergency_close(
        self, authorization: str | None, body: bytes, peer: str
    ) -> tuple[int, dict[str, Any]]:
        gate = self.roof.gate
        if gate is None:
            return 404, {"ok": False, "error": "no emergency close channel configured"}
        if not gate.authorized(authorization):
            log.warning("emergency close from %s refused: bad or missing token", peer)
            return 401, {"ok": False, "error": "not authorized"}
        who = parse_body(body)
        try:
            self._call(self.roof.close, "EmergencyClose")
        except AlpacaException as exc:
            gate.record({**who, "peer": peer, "ok": False, "error": str(exc)})
            return 502, {"ok": False, "error": f"close failed: {exc}"}
        entry = gate.record({**who, "peer": peer, "ok": True})
        try:
            shutter = self._call(self.roof.shutter_status, "ShutterStatus")
        except AlpacaException:
            shutter = None
        return 200, {"ok": True, "closing": True, "shutter": shutter, "latched": entry}

    # -- management ----------------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast MEarth roof ({self.config.name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": self.config.name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{self.config.name} roof",
                    "DeviceType": "Dome",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-mearth-roof-{self.config.name}-dome-0",
                }
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>MEarth roof</title>"
            f"</head><body><h1>robofast MEarth roof: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- device members --------------------------------------------------------------------

    def connected(self, client: int) -> bool:
        with self._clients_lock:
            return client in self._clients

    def _set_connected(self, client: int, value: bool) -> None:
        if value:
            if not self.connected(client):
                self._call(lambda: self.roof.limits(max_age=0), "Connect")
                with self._clients_lock:
                    self._clients.add(client)
        else:
            with self._clients_lock:
                self._clients.discard(client)

    def member(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        if verb == "PUT":
            if member == "connected":
                self._set_connected(client, _bool_param(params, "Connected"))
                return None
            if member in ("connect", "disconnect"):
                self._set_connected(client, member == "connect")
                return None
        elif member in COMMON_NAMES and member not in ("connect", "disconnect"):
            return {
                "connected": self.connected(client),
                "connecting": False,
                "description": f"MEarth South roll-off roof {cfg.name} (parallel-port interface)",
                "driverinfo": "robofast_core.devices.mearth_roof_server: Alpaca Dome for the"
                " MEarth South roof",
                "driverversion": DRIVER_VERSION,
                "interfaceversion": 3,
                "name": f"robofast MEarth roof {cfg.name}",
                "supportedactions": list(ACTIONS),
            }[member]
        if member in COMMON_NAMES:
            raise _BadRequest(f"{verb} {member} is not allowed")
        if verb == "GET" and member in CAN:
            return CAN[member]
        if member in ("connect", "disconnect", "action") and verb == "GET":
            raise _BadRequest(f"{member} is a PUT method")

        if not self.connected(client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")
        r = self.roof
        if verb == "GET":
            if member == "shutterstatus":
                return self._call(r.shutter_status, "ShutterStatus")
            if member in ("athome", "atpark", "slaved"):
                return False
            if member == "slewing":
                return self._call(r.shutter_status, "Slewing") in (OPENING, CLOSING)
            if member == "devicestate":
                return self._device_state()
            if member in NOT_IMPLEMENTED_GET:
                raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
            raise _BadRequest(f"{verb} {member}: no such member")
        if member == "openshutter":
            return self._call(r.open, "OpenShutter")
        if member == "closeshutter":
            return self._call(r.close, "CloseShutter")
        if member == "abortslew":
            return self._call(r.abort, "AbortSlew")
        if member == "slaved":
            if _bool_param(params, "Slaved"):
                raise AlpacaException(NOT_IMPLEMENTED, "Slaved: this roof cannot slave")
            return None
        if member == "action":
            return self._action(params.get("Action", ""), params.get("Parameters", ""), client)
        if member in NOT_IMPLEMENTED_PUT:
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        raise _BadRequest(f"{verb} {member}: no such member")

    def _action(self, name: str, parameters: str, client: int) -> str:
        r = self.roof
        key = name.lower()
        if key == "status":
            return json.dumps(self._call(r.status, "Status"))
        if key == "faults":
            return json.dumps(self._call(r.faults, "Faults"))
        if key == "clearfaults":
            text = parameters.strip()
            note = f"ClientID {client}: {text or 'no reason given'}"
            auto = text.startswith(AUTO_CLEAR_PREFIX)
            return self._call(lambda: r.clear_faults(note, auto=auto), "ClearFaults")
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _device_state(self) -> list[dict[str, Any]]:
        state: list[dict[str, Any]] = [
            {"Name": "AtHome", "Value": False},
            {"Name": "AtPark", "Value": False},
        ]
        try:
            shutter = self._call(self.roof.shutter_status, "ShutterStatus")
            state.append({"Name": "ShutterStatus", "Value": shutter})
            state.append({"Name": "Slewing", "Value": shutter in (OPENING, CLOSING)})
        except AlpacaException as exc:
            log.warning("DeviceState without ShutterStatus: %s", exc)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
        state.append({"Name": "TimeStamp", "Value": stamp})
        return state

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> MEarthRoofServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="roof-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        """Stop serving. Sends nothing to the roof: the box keeps its latch."""
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.roof.shutdown(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self.roof.iface.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real parallel port (the config must also say allow_hardware)",
    )
    ap.add_argument("--simulate", action="store_true", help="use the simulator as the port")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = MEarthRoofConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    if args.simulate:
        config.port.type = "sim"
    try:
        server = MEarthRoofServer(config, hardware=args.hardware)
    except (HardwareNotAllowed, RoofPortError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    server.start()
    where = "simulator" if server.sim is not None else config.port.device
    print(json.dumps({"url": f"http://{server.address}", "port": where}), flush=True)
    stop = threading.Event()
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
