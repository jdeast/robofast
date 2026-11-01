"""Alpaca server for the FLWO 60-inch's atmospheric dispersion corrector (ADC).

The ADC is part of the telescope and feeds both of its instruments (TRES
and FAST): two counter-rotating prisms, each on a stepper motor, on one
serial line (:mod:`robofast_core.devices.adc_mdrive`). This server keeps
the prisms set for the telescope's current position, reading that from
robofast's own Alpaca Telescope (the TCSGO server), so it works with or
without the legacy TCS software. The tracking math is ported from FLWO's
legacy ADC daemon (``adc_d``, part of the 60-inch TCS software "telshell",
T. Groner; with thanks to FLWO) and checked against it with test vectors
(:mod:`robofast_core.devices.adc_math`); the control logic is new.
Protocol, math, mapping and open questions: ``docs/adc.md``.

Devices
-------
* ``switch/0`` (ISwitchV2): the tracking service. Switch 0, ``Tracking``,
  is the only writable one: the legacy ``adc_on``/``adc_off``. Writing
  true starts tracking (refused until the motors are referenced) and
  clears a fault; writing false stops it and, with ``neutral_on_off``,
  turns both prisms to home, their neutral setting (the legacy ``adc_off``
  homed them). The rest are read-only: ``Settled`` (tracking, fresh input,
  read after the last ``Tracking`` write, prisms at their targets),
  ``InputFresh``, ``Fault``, ``Referenced``, and the values
  ``ParallacticAngle``, ``ZenithDistance``, ``Deflection`` (deg; valid
  while ``InputFresh``) and ``PrismA``, ``PrismB`` (each motor's angle
  from home, deg).
* ``rotator/0`` and ``rotator/1`` (IRotatorV4): prism A and prism B, for
  reading and for manual moves. ``MechanicalPosition`` is the motor's
  angle from home; ``Position`` adds the in-memory ``Sync`` offset and
  ``Reverse``. Moves are refused while tracking is on or a move or homing
  runs, and before the motors are referenced. ``Halt`` is not implemented
  (the legacy daemon never stops a motor; a move is bounded instead).

Actions (switch)
----------------
* ``Status``: JSON of everything: input, targets, motors, fault, hold reason.
* ``Home``: run each motor's homing routine (A, then B, as the legacy
  daemon did). **This moves the prisms.** Refused while tracking.
* ``Neutral``: turn both prisms to home (step 0) without homing. **Moves.**
* ``Instrument``: select the instrument (``Parameters``: its name in
  ``instruments``); it only changes the wavelengths of the ``refraction``
  deflection model and an optional PA offset.

Tracking (every ``update_interval`` s while ``Tracking`` is on)
--------------------------------------------------------------
Reads the Telescope's ``DeviceState`` (altitude, azimuth, slewing, at
park), computes the parallactic angle and zenith distance with ERFA,
the deflection (``deflection``: ``legacy_table`` or ``refraction``) and
the two motor targets, and moves the motors when either target is more than
``deadband`` deg away. It **holds** (no move) when the input is missing,
invalid or older than ``stale_after``; while the telescope slews and for
``settle_after_slew`` s after; beyond ``max_zenith_distance``; and after a
fault. At park it turns the prisms to neutral once (``park_action``
``"neutral"``) or holds (``"hold"``). Near the zenith (no deflection) it
does not chase the parallactic angle: two prisms at the null cancel
whatever their common angle.

Safety
------
``port.type`` ``"serial"`` needs ``"allow_hardware": true`` **and**
``--hardware``; ``"sim"`` runs :mod:`robofast_core.devices.adc_sim`.
Starting, connecting, reading and stopping never move a motor (startup
only reads the positions; no controller reset). Homing only on
``Action("Home")``. Every move is to a step count in 0..3999 and is
bounded by ``move_timeout``; a move that ends off target, stalls or times
out is a fault, reported in ``Fault``/``Status``, and tracking holds until
``Tracking`` is written true again. Nothing leaves this process but the
Alpaca answers and its log.

Running
-------
::

    python -m robofast_core.devices.adc_server --config adc.json [--hardware | --simulate]

with a JSON config (``sites/flwo60/adc.json``). It prints one JSON line,
``{"url": ..., "port": ...}``, once listening.
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
from typing import Any, Protocol

from robofast_core.cancel import stop_task
from robofast_core.devices import adc_math
from robofast_core.devices.adc_mdrive import (
    STEPS_PER_TURN,
    MotorError,
    Motors,
    MotorTimeout,
    SerialTransport,
    Transport,
)
from robofast_core.devices.adc_sim import ADCMotorsSim
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
from robofast_core.devices.mearth_roof_port import HardwareNotAllowed
from robofast_core.devices.tcsgo_server import (
    COMMON_NAMES,
    _float_param,
    _Handler,
    _now_stamp,
)

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
TIMEOUT = 0x502
ACTIONS = ("Status", "Home", "Neutral", "Instrument")
SIM_KEYS = frozenset({"position", "speed"})
DEG_PER_STEP = 360.0 / STEPS_PER_TURN


@dataclass(frozen=True)
class SwitchSpec:
    name: str
    description: str
    minimum: float = 0.0
    maximum: float = 1.0
    step: float = 1.0
    writable: bool = False


#: The switch device's switches, by id.
SWITCHES: tuple[SwitchSpec, ...] = (
    SwitchSpec("Tracking", "ADC tracking on (legacy adc_on) or off (adc_off)", writable=True),
    SwitchSpec("Settled", "Tracking, with fresh input, prisms at their targets"),
    SwitchSpec("InputFresh", "The telescope's position was read within stale_after"),
    SwitchSpec("Fault", "A motor fault; tracking holds until Tracking is written true"),
    SwitchSpec("Referenced", "The motors are homed (or trusted at startup)"),
    SwitchSpec("ParallacticAngle", "Parallactic angle, deg", -180.0, 180.0, 0.01),
    SwitchSpec("ZenithDistance", "Zenith distance, deg", 0.0, 90.0, 0.01),
    SwitchSpec("Deflection", "Prism deflection from the null, deg", 0.0, 90.0, 0.01),
    SwitchSpec("PrismA", "Motor A angle from home, deg", 0.0, 360.0, 0.01),
    SwitchSpec("PrismB", "Motor B angle from home, deg", 0.0, 360.0, 0.01),
)
SWITCH_IDS = {s.name: i for i, s in enumerate(SWITCHES)}


# -- configuration ----------------------------------------------------------------


@dataclass
class PortConfig:
    #: "serial" (a real port; gated) or "sim" (the in-process simulator).
    type: str = "serial"
    device: str = "/dev/ttyS0"
    baud: int = 9600
    #: Bound on one exchange.
    timeout: float = 1.0
    sim: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstrumentConfig:
    #: The band the ``refraction`` model corrects (microns).
    blue_um: float = 0.39
    red_um: float = 0.91
    #: Added to the parallactic angle (deg), if the instrument needs it.
    pa_offset: float = 0.0


@dataclass
class ADCConfig:
    port: PortConfig = field(default_factory=PortConfig)
    #: Must be true (and ``--hardware`` given) for a real port.
    allow_hardware: bool = False
    name: str = "flwo60"
    listen: str = "127.0.0.1:11180"
    #: The Alpaca Telescope to follow, ``host:port/N``; None: no input.
    telescope: str | None = None
    #: Site latitude (deg), for the parallactic angle.
    latitude: float = 31.6809
    #: The legacy ``adc_d_swap_congruent`` variant (prism targets exchanged).
    swap_prisms: bool = False
    #: "legacy_table" (the legacy daemon's) or "refraction" (ERFA).
    deflection: str = "legacy_table"
    max_deflection: float = 80.0
    #: The ADC's full correction (arcsec) for the ``refraction`` model.
    max_dispersion_arcsec: float = 3.8
    atmosphere: dict[str, float] = field(default_factory=dict)
    instruments: dict[str, dict[str, float]] = field(
        default_factory=lambda: {"tres": {}, "fast": {}}
    )
    instrument: str = "tres"
    #: Added to the parallactic angle (deg).
    pa_offset: float = 0.0
    #: Tracking cadence (legacy: 2 s) and the move threshold (deg).
    update_interval: float = 2.0
    deadband: float = 1.0
    #: Input older than this (s) is stale: hold.
    stale_after: float = 10.0
    settle_after_slew: float = 2.0
    #: Hold beyond this zenith distance (deg).
    max_zenith_distance: float = 75.0
    #: At park: "neutral" (turn the prisms home once) or "hold".
    park_action: str = "neutral"
    #: Writing Tracking false turns the prisms home (the legacy adc_off homed).
    neutral_on_off: bool = True
    #: Take the controllers' step counters as referenced at startup.
    trust_counter: bool = False
    #: Motor names on the line.
    motors: list[str] = field(default_factory=lambda: ["A", "B"])
    move_timeout: float = 30.0
    #: A move whose position has not changed for this long (s) has stalled.
    stall_time: float = 2.0
    #: Seconds to leave each motor's homing routine alone (legacy 8, 12).
    home_wait: list[float] = field(default_factory=lambda: [8.0, 12.0])
    home_timeout: float = 60.0
    #: Steps from the target that count as there.
    tolerance: int = 2
    poll_interval: float = 0.25
    #: Idle refresh of the motor positions (s).
    idle_poll: float = 5.0
    telescope_timeout: float = 3.0
    request_timeout: float = 4.5
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ADCConfig:
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
        if cfg.port.type not in ("serial", "sim"):
            raise ValueError("port.type must be 'serial' or 'sim'")
        if set(cfg.port.sim) - SIM_KEYS:
            raise ValueError(f"unknown port.sim keys: {sorted(set(cfg.port.sim) - SIM_KEYS)}")
        if cfg.deflection not in ("legacy_table", "refraction"):
            raise ValueError("deflection must be 'legacy_table' or 'refraction'")
        if cfg.park_action not in ("neutral", "hold"):
            raise ValueError("park_action must be 'neutral' or 'hold'")
        if len(cfg.motors) != 2 or len(set(cfg.motors)) != 2:
            raise ValueError("motors: two distinct names")
        if len(cfg.home_wait) != 2 or min(cfg.home_wait) < 0:
            raise ValueError("home_wait: two non-negative times")
        cfg.instruments = {k: v for k, v in cfg.instruments.items() if not k.startswith("_")}
        for name, inst in cfg.instruments.items():
            try:
                instrument_config(inst)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"instruments.{name}: {exc}") from None
        if cfg.instrument not in cfg.instruments:
            raise ValueError(f"instrument {cfg.instrument!r} is not in instruments")
        try:
            adc_math.Atmosphere(**cfg.atmosphere)
        except TypeError as exc:
            raise ValueError(f"atmosphere: {exc}") from None
        if not -90.0 <= cfg.latitude <= 90.0:
            raise ValueError("latitude must be in -90..90")
        if not 0.0 <= cfg.max_deflection <= 90.0:
            raise ValueError("max_deflection must be in 0..90")
        if not 0.0 < cfg.max_zenith_distance <= 90.0:
            raise ValueError("max_zenith_distance must be in (0, 90]")
        for name in (
            "update_interval",
            "deadband",
            "stale_after",
            "move_timeout",
            "stall_time",
            "home_timeout",
            "poll_interval",
            "idle_poll",
            "telescope_timeout",
            "max_dispersion_arcsec",
        ):
            if getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if cfg.tolerance < 0 or cfg.settle_after_slew < 0:
            raise ValueError("tolerance and settle_after_slew must not be negative")
        return cfg


def instrument_config(d: dict[str, Any]) -> InstrumentConfig:
    inst = InstrumentConfig(**{k: v for k, v in d.items() if not k.startswith("_")})
    if not 0.2 < inst.blue_um < inst.red_um < 3.0:
        raise ValueError("need 0.2 < blue_um < red_um < 3.0")
    return inst


def make_transport(cfg: PortConfig) -> Transport:
    if cfg.type == "sim":
        sim = dict(cfg.sim)
        if "position" in sim:
            sim["position"] = tuple(sim["position"])
        return ADCMotorsSim(**sim)
    return SerialTransport(cfg.device, cfg.baud)


# -- the telescope's position -----------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One read of the telescope (altitude and azimuth in deg)."""

    altitude: float
    azimuth: float
    slewing: bool
    at_park: bool


class TelescopeFeed(Protocol):
    async def read(self) -> Reading: ...

    async def close(self) -> None: ...


class AlpacaTelescopeFeed:
    """Reads an Alpaca Telescope: ``DeviceState`` (one request), or the
    four properties if it has none. Only ``Connected`` is ever written."""

    def __init__(self, address: str, number: int, timeout: float) -> None:
        from robofast_core.alpaca_client import AlpacaDevice

        self.device = AlpacaDevice(address, "telescope", number)
        self.timeout = timeout
        self._connected = False
        self._state = True

    async def read(self) -> Reading:
        from robofast_core.alpaca_client import NotImplementedException

        dev, t = self.device, self.timeout
        if not self._connected:
            await dev.put("Connected", timeout=t, Connected=True)
            self._connected = True
        values: dict[str, Any] = {}
        if self._state:
            try:
                state = await dev.get("DeviceState", timeout=t)
                values = {
                    str(x.get("Name")): x.get("Value") for x in state or [] if isinstance(x, dict)
                }
            except NotImplementedException:
                self._state = False
        for name in ("Altitude", "Azimuth", "Slewing", "AtPark"):
            if name not in values:
                values[name] = await dev.get(name, timeout=t)
        return Reading(
            float(values["Altitude"]),
            float(values["Azimuth"]),
            bool(values["Slewing"]),
            bool(values["AtPark"]),
        )

    async def close(self) -> None:
        await self.device.aclose()


# -- the ADC ----------------------------------------------------------------------


@dataclass
class Input:
    """The last good reading, with what was computed from it."""

    reading: Reading
    #: ``time.monotonic()`` when the read started.
    t: float
    pa: float
    zd: float


@dataclass
class Target:
    pa: float
    zd: float
    deflection: float
    #: Motor angles from home (deg).
    motor: tuple[float, float]


@dataclass
class Job:
    kind: str  # "track", "manual", "neutral", "home"
    #: Target steps per motor (only the motors that move).
    steps: dict[str, int]
    started: float
    last_change: float
    last_pos: dict[str, int | None] = field(default_factory=dict)


class ADC:
    """The tracking logic over :class:`Motors`. Runs on one event loop."""

    def __init__(
        self, motors: Motors, config: ADCConfig, feed: TelescopeFeed | None = None
    ) -> None:
        self.motors = motors
        self.config = config
        self.feed = feed
        self.names: tuple[str, str] = (config.motors[0], config.motors[1])
        self.positions: dict[str, int | None] = dict.fromkeys(self.names)
        self.positions_at: float | None = None
        self.referenced = False
        self.tracking = False
        self.fault: str | None = None
        self.job: Job | None = None
        self.input: Input | None = None
        self.input_error: str | None = None
        self.target: Target | None = None
        self.hold: str | None = "tracking off"
        self.instrument = config.instrument
        #: When Tracking was last written true (Settled needs a read after it).
        self.armed_at = 0.0
        self._slewing_seen = False
        self._slew_ended: float | None = None
        self._parked_neutral = False
        #: Tracking went off during a move: neutral once it ends.
        self._pending_neutral = False
        self._next_update = 0.0
        self._poke = asyncio.Event()
        self._home_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._stopping = False
        self.moves = 0
        self.last_move: dict[str, Any] | None = None

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._run_task = asyncio.ensure_future(self._run())

    async def shutdown(self) -> None:
        self._stopping = True
        self._poke.set()
        await stop_task(self._home_task, 2.0)
        await stop_task(self._run_task, 2.0)
        if self.feed is not None:
            with contextlib.suppress(Exception):
                await self.feed.close()

    async def connect(self) -> None:
        """Read both positions. Sends no motion."""
        await self.read_positions()
        if self.config.trust_counter and not self.referenced:
            self.referenced = True
            log.warning("ADC: step counters trusted as referenced (trust_counter)")

    async def read_positions(self) -> dict[str, int | None]:
        for m in self.names:
            self.positions[m] = await self.motors.position(m) % STEPS_PER_TURN
        self.positions_at = time.monotonic()
        return self.positions

    # -- state ------------------------------------------------------------------

    def motor_deg(self, m: str) -> float | None:
        p = self.positions[m]
        return None if p is None else (p * DEG_PER_STEP) % 360.0

    def input_fresh(self) -> bool:
        return (
            self.input is not None
            and time.monotonic() - self.input.t <= self.config.stale_after
            and self.input_error is None
        )

    def on_target(self) -> bool:
        tgt = self.target
        if tgt is None:
            return False
        a, b = (self.motor_deg(m) for m in self.names)
        if a is None or b is None:
            return False
        db = max(self.config.deadband, self.config.tolerance * DEG_PER_STEP)
        if tgt.deflection == 0.0 and adc_math.angle_diff(a, b) <= db:
            return True  # at the null: any common angle cancels
        return all(adc_math.angle_diff(p, t) <= db for p, t in zip((a, b), tgt.motor, strict=True))

    def settled(self) -> bool:
        return (
            self.tracking
            and self.fault is None
            and self.job is None
            and self.hold is None
            and self.input_fresh()
            and self.input is not None
            and self.input.t >= self.armed_at
            and self.on_target()
        )

    def moving(self, motor: str | None = None) -> bool:
        if self.job is None:
            return False
        return motor is None or self.job.kind == "home" or motor in self.job.steps

    def switch_value(self, i: int) -> float:
        spec = SWITCHES[i]
        inp, tgt = self.input, self.target
        value: float | None
        if spec.name == "Tracking":
            value = float(self.tracking)
        elif spec.name == "Settled":
            value = float(self.settled())
        elif spec.name == "InputFresh":
            value = float(self.input_fresh())
        elif spec.name == "Fault":
            value = float(self.fault is not None)
        elif spec.name == "Referenced":
            value = float(self.referenced)
        elif spec.name == "ParallacticAngle":
            value = None if inp is None else inp.pa
        elif spec.name == "ZenithDistance":
            value = None if inp is None else inp.zd
        elif spec.name == "Deflection":
            value = None if tgt is None else tgt.deflection
        else:
            value = self.motor_deg(self.names[0 if spec.name == "PrismA" else 1])
        if value is None:
            return spec.minimum if spec.minimum >= 0 else 0.0
        value = round(round(value / spec.step) * spec.step, 6)
        return min(
            max(value, spec.minimum), spec.maximum - (spec.step if spec.maximum == 360 else 0)
        )

    def status(self) -> dict[str, Any]:
        inp, tgt, job = self.input, self.target, self.job
        now = time.monotonic()
        return {
            "tracking": self.tracking,
            "settled": self.settled(),
            "hold": self.hold,
            "fault": self.fault,
            "referenced": self.referenced,
            "instrument": self.instrument,
            "deflection_model": self.config.deflection,
            "input": None
            if inp is None
            else {
                "altitude": round(inp.reading.altitude, 4),
                "azimuth": round(inp.reading.azimuth, 4),
                "slewing": inp.reading.slewing,
                "at_park": inp.reading.at_park,
                "parallactic_angle": round(inp.pa, 3),
                "zenith_distance": round(inp.zd, 3),
                "age": round(now - inp.t, 1),
                "fresh": self.input_fresh(),
            },
            "input_error": self.input_error,
            "target": None
            if tgt is None
            else {
                "deflection": tgt.deflection,
                "motor_deg": [round(x, 3) for x in tgt.motor],
                "prism_angle": [
                    round(adc_math.prism_angle(x, h), 3)
                    for x, h in zip(tgt.motor, adc_math.HOME_ANGLES, strict=True)
                ],
            },
            "motors": {
                m: {
                    "steps": self.positions[m],
                    "deg": None if (d := self.motor_deg(m)) is None else round(d, 3),
                }
                for m in self.names
            },
            "positions_age": None
            if self.positions_at is None
            else round(now - self.positions_at, 1),
            "job": None
            if job is None
            else {
                "kind": job.kind,
                "steps": job.steps,
                "elapsed": round(now - job.started, 1),
            },
            "moves": self.moves,
            "last_move": self.last_move,
            "last_motion_command": self.motors.last_motion,
        }

    # -- commands ---------------------------------------------------------------

    def _busy(self, what: str) -> None:
        if self.job is not None:
            raise AlpacaException(
                INVALID_OPERATION, f"{what}: the prisms are moving ({self.job.kind})"
            )

    def _need_referenced(self, what: str) -> None:
        if not self.referenced:
            raise AlpacaException(
                INVALID_OPERATION, f"{what}: motors not referenced: Action('Home') first"
            )

    async def set_tracking(self, on: bool) -> None:
        if on:
            self._need_referenced("Tracking")
            if self.fault is not None:
                log.warning("ADC: fault cleared by Tracking on: %s", self.fault)
            self.fault = None
            if not self.tracking:
                log.info("ADC: tracking on")
            self.tracking = True
            self.armed_at = time.monotonic()
            self._parked_neutral = self._pending_neutral = False
            self._next_update = 0.0
            self.hold = "waiting for input"
            self._poke.set()
            return
        if not self.tracking:
            return  # already off: nothing moves
        self.tracking = False
        self.hold = "tracking off"
        log.info("ADC: tracking off")
        if self.config.neutral_on_off and self.referenced:
            if self.job is None:
                await self._start_move("neutral", dict.fromkeys(self.names, 0))
            else:
                self._pending_neutral = True

    async def neutral(self) -> str:
        if self.tracking:
            raise AlpacaException(INVALID_OPERATION, "Neutral: tracking is on")
        self._busy("Neutral")
        self._need_referenced("Neutral")
        self.fault = None
        await self._start_move("neutral", dict.fromkeys(self.names, 0))
        return "turning both prisms to home (neutral)"

    async def move_motor(self, motor: str, deg: float, what: str) -> None:
        if self.tracking:
            raise AlpacaException(INVALID_OPERATION, f"{what}: tracking is on; turn it off first")
        self._busy(what)
        self._need_referenced(what)
        self.fault = None
        await self._start_move("manual", {motor: adc_math.steps_for(deg % 360.0) % STEPS_PER_TURN})

    async def home(self) -> str:
        if self.tracking:
            raise AlpacaException(INVALID_OPERATION, "Home: tracking is on; turn it off first")
        self._busy("Home")
        self.fault = None
        self.referenced = False
        now = time.monotonic()
        self.job = Job("home", {}, now, now)
        self._home_task = asyncio.ensure_future(self._home())
        return "homing: motor A, then motor B (the prisms turn)"

    def set_instrument(self, name: str) -> str:
        if name not in self.config.instruments:
            raise AlpacaException(
                INVALID_VALUE, f"no instrument {name!r} (have {sorted(self.config.instruments)})"
            )
        self.instrument = name
        self.armed_at = time.monotonic()  # Settled waits for a read with it
        self._next_update = 0.0
        self._poke.set()
        return name

    # -- motion -----------------------------------------------------------------

    async def _start_move(self, kind: str, steps: dict[str, int]) -> None:
        cur = {m: self.positions[m] for m in steps}
        todo = {m: s for m, s in steps.items() if cur[m] is None or abs(cur[m] - s) > 0}
        if not todo:
            return
        now = time.monotonic()
        self.job = Job(kind, todo, now, now, dict(cur))
        self.moves += 1
        self.last_move = {"kind": kind, "steps": dict(todo), "from": cur}
        log.info("ADC: %s move %s -> %s", kind, cur, todo)
        try:
            for m, s in todo.items():
                await self.motors.move(m, s)
        except MotorError as exc:
            self.job = None
            self._fail(f"{kind} move: {exc}")
            if isinstance(exc, MotorTimeout):
                raise AlpacaException(TIMEOUT, str(exc)) from None
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        self._poke.set()

    def _fail(self, why: str) -> None:
        self.fault = why
        log.error("ADC fault: %s", why)

    async def _check_job(self) -> None:
        job = self.job
        if job is None or job.kind == "home":
            return
        cfg = self.config
        try:
            await self.read_positions()
        except MotorError as exc:
            self.job = None
            self._fail(f"{job.kind} move: position read failed: {exc}")
            return
        now = time.monotonic()
        pos = {m: self.positions[m] for m in job.steps}
        if pos != job.last_pos:
            job.last_pos, job.last_change = dict(pos), now
        there = all(
            p is not None and abs(p - s) <= cfg.tolerance
            for p, s in zip(pos.values(), job.steps.values(), strict=True)
        )
        if there:
            self.job = None
            log.info("ADC: %s move done after %.1f s", job.kind, now - job.started)
            self._poke.set()
        elif now - job.started > cfg.move_timeout:
            self.job = None
            self._fail(
                f"{job.kind} move did not finish within {cfg.move_timeout:g} s:"
                f" at {pos}, target {job.steps}"
            )
        elif now - job.last_change > cfg.stall_time:
            self.job = None
            self._fail(f"{job.kind} move stopped at {pos}, target {job.steps}")

    async def _home(self) -> None:
        cfg = self.config
        try:
            for m, wait in zip(self.names, cfg.home_wait, strict=True):
                log.warning("ADC: homing motor %s (cl HO)", m)
                await self.motors.home(m)
                self.moves += 1
                await asyncio.sleep(wait)  # the legacy daemon sends nothing meanwhile
                deadline = time.monotonic() + cfg.home_timeout
                last = None
                while True:
                    p = await self.motors.position(m)
                    if p == last:
                        break
                    if time.monotonic() > deadline:
                        raise MotorError(f"motor {m} still moving after {cfg.home_timeout:g} s")
                    last = p
                    await asyncio.sleep(max(cfg.poll_interval, 0.05))
                self.positions[m] = p % STEPS_PER_TURN
                self.positions_at = time.monotonic()
                if min(p % STEPS_PER_TURN, STEPS_PER_TURN - p % STEPS_PER_TURN) > cfg.tolerance:
                    raise MotorError(f"motor {m} homed at step {p}, not 0")
        except (MotorError, TimeoutError) as exc:
            self._fail(f"home: {exc}")
        else:
            self.referenced = True
            log.warning("ADC: homed; motors referenced")
        finally:
            self.job = None
            self.last_move = {"kind": "home", "steps": dict.fromkeys(self.names, 0)}
            self._poke.set()

    # -- tracking ---------------------------------------------------------------

    async def _read_input(self) -> None:
        cfg = self.config
        if self.feed is None:
            self.input_error = "no telescope configured"
            return
        t = time.monotonic()
        try:
            async with asyncio.timeout(cfg.telescope_timeout + 0.5):
                r = await self.feed.read()
        except Exception as exc:  # any failure is no input: hold
            self.input_error = f"telescope read failed: {type(exc).__name__}: {exc}"
            return
        if not (
            math.isfinite(r.altitude) and math.isfinite(r.azimuth) and -90.0 <= r.altitude <= 90.0
        ):
            self.input_error = f"telescope position invalid: alt {r.altitude!r} az {r.azimuth!r}"
            return
        pa, zd = adc_math.from_altaz(r.altitude, r.azimuth % 360.0, cfg.latitude)
        self.input = Input(r, t, pa, zd)
        self.input_error = None

    def _compute(self, inp: Input) -> Target:
        cfg = self.config
        inst = instrument_config(cfg.instruments[self.instrument])
        if cfg.deflection == "legacy_table":
            defl = min(adc_math.legacy_deflection(inp.zd), cfg.max_deflection)
        else:
            defl = adc_math.refraction_deflection(
                inp.zd,
                inst.blue_um,
                inst.red_um,
                cfg.max_dispersion_arcsec,
                cfg.max_deflection,
                adc_math.Atmosphere(**cfg.atmosphere),
            )
        pa = inp.pa + cfg.pa_offset + inst.pa_offset
        motor = adc_math.motor_degrees(pa, defl, swap=cfg.swap_prisms)
        return Target(inp.pa, inp.zd, defl, motor)

    async def _track(self) -> None:
        cfg = self.config
        await self._read_input()
        now = time.monotonic()
        inp = self.input
        if not self.input_fresh() or inp is None:
            self.hold = self.input_error or "telescope input stale"
            return
        r = inp.reading
        if r.at_park:
            self.hold = "telescope parked"
            if cfg.park_action == "neutral" and not self._parked_neutral and self.job is None:
                self._parked_neutral = True
                log.info("ADC: telescope parked: prisms to neutral")
                self.target = None
                with contextlib.suppress(AlpacaException):
                    await self._start_move("neutral", dict.fromkeys(self.names, 0))
            return
        self._parked_neutral = False
        if r.slewing:
            self._slewing_seen, self._slew_ended = True, None
            self.hold = "telescope slewing"
            return
        if self._slewing_seen:
            self._slewing_seen, self._slew_ended = False, now
        if self._slew_ended is not None and now - self._slew_ended < cfg.settle_after_slew:
            self.hold = "settling after a slew"
            return
        self._slew_ended = None
        if not 0.0 <= inp.zd <= cfg.max_zenith_distance:
            self.hold = f"zenith distance {inp.zd:.1f} beyond {cfg.max_zenith_distance:g}"
            return
        if self.fault is not None:
            self.hold = "fault"
            return
        self.target = self._compute(inp)
        self.hold = None
        if self.job is not None or self.on_target():
            return
        steps = {
            m: adc_math.steps_for(d) % STEPS_PER_TURN
            for m, d in zip(self.names, self.target.motor, strict=True)
        }
        with contextlib.suppress(AlpacaException):  # a failed command is the fault
            await self._start_move("track", steps)

    async def _run(self) -> None:
        cfg = self.config
        while not self._stopping:
            self._poke.clear()
            now = time.monotonic()
            try:
                if self.job is not None:
                    await self._check_job()
                if self._pending_neutral and self.job is None and not self.tracking:
                    self._pending_neutral = False
                    if self.fault is None:
                        with contextlib.suppress(AlpacaException):
                            await self._start_move("neutral", dict.fromkeys(self.names, 0))
                if self.tracking and now >= self._next_update:
                    self._next_update = now + cfg.update_interval
                    await self._track()
                elif self.job is None and (
                    self.positions_at is None or now - self.positions_at > cfg.idle_poll
                ):
                    try:
                        await self.read_positions()
                    except MotorError as exc:
                        log.warning("ADC: idle position read failed: %s", exc)
                        self.positions_at = now  # retry after idle_poll
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ADC loop")
            wait = cfg.poll_interval if self.job is not None else cfg.poll_interval * 4
            if self.tracking:
                wait = min(wait, max(0.01, self._next_update - time.monotonic()))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await self._poke.wait()


# -- Alpaca over HTTP ------------------------------------------------------------------


@dataclass
class _RotatorFrame:
    """Per-rotator ``Sync``/``Reverse`` state (not persisted)."""

    offset: float = 0.0
    reverse: bool = False
    target: float | None = None


class ADCServer(ThreadingHTTPServer):
    """The Alpaca server; the ADC runs on a private event-loop thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        config: ADCConfig,
        *,
        hardware: bool = False,
        feed: TelescopeFeed | None = None,
    ) -> None:
        allow = config.allow_hardware and hardware
        if config.port.type == "serial" and not allow:
            raise HardwareNotAllowed(
                f"port {config.port.device!r} is real hardware: it needs allow_hardware in the"
                " config AND --hardware (port.type 'sim' runs the simulator)"
            )
        self.transport: Transport = make_transport(config.port)
        host, _, port = config.listen.rpartition(":")
        try:
            super().__init__((host, int(port)), _Handler)  # type: ignore[arg-type]
        except BaseException:
            self.transport.close()
            raise
        self.config = config
        self.devices = ("switch", "rotator", "rotator/1")
        self.frames = {m: _RotatorFrame() for m in config.motors}
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="adc-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self._stopped = False
        self.adc: ADC = self._run_sync(self._build(feed))

    @property
    def sim(self) -> ADCMotorsSim | None:
        return self.transport if isinstance(self.transport, ADCMotorsSim) else None

    async def _build(self, feed: TelescopeFeed | None) -> ADC:
        cfg = self.config
        if feed is None and cfg.telescope is not None:
            from robofast_core.config import device_address

            address, number = device_address(cfg.telescope)
            feed = AlpacaTelescopeFeed(address, number, cfg.telescope_timeout)
        motors = Motors(
            self.transport, names=(cfg.motors[0], cfg.motors[1]), timeout=cfg.port.timeout
        )
        adc = ADC(motors, cfg, feed)
        try:
            await adc.connect()
        except MotorError as exc:
            adc._fail(f"motors not answering at startup: {exc}")
        adc.start()
        return adc

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_thread.is_alive():
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(15)  # type: ignore[arg-type]

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

    def _call(self, fn: Callable[[], Any], what: str) -> Any:
        async def run() -> Any:
            value = fn()
            return await value if asyncio.iscoroutine(value) else value

        fut = asyncio.run_coroutine_threadsafe(run(), self._loop)
        try:
            return fut.result(self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AlpacaException(
                TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except MotorTimeout as exc:
            raise AlpacaException(TIMEOUT, str(exc)) from None
        except MotorError as exc:
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management ----------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast ADC ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            a, b = self.config.motors
            return [
                {
                    "DeviceName": f"{name} ADC tracking",
                    "DeviceType": "Switch",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-adc-{name}-switch-0",
                },
                {
                    "DeviceName": f"{name} ADC prism {a}",
                    "DeviceType": "Rotator",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-adc-{name}-rotator-0",
                },
                {
                    "DeviceName": f"{name} ADC prism {b}",
                    "DeviceType": "Rotator",
                    "DeviceNumber": 1,
                    "UniqueID": f"robofast-adc-{name}-rotator-1",
                },
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>ADC</title>"
            f"</head><body><h1>robofast ADC: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members -------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        with self._clients_lock:
            if value:
                self._clients.add((device, client))
            else:
                self._clients.discard((device, client))

    def _motor(self, device: str) -> str:
        return self.config.motors[1 if device == "rotator/1" else 0]

    def _common(self, device: str, member: str, client: int) -> Any:
        cfg = self.config
        if device == "switch":
            desc, info, version, name = (
                f"ADC tracking for the {cfg.name} telescope",
                "robofast_core.devices.adc_server: ADC tracking service",
                2,
                f"robofast ADC {cfg.name}",
            )
            actions = list(ACTIONS)
        else:
            m = self._motor(device)
            desc, info, version, name = (
                f"ADC prism {m} ({cfg.name}): motor angle from home",
                "robofast_core.devices.adc_server: ADC prism motor",
                4,
                f"robofast ADC {cfg.name} prism {m}",
            )
            actions = []
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": desc,
            "driverinfo": info,
            "driverversion": DRIVER_VERSION,
            "interfaceversion": version,
            "name": name,
            "supportedactions": actions,
        }[member]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        if verb == "PUT" and member in ("connected", "connect", "disconnect"):
            on = member == "connect" or (member == "connected" and _bool_param(params, "Connected"))
            self._set_connected(device, client, on)
            return None
        if member in COMMON_NAMES:
            if verb != "GET" or member in ("connect", "disconnect"):
                raise _BadRequest(f"{verb} {member} is not allowed")
            return self._common(device, member, client)
        if member == "action" and verb == "GET":
            raise _BadRequest("action is a PUT method")
        if device == "switch":
            return self._switch(verb, member, params, client)
        return self._rotator(device, verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    @staticmethod
    def _switch_id(params: dict[str, str], verb: str) -> int:
        key = "id" if verb == "GET" else "Id"
        if key not in params:
            raise _BadRequest("missing parameter Id")
        try:
            i = int(params[key])
        except ValueError:
            raise _BadRequest(f"Id={params[key]!r} is not an integer") from None
        if not 0 <= i < len(SWITCHES):
            raise AlpacaException(INVALID_VALUE, f"no switch {i}")
        return i

    def _switch(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        if member == "maxswitch" and verb == "GET":
            return len(SWITCHES)
        self._need("switch", member, client)
        adc = self.adc
        if member == "action":
            return self._action(params.get("Action", ""), params.get("Parameters", ""))
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        if verb == "GET":
            i = self._switch_id(params, verb)
            spec = SWITCHES[i]
            if member == "canwrite":
                return spec.writable
            if member == "getswitchname":
                return spec.name
            if member == "getswitchdescription":
                return spec.description
            if member == "minswitchvalue":
                return spec.minimum
            if member == "maxswitchvalue":
                return spec.maximum
            if member == "switchstep":
                return spec.step
            if member in ("getswitch", "getswitchvalue"):
                v = self._call(lambda: adc.switch_value(i), spec.name)
                return v if member == "getswitchvalue" else v > spec.minimum
            raise _BadRequest(f"unknown member {verb} {member}")
        if member in ("setswitch", "setswitchvalue"):
            i = self._switch_id(params, verb)
            spec = SWITCHES[i]
            if not spec.writable:
                raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} ({spec.name}) is read-only")
            if member == "setswitch":
                on = _bool_param(params, "State")
            else:
                v = _float_param(params, "Value")
                if not 0.0 <= v <= 1.0:
                    raise AlpacaException(INVALID_VALUE, f"switch {i}: value {v} not in [0, 1]")
                on = v >= 0.5
            return self._call(lambda: adc.set_tracking(on), "SetSwitch")
        if member == "setswitchname":
            raise AlpacaException(NOT_IMPLEMENTED, "names are fixed")
        raise _BadRequest(f"unknown member {verb} {member}")

    def _action(self, name: str, parameters: str) -> str:
        adc = self.adc
        key = name.lower()
        if key == "status":
            return json.dumps(self._call(adc.status, "Status"))
        if key == "home":
            return self._call(adc.home, "Home")
        if key == "neutral":
            return self._call(adc.neutral, "Neutral")
        if key == "instrument":
            inst = parameters.strip()
            if inst:
                return self._call(lambda: adc.set_instrument(inst), "Instrument")
            return self._call(lambda: adc.instrument, "Instrument")
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _rotator(
        self, device: str, verb: str, member: str, params: dict[str, str], client: int
    ) -> Any:
        adc, m = self.adc, self._motor(device)
        fr = self.frames[m]
        if verb == "GET" and member == "canreverse":
            return True
        self._need(device, member, client)

        def mech() -> float:
            d = adc.motor_deg(m)
            if d is None:
                raise AlpacaException(DRIVER_ERROR, f"motor {m}: position unknown")
            return d

        def sky(x: float) -> float:
            return ((-x if fr.reverse else x) + fr.offset) % 360.0

        def to_mech(p: float) -> float:
            x = (p - fr.offset) % 360.0
            return (-x if fr.reverse else x) % 360.0

        if verb == "GET":
            if member == "ismoving":
                return self._call(lambda: adc.moving(m), "IsMoving")
            if member == "mechanicalposition":
                return round(self._call(mech, member), 4)
            if member == "position":
                return round(sky(self._call(mech, member)), 4)
            if member == "targetposition":
                if fr.target is not None:
                    return round(fr.target, 4)
                return round(sky(self._call(mech, member)), 4)
            if member == "reverse":
                return fr.reverse
            if member == "stepsize":
                return DEG_PER_STEP
            if member == "devicestate":
                state: list[dict[str, Any]] = [
                    {"Name": "IsMoving", "Value": self._call(lambda: adc.moving(m), "IsMoving")}
                ]
                with contextlib.suppress(AlpacaException):
                    x = self._call(mech, "DeviceState")
                    state += [
                        {"Name": "MechanicalPosition", "Value": round(x, 4)},
                        {"Name": "Position", "Value": round(sky(x), 4)},
                    ]
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            raise _BadRequest(f"unknown member {verb} {member}")

        def angle(name: str) -> float:
            v = _float_param(params, name)
            if not 0.0 <= v < 360.0:
                raise AlpacaException(INVALID_VALUE, f"{name} {v} not in [0, 360)")
            return v

        if member == "reverse":
            fr.reverse = _bool_param(params, "Reverse")
            return None
        if member == "movemechanical":
            x = angle("Position")
            fr.target = sky(x)
            return self._call(lambda: adc.move_motor(m, x, "MoveMechanical"), member)
        if member == "moveabsolute":
            p = angle("Position")
            fr.target = p
            return self._call(lambda: adc.move_motor(m, to_mech(p), "MoveAbsolute"), member)
        if member == "move":
            d = _float_param(params, "Position")
            if not -360.0 < d < 360.0:
                raise AlpacaException(INVALID_VALUE, f"Position {d} not in (-360, 360)")
            p = (sky(self._call(mech, member)) + d) % 360.0
            fr.target = p
            return self._call(lambda: adc.move_motor(m, to_mech(p), "Move"), member)
        if member == "sync":
            p = angle("Position")
            x = self._call(mech, member)
            fr.offset = (p - (-x if fr.reverse else x)) % 360.0
            fr.target = None
            return None
        if member == "halt":
            raise AlpacaException(
                NOT_IMPLEMENTED,
                "Halt: the ADC motors have no stop command here (moves are bounded)",
            )
        if member == "action":
            raise AlpacaException(ACTION_NOT_IMPLEMENTED, "no rotator actions")
        if member in ("commandblind", "commandbool", "commandstring"):
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        raise _BadRequest(f"unknown member {verb} {member}")

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> ADCServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="adc-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        """Stop serving. Sends nothing to the motors: a move in progress completes."""
        if self._stopped:
            return
        self._stopped = True
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.adc.shutdown(), self._loop).result(5)
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
    mode.add_argument("--simulate", action="store_true", help="use the simulated motors")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = ADCConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    if args.simulate:
        config.port.type = "sim"
    try:
        server = ADCServer(config, hardware=args.hardware)
    except (HardwareNotAllowed, MotorError) as exc:
        log.error("%s", exc)
        sys.exit(2)
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
