"""Keeping an imaging camera cooled (docs/cooling.md).

A camera's ``cooling`` entry, part of the camera's own config in the
enclosure config (:func:`robofast.config.camera_entry`)::

    "camera": {"address": "host:port/N",
               "cooling": {"setpoint": -50, "tolerance": 1.0, "settle_timeout": 900}}

makes robofast keep that camera's CCD at ``setpoint`` (C) all night, with
nothing outside robofast setting coolers. One path sets the cooler,
:func:`set_cooling`: ``SetCCDTemperature`` then ``CoolerOn = True``, and it
runs

* every time the camera's :class:`~robofast.owner.DeviceOwner` connects it
  afresh (its ``on_connect`` hook: :meth:`CameraCooling.on_connect`): at
  startup (the sequencer touches each cooled camera as it starts), after a
  timeout dropped the driver, and after every rung of the recovery ladder
  (``reconnect``, ``restart``, ``powercycle``: each reconnects). MINERVA
  T1's Andor ASCOM driver switches the cooler **off** when it is connected
  again (``Connected`` false -> true); this puts it back on;
* when the cooler is found off before an exposure (:meth:`CameraCooling.check`):
  turned back on, with an alarm, at most once per ``restart_interval`` s.

A camera that cannot set its temperature (``CanSetCCDTemperature`` false,
or ``SetCCDTemperature``/``CoolerOn`` answering NotImplemented) raises
:class:`~robofast.owner.Unsupported` from :func:`set_cooling`;
:class:`CameraCooling` logs that once and leaves the camera alone for the
rest of the night.

Before each exposure (:meth:`CameraCooling.check`) the CCD temperature is
read: within ``tolerance`` of the setpoint it is settled. If not, a light
frame waits for it -- but never past ``settle_timeout`` s after the cooler
was last set. After that:

* a CCD still **warmer** than its setpoint by more than ``tolerance``, with
  the cooler on (the commanded setpoint was not reached: MINERVA T4,
  2026-11-27/28, CCD at 20.7 C all night with its cooler at 100% and the
  setpoint -50 C) is a camera fault (``not_settled: "fault"``, the
  default): :meth:`CameraCooling.check` raises :class:`CoolingFault`
  (a ``cooling_fault`` event), the sequencer makes the telescope safe and
  climbs the camera's recovery ladder, waiting again for the CCD to settle
  after each rung (:meth:`CameraCooling.settle`), and a camera the ladder
  cannot bring back (or one without a ladder) is **disabled** for the
  night: no more exposures on it, and the plan no longer gets imaging for
  that telescope (spectra go on; docs/cooling.md, "A cooler that cannot
  reach its setpoint");
* otherwise (a CCD colder than a warm setpoint, a temperature that cannot
  be read, a cooler that is off, or ``not_settled: "warn"``) the frames are
  taken anyway, with a warning event and ``COOLOK = F`` in their headers.

A camera that cannot set its temperature, or one robofast does not cool
(no ``cooling`` entry), never faults here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robofast.owner import Unsupported

log = logging.getLogger(__name__)

__all__ = [
    "COOLING_FAULT",
    "CameraCooling",
    "CoolingConfig",
    "CoolingFault",
    "CoolingStatus",
    "is_cooling_fault",
    "set_cooling",
]

#: ASCOM NotImplemented and PropertyNotImplemented.
NOT_IMPLEMENTED = (0x400, 0x401)
#: In every :class:`CoolingFault`'s message (and so in the sequencer's error
#: text for it, its ``repr``): :func:`is_cooling_fault`.
COOLING_FAULT = "camera cooling fault"
#: ``not_settled`` values.
NOT_SETTLED = ("fault", "warn")


class CoolingFault(Exception):
    """The camera's cooler is on but the CCD did not reach its setpoint
    within ``settle_timeout`` (module doc): the camera cannot take useful
    frames. Not a :class:`~robofast.owner.DeviceError`: the camera answers."""


def is_cooling_fault(error: str) -> bool:
    """A :class:`CoolingFault`, from the sequencer's error text."""
    return COOLING_FAULT in error


@dataclass(frozen=True)
class CoolingConfig:
    """A camera's ``cooling`` entry (module doc)."""

    #: CCD setpoint (C).
    setpoint: float
    #: Settled when the CCD is within this of the setpoint (C).
    tolerance: float = 1.0
    #: How long after the cooler was set a light frame may wait for the CCD
    #: to settle (s); later, frames go ahead with a warning.
    settle_timeout: float = 900.0
    #: A cooler found off is turned back on at most once per this (s).
    restart_interval: float = 300.0
    #: How often to read the temperature while waiting to settle (s).
    poll: float = 5.0
    #: A CCD still too warm ``settle_timeout`` after the cooler was set,
    #: with the cooler on: ``"fault"`` (the camera's recovery ladder, then
    #: disabled for the night) or ``"warn"`` (expose anyway, as before
    #: 2026-11-28).
    not_settled: str = "fault"
    #: After each rung of the camera's recovery ladder for a cooling fault:
    #: how long (s) to wait for the CCD to reach its setpoint before the
    #: rung counts as failed. None: ``settle_timeout``.
    recovery_settle_timeout: float | None = None

    def __post_init__(self) -> None:
        if self.not_settled not in NOT_SETTLED:
            raise ValueError(f"cooling.not_settled: one of {NOT_SETTLED}")
        names = ["setpoint", "tolerance", "settle_timeout", "restart_interval", "poll"]
        if self.recovery_settle_timeout is not None:
            names.append("recovery_settle_timeout")
        for name in names:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"cooling.{name}: a number (got {v!r})")
        if not -150.0 <= self.setpoint <= 50.0:
            raise ValueError(f"cooling.setpoint {self.setpoint}: outside -150..50 C")
        if self.tolerance <= 0:
            raise ValueError("cooling.tolerance: must be > 0")
        if self.settle_timeout < 0 or self.restart_interval < 0 or self.poll <= 0:
            raise ValueError("cooling: settle_timeout, restart_interval >= 0 and poll > 0")
        if self.recovery_settle_timeout is not None and self.recovery_settle_timeout < 0:
            raise ValueError("cooling.recovery_settle_timeout: must be >= 0")

    @property
    def recovery_settle(self) -> float:
        """How long a recovery rung waits for the CCD (s; ``recovery_settle_timeout``)."""
        if self.recovery_settle_timeout is None:
            return self.settle_timeout
        return float(self.recovery_settle_timeout)

    @classmethod
    def from_dict(cls, d: Any) -> CoolingConfig:
        if isinstance(d, bool) or isinstance(d, (int, float)):
            d = {"setpoint": d}
        if not isinstance(d, dict):
            raise ValueError("cooling: expected {'setpoint': C, ...}")
        d = {k: v for k, v in d.items() if not str(k).startswith("_")}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"cooling: unknown keys {sorted(unknown)}")
        if "setpoint" not in d:
            raise ValueError("cooling: no setpoint")
        return cls(**d)


def _not_implemented(exc: BaseException) -> bool:
    return getattr(exc, "number", None) in NOT_IMPLEMENTED or "NotImplemented" in type(exc).__name__


async def set_cooling(device: Any, setpoint: float, *, timeout: float | None = None) -> None:
    """THE path that cools a camera: ``SetCCDTemperature`` = ``setpoint``,
    then ``CoolerOn`` = True. ``device`` is a driver or a
    :class:`~robofast.owner.Grant` (both ``get``/``set`` with ``timeout``).
    Raises :class:`~robofast.owner.Unsupported` if the camera cannot."""
    kw = {} if timeout is None else {"timeout": timeout}
    try:
        can = await device.get("CanSetCCDTemperature", **kw)
    except Exception as exc:
        if _not_implemented(exc):
            raise Unsupported(f"CanSetCCDTemperature not implemented ({exc})") from exc
        raise
    if not can:
        raise Unsupported("the camera cannot set its CCD temperature (CanSetCCDTemperature false)")
    for member, value in (("SetCCDTemperature", float(setpoint)), ("CoolerOn", True)):
        try:
            await device.set(member, value, **kw)
        except Exception as exc:
            if _not_implemented(exc):
                raise Unsupported(f"{member} not implemented ({exc})") from exc
            raise


@dataclass
class CoolingStatus:
    """What :meth:`CameraCooling.check` found before an exposure."""

    ccd_temp: float | None
    cooler_on: bool | None
    #: Within tolerance of the setpoint.
    settled: bool
    #: The cooler was found off and turned back on now.
    restarted: bool = False
    #: Seconds waited for the CCD to settle.
    waited: float = 0.0


class CameraCooling:
    """One camera's cooling (module doc). ``name`` is the telescope's.

    ``alarm(text)`` and ``event(what, **detail)`` report (the sequencer
    wires them to its own)."""

    def __init__(
        self,
        name: str,
        config: CoolingConfig,
        *,
        alarm: Callable[[str], None] | None = None,
        event: Callable[..., None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.alarm = alarm or (lambda text: None)
        self.event = event or (lambda what, **kw: None)
        # time.monotonic read at each call, not bound here (virtual time
        # patches it: robofast/testing/virtual_time.py).
        self.clock = clock or (lambda: time.monotonic())
        #: Why this camera cannot be cooled (None: it can, as far as known).
        self.unsupported: str | None = None
        #: ``clock()`` when the cooler was last set (None: not yet).
        self.applied_at: float | None = None
        #: ``clock()`` when a cooler found off was last turned back on.
        self.restarted_at: float | None = None
        #: The CCD was out of tolerance past the settle time (warned once
        #: until it settles again).
        self.warned = False

    async def apply(self, device: Any, why: str, *, timeout: float | None = None) -> bool:
        """Set the cooler through :func:`set_cooling`; True if set. An
        :class:`Unsupported` camera is logged once and left alone after."""
        if self.unsupported is not None:
            return False
        sp = self.config.setpoint
        try:
            await set_cooling(device, sp, timeout=timeout)
        except Unsupported as exc:
            self.unsupported = str(exc)
            log.warning("%s: camera cooling unsupported: %s", self.name, exc)
            self.event("cooling_unsupported", reason=str(exc)[:200], level="warning")
            return False
        self.applied_at = self.clock()
        self.warned = False
        log.info("%s: camera cooler on, setpoint %.1f C (%s)", self.name, sp, why)
        self.event("cooling_set", setpoint=sp, why=why)
        return True

    async def on_connect(self, driver: Any, timeout: float) -> None:
        """The camera owner's ``on_connect`` hook: the camera was (re)connected."""
        await self.apply(driver, "connected", timeout=timeout)

    def _in_tolerance(self, temp: float | None) -> bool:
        return temp is not None and abs(temp - self.config.setpoint) <= self.config.tolerance

    def _too_warm(self, temp: float | None) -> bool:
        """Warmer than the setpoint by more than the tolerance (a CCD colder
        than a warm setpoint is not a fault: module doc)."""
        return temp is not None and temp > self.config.setpoint + self.config.tolerance

    def _fault_text(self, temp: float | None, seconds: float) -> str:
        cfg = self.config
        return (
            f"{self.name} {COOLING_FAULT}: CCD at {temp if temp is None else round(temp, 1)} C,"
            f" not within {cfg.tolerance} C of its setpoint {cfg.setpoint} C"
            f" {seconds:g} s after the cooler was set (cooler on)"
        )

    async def settle(self, grant: Any, why: str) -> float | None:
        """After a recovery rung for a :class:`CoolingFault`: set the cooler
        again (:meth:`apply`: setpoint and ``CoolerOn``; harmless after the
        reconnect's own ``on_connect``) and wait, at most
        ``recovery_settle_timeout`` s, for the CCD to reach its setpoint
        (within ``tolerance``, or colder). Returns the CCD temperature;
        raises :class:`CoolingFault` if it does not get there, and
        :class:`RuntimeError` if the cooler cannot be set or the camera does
        not answer."""
        cfg = self.config
        if not await self.apply(grant, why):
            raise RuntimeError(f"{self.name}: the cooler could not be set ({self.unsupported})")
        bound = cfg.recovery_settle
        deadline = self.clock() + bound
        temp = _float(await grant.get("CCDTemperature"))
        while temp is None or self._too_warm(temp):
            if self.clock() >= deadline:
                text = self._fault_text(temp, bound)
                self.event("cooling_not_recovered", ccd_temp=temp, setpoint=cfg.setpoint, text=text)
                raise CoolingFault(text)
            await asyncio.sleep(min(cfg.poll, max(0.0, deadline - self.clock())))
            temp = _float(await grant.get("CCDTemperature"))
        self.warned = False
        self.event("cooling_recovered", ccd_temp=temp, setpoint=cfg.setpoint)
        return temp

    async def check(self, grant: Any, *, wait: bool) -> CoolingStatus | None:
        """Before an exposure (module doc): turn a cooler found off back on
        (rate-limited, with an alarm); with ``wait`` (a light frame) and the
        cooler on, wait for the CCD to settle, no later than
        ``settle_timeout`` after the cooler was set. None: the camera cannot be cooled.

        Raises :class:`CoolingFault` (``not_settled: "fault"``) when the
        cooler is on, ``settle_timeout`` has passed since it was set, and
        the CCD is still too warm (module doc); light and dark frames alike."""
        if self.unsupported is not None:
            return None
        cfg = self.config
        cooler = bool(await grant.get("CoolerOn"))
        restarted = False
        if not cooler:
            now = self.clock()
            if self.restarted_at is None or now - self.restarted_at >= cfg.restart_interval:
                self.restarted_at = now
                self.alarm(f"{self.name} camera: cooler found off; turning it back on")
                restarted = await self.apply(grant, "cooler found off")
                if self.unsupported is not None:
                    return None
                cooler = restarted
            else:
                self.event(
                    "cooler_off",
                    level="warning",
                    restart_in=round(cfg.restart_interval - (now - self.restarted_at), 1),
                )
        temp = _float(await grant.get("CCDTemperature"))
        t0 = self.clock()
        if wait and cooler and not self._in_tolerance(temp) and self.applied_at is not None:
            deadline = self.applied_at + cfg.settle_timeout
            while not self._in_tolerance(temp) and self.clock() < deadline:
                await asyncio.sleep(min(cfg.poll, max(0.0, deadline - self.clock())))
                temp = _float(await grant.get("CCDTemperature"))
        waited = self.clock() - t0
        settled = self._in_tolerance(temp)
        if waited > 0.5:
            self.event("cooling_waited", seconds=round(waited, 1), ccd_temp=temp, settled=settled)
        if settled:
            self.warned = False
        elif (
            cfg.not_settled == "fault"
            and cooler
            and self.applied_at is not None
            and self.clock() - self.applied_at >= cfg.settle_timeout
            and self._too_warm(temp)
        ):
            text = self._fault_text(temp, cfg.settle_timeout)
            log.error("%s", text)
            self.event(
                "cooling_fault",
                ccd_temp=temp,
                setpoint=cfg.setpoint,
                level="critical",
                text=text,
            )
            raise CoolingFault(text)
        elif wait and not self.warned:
            self.warned = True
            text = (
                f"CCD at {temp if temp is None else round(temp, 1)} C, not within"
                f" {cfg.tolerance} C of its setpoint {cfg.setpoint} C"
                + (
                    f" {cfg.settle_timeout:g} s after the cooler was set"
                    if cooler
                    else " (its cooler is off)"
                )
                + ": exposing anyway"
            )
            log.warning("%s: %s", self.name, text)
            self.event(
                "cooling_not_settled",
                ccd_temp=temp,
                setpoint=cfg.setpoint,
                level="warning",
                text=text,
            )
        return CoolingStatus(temp, cooler, settled, restarted, round(waited, 1))


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
