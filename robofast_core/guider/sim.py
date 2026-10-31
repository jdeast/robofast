"""The guider simulator: a star, the atmosphere, a tip/tilt and a mount.

:class:`SimWorld` holds the truth: where the star is on the guide camera at
any time, which is

    nominal position + atmospheric jitter (seeing, wind shake, drift)
    + tip/tilt matrix @ (tip/tilt position - its centre)
    + mount matrix @ (mount offset total)

each device following its commands with its own response (a first-order lag
``tau`` after a dead time ``delay``). :class:`Jitter` is the atmosphere's
image motion as a time series; :class:`Fiber` the fiber entrance (a hole in
the image, or a bright disk when backlit). :class:`SimCamera` exposes the
world: it waits the exposure on the world's clock, renders the window with
the star at its *mean* position over the exposure (:func:`render_window`:
the shared renderer :mod:`robofast_core.testing.starfield`, then the fiber,
then photon and read noise), and stamps the frame's end time.
:class:`SimTipTilt` is a tip/tilt with limits and its own response time;
:class:`SimMount` is an Alpaca-like telescope driver answering
``Action("Offset")`` and ``PulseGuide``.

On a :class:`~robofast_core.guider.base.VirtualClock` a closed-loop run of
minutes takes seconds of CPU and is reproducible; on a
:class:`~robofast_core.guider.base.RealClock` the loop's latency (exposure
end to command sent) is the real compute time.

:meth:`SimWorld.residual_rms` measures the truth the guider cannot see: the
rms distance of the star from the target over a time span, sampled every
millisecond.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.guider.base import ROI, Clock, Frame, MoveResult, RealClock
from robofast_core.guider.calibration import sky_matrix
from robofast_core.guider.tiptilt import Limits, _Tally
from robofast_core.testing import starfield as sf


@dataclass(frozen=True)
class Fiber:
    """A fiber entrance on the acquisition camera, seen in reflection: a
    hole (light falling on it goes into the fiber, so it is missing from the
    image) or, with the backlight on, a bright disk."""

    x: float
    y: float
    #: Radius in pixels.
    radius: float = 3.0
    #: Fraction of light lost in the hole (1: the hole is black).
    depth: float = 1.0
    #: Electrons per pixel when backlit.
    backlight: float = 2000.0


@dataclass
class Jitter:
    """Image motion in pixels as a function of time (module doc).

    The seeing part has a one-sided power spectrum ``P(f) ~ 1 / (1 +
    (f/knee)^slope)`` (flat below the knee; ``slope`` 11/3 is the textbook
    Kolmogorov tip/tilt fall-off above it), scaled to ``rms`` pixels per
    axis. ``shake`` adds wind-shake lines: ``(frequency Hz, amplitude
    pixels)``, each with a random phase and direction. ``drift`` is pixels
    per second (x, y). Synthesised on a ``dt`` grid over ``duration``
    seconds, then repeated (the series is periodic by construction)."""

    rms: float = 0.5
    knee: float = 1.0
    slope: float = 11.0 / 3.0
    shake: Sequence[tuple[float, float]] = ()
    drift: tuple[float, float] = (0.0, 0.0)
    duration: float = 64.0
    dt: float = 1e-3
    seed: int | None = 1
    _series: np.ndarray = field(init=False, repr=False)
    _cum: np.ndarray = field(init=False, repr=False)
    _shake: list[tuple[float, float, float, float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        rng = np.random.default_rng(self.seed)
        n = round(self.duration / self.dt)
        n += n % 2
        f = np.fft.rfftfreq(n, self.dt)
        psd = 1.0 / (1.0 + (np.maximum(f, 1e-12) / self.knee) ** self.slope)
        psd[0] = 0.0
        series = np.empty((n, 2))
        for axis in range(2):
            phases = rng.uniform(0, 2 * math.pi, f.size)
            spec = np.sqrt(psd) * np.exp(1j * phases)
            s = np.fft.irfft(spec, n)
            sd = s.std()
            series[:, axis] = s * (self.rms / sd if sd > 0 else 0.0)
        self._series = series
        # Running integral of the linear interpolation :meth:`at` uses
        # (trapezoids; periodic), with a leading zero, for exposure means.
        nxt = np.roll(series, -1, axis=0)
        steps = 0.5 * (series + nxt) * self.dt
        self._cum = np.vstack([np.zeros((1, 2)), np.cumsum(steps, axis=0)])
        self._shake = []
        for freq, amp in self.shake:
            angle = rng.uniform(0, math.pi)
            phase = rng.uniform(0, 2 * math.pi)
            self._shake.append((freq, amp, angle, phase))

    @property
    def period(self) -> float:
        return self._series.shape[0] * self.dt

    def _integral(self, t: float) -> np.ndarray:
        """Integral of the seeing part from 0 to ``t`` (periodic)."""
        n = self._series.shape[0]
        whole, rest = divmod(t, self.period)
        k = rest / self.dt
        i = int(k)
        frac = k - i
        inner = self._cum[i] + (self._cum[min(i + 1, n)] - self._cum[i]) * frac
        return whole * self._cum[n] + inner

    def at(self, t: float) -> np.ndarray:
        """(dx, dy) at time ``t``."""
        n = self._series.shape[0]
        k = (t % self.period) / self.dt
        i = int(k) % n
        frac = k - int(k)
        v = self._series[i] * (1 - frac) + self._series[(i + 1) % n] * frac
        return v + self._shake_at(t) + np.asarray(self.drift) * t

    def _shake_at(self, t: float) -> np.ndarray:
        out = np.zeros(2)
        for freq, amp, angle, phase in self._shake:
            a = amp * math.sin(2 * math.pi * freq * t + phase)
            out += a * np.array([math.cos(angle), math.sin(angle)])
        return out

    def mean(self, t0: float, t1: float) -> np.ndarray:
        """The mean of :meth:`at` over ``[t0, t1]`` (what a centroid of an
        exposure measures)."""
        if t1 <= t0:
            return self.at(t0)
        span = t1 - t0
        seeing = (self._integral(t1) - self._integral(t0)) / span
        shake = np.zeros(2)
        for freq, amp, angle, phase in self._shake:
            w = 2 * math.pi * freq
            m = amp * (math.cos(w * t0 + phase) - math.cos(w * t1 + phase)) / (w * span)
            shake += m * np.array([math.cos(angle), math.sin(angle)])
        drift = np.asarray(self.drift) * 0.5 * (t0 + t1)
        return seeing + shake + drift


def render_window(
    roi: ROI,
    stars: Sequence[tuple[float, float, float]],
    *,
    fwhm: float = 4.0,
    exposure: float = 1.0,
    offset: tuple[float, float] = (0.0, 0.0),
    sky: float = 200.0,
    fiber: Fiber | None = None,
    backlit: bool = False,
    gain: float = 1.0,
    read_noise: float = 3.0,
    bias: float = 100.0,
    sensor: tuple[int, int] = (640, 480),
    profile: str = "gaussian",
    oversample: int = 2,
    noise: bool = True,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """A guide window (uint16 ADU): ``stars`` as (x, y, electrons per
    second), shifted by ``offset`` pixels, rendered by the shared renderer
    (:func:`robofast_core.testing.starfield.render`; seeing ``fwhm`` in
    pixels), then the fiber's hole (or the backlit fiber), then photon and
    read noise."""
    rng = rng or np.random.default_rng()
    det = sf.Detector(
        shape=(sensor[1], sensor[0]), gain=gain, read_noise=read_noise, bias=bias, sky=sky
    )
    frame = sf.render(
        [sf.Star(x, y, f) for x, y, f in stars],
        sf.SimOptics(pixel_scale=1.0),
        det,
        seeing=fwhm,
        exposure=exposure,
        roi=(roi.x, roi.y, roi.width, roi.height),
        offset=offset,
        profile=profile,
        noise=False,
        oversample=oversample,
    )
    e = (np.asarray(frame.image, dtype=float) - bias) * gain  # electrons, with sky
    if fiber is not None:
        yy, xx = np.mgrid[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
        inside = (xx - fiber.x) ** 2 + (yy - fiber.y) ** 2 <= fiber.radius**2
        if backlit:
            e[inside] += fiber.backlight * exposure
        else:
            sky_e = sky * exposure
            e[inside] = sky_e + (1.0 - fiber.depth) * (e[inside] - sky_e)
    if noise:
        e = rng.poisson(np.clip(e, 0.0, None)).astype(float)
        e += rng.normal(0.0, read_noise, e.shape)
    adu = np.rint(e / gain + bias)
    return np.clip(adu, 0, 65535).astype(np.uint16)


class _Response:
    """A two-axis first-order lag after a dead time, with its full command
    history (so the position at any past time can be computed)."""

    def __init__(self, start: tuple[float, float], tau: float, delay: float) -> None:
        self.tau = tau
        self.delay = delay
        # (time the command takes effect, position then, new target)
        self.history: list[tuple[float, np.ndarray, np.ndarray]] = [
            (-math.inf, np.asarray(start, float), np.asarray(start, float))
        ]
        self._times: list[float] = [-math.inf]

    def command(self, t: float, target: tuple[float, float]) -> None:
        te = t + self.delay
        # Commands take effect in order (a later one cannot overtake).
        te = max(te, self.history[-1][0])
        self.history.append((te, self.at(te), np.asarray(target, float)))
        self._times.append(te)

    def at(self, t: float) -> np.ndarray:
        k = bisect.bisect_right(self._times, t) - 1
        te, p0, target = self.history[max(k, 0)]
        if not math.isfinite(te) or self.tau <= 0:
            return target.copy()
        f = math.exp(-(t - te) / self.tau)
        return target + (p0 - target) * f

    def mean(self, t0: float, t1: float, n: int = 8) -> np.ndarray:
        if t1 <= t0:
            return self.at(t0)
        ts = t0 + (np.arange(n) + 0.5) * (t1 - t0) / n
        return np.mean([self.at(float(t)) for t in ts], axis=0)


class SimTipTilt(_Tally):
    """A simulated tip/tilt mirror: limits, a response time ``tau`` after a
    dead time ``delay`` (seconds), and a ``latency`` the command itself takes
    (a serial/USB round trip, waited on the clock)."""

    def __init__(
        self,
        *,
        limits: Limits | None = ((-100.0, 100.0), (-100.0, 100.0)),
        tau: float = 2e-3,
        delay: float = 0.0,
        latency: float = 0.0,
        clock: Clock | None = None,
        units: str = "units",
        name: str = "simtt",
    ) -> None:
        centre = (0.0, 0.0)
        if limits is not None:
            centre = ((limits[0][0] + limits[0][1]) / 2, (limits[1][0] + limits[1][1]) / 2)
        super().__init__(limits, centre)
        self.clock = clock or RealClock()
        self.units = units
        self.name = name
        self.latency = latency
        self.plant = _Response(centre, tau, delay)
        self.moves = 0
        self.fail: str | None = None

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def _go(self, a: float, b: float) -> MoveResult:
        if self.fail:
            from robofast_core.guider.base import TipTiltError

            raise TipTiltError(self.fail)
        ca, cb, clipped = self._clip(a, b)
        t_sent = self.clock.now()
        if self.latency:
            await self.clock.sleep(self.latency)
        self.plant.command(t_sent, (ca, cb))
        self._pos = (ca, cb)
        self.moves += 1
        return MoveResult(self._pos, at_limit=clipped, clipped=(a - ca, b - cb), t_sent=t_sent)

    async def move_by(self, da: float, db: float) -> MoveResult:
        return await self._go(self._pos[0] + da, self._pos[1] + db)

    async def home(self) -> MoveResult:
        return await self._go(*self._centre)

    def position_at(self, t: float) -> np.ndarray:
        return self.plant.at(t) - np.asarray(self._centre)

    def mean_at(self, t0: float, t1: float) -> np.ndarray:
        return self.plant.mean(t0, t1) - np.asarray(self._centre)


class PlantView:
    """Wraps a device simulator's ``at(t) -> (a, b)`` (relative to its
    centre) as a plant for :class:`SimWorld` (e.g. the SX AO and PI
    protocol simulators)."""

    def __init__(self, at: Callable[[float], Any]) -> None:
        self._at = at

    def position_at(self, t: float) -> np.ndarray:
        return np.asarray(self._at(t), dtype=float)

    def mean_at(self, t0: float, t1: float, n: int = 8) -> np.ndarray:
        if t1 <= t0:
            return self.position_at(t0)
        ts = t0 + (np.arange(n) + 0.5) * (t1 - t0) / n
        return np.mean([self.position_at(float(t)) for t in ts], axis=0)


class SimMount:
    """An Alpaca-telescope stand-in for :class:`~robofast_core.guider.tiptilt.MountOffset`:
    ``Action("Offset", json)`` and ``PulseGuide(direction, ms)`` (at
    ``guide_rate`` arcsec/s) move its offset total (arcsec east, north) with
    response ``tau`` after ``delay``. ``refuse`` makes every call fail (a
    parked mount)."""

    def __init__(
        self,
        *,
        tau: float = 0.5,
        delay: float = 0.1,
        guide_rate: float = 7.5,
        clock: Clock | None = None,
    ) -> None:
        self.clock = clock or RealClock()
        self.plant = _Response((0.0, 0.0), tau, delay)
        self.total = np.zeros(2)
        self.guide_rate = guide_rate
        self.calls: list[tuple[str, tuple]] = []
        self.refuse: str | None = None

    async def call(self, member: str, *args: Any, timeout: float | None = None) -> Any:
        self.calls.append((member, args))
        if self.refuse:
            raise RuntimeError(self.refuse)
        if member == "Action" and args[0] == "Offset":
            p = json.loads(args[1])
            d = np.array([float(p.get("east", 0)), float(p.get("north", 0))])
        elif member == "PulseGuide":
            direction, ms = int(args[0]), int(args[1])
            arcsec = self.guide_rate * ms / 1000.0
            d = {0: (0, arcsec), 1: (0, -arcsec), 2: (arcsec, 0), 3: (-arcsec, 0)}[direction]
            d = np.asarray(d, float)
        else:
            raise RuntimeError(f"SimMount: no {member}")
        self.total = self.total + d
        self.plant.command(self.clock.now(), (float(self.total[0]), float(self.total[1])))
        return json.dumps({"east": float(d[0]), "north": float(d[1])})

    def mean_at(self, t0: float, t1: float) -> np.ndarray:
        return self.plant.mean(t0, t1)

    def position_at(self, t: float) -> np.ndarray:
        return self.plant.at(t)


@dataclass
class SimWorld:
    """The truth (module doc). Positions in detector pixels; ``flux`` in
    electrons per second; ``sky`` electrons per pixel per second."""

    clock: Clock = field(default_factory=RealClock)
    sensor: tuple[int, int] = (640, 480)
    star: tuple[float, float] = (320.0, 240.0)
    flux: float = 2e6
    others: list[tuple[float, float, float]] = field(default_factory=list)
    #: Seeing FWHM, pixels (the shared renderer's profile).
    fwhm: float = 4.0
    profile: str = "gaussian"
    sky: float = 200.0
    gain: float = 1.0
    read_noise: float = 3.0
    bias: float = 100.0
    jitter: Jitter | None = None
    fiber: Fiber | None = None
    backlit: bool = False
    #: Multiplies the star's flux (clouds; 0: the star is gone).
    transparency: float = 1.0
    tiptilt: SimTipTilt | PlantView | None = None
    #: Pixels per tip/tilt unit.
    tt_matrix: np.ndarray = field(default_factory=lambda: np.array([[0.1, 0.0], [0.0, 0.1]]))
    mount: SimMount | None = None
    #: Plate scale and PA: the mount's offsets in pixels (the star moves
    #: opposite to the pointing).
    plate_scale: float = 0.5
    pa: float = 30.0
    parity: int = 1
    seed: int = 0
    noise: bool = True
    rng: np.random.Generator = field(init=False, repr=False)
    t_origin: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        #: The jitter's time zero (its drift starts here).
        self.t_origin = self.clock.now()

    @property
    def mount_matrix(self) -> np.ndarray:
        """Pixels per arcsec of mount offset (east, north)."""
        return -np.linalg.inv(sky_matrix(self.plate_scale, self.pa, self.parity))

    def motion(self, t0: float, t1: float) -> np.ndarray:
        """Mean image motion (pixels) over [t0, t1]."""
        d = np.zeros(2)
        if self.jitter is not None:
            d += self.jitter.mean(t0 - self.t_origin, t1 - self.t_origin)
        if self.tiptilt is not None:
            d += self.tt_matrix @ self.tiptilt.mean_at(t0, t1)
        if self.mount is not None:
            d += self.mount_matrix @ self.mount.mean_at(t0, t1)
        return d

    def motion_at(self, t: float) -> np.ndarray:
        d = np.zeros(2)
        if self.jitter is not None:
            d += self.jitter.at(t - self.t_origin)
        if self.tiptilt is not None:
            d += self.tt_matrix @ self.tiptilt.position_at(t)
        if self.mount is not None:
            d += self.mount_matrix @ self.mount.position_at(t)
        return d

    def star_at(self, t: float) -> np.ndarray:
        return np.asarray(self.star) + self.motion_at(t)

    def residual_rms(
        self, target: tuple[float, float], t0: float, t1: float, dt: float = 1e-3
    ) -> float:
        """Rms distance (pixels) of the true star from ``target`` over
        [t0, t1] (module doc)."""
        ts = np.arange(t0, t1, dt)
        tgt = np.asarray(target)
        d = np.array([self.star_at(float(t)) - tgt for t in ts])
        return float(np.sqrt(np.mean(np.sum(d * d, axis=1))))

    def render(self, roi: ROI, t0: float, t1: float) -> np.ndarray:
        shift = self.motion(t0, t1)
        k = self.transparency
        stars = [(self.star[0], self.star[1], self.flux * k)]
        stars += [(x, y, f * k) for x, y, f in self.others]
        return render_window(
            roi,
            [] if self.backlit else stars,
            fwhm=self.fwhm,
            exposure=max(t1 - t0, 1e-6),
            offset=(float(shift[0]), float(shift[1])),
            sky=self.sky,
            fiber=self.fiber,
            backlit=self.backlit,
            gain=self.gain,
            read_noise=self.read_noise,
            bias=self.bias,
            sensor=self.sensor,
            profile=self.profile,
            noise=self.noise,
            rng=self.rng,
        )


class SimCamera:
    """A guide camera looking at a :class:`SimWorld` (module doc).
    ``readout`` seconds pass (on the clock) after each exposure;
    ``fail`` makes :meth:`expose` raise."""

    def __init__(self, world: SimWorld, *, readout: float = 0.0, name: str = "simcam") -> None:
        self.world = world
        self.readout = readout
        self.name = name
        self.fail: str | None = None
        self.frames = 0
        self.last_roi: ROI | None = None

    @property
    def sensor(self) -> tuple[int, int]:
        return self.world.sensor

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        from robofast_core.guider.base import GuiderError

        if self.fail:
            raise GuiderError(self.fail)
        clock = self.world.clock
        roi = roi or ROI(0, 0, *self.world.sensor)
        t0 = clock.now()
        await clock.sleep(exposure)
        t1 = clock.now()
        data = self.world.render(roi, t0, t1)
        if self.readout:
            await clock.sleep(self.readout)
        self.frames += 1
        self.last_roi = roi
        return Frame(
            data,
            roi,
            exposure,
            t0,
            t1,
            index=self.frames,
            utc_end=datetime.now(UTC),
            gain=self.world.gain,
            t_ready=clock.now(),
        )
