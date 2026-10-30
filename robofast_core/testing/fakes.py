"""Pure-Python stand-ins for alpyca devices (no HTTP, no simulator).

They expose the same member names the core uses (``ShutterStatus``,
``AbortExposure``, ...), keep time with ``time.monotonic()``, and are driven
from the owner's worker threads exactly like alpyca objects. Each has fault
injection:

* ``hang()`` / ``unhang()`` -- every member access blocks until released
  (a device that stops answering);
* ``fail(msg)`` / ``unfail()`` -- every member access raises (a device that
  has disappeared or errors).

``FakeWeather`` backs both ``FakeSafetyMonitor`` and
``FakeObservingConditions``; a test sets it safe/unsafe, or "freezes" it so
``TimeSinceLastUpdate`` grows (a feed that stopped updating).
"""

from __future__ import annotations

import threading
import time
from typing import Any


class FakeDeviceError(Exception):
    pass


class FakeDevice:
    _plain_attrs = frozenset(
        {"_gate", "_hung", "_failure", "_lock", "_now", "hang", "unhang", "fail", "unfail"}
    )

    def __init__(self) -> None:
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_hung", threading.Event())
        object.__setattr__(self, "_failure", None)
        self.Connected = True

    # Every public member access (get, set or call) passes the gate first.
    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_") and name not in FakeDevice._plain_attrs:
            object.__getattribute__(self, "_gate")()
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_"):
            object.__getattribute__(self, "_gate")()
        object.__setattr__(self, name, value)

    def _gate(self) -> None:
        hung = object.__getattribute__(self, "_hung")
        while hung.is_set():
            time.sleep(0.01)
        failure = object.__getattribute__(self, "_failure")
        if failure is not None:
            raise FakeDeviceError(failure)

    def hang(self) -> None:
        self._hung.set()

    def unhang(self) -> None:
        self._hung.clear()

    def fail(self, msg: str = "device unreachable") -> None:
        object.__setattr__(self, "_failure", msg)

    def unfail(self) -> None:
        object.__setattr__(self, "_failure", None)

    @staticmethod
    def _now() -> float:
        return time.monotonic()


class FakeDome(FakeDevice):
    """A shutter that takes ``travel`` seconds to open or close."""

    def __init__(self, travel: float = 0.2) -> None:
        super().__init__()
        self._travel = travel
        self._target = 1  # closed
        self._moved_at = -1e9

    @property
    def ShutterStatus(self) -> int:
        with self._lock:
            if self._now() - self._moved_at >= self._travel:
                return self._target
            return 2 if self._target == 0 else 3

    def _move(self, target: int) -> None:
        with self._lock:
            if self._target != target:
                self._target = target
                self._moved_at = self._now()

    def OpenShutter(self) -> None:
        self._move(0)

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


class FakeCamera(FakeDevice):
    def __init__(self) -> None:
        super().__init__()
        self._state = 0
        self._ends = 0.0
        self._ready = False
        self.exposures_started = 0
        self.exposures_aborted = 0

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

    def TimeSinceLastUpdate(self, sensor: str) -> float:
        return self._weather.age()
