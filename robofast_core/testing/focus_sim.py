"""A focuser and a star-field camera for autofocus tests (no hardware).

:class:`SimFocuser` is an absolute Alpaca focuser (``Move``, ``Position``,
``IsMoving``, ``Halt``, ``MaxStep``, ``StepSize``, ``Temperature``) that
moves at ``speed`` steps per second and has optional mechanical
**backlash**: the optics follow the reported position through a dead band
of ``backlash`` steps (a "play" hysteresis). Moving *down* the optics sit
at the reported position; moving *up* they lag ``backlash`` steps behind.
So approaching every position from above lands the optics exactly there,
and approaching from below lands them ``backlash`` steps low. Faults:
``stuck`` (``IsMoving`` never clears), ``fail_moves`` (the next N
``Move`` calls raise), ``arrival_error`` (stops that many steps off), and
the gate faults of :class:`~robofast_core.testing.fakes.FakeDevice`.

:class:`StarfieldCamera` is a :class:`~robofast_core.testing.fakes.FakeCamera`
whose ``ImageArray`` is rendered by :mod:`robofast_core.testing.starfield`
from a :class:`FocusScene`, at the focuser's optical position when the
exposure started; it honours ``BinX``/``BinY`` and the subframe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from robofast_core.imagebytes import ImageArray
from robofast_core.testing.fakes import FakeCamera, FakeDevice, FakeDeviceError
from robofast_core.testing.starfield import Detector, SimOptics, Star, random_stars, render

__all__ = ["FocusScene", "SimFocuser", "StarfieldCamera"]


class SimFocuser(FakeDevice):
    def __init__(
        self,
        position: int = 20000,
        *,
        max_step: int = 60000,
        speed: float = 1e6,
        backlash: float = 0.0,
        step_size: float = 1.0,
        temperature: float = 10.0,
    ) -> None:
        super().__init__()
        self.Absolute = True
        self.MaxStep = max_step
        self.MaxIncrement = max_step
        self.StepSize = step_size
        self.Temperature = temperature
        self.TempComp = False
        self.speed = speed
        self.backlash = backlash
        self._from = float(position)
        self._to = float(position)
        self._t0 = 0.0
        self._t1 = 0.0
        #: Optical position (follows the mechanism through the backlash).
        self._optical = float(position)
        self.stuck = False
        self.fail_moves = 0
        self.arrival_error = 0
        self.moves: list[int] = []

    def _mech(self) -> float:
        now = self._now()
        if now >= self._t1:
            return self._to
        f = (now - self._t0) / max(self._t1 - self._t0, 1e-9)
        return self._from + f * (self._to - self._from)

    def _play(self, mech: float) -> None:
        # Moving up the optics trail by `backlash`; moving down they follow.
        self._optical = min(max(self._optical, mech - self.backlash), mech)

    @property
    def IsMoving(self) -> bool:
        return self.stuck or self._now() < self._t1

    @property
    def Position(self) -> int:
        return round(self._mech())

    @property
    def optical(self) -> float:
        """Where the optics are now (ground truth for tests)."""
        mech = self._mech()
        # Moves are monotonic, so applying the play at the current point is exact.
        self._play(mech)
        return self._optical

    def Move(self, position: int) -> None:
        if self.fail_moves > 0:
            self.fail_moves -= 1
            raise FakeDeviceError("focuser move failed")
        position = int(position)
        if not 0 <= position <= self.MaxStep:
            raise FakeDeviceError(f"position {position} outside 0..{self.MaxStep}")
        self._play(self._mech())
        start = self._mech()
        target = position + self.arrival_error
        self._from, self._to = start, float(target)
        self._t0 = self._now()
        self._t1 = self._t0 + abs(target - start) / self.speed
        self.moves.append(position)
        if self._t1 <= self._t0:
            self._play(self._to)

    def Halt(self) -> None:
        mech = self._mech()
        self._play(mech)
        self._from = self._to = mech
        self._t1 = self._now()
        self.stuck = False


@dataclass
class FocusScene:
    """What the camera sees: stars, optics, the true focus, the seeing."""

    stars: list[Star]
    optics: SimOptics = field(default_factory=SimOptics)
    detector: Detector = field(default_factory=lambda: Detector(shape=(768, 768)))
    #: Optical focuser position of best focus.
    true_focus: float = 20000.0
    #: Microns of focal-plane motion per focuser step (sign: direction).
    focal_um_per_step: float = 1.0
    #: FWHM in arcsec; or a function of the exposure number.
    seeing: float | Callable[[int], float] = 2.0
    #: Effective exposure (s) for the photon count, whatever was asked (so
    #: tests can take short exposures); None: as asked.
    effective_exposure: float | None = 1.0
    #: Flux scale (clouds < 1).
    transparency: float = 1.0
    jitter: float = 0.0
    seed: int = 1
    rng: np.random.Generator = field(init=False)
    frames: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    @classmethod
    def random(cls, n: int = 10, *, seed: int = 1, **kw) -> FocusScene:
        det = kw.get("detector") or Detector(shape=(768, 768))
        rng = np.random.default_rng(seed)
        stars = random_stars(n, det.shape, flux=(1e5, 6e5), margin=90, min_separation=170, rng=rng)
        kw.setdefault("detector", det)
        return cls(stars, seed=seed, **kw)

    def defocus(self, optical: float) -> float:
        return (optical - self.true_focus) * self.focal_um_per_step

    def render(self, optical: float, duration: float, roi, binning: int) -> np.ndarray:
        seeing = self.seeing(self.frames) if callable(self.seeing) else self.seeing
        self.frames += 1
        exposure = self.effective_exposure if self.effective_exposure is not None else duration
        stars = self.stars
        if self.transparency != 1.0:
            stars = [Star(s.x, s.y, s.flux * self.transparency) for s in stars]
        frame = render(
            stars,
            self.optics,
            self.detector,
            seeing=seeing,
            defocus=self.defocus(optical),
            exposure=exposure,
            roi=roi,
            binning=binning,
            jitter=self.jitter,
            rng=self.rng,
        )
        return frame.image


class StarfieldCamera(FakeCamera):
    def __init__(self, focuser: SimFocuser, scene: FocusScene) -> None:
        ny, nx = scene.detector.shape
        super().__init__(numx=nx, numy=ny)
        self.focuser = focuser
        self.scene = scene
        self.PixelSizeX = self.PixelSizeY = scene.optics.pixel_size
        self.ElectronsPerADU = scene.detector.gain
        self.MaxADU = int(scene.detector.saturation)
        self._optical_at_start = focuser.optical
        self._duration = 0.0
        #: Optical focuser position of every exposure (ground truth).
        self.exposed_at: list[float] = []

    def StartExposure(self, duration: float, light: bool) -> None:
        super().StartExposure(duration, light)
        self._optical_at_start = self.focuser.optical
        self._duration = float(duration)
        self.exposed_at.append(self._optical_at_start)

    async def get(self, member: str, *, timeout: float | None = None) -> Any:
        if member != "ImageArray":
            return await super().get(member, timeout=timeout)
        if self.download_time:
            await asyncio.sleep(self.download_time)
        await self._gate()
        # Rendered off the event loop, as a real camera's download never
        # blocks it: a sky frame takes ~0.3 s of CPU, more on a loaded CI
        # runner, and a blocked loop starves the supervisor's weather reads
        # (a short stale_after then revokes the lease mid-run).
        return await asyncio.to_thread(lambda: self.ImageArray)

    @property
    def ImageArray(self) -> ImageArray:
        if self.image_error is not None:
            raise FakeDeviceError(self.image_error)
        if not self._ready:
            raise FakeDeviceError("no image available")
        self.downloads += 1
        b = int(self.BinX)
        roi = (int(self.StartX) * b, int(self.StartY) * b, int(self.NumX) * b, int(self.NumY) * b)
        img = self.scene.render(self._optical_at_start, self._duration, roi, b)
        data = np.ascontiguousarray(img.T.astype("<i4"))  # ASCOM order: [x, y]
        return ImageArray(data, 2, "imagebytes", data.nbytes + 44)
