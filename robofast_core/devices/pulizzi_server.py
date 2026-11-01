"""Alpaca Switch server for the calibration lamps on a Pulizzi power controller.

The FLWO 60-inch's TRES calibration lamps (ThAr, incandescent) are outlets
of a Pulizzi IPC remote power controller on a serial line
(:mod:`robofast_core.devices.pulizzi`; protocol, outlet map and open
questions in ``docs/pulizzi.md``). The outlet assignments and the startup
and shutdown behaviour follow FLWO's legacy 60-inch software (with thanks
to FLWO); the code is new.

Device
------
``switch/0`` (ISwitchV2): one boolean switch per outlet named in the
config, in order (switch id 0 is the first listed). Only outlets with
``"lamp": true`` are writable (``CanWrite``); any other listed outlet is
read-only (``SetSwitch`` raises ``MethodNotImplemented``), and an outlet
not listed is not served at all.

Actions: ``Action("Status", "")`` (JSON: every listed outlet's state, how
long each lamp has been on, its limit, and the lamps the limit switched
off) and ``Action("LampsOff", "")`` (every lamp outlet off, whoever asks:
switching a lamp off is always allowed).

Lamp safety
-----------
* **Starting never switches.** The server reads the unit (login, ``DX0``)
  and nothing else; a lamp found on is left on (and timed from then).
* **Reading never switches** (``GetSwitch``, ``Status``, ``Connected``).
* **Max-on limit.** Every lamp has ``max_on`` seconds (default 1800). The
  server polls the unit every ``poll_interval`` s, whether or not a client
  is connected, and switches off any lamp that has been on longer, logging
  an error and listing it in ``Status`` (``tripped``). The clock starts
  when the server switches the lamp on, or first sees it on (a lamp
  switched on by hand, or found on at startup, gets its full ``max_on``
  from then). ``SetSwitch`` on a lamp that is already on does **not**
  restart its clock: to get a fresh ``max_on``, switch it off first.
* **A clean stop switches every lamp off** (SIGTERM/SIGINT;
  ``lamps_off_on_stop``, default true): a lamp left on overheats and ages,
  and nothing else would be watching it. A crash cannot; the next start's
  max-on limit (and the sequencer's cleanup, ``docs/pulizzi.md``) catches
  it.
* After every switch the server reads the unit back and reports an error
  if the outlet is not in the asked state (the front panel's manual
  switches and remote-disable switch override remote commands).

Hardware gate: ``port.type`` ``"serial"`` (a real port) needs
``"allow_hardware": true`` in the config **and** ``--hardware`` on the
command line; ``"sim"`` runs the in-process simulator
(:mod:`robofast_core.devices.pulizzi_sim`).

Running
-------
::

    python -m robofast_core.devices.pulizzi_server --config pulizzi.json [--hardware | --simulate]

with a JSON config (``sites/flwo60/pulizzi.json``). It prints one JSON
line, ``{"url": ..., "port": ...}``, once listening.
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
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
)
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.devices.pulizzi import (
    OUTLETS,
    PulizziError,
    PulizziIPC,
    PulizziTimeout,
    SerialTransport,
    Status,
    Transport,
)
from robofast_core.devices.pulizzi_sim import PulizziSim
from robofast_core.devices.tcsgo_server import COMMON_NAMES, _Handler, _int_param

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
TIMEOUT = 0x502
ACTIONS = ("Status", "LampsOff")
SIM_KEYS = frozenset({"on", "auto_event", "nulls"})
#: No lamp may be configured to stay on longer than this (s).
MAX_ON_CEILING = 4 * 3600.0


# -- configuration ----------------------------------------------------------------


@dataclass
class PortConfig:
    #: "serial" (a real port; gated) or "sim" (the in-process simulator).
    type: str = "serial"
    device: str = "/dev/ttyS0"
    baud: int = 9600
    #: Bound on one answer from the unit (the manual: at least 0.5 s).
    timeout: float = 1.0
    #: Simulator parameters (:class:`PulizziSim`) when type is "sim".
    sim: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutletConfig:
    outlet: int
    name: str
    #: A calibration lamp: writable, and switched off after ``max_on`` s.
    #: Anything else is read-only.
    lamp: bool = False
    max_on: float = 1800.0
    description: str = ""


@dataclass
class PulizziConfig:
    port: PortConfig = field(default_factory=PortConfig)
    #: Must be true (and ``--hardware`` given) for a real port.
    allow_hardware: bool = False
    name: str = "lamps"
    listen: str = "127.0.0.1:11170"
    #: The unit's login name (factory ``@@@@``).
    unit_name: str = "@@@@"
    outlets: list[OutletConfig] = field(default_factory=list)
    #: How often the max-on monitor reads the unit.
    poll_interval: float = 2.0
    #: Reads reuse a status this recent.
    status_max_age: float = 1.0
    #: Switch every lamp off on a clean stop.
    lamps_off_on_stop: bool = True
    request_timeout: float = 8.0
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PulizziConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        if set(d) - names:
            raise ValueError(f"unknown config keys: {sorted(set(d) - names)}")
        port = {k: v for k, v in d.pop("port", {}).items() if not k.startswith("_")}
        port_names = {f.name for f in dataclasses.fields(PortConfig)}
        if set(port) - port_names:
            raise ValueError(f"unknown port keys: {sorted(set(port) - port_names)}")
        outs = []
        for o in d.pop("outlets", []):
            o = {k: v for k, v in dict(o).items() if not k.startswith("_")}
            o_names = {f.name for f in dataclasses.fields(OutletConfig)}
            if set(o) - o_names:
                raise ValueError(
                    f"outlet {o.get('outlet')}: unknown keys {sorted(set(o) - o_names)}"
                )
            outs.append(OutletConfig(**o))
        cfg = cls(port=PortConfig(**port), outlets=outs, **d)
        if cfg.port.type not in ("serial", "sim"):
            raise ValueError("port.type must be 'serial' or 'sim'")
        if set(cfg.port.sim) - SIM_KEYS:
            raise ValueError(f"unknown port.sim keys: {sorted(set(cfg.port.sim) - SIM_KEYS)}")
        if not cfg.outlets:
            raise ValueError("outlets: at least one")
        for oc in cfg.outlets:
            if isinstance(oc.outlet, bool) or not isinstance(oc.outlet, int):
                raise ValueError(f"outlet {oc.outlet!r}: not an integer")
            if not 1 <= oc.outlet <= OUTLETS:
                raise ValueError(f"outlet {oc.outlet}: not in 1..{OUTLETS}")
            if not oc.name or not oc.name.isprintable():
                raise ValueError(f"outlet {oc.outlet}: bad name {oc.name!r}")
            if not isinstance(oc.lamp, bool):
                raise ValueError(f"outlet {oc.outlet}: lamp must be true or false")
            if not (
                isinstance(oc.max_on, int | float)
                and math.isfinite(oc.max_on)
                and 0 < oc.max_on <= MAX_ON_CEILING
            ):
                raise ValueError(f"outlet {oc.outlet}: max_on must be in (0, {MAX_ON_CEILING:g}] s")
        if len({o.outlet for o in cfg.outlets}) != len(cfg.outlets):
            raise ValueError("outlets: an outlet is listed twice")
        if len({o.name for o in cfg.outlets}) != len(cfg.outlets):
            raise ValueError("outlets: a name is used twice")
        for name in ("poll_interval", "request_timeout", "status_max_age"):
            if getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be positive")
        return cfg


def make_transport(cfg: PortConfig) -> Transport:
    if cfg.type == "sim":
        return PulizziSim(**cfg.sim)
    return SerialTransport(cfg.device, cfg.baud)


# -- the lamps --------------------------------------------------------------------


class Lamps:
    """The outlet logic and the max-on monitor. Runs on one event loop."""

    def __init__(self, unit: PulizziIPC, config: PulizziConfig) -> None:
        self.unit = unit
        self.config = config
        self._status: Status | None = None
        #: Lamp outlet -> ``time.monotonic()`` when its max-on clock started.
        self.on_since: dict[int, float] = {}
        #: Lamps the max-on limit switched off (newest last, bounded).
        self.tripped: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None
        self._stopping = False
        self._lamps = {o.outlet: o for o in config.outlets if o.lamp}

    def start(self) -> None:
        self._monitor = asyncio.ensure_future(self._run_monitor())

    async def shutdown(self) -> None:
        self._stopping = True
        await stop_task(self._monitor, 2.0)

    # -- reading (never switches) ---------------------------------------------------

    def _seen(self, st: Status) -> None:
        now = time.monotonic()
        for outlet in self._lamps:
            if st.outlets.get(outlet):
                if outlet not in self.on_since:
                    self.on_since[outlet] = now
                    log.warning("lamp %s seen on; its max-on clock starts now", self._name(outlet))
            else:
                self.on_since.pop(outlet, None)

    def _name(self, outlet: int) -> str:
        oc = next((o for o in self.config.outlets if o.outlet == outlet), None)
        return f"{oc.name} (outlet {outlet})" if oc else f"outlet {outlet}"

    async def status(self, max_age: float | None = None) -> Status:
        max_age = self.config.status_max_age if max_age is None else max_age
        async with self._lock:
            st = self._status
            if st is None or time.monotonic() - st.at > max_age:
                st = await self.unit.status()
                self._status = st
                self._seen(st)
            return st

    async def is_on(self, outlet: int) -> bool:
        st = await self.status()
        if outlet not in st.outlets:
            raise PulizziError(f"outlet {outlet} is not in the unit's status")
        return st.outlets[outlet]

    # -- switching --------------------------------------------------------------------

    async def switch(self, outlet: int, on: bool, why: str) -> None:
        oc = self._lamps.get(outlet)
        if oc is None:
            raise AlpacaException(NOT_IMPLEMENTED, f"outlet {outlet} is not a lamp: read-only")
        async with self._lock:
            was_on = outlet in self.on_since
            log.warning("lamp %s %s (%s)", self._name(outlet), "ON" if on else "OFF", why)
            try:
                st = await self.unit.switch(outlet, on)
            finally:
                self._status = None  # whatever happened, read afresh next time
            self._status = st
            if on and not was_on:
                self.on_since[outlet] = time.monotonic()
            self._seen(st)

    async def lamps_off(self, why: str) -> list[str]:
        """Switch off every lamp that reads on (or whose state is unknown);
        returns the names switched off. Raises the first failure after
        trying them all."""
        try:
            st = await self.status(0)
            on = [o for o in self._lamps if st.outlets.get(o) is not False]
        except PulizziError:
            on = list(self._lamps)
        done, first = [], None
        for outlet in on:
            try:
                await self.switch(outlet, False, why)
                done.append(self._lamps[outlet].name)
            except (PulizziError, AlpacaException) as exc:
                log.error("lamp %s: off failed: %s", self._name(outlet), exc)
                first = first or exc
        if first is not None:
            raise first
        return done

    # -- the max-on monitor -----------------------------------------------------------

    async def check_limits(self) -> None:
        await self.status(0)
        now = time.monotonic()
        for outlet, since in list(self.on_since.items()):
            oc = self._lamps[outlet]
            on_for = now - since
            if on_for <= oc.max_on:
                continue
            log.error(
                "lamp %s on for %.0f s, longer than its max_on %.0f s: switching it off",
                self._name(outlet),
                on_for,
                oc.max_on,
            )
            await self.switch(outlet, False, f"max_on {oc.max_on:g} s exceeded")
            self.tripped.append(
                {
                    "name": oc.name,
                    "outlet": outlet,
                    "on_for": round(on_for, 1),
                    "max_on": oc.max_on,
                    "at": time.time(),
                }
            )
            del self.tripped[:-20]

    async def _run_monitor(self) -> None:
        while not self._stopping:
            try:
                await self.check_limits()
                if self.last_error is not None:
                    log.info("pulizzi: the unit answers again")
                self.last_error = None
            except (PulizziError, AlpacaException) as exc:
                if str(exc) != self.last_error:
                    log.error("pulizzi monitor: %s", exc)
                self.last_error = str(exc)
            await asyncio.sleep(self.config.poll_interval)

    async def report(self) -> dict[str, Any]:
        st = await self.status()
        now = time.monotonic()
        outlets = []
        for i, oc in enumerate(self.config.outlets):
            entry: dict[str, Any] = {
                "id": i,
                "outlet": oc.outlet,
                "name": oc.name,
                "lamp": oc.lamp,
                "on": st.outlets.get(oc.outlet),
            }
            if oc.lamp:
                since = self.on_since.get(oc.outlet)
                entry["max_on"] = oc.max_on
                entry["on_for"] = None if since is None else round(now - since, 1)
                entry["remaining"] = None if since is None else round(oc.max_on - (now - since), 1)
            outlets.append(entry)
        return {
            "outlets": outlets,
            "tripped": list(self.tripped),
            "unit": st.other,
            "monitor_error": self.last_error,
            "commands_sent": len(self.unit.sent),
        }


# -- Alpaca over HTTP ------------------------------------------------------------------


class PulizziServer(ThreadingHTTPServer):
    """The Alpaca server (the TCSGO server's HTTP handler, which serves
    ``self.devices``); the lamp logic runs on a private event loop thread."""

    daemon_threads = True
    allow_reuse_address = True
    devices = ("switch",)

    def __init__(self, config: PulizziConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.port.type == "serial" and not allow:
            raise HardwareNotAllowed(
                f"port {config.port.device!r} is real hardware: it needs allow_hardware in the"
                " config AND --hardware (port.type 'sim' runs the simulator)"
            )
        if allow:
            log.warning("HARDWARE MODE: Pulizzi unit on %s", config.port.device)
        self.transport: Transport = make_transport(config.port)
        host, _, port = config.listen.rpartition(":")
        try:
            super().__init__((host, int(port)), _Handler)  # type: ignore[arg-type]
        except BaseException:
            self.transport.close()
            raise
        self.config = config
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[int] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="pulizzi-loop", daemon=True
        )
        self._loop_thread.start()
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self._stopped = False
        self.lamps: Lamps = asyncio.run_coroutine_threadsafe(self._build(), self._loop).result(5)

    async def _build(self) -> Lamps:
        cfg = self.config
        unit = PulizziIPC(self.transport, name=cfg.unit_name, timeout=cfg.port.timeout)
        return Lamps(unit, cfg)

    @property
    def sim(self) -> PulizziSim | None:
        return self.transport if isinstance(self.transport, PulizziSim) else None

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
        except PulizziTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(TIMEOUT, f"{what}: {exc}") from None
        except PulizziError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, f"{what}: {exc}") from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management --------------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast lamps ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{name} lamps",
                    "DeviceType": "Switch",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-pulizzi-{name}-switch-0",
                }
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Lamps</title>"
            f"</head><body><h1>robofast lamps: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members -----------------------------------------------------------------------

    def connected(self, client: int) -> bool:
        with self._clients_lock:
            return client in self._clients

    def _set_connected(self, client: int, value: bool) -> None:
        if value:
            if not self.connected(client):
                self._call(lambda: self.lamps.status(0), "Connect")  # reads only
                with self._clients_lock:
                    self._clients.add(client)
        else:
            with self._clients_lock:
                self._clients.discard(client)

    def _outlet(self, params: dict[str, str], verb: str) -> tuple[int, OutletConfig]:
        key = "id" if verb == "GET" else "Id"
        if key not in params:
            raise _BadRequest("missing parameter Id")
        i = _int_param(params, key)
        if not 0 <= i < len(self.config.outlets):
            raise AlpacaException(INVALID_VALUE, f"no switch {i}")
        return i, self.config.outlets[i]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        cfg = self.config
        if verb == "PUT" and member in ("connected", "connect", "disconnect"):
            on = member == "connect" or (member == "connected" and _bool_param(params, "Connected"))
            self._set_connected(client, on)
            return None
        if member in COMMON_NAMES:
            if verb != "GET" or member in ("connect", "disconnect"):
                raise _BadRequest(f"{verb} {member} is not allowed")
            return {
                "connected": self.connected(client),
                "connecting": False,
                "description": f"{cfg.name}: calibration lamps on a Pulizzi power controller",
                "driverinfo": "robofast_core.devices.pulizzi_server: Alpaca Switch for lamps",
                "driverversion": DRIVER_VERSION,
                "interfaceversion": 2,
                "name": f"robofast lamps {cfg.name}",
                "supportedactions": list(ACTIONS),
            }[member]
        if member == "maxswitch" and verb == "GET":
            return len(cfg.outlets)
        if member == "action" and verb == "GET":
            raise _BadRequest("action is a PUT method")
        if not self.connected(client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")
        if member == "action":
            return self._action(params.get("Action", ""), client)
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        if verb == "GET":
            i, oc = self._outlet(params, verb)
            if member == "canwrite":
                return oc.lamp
            if member == "getswitchname":
                return oc.name
            if member == "getswitchdescription":
                if oc.description:
                    return oc.description
                if oc.lamp:
                    return f"lamp on outlet {oc.outlet} (switched off after {oc.max_on:g} s)"
                return f"outlet {oc.outlet} (read-only)"
            if member in ("getswitch", "getswitchvalue"):
                on = self._call(lambda: self.lamps.is_on(oc.outlet), "GetSwitch")
                return on if member == "getswitch" else (1.0 if on else 0.0)
            if member == "minswitchvalue":
                return 0.0
            if member in ("maxswitchvalue", "switchstep"):
                return 1.0
            raise _BadRequest(f"unknown member {verb} {member}")
        if member in ("setswitch", "setswitchvalue"):
            i, oc = self._outlet(params, verb)
            if not oc.lamp:
                raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} ({oc.name}) is not writable")
            if member == "setswitch":
                on = _bool_param(params, "State")
            else:
                if "Value" not in params:
                    raise _BadRequest("missing parameter Value")
                try:
                    v = float(params["Value"])
                except ValueError:
                    raise _BadRequest("Value is not a number") from None
                if not 0.0 <= v <= 1.0:
                    raise AlpacaException(INVALID_VALUE, f"switch {i}: value {v} not in [0, 1]")
                on = v >= 0.5
            why = f"client {client}"
            self._call(lambda: self.lamps.switch(oc.outlet, on, why), "SetSwitch")
            return None
        if member == "setswitchname":
            raise AlpacaException(NOT_IMPLEMENTED, "names come from the config")
        raise _BadRequest(f"unknown member {verb} {member}")

    def _action(self, name: str, client: int) -> str:
        key = name.lower()
        if key == "status":
            return json.dumps(self._call(self.lamps.report, "Status"))
        if key == "lampsoff":
            off = self._call(lambda: self.lamps.lamps_off(f"LampsOff by client {client}"), name)
            return json.dumps({"off": off})
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> PulizziServer:
        """Serve, and start the max-on monitor. Reads the unit; switches nothing."""
        self._loop.call_soon_threadsafe(self.lamps.start)
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="pulizzi-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        """Stop serving; with ``lamps_off_on_stop``, switch every lamp off first."""
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
            await self.lamps.shutdown()
            if self.config.lamps_off_on_stop:
                try:
                    off = await self.lamps.lamps_off("server stopping")
                    log.info("server stopping: lamps off %s", off or "(none was on)")
                except (PulizziError, AlpacaException) as exc:
                    log.critical("server stopping: a lamp may still be ON: %s", exc)

        try:
            asyncio.run_coroutine_threadsafe(down(), self._loop).result(10)
        except Exception as exc:
            log.critical("server stopping: lamps-off did not finish: %r", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self.transport.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real serial port (the config must also say allow_hardware)",
    )
    mode.add_argument("--simulate", action="store_true", help="use the simulated unit")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = PulizziConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    if args.simulate:
        config.port.type = "sim"
    try:
        server = PulizziServer(config, hardware=args.hardware)
    except (HardwareNotAllowed, PulizziError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    # The handlers first: a stop signal at any time from here on is a clean
    # stop (lamps off), never a kill.
    stop = threading.Event()
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server.start()
    where = "simulator" if server.sim is not None else config.port.device
    print(json.dumps({"url": f"http://{server.address}", "port": where}), flush=True)
    stop.wait()
    server.stop()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
