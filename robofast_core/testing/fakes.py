"""Pure-Python stand-in drivers (no HTTP, no simulator).

They expose the same member names the core uses (``ShutterStatus``,
``AbortExposure``, ...) through the same async ``get``/``set``/``call``
protocol as :class:`~robofast_core.alpaca_client.AlpacaDevice`, and keep time
with ``time.monotonic()``. Each has fault injection:

* ``hang()`` / ``unhang()`` -- every command through the protocol waits until
  released (a device that stops answering);
* ``fail(msg)`` / ``unfail()`` -- every command raises (a device that has
  disappeared or errors).

Tests read ground truth by touching the members directly, bypassing the gate.

``FakeWeather`` backs both ``FakeSafetyMonitor`` and
``FakeObservingConditions``; a test sets it safe/unsafe, or "freezes" it so
``TimeSinceLastUpdate`` grows (a feed that stopped updating).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.imagebytes import ImageArray


class FakeDeviceError(Exception):
    pass


class FakeDevice:
    """The owner's driver protocol (async ``get``/``set``/``call``) over plain
    Python members; every access passes the fault gate first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hung = threading.Event()
        self._failure: str | None = None
        self.Connected = True

    async def _gate(self) -> None:
        while self._hung.is_set():
            await asyncio.sleep(0.01)
        if self._failure is not None:
            raise FakeDeviceError(self._failure)

    async def get(self, member: str, *, timeout: float | None = None) -> Any:
        await self._gate()
        return getattr(self, member)

    async def set(self, member: str, value: Any, *, timeout: float | None = None) -> None:
        await self._gate()
        setattr(self, member, value)

    async def call(self, member: str, *args: Any, timeout: float | None = None) -> Any:
        await self._gate()
        return getattr(self, member)(*args)

    def hang(self) -> None:
        self._hung.set()

    def unhang(self) -> None:
        self._hung.clear()

    def fail(self, msg: str = "device unreachable") -> None:
        self._failure = msg

    def unfail(self) -> None:
        self._failure = None

    @staticmethod
    def _now() -> float:
        return time.monotonic()


class FakeDome(FakeDevice):
    """A shutter that takes ``travel`` seconds to open or close.

    With ``heartbeat_timeout`` it also models the enclosure controller's
    hardware watchdog (the contract is in
    :mod:`robofast_core.heartbeat_watchdog`): ``Action("Heartbeat", "")``
    restarts a countdown that began at power-on; when it runs out the
    shutter starts closing by itself, and ``OpenShutter`` is refused until
    the pings resume. (Evaluated lazily, but stamped at the moment it ran
    out, so ground-truth timings are exact.)
    """

    def __init__(self, travel: float = 0.2, heartbeat_timeout: float | None = None) -> None:
        super().__init__()
        self._travel = travel
        self._target = 1  # closed
        self._moved_at = -1e9
        self._heartbeat_timeout = heartbeat_timeout
        self._pinged_at = self._now()
        self.watchdog_trips = 0
        self.SupportedActions = [] if heartbeat_timeout is None else ["Heartbeat"]

    def _tripped_at(self) -> float | None:
        """When the watchdog ran out, if it has (call under the lock)."""
        if self._heartbeat_timeout is None:
            return None
        deadline = self._pinged_at + self._heartbeat_timeout
        return deadline if self._now() > deadline else None

    def _watchdog(self) -> None:
        tripped = self._tripped_at()
        if tripped is not None and self._target != 1:
            self._target = 1
            self._moved_at = max(tripped, self._moved_at)
            self.watchdog_trips += 1

    def state(self) -> int:
        """The shutter state, as ground truth (no fault gate)."""
        with self._lock:
            self._watchdog()
            if self._now() - self._moved_at >= self._travel:
                return self._target
            return 2 if self._target == 0 else 3

    #: Test hook: what ShutterStatus reports instead of the real state, an
    #: int (e.g. 4 = shutterError) or a str (raise that as a device error).
    status_override: int | str | None = None

    @property
    def ShutterStatus(self) -> int:
        override = self.status_override
        if isinstance(override, str):
            raise FakeDeviceError(override)
        return self.state() if override is None else override

    def _move(self, target: int) -> None:
        with self._lock:
            if self._target != target:
                self._target = target
                self._moved_at = self._now()

    def Action(self, action: str, parameters: str = "") -> str:
        if self._heartbeat_timeout is None or action.lower() != "heartbeat":
            raise FakeDeviceError(f"action {action!r} not supported")
        with self._lock:
            self._watchdog()  # a late ping does not undo a trip that happened
            self._pinged_at = self._now()
        return "ok"

    def OpenShutter(self) -> None:
        with self._lock:
            self._watchdog()
            if self._tripped_at() is not None:
                raise FakeDeviceError("heartbeat lost: the watchdog holds the shutter closed")
        self.opens += 1
        self._move(0)

    #: How many times OpenShutter was accepted.
    opens = 0

    def CloseShutter(self) -> None:
        self._move(1)


class FakeTelescope(FakeDevice):
    def __init__(self, slew_time: float = 0.3, park_time: float = 0.3) -> None:
        super().__init__()
        self._slew_time = slew_time
        self._park_time = park_time
        self._parked = True
        self._park_until = 0.0
        self._slew_until = 0.0
        self._tracking = False
        self.SiteLatitude = 31.68
        self.ra = 0.0
        self.dec = 0.0
        #: ASCOM EquatorialSystem: 2 = J2000.
        self.EquatorialSystem = 2
        self.Altitude = 60.0
        self.Azimuth = 120.0

    @property
    def RightAscension(self) -> float:
        return self.ra

    @property
    def Declination(self) -> float:
        return self.dec

    @property
    def AtPark(self) -> bool:
        return self._parked and self._now() >= self._park_until

    @property
    def Slewing(self) -> bool:
        return self._now() < max(self._slew_until, self._park_until)

    @property
    def SiderealTime(self) -> float:
        return (self._now() / 3600.0) % 24.0

    @property
    def Tracking(self) -> bool:
        return self._tracking

    @Tracking.setter
    def Tracking(self, value: bool) -> None:
        if self._parked:
            raise FakeDeviceError("parked")
        self._tracking = bool(value)

    def Unpark(self) -> None:
        self._parked = False

    def Park(self) -> None:
        self._tracking = False
        self._slew_until = 0.0
        self._parked = True
        self._park_until = self._now() + self._park_time

    def AbortSlew(self) -> None:
        self._slew_until = 0.0

    def SlewToCoordinatesAsync(self, ra: float, dec: float) -> None:
        if self._parked:
            raise FakeDeviceError("parked")
        self.ra, self.dec = ra, dec
        self._slew_until = self._now() + self._slew_time

    #: The Alpaca Action that selects an M3 port (None: no port selection);
    #: its parameter is the port name, one of ``ports``.
    port_action: str | None = None
    ports: tuple[str, ...] = ("1", "2")
    port: str | None = None

    def Action(self, action: str, parameters: str = "") -> str:
        if self.port_action is None or action != self.port_action:
            raise FakeDeviceError(f"ActionNotImplemented: {action}")
        if parameters not in self.ports:
            raise FakeDeviceError(f"no port {parameters!r}")
        self.port = parameters
        return ""


class FakeFocuser(FakeDevice):
    def __init__(self, position: int = 12345, temperature: float = 8.5) -> None:
        super().__init__()
        self.Position = position
        self.Temperature = temperature


class FakeFilterWheel(FakeDevice):
    """A filter wheel that takes ``move_time`` seconds per move; ``Position``
    reads -1 while it moves (as the ASCOM interface says)."""

    def __init__(self, names: tuple[str, ...] = ("B", "V", "R", "I"), move_time: float = 0.1):
        super().__init__()
        self.Names = list(names)
        self._move_time = move_time
        self._position = 0
        self._until = 0.0
        self.moves = 0

    @property
    def Position(self) -> int:
        return -1 if self._now() < self._until else self._position

    @Position.setter
    def Position(self, value: int) -> None:
        if not 0 <= value < len(self.Names):
            raise FakeDeviceError(f"no position {value}")
        self._position = int(value)
        self._until = self._now() + self._move_time
        self.moves += 1


class FakeCamera(FakeDevice):
    """A camera whose image is ``NumX x NumY`` UInt16 pixels (value: the
    exposure number), with the state members the frame pipeline reads.
    ``image_error`` makes ``ImageArray`` raise; ``download_time`` delays it."""

    def __init__(self, numx: int = 32, numy: int = 24) -> None:
        super().__init__()
        self._state = 0
        self._ends = 0.0
        self._ready = False
        self.exposures_started = 0
        self.exposures_aborted = 0
        self.Name = "FakeCamera"
        self.SensorName = "FakeSensor"
        self.CameraXSize, self.CameraYSize = numx, numy
        self.NumX, self.NumY = numx, numy
        self.StartX = self.StartY = 0
        self.BinX = self.BinY = 1
        self.PixelSizeX = self.PixelSizeY = 9.0
        self.ElectronsPerADU = 1.5
        self.MaxADU = 65535
        self.ReadoutModes = ["Normal", "Fast"]
        self.ReadoutMode = 0
        self.CCDTemperature = -20.0
        self.SetCCDTemperature = -20.0
        self.CoolerOn = True
        self.CoolerPower = 40.0
        self.Gain = 1
        self.Offset = 10
        self.LastExposureStartTime: str | None = None
        self.LastExposureDuration: float | None = None
        self.image_error: str | None = None
        self.download_time = 0.0
        self.downloads = 0

    @property
    def ImageArray(self) -> ImageArray:
        if self.image_error is not None:
            raise FakeDeviceError(self.image_error)
        if not self._ready:
            raise FakeDeviceError("no image available")
        self.downloads += 1
        data = np.full((self.NumX, self.NumY), self.exposures_started, dtype="<u2")
        data[self.NumX - 1, 0] = 1000  # x = last, y = 0: FITS row 0, last column
        return ImageArray(data, 8, "imagebytes", data.nbytes + 44)

    async def get(self, member: str, *, timeout: float | None = None) -> Any:
        if member == "ImageArray" and self.download_time:
            await asyncio.sleep(self.download_time)
        return await super().get(member, timeout=timeout)

    def _update(self) -> None:
        if self._state == 2 and self._now() >= self._ends:
            self._state = 0
            self._ready = True

    @property
    def CameraState(self) -> int:
        self._update()
        return self._state

    @property
    def ImageReady(self) -> bool:
        self._update()
        return self._ready

    def StartExposure(self, duration: float, light: bool) -> None:
        self._update()
        if self._state != 0:
            raise FakeDeviceError("camera busy")
        self._state = 2
        self._ready = False
        self._ends = self._now() + duration
        self.exposures_started += 1
        start = datetime.now(UTC)
        self.LastExposureStartTime = start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        self.LastExposureDuration = float(duration)

    def AbortExposure(self) -> None:
        self._update()
        if self._state != 0:
            self.exposures_aborted += 1
        self._state = 0
        self._ready = False

    def StopExposure(self) -> None:
        self._update()
        if self._state != 0:
            self._state = 0
            self._ready = True


class FakeWeather:
    """Shared state behind the fake SafetyMonitor/ObservingConditions."""

    def __init__(self, safe: bool = True) -> None:
        self._lock = threading.Lock()
        self.safe = safe
        self.frozen_at: float | None = None
        self.last_update = time.monotonic()

    def set_safe(self, safe: bool) -> None:
        with self._lock:
            self.safe = safe
            if self.frozen_at is None:
                self.last_update = time.monotonic()

    def freeze(self) -> None:
        """The feed stops updating; its last value stays (and goes stale)."""
        with self._lock:
            self.frozen_at = time.monotonic()

    def thaw(self) -> None:
        with self._lock:
            self.frozen_at = None
            self.last_update = time.monotonic()

    def age(self) -> float:
        with self._lock:
            if self.frozen_at is None:
                self.last_update = time.monotonic()
            return time.monotonic() - self.last_update


class FakeSafetyMonitor(FakeDevice):
    def __init__(self, weather: FakeWeather) -> None:
        super().__init__()
        self._weather = weather

    @property
    def IsSafe(self) -> bool:
        return self._weather.safe


class FakeObservingConditions(FakeDevice):
    def __init__(self, weather: FakeWeather) -> None:
        super().__init__()
        self._weather = weather
        self.Humidity = 30.0
        self.Temperature = 11.5

    def TimeSinceLastUpdate(self, sensor: str) -> float:
        return self._weather.age()
