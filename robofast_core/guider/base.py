"""The guider's parts and the types they exchange (docs/guider.md).

Each part is a typed :class:`~typing.Protocol`, so a backend is anything with
the right methods:

* :class:`Camera` -- exposes a region of interest and returns a
  :class:`Frame` with its pixels and the time the exposure ended;
* :class:`Centroider` -- measures a star in a frame (:class:`Measurement`);
* :class:`TipTilt` -- moves the image, in its own units (steps, the PI
  stage's units, or arcseconds of mount offset), relative or absolute;
* the controller and offloader are concrete classes
  (:mod:`~robofast_core.guider.controller`,
  :mod:`~robofast_core.guider.offload`).

Times are from a :class:`Clock`: :class:`RealClock` (``time.monotonic``)
in service, :class:`VirtualClock` in simulations, where time advances only
when something waits, so a closed-loop run of minutes takes seconds and its
results do not depend on the machine.
"""

from __future__ import annotations

import asyncio
import heapq
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np


class GuiderError(Exception):
    """A guider part failed (camera, centroider, device)."""


class HardwareNotAllowed(GuiderError):
    """A real (non-simulated) device backend was configured without both
    ``allow_hardware: true`` in the config and ``--hardware``."""


class TipTiltError(GuiderError):
    """The tip/tilt device (or the mount offset) failed or timed out."""


def check_hardware(what: str, *, allowed: bool) -> None:
    """Refuse a real device backend unless hardware use is allowed (the
    config's ``allow_hardware`` AND the command line's ``--hardware``)."""
    if not allowed:
        raise HardwareNotAllowed(
            f"{what} is real hardware: refusing without allow_hardware in the config AND"
            " --hardware on the command line"
        )


def check_address(address: str, what: str, *, allowed: bool) -> None:
    """A ``host:port`` network device: loopback is always allowed (a
    simulator); anything else needs hardware use allowed."""
    host = address.rsplit(":", 1)[0].strip("[]")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addrs = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
    except (socket.gaierror, ValueError) as exc:
        raise HardwareNotAllowed(f"{what}: cannot resolve {host!r}: {exc}") from exc
    if not all(a.is_loopback for a in addrs):
        check_hardware(f"{what} at {address}", allowed=allowed)


# -- clocks -------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """``time.monotonic`` and ``asyncio.sleep``."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    """Simulated time for a single event loop (discrete-event): a task's
    :meth:`sleep` waits until every other task is blocked, then time jumps
    to the earliest pending wake-up. So a run of minutes takes only its CPU
    time, several tasks sleeping at once see one consistent time, and
    compute costs no simulated time (a simulator adds latency explicitly).
    Only for simulations: a task waiting on real I/O does not hold time
    back."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self._heap: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0
        self._scheduled = False

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._seq += 1
        heapq.heappush(self._heap, (self.t + max(0.0, seconds), self._seq, fut))
        self._schedule(loop)
        try:
            await fut
        finally:
            if not fut.done():
                fut.cancel()

    def _schedule(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._scheduled:
            self._scheduled = True
            loop.call_soon(self._advance, loop, 0)

    def _advance(self, loop: asyncio.AbstractEventLoop, spins: int) -> None:
        self._scheduled = False
        # Let everything that can still run, run first.
        ready = getattr(loop, "_ready", None)
        busy = len(ready) > 0 if ready is not None else spins < 20
        if busy and spins < 100000:
            self._scheduled = True
            loop.call_soon(self._advance, loop, spins + 1)
            return
        while self._heap:
            t, _, fut = heapq.heappop(self._heap)
            if fut.cancelled():
                continue
            self.t = max(self.t, t)
            fut.set_result(None)
            break
        if self._heap:
            self._schedule(loop)

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


# -- frames and measurements --------------------------------------------------------


@dataclass(frozen=True)
class ROI:
    """A window on the detector, unbinned pixels; ``x``, ``y`` the corner."""

    x: int
    y: int
    width: int
    height: int

    @classmethod
    def centred(cls, cx: float, cy: float, size: int, sensor: tuple[int, int]) -> ROI:
        """A ``size`` x ``size`` window centred on (cx, cy), kept on the
        sensor (``sensor`` is (width, height))."""
        w, h = sensor
        size = max(4, min(size, w, h))
        x = round(cx - size / 2)
        y = round(cy - size / 2)
        return cls(min(max(0, x), w - size), min(max(0, y), h - size), size, size)

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (
            self.x + margin <= x < self.x + self.width - margin
            and self.y + margin <= y < self.y + self.height - margin
        )

    def as_json(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class Frame:
    """One exposure: ``data[row, column]`` is detector pixel
    ``(roi.x + column, roi.y + row)``."""

    data: np.ndarray
    roi: ROI
    exposure: float
    #: Clock time of the exposure's start and end (the camera's best
    #: estimate; the end is where the loop's latency is measured from).
    t_start: float
    t_end: float
    index: int = 0
    utc_end: datetime | None = None
    #: Clock time the pixels reached the guider (after readout/transfer);
    #: from here to the command is the guider's own processing time.
    t_ready: float | None = None
    #: Electrons per ADU, if known (for flux in electrons).
    gain: float | None = None


@dataclass
class Measurement:
    """A centroider's answer. ``x``, ``y`` in detector pixels."""

    x: float
    y: float
    #: Background-subtracted counts (ADU) in the aperture.
    flux: float
    #: Full width at half maximum from the second moments (pixels); NaN if
    #: unknown.
    fwhm: float = float("nan")
    #: Peak (background-subtracted) and background, ADU.
    peak: float = 0.0
    background: float = 0.0
    #: Signal-to-noise estimate of the flux.
    snr: float = 0.0
    valid: bool = True
    reason: str = ""

    @classmethod
    def invalid(cls, reason: str) -> Measurement:
        nan = float("nan")
        return cls(nan, nan, 0.0, nan, valid=False, reason=reason)


@dataclass
class MoveResult:
    """What a tip/tilt move did, in device units."""

    position: tuple[float, float]
    #: The device stopped at (or the request was clipped to) a range limit.
    at_limit: bool = False
    #: The part of the request that was not applied (clipped), device units.
    clipped: tuple[float, float] = (0.0, 0.0)
    #: Clock time the command went on the wire (after any queueing).
    t_sent: float = 0.0


# -- protocols ----------------------------------------------------------------------


@runtime_checkable
class Camera(Protocol):
    """A guide camera, opened by the guider on the machine it plugs into."""

    name: str

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def sensor(self) -> tuple[int, int]:
        """(width, height) in unbinned pixels."""
        ...

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        """One exposure of ``roi`` (None: full frame)."""
        ...


@runtime_checkable
class Centroider(Protocol):
    name: str

    def measure(self, frame: Frame, guess: tuple[float, float] | None) -> Measurement:
        """The star nearest ``guess`` (detector pixels; None: the brightest
        in the frame)."""
        ...


@runtime_checkable
class TipTilt(Protocol):
    """Something that moves the image: a tip/tilt mirror or the mount.
    Positions are (a, b) in :attr:`units`; ``limits`` per axis, None if
    unbounded (the mount)."""

    name: str
    units: str

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def position(self) -> tuple[float, float]: ...

    @property
    def centre(self) -> tuple[float, float]: ...

    @property
    def limits(self) -> tuple[tuple[float, float], tuple[float, float]] | None: ...

    async def move_by(self, da: float, db: float) -> MoveResult: ...

    async def home(self) -> MoveResult:
        """Back to :attr:`centre`."""
        ...


def range_fraction(tt: TipTilt) -> float:
    """How far from its centre ``tt`` is, as a fraction of the distance to
    the nearest limit on the worse axis (0 at the centre, 1 at a limit, 0
    for an unbounded device)."""
    lim = tt.limits
    if lim is None:
        return 0.0
    out = 0.0
    for p, c, (lo, hi) in zip(tt.position, tt.centre, lim, strict=True):
        half = (hi - c) if p >= c else (c - lo)
        if half > 0:
            out = max(out, abs(p - c) / half)
    return out


@dataclass
class Alarm:
    kind: str
    message: str
    t: float
    utc: str = ""
    count: int = 1
    extra: dict = field(default_factory=dict)
