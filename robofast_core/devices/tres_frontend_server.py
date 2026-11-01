"""Alpaca server for the TRES front end on the FLWO 60-inch: its calibration stage.

The front end's calibration stage ("calstage") is a PI L-505 linear stage on
a PI C-663 Mercury Step controller, reached as a serial port on the
front-end computer (:mod:`robofast_core.devices.pi_gcs`; hardware, protocol
and open questions in ``docs/tres_frontend.md``). Adapted from the TRES
front-end software (jdeast/tres-guider ``calstage.py``, J. Eastman and
J. Kansky, relicensed BSD-3 with their agreement); the stage logic is
rewritten, the positions and the controller usage are theirs.

Devices
-------
* ``filterwheel/0`` (IFilterWheelV3): the stage by **named position**.
  ``Names`` are the configured positions, in order (``out``, ``sky``,
  ``science``, ...); ``Position`` = n moves there (``MOV``) and returns;
  reading ``Position`` gives -1 while the stage moves, else the index of
  the named position it rests at (within ``tolerance`` mm), and an
  ``InvalidOperation`` error if it rests anywhere else or is not
  referenced. A position whose millimetre value is ``null`` in the config
  (not yet confirmed) is listed but refused. ``FocusOffsets`` are zero.
* ``focuser/0`` (IFocuserV4, absolute; off with ``"focuser": false``): the
  same stage in **microns** from the negative travel limit, for the legacy
  window's ``calstage position <mm>``: ``Move``, ``Position``,
  ``IsMoving``, ``Halt`` (``HLT``). ``MaxStep`` is the controller's travel
  (``TMX? - TMN?``) in microns.

Actions (filterwheel)
---------------------
* ``Action("Status", "")``: JSON of the position (mm and name), motion,
  referencing, servo, travel, the controller's identification and the last
  failed move (``fault``).
* ``Action("Reference", "")``: the reference move (``FNL``, to the negative
  limit, as the legacy front end does on every connect). Needed once after
  the controller is powered on; until then every move is refused. Refused
  while the stage moves. **This moves the stage.**

A failed move (it stopped off target, or did not finish within
``move_timeout`` and was halted) is reported in ``Status`` and the log and
leaves ``Position`` reading as an error; it does not block the next move.

Safety
------
``port.type`` ``"serial"`` (a real port) needs ``"allow_hardware": true``
in the config **and** ``--hardware`` on the command line; ``"sim"`` runs
the in-process simulator (:mod:`robofast_core.devices.tres_frontend_sim`).
**Starting the server never moves the stage**: it only reads (``*IDN?``,
``TMN?``, ``TMX?``, ``POS?``, ``FRF?``, ``#5``); unlike the legacy front
end it does not reference on connect. Stopping it sends nothing (a move in
progress completes on the controller). The stage is never moved by the
safety supervisor (no position is safer than another for the enclosure).

Optional ``legacy_redis``: the transition shim
(:mod:`robofast_core.devices.calstage_redis`), off by default.

Running
-------
::

    python -m robofast_core.devices.tres_frontend_server --config tres_frontend.json [--hardware]

with a JSON config (``sites/flwo60/tres_frontend.json``). It prints one
JSON line, ``{"url": ..., "port": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import math
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
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
)
from robofast_core.devices.calstage_redis import (
    LegacyCalstageMirror,
    LegacyKeys,
    LegacyRedisConfig,
)
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.devices.pi_gcs import GCSAxis, GCSError, GCSTimeout, SerialTransport, Transport
from robofast_core.devices.tcsgo_client import HardwareNotAllowed as TCSHardwareNotAllowed
from robofast_core.devices.tcsgo_client import check_tcs_address
from robofast_core.devices.tcsgo_server import COMMON_NAMES, _Handler, _int_param, _now_stamp
from robofast_core.devices.tres_frontend_sim import C663Sim

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
TIMEOUT = 0x502
ACTIONS = ("Status", "Reference")
SIM_KEYS = frozenset({"position", "referenced", "servo", "velocity", "travel"})
INTERFACE = {"filterwheel": 3, "focuser": 4}

#: Placeholder positions (mm): ``out``, ``sky`` and ``science`` are the
#: 2021 public tres-guider values, unconfirmed; the rest are unknown.
DEFAULT_POSITIONS: dict[str, float | None] = {
    "out": 0.0,
    "sky": 10.0,
    "science": 20.0,
    "in": None,
    "block_sky": None,
    "block_science": None,
}


# -- configuration ----------------------------------------------------------------


@dataclass
class PortConfig:
    #: "serial" (a real port; gated) or "sim" (the in-process simulator).
    type: str = "serial"
    device: str = "/dev/ttyUSB0"
    baud: int = 115200
    #: Bound on one GCS exchange.
    timeout: float = 2.0
    #: Simulator parameters (:class:`C663Sim`) when type is "sim".
    sim: dict[str, Any] = field(default_factory=dict)


@dataclass
class TRESFrontEndConfig:
    port: PortConfig = field(default_factory=PortConfig)
    #: Must be true (and ``--hardware`` given) for a real port or a
    #: non-loopback legacy Redis.
    allow_hardware: bool = False
    name: str = "tres"
    listen: str = "127.0.0.1:11160"
    #: The controller's axis identifier.
    axis: str = "1"
    #: Named positions in mm, in ``Names`` order; null = not yet known (refused).
    positions: dict[str, float | None] = field(default_factory=lambda: dict(DEFAULT_POSITIONS))
    #: How close (mm) counts as "at" a named position (legacy: 0.01).
    tolerance: float = 0.01
    #: Serve the absolute-position Focuser as well.
    focuser: bool = True
    #: Bounds on a move and on the reference move (the legacy window waits 60 s).
    move_timeout: float = 60.0
    reference_timeout: float = 120.0
    #: A move not yet seen moving may read "stopped" for this long.
    start_grace: float = 0.5
    poll_interval: float = 0.2
    #: Reads reuse a status this recent.
    status_max_age: float = 0.2
    request_timeout: float = 4.5
    discovery_port: int | None = None
    #: The Redis transition shim (``calstage_redis``); None: off.
    legacy_redis: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TRESFrontEndConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        port = {k: v for k, v in d.pop("port", {}).items() if not k.startswith("_")}
        port_names = {f.name for f in dataclasses.fields(PortConfig)}
        if set(port) - port_names:
            raise ValueError(f"unknown port keys: {sorted(set(port) - port_names)}")
        if "positions" in d:
            d["positions"] = {k: v for k, v in d["positions"].items() if not k.startswith("_")}
        cfg = cls(port=PortConfig(**port), **d)
        if cfg.port.type not in ("serial", "sim"):
            raise ValueError("port.type must be 'serial' or 'sim'")
        if set(cfg.port.sim) - SIM_KEYS:
            raise ValueError(f"unknown port.sim keys: {sorted(set(cfg.port.sim) - SIM_KEYS)}")
        if not cfg.positions:
            raise ValueError("positions: at least one named position")
        for name, mm in cfg.positions.items():
            if not name or not name.isprintable():
                raise ValueError(f"positions: bad name {name!r}")
            if mm is not None and (not isinstance(mm, int | float) or not math.isfinite(mm)):
                raise ValueError(f"positions.{name}: {mm!r} is not a number or null")
        known = [mm for mm in cfg.positions.values() if mm is not None]
        for i, a in enumerate(known):
            if any(abs(a - b) <= 2 * cfg.tolerance for b in known[i + 1 :]):
                raise ValueError("positions: two named positions closer than 2 x tolerance")
        for name in ("tolerance", "move_timeout", "reference_timeout", "poll_interval"):
            if getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if cfg.legacy_redis is not None:
            LegacyRedisConfig.from_dict(cfg.legacy_redis)
        return cfg


def make_transport(cfg: PortConfig) -> Transport:
    if cfg.type == "sim":
        return C663Sim(**cfg.sim)
    return SerialTransport(cfg.device, cfg.baud)


# -- the stage ---------------------------------------------------------------------


@dataclass
class Snapshot:
    position: float
    moving: bool
    referenced: bool
    at: float


@dataclass
class Job:
    kind: str  # "move" or "reference"
    target: float | None
    started: float
    seen_moving: bool = False


class Calstage:
    """The stage logic over a :class:`GCSAxis`. Runs on one event loop."""

    def __init__(self, axis: GCSAxis, config: TRESFrontEndConfig) -> None:
        self.axis = axis
        self.config = config
        self.travel: tuple[float, float] | None = None
        self.idn: str | None = None
        self.job: Job | None = None
        #: The last failed move or reference; cleared when the next starts.
        self.fault: str | None = None
        self._snap: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None

    def start(self) -> None:
        self._monitor = asyncio.ensure_future(self._run_monitor())

    async def shutdown(self) -> None:
        await stop_task(self._monitor, 2.0)

    # -- reading (never moves) --------------------------------------------------------

    async def connect(self) -> None:
        """Read the controller's identity and travel. Sends no motion."""
        if self.idn is None:
            self.idn = await self.axis.identify()
            log.info("calstage controller: %s", self.idn)
        if self.travel is None:
            self.travel = await self.axis.limits()
        await self.snapshot(0)

    async def snapshot(self, max_age: float | None = None) -> Snapshot:
        max_age = self.config.status_max_age if max_age is None else max_age
        async with self._lock:
            snap = self._snap
            if snap is None or time.monotonic() - snap.at > max_age:
                t = time.monotonic()
                snap = Snapshot(
                    await self.axis.position(),
                    await self.axis.moving(),
                    await self.axis.referenced(),
                    t,
                )
                self._snap = snap
                await self._check_job(snap)
            return snap

    async def _check_job(self, snap: Snapshot) -> None:
        job = self.job
        if job is None:
            return
        cfg = self.config
        elapsed = time.monotonic() - job.started
        if snap.moving:
            job.seen_moving = True
            limit = cfg.reference_timeout if job.kind == "reference" else cfg.move_timeout
            if elapsed > limit:
                self.job = None
                self.fault = f"{job.kind} did not finish within {limit:g} s; halted"
                log.error("calstage: %s", self.fault)
                with contextlib.suppress(GCSError):
                    await self.axis.halt()
            return
        if job.kind == "reference":
            done = snap.referenced
            why = "reference move ended without the axis referenced"
        else:
            assert job.target is not None
            done = abs(snap.position - job.target) <= cfg.tolerance
            why = f"stopped at {snap.position:.3f} mm, target {job.target:.3f} mm"
        if done:
            self.job = None
            log.info("calstage: %s complete after %.1f s", job.kind, elapsed)
        elif job.seen_moving or elapsed > cfg.start_grace:
            self.job = None
            self.fault = why
            log.error("calstage: %s", why)

    async def _run_monitor(self) -> None:
        while True:
            if self.job is not None:
                try:
                    await self.snapshot(0)
                except GCSError as exc:
                    log.error("calstage monitor: %s", exc)
            await asyncio.sleep(self.config.poll_interval)

    async def moving(self) -> bool:
        snap = await self.snapshot()
        return snap.moving or self.job is not None

    def name_at(self, mm: float) -> str | None:
        for name, pos in self.config.positions.items():
            if pos is not None and abs(mm - pos) <= self.config.tolerance:
                return name
        return None

    async def resting(self) -> Snapshot:
        """The snapshot, if the stage rests referenced; else InvalidOperation."""
        snap = await self.snapshot()
        if not snap.referenced:
            raise AlpacaException(
                INVALID_OPERATION, "stage not referenced: Action('Reference') first"
            )
        return snap

    # -- commands ----------------------------------------------------------------------

    async def move_to(self, mm: float) -> None:
        if self.travel is None:
            await self.connect()
        assert self.travel is not None
        lo, hi = self.travel
        if not lo <= mm <= hi:
            raise AlpacaException(
                INVALID_VALUE, f"{mm:g} mm is outside the travel [{lo:g}, {hi:g}]"
            )
        snap = await self.snapshot(0)
        if not snap.referenced or (self.job is not None and self.job.kind == "reference"):
            raise AlpacaException(
                INVALID_OPERATION, "stage not referenced: Action('Reference') first"
            )
        if not await self.axis.servo():
            await self.axis.servo_on()
        log.info("calstage: move %.3f -> %.3f mm", snap.position, mm)
        self.fault = None
        self.job = Job("move", float(mm), time.monotonic())
        try:
            await self.axis.move(mm)
        except BaseException:
            self.job = None
            raise
        self._snap = None

    async def move_to_name(self, name: str) -> None:
        if name not in self.config.positions:
            raise AlpacaException(INVALID_VALUE, f"no named position {name!r}")
        mm = self.config.positions[name]
        if mm is None:
            raise AlpacaException(
                INVALID_OPERATION, f"position {name!r} is not configured yet (confirm its mm value)"
            )
        await self.move_to(mm)

    async def reference(self) -> str:
        if await self.moving():
            raise AlpacaException(INVALID_OPERATION, "not referencing: the stage is moving")
        if not await self.axis.servo():
            await self.axis.servo_on()
        log.warning("calstage: reference move to the negative limit (FNL)")
        self.fault = None
        self.job = Job("reference", None, time.monotonic())
        try:
            await self.axis.reference_negative()
        except BaseException:
            self.job = None
            raise
        self._snap = None
        return "referencing: the stage moves to its negative limit"

    async def halt(self) -> None:
        self.job = None
        await self.axis.halt()
        self._snap = None

    # -- state -----------------------------------------------------------------------

    async def wheel_position(self) -> int:
        if await self.moving():
            return -1
        snap = await self.resting()
        name = self.name_at(snap.position)
        if name is None:
            extra = f" (last move: {self.fault})" if self.fault else ""
            raise AlpacaException(
                INVALID_OPERATION,
                f"stage at {snap.position:.3f} mm, not at a named position{extra}",
            )
        return list(self.config.positions).index(name)

    def _offset(self) -> float:
        assert self.travel is not None
        return self.travel[0]

    async def microns(self) -> int:
        snap = await self.resting()
        return round((snap.position - self._offset()) * 1000.0)

    async def max_step(self) -> int:
        if self.travel is None:
            await self.connect()
        assert self.travel is not None
        return round((self.travel[1] - self.travel[0]) * 1000.0)

    async def move_microns(self, steps: int) -> None:
        top = await self.max_step()
        if not 0 <= steps <= top:
            raise AlpacaException(INVALID_VALUE, f"Position {steps} outside 0..{top}")
        await self.move_to(self._offset() + steps / 1000.0)

    async def legacy_state(self) -> str | None:
        """The legacy ``calstage_status`` string (the Redis shim)."""
        snap = await self.snapshot()
        if snap.moving or self.job is not None:
            return "moving"
        if not snap.referenced:
            return "unreferenced"
        return self.name_at(snap.position) or f"{snap.position:.3f}"

    async def resting_name(self) -> str | None:
        snap = await self.snapshot()
        if snap.moving or self.job is not None or not snap.referenced:
            return None
        return self.name_at(snap.position)

    async def status(self) -> dict[str, Any]:
        snap = await self.snapshot(0)
        servo = await self.axis.servo()
        job = self.job
        return {
            "position_mm": round(snap.position, 4),
            "name": None if snap.moving else self.name_at(snap.position),
            "moving": snap.moving or job is not None,
            "job": None
            if job is None
            else {
                "kind": job.kind,
                "target_mm": job.target,
                "elapsed": round(time.monotonic() - job.started, 1),
            },
            "referenced": snap.referenced,
            "servo": servo,
            "travel_mm": None if self.travel is None else list(self.travel),
            "positions": dict(self.config.positions),
            "controller": self.idn,
            "fault": self.fault,
            "last_motion_command": self.axis.last_motion,
        }


# -- Alpaca over HTTP ------------------------------------------------------------------


class TRESFrontEndServer(ThreadingHTTPServer):
    """The Alpaca server (the TCSGO server's HTTP handler, which serves
    ``self.devices``); the stage logic runs on a private event loop thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: TRESFrontEndConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.port.type == "serial" and not allow:
            raise HardwareNotAllowed(
                f"port {config.port.device!r} is real hardware: it needs allow_hardware in the"
                " config AND --hardware (port.type 'sim' runs the simulator)"
            )
        self.redis_config = (
            None
            if config.legacy_redis is None
            else LegacyRedisConfig.from_dict(config.legacy_redis)
        )
        redis_ip = None
        if self.redis_config is not None:
            try:
                redis_ip = check_tcs_address(self.redis_config.host, allow_hardware=allow)
            except TCSHardwareNotAllowed:
                raise HardwareNotAllowed(
                    f"legacy_redis host {self.redis_config.host!r} is not loopback (or does not"
                    " resolve): a real Redis needs allow_hardware in the config AND --hardware"
                ) from None
        self.transport: Transport = make_transport(config.port)
        host, _, port = config.listen.rpartition(":")
        try:
            super().__init__((host, int(port)), _Handler)  # type: ignore[arg-type]
        except BaseException:
            self.transport.close()
            raise
        self.config = config
        self.devices = ("filterwheel",) + (("focuser",) if config.focuser else ())
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="calstage-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self.mirror: LegacyCalstageMirror | None = None
        self._stopped = False
        self.stage: Calstage = self._run_sync(self._build(redis_ip))

    @property
    def sim(self) -> C663Sim | None:
        return self.transport if isinstance(self.transport, C663Sim) else None

    async def _build(self, redis_ip: str | None) -> Calstage:
        cfg = self.config
        stage = Calstage(GCSAxis(self.transport, axis=cfg.axis, timeout=cfg.port.timeout), cfg)
        stage.start()
        rc = self.redis_config
        if rc is not None and redis_ip is not None:
            keys = LegacyKeys(
                redis_ip, rc.port, rc.db, {rc.command_key, rc.status_key}, timeout=rc.timeout
            )
            self.mirror = LegacyCalstageMirror(
                keys,
                rc,
                names=[n for n, mm in cfg.positions.items() if mm is not None],
                state=self._quiet(stage.legacy_state),
                at_name=self._quiet(stage.resting_name),
                move_name=stage.move_to_name,
                move_mm=stage.move_to,
            )
            self.mirror.start()
            log.warning(
                "legacy Redis shim on %s:%d db %d (accept_commands=%s)",
                redis_ip,
                rc.port,
                rc.db,
                rc.accept_commands,
            )
        return stage

    @staticmethod
    def _quiet(fn: Callable[[], Awaitable[str | None]]) -> Callable[[], Awaitable[str | None]]:
        async def call() -> str | None:
            try:
                return await fn()
            except (GCSError, AlpacaException) as exc:
                log.debug("legacy state unavailable: %s", exc)
                return None

        return call

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
                TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except GCSTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(TIMEOUT, str(exc)) from None
        except GCSError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management --------------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast TRES front end ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            kinds = {"filterwheel": "FilterWheel", "focuser": "Focuser"}
            units = {"filterwheel": "named", "focuser": "microns"}
            return [
                {
                    "DeviceName": f"{name} calstage ({units[d]})",
                    "DeviceType": kinds[d],
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-tres-frontend-{name}-{d}-0",
                }
                for d in self.devices
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>TRES front end</title>"
            f"</head><body><h1>robofast TRES front end: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members -----------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        if value:
            if not self.connected(device, client):
                self._call(self.stage.connect, "Connect")
                with self._clients_lock:
                    self._clients.add((device, client))
        else:
            with self._clients_lock:
                self._clients.discard((device, client))

    def _common(self, device: str, member: str, client: int) -> Any:
        cfg = self.config
        what = "named positions" if device == "filterwheel" else "absolute position, microns"
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": f"TRES front-end calibration stage ({what}), {cfg.name}",
            "driverinfo": "robofast_core.devices.tres_frontend_server: PI C-663 calibration stage",
            "driverversion": DRIVER_VERSION,
            "interfaceversion": INTERFACE[device],
            "name": f"robofast TRES calstage {cfg.name} {device}",
            "supportedactions": list(ACTIONS) if device == "filterwheel" else [],
        }[member]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        if verb == "PUT":
            if member == "connected":
                self._set_connected(device, client, _bool_param(params, "Connected"))
                return None
            if member in ("connect", "disconnect"):
                self._set_connected(device, client, member == "connect")
                return None
        elif member in COMMON_NAMES and member not in ("connect", "disconnect"):
            return self._common(device, member, client)
        if member in COMMON_NAMES:
            raise _BadRequest(f"{verb} {member} is not allowed")
        if member in ("connect", "disconnect", "action") and verb == "GET":
            raise _BadRequest(f"{member} is a PUT method")
        if device == "filterwheel":
            return self._wheel(verb, member, params, client)
        return self._focuser(verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    @staticmethod
    def _unknown(verb: str, member: str) -> Any:
        raise _BadRequest(f"{verb} {member}: no such member")

    def _wheel(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        names = list(self.config.positions)
        s = self.stage
        if verb == "GET" and member == "names":
            self._need("filterwheel", member, client)
            return names
        if verb == "GET" and member == "focusoffsets":
            self._need("filterwheel", member, client)
            return [0] * len(names)
        self._need("filterwheel", member, client)
        if verb == "GET":
            if member == "position":
                return self._call(s.wheel_position, "Position")
            if member == "devicestate":
                state: list[dict[str, Any]] = []
                with contextlib.suppress(AlpacaException):
                    state.append(
                        {"Name": "Position", "Value": self._call(s.wheel_position, "Position")}
                    )
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)
        if member == "position":
            n = _int_param(params, "Position")
            if not 0 <= n < len(names):
                raise AlpacaException(INVALID_VALUE, f"Position {n} outside 0..{len(names) - 1}")
            return self._call(lambda: s.move_to_name(names[n]), "Position")
        if member == "action":
            return self._action(params.get("Action", ""))
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    def _action(self, name: str) -> str:
        key = name.lower()
        if key == "status":
            return json.dumps(self._call(self.stage.status, "Status"))
        if key == "reference":
            return self._call(self.stage.reference, "Reference")
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _focuser(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        s = self.stage
        if verb == "GET":
            static = {
                "absolute": True,
                "stepsize": 1.0,  # microns
                "tempcomp": False,
                "tempcompavailable": False,
            }
            if member in static:
                return static[member]
            if member == "temperature":
                raise AlpacaException(NOT_IMPLEMENTED, "Temperature is not implemented")
        self._need("focuser", member, client)
        if verb == "GET":
            if member in ("maxstep", "maxincrement"):
                return self._call(s.max_step, "MaxStep")
            if member == "position":
                return self._call(s.microns, "Position")
            if member == "ismoving":
                return self._call(s.moving, "IsMoving")
            if member == "devicestate":
                state: list[dict[str, Any]] = []
                for name, fn in (("IsMoving", s.moving), ("Position", s.microns)):
                    with contextlib.suppress(AlpacaException):
                        state.append({"Name": name, "Value": self._call(fn, name)})
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)
        if member == "move":
            pos = _int_param(params, "Position")
            return self._call(lambda: s.move_microns(pos), "Move")
        if member == "halt":
            return self._call(s.halt, "Halt")
        if member == "tempcomp":
            if _bool_param(params, "TempComp"):
                raise AlpacaException(NOT_IMPLEMENTED, "no temperature compensation")
            return None
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no focuser actions")
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> TRESFrontEndServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="calstage-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        """Stop serving. Sends nothing to the stage: a move in progress completes."""
        if self._stopped:
            return
        self._stopped = True
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)

        async def down() -> None:
            if self.mirror is not None:
                await self.mirror.shutdown()
            await self.stage.shutdown()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(down(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self.transport.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real serial port (the config must also say allow_hardware)",
    )
    ap.add_argument("--simulate", action="store_true", help="use the simulated controller")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = TRESFrontEndConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    if args.simulate:
        config.port.type = "sim"
    try:
        server = TRESFrontEndServer(config, hardware=args.hardware)
    except (HardwareNotAllowed, GCSError) as exc:
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
