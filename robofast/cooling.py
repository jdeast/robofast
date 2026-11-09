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
was last set; after that the frames are taken anyway, with a warning event
and ``COOLOK = F`` in their headers. Cooling never stops the night.
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

__all__ = ["CameraCooling", "CoolingConfig", "CoolingStatus", "set_cooling"]

#: ASCOM NotImplemented and PropertyNotImplemented.
NOT_IMPLEMENTED = (0x400, 0x401)


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

    def __post_init__(self) -> None:
        for name in ("setpoint", "tolerance", "settle_timeout", "restart_interval", "poll"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"cooling.{name}: a number (got {v!r})")
        if not -150.0 <= self.setpoint <= 50.0:
            raise ValueError(f"cooling.setpoint {self.setpoint}: outside -150..50 C")
        if self.tolerance <= 0:
            raise ValueError("cooling.tolerance: must be > 0")
        if self.settle_timeout < 0 or self.restart_interval < 0 or self.poll <= 0:
            raise ValueError("cooling: settle_timeout, restart_interval >= 0 and poll > 0")

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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self.alarm = alarm or (lambda text: None)
        self.event = event or (lambda what, **kw: None)
        self.clock = clock
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

    async def check(self, grant: Any, *, wait: bool) -> CoolingStatus | None:
        """Before an exposure (module doc): turn a cooler found off back on
        (rate-limited, with an alarm); with ``wait`` (a light frame) and the
        cooler on, wait for the CCD to settle, no later than
        ``settle_timeout`` after the cooler was set. None: the camera cannot be cooled."""
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
