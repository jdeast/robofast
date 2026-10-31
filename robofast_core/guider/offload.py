"""Offloading: hand the tip/tilt's average correction to the mount.

A tip/tilt mirror has a small range; the mount has a large one but is slow.
The :class:`Offloader` watches the tip/tilt's position (a running mean over
``window`` seconds, so seeing jitter is not offloaded) and, when it nears a
limit (``range_fraction`` past ``threshold``) or every ``period`` seconds
if it has wandered more than ``min_pixels``, moves the mount by the offset
that shifts the star as much as the tip/tilt's mean displacement does.

It does **not** move the tip/tilt back itself: as the mount moves (with its
own, slow response) the star drifts and the fast loop follows it, which
walks the tip/tilt back towards its centre with no jump in the image. So
after an offload it waits ``settle`` seconds before considering another
(otherwise it would offload the same displacement twice).

Both directions go through measured calibrations (pixels per tip/tilt unit,
pixels per arcsec of mount offset), so the offload needs no sign
conventions.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np

from robofast_core.guider.base import Clock, RealClock, TipTilt, range_fraction
from robofast_core.guider.calibration import Calibration

log = logging.getLogger(__name__)


@dataclass
class OffloadConfig:
    threshold: float = 0.5
    period: float = 30.0
    min_pixels: float = 1.0
    window: float = 2.0
    settle: float = 3.0


class Offloader:
    def __init__(
        self,
        tiptilt: TipTilt,
        tt_cal: Calibration,
        mount: TipTilt,
        mount_cal: Calibration,
        config: OffloadConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.tiptilt = tiptilt
        self.tt_cal = tt_cal
        self.mount = mount
        self.mount_cal = mount_cal
        self.config = config or OffloadConfig()
        self.clock = clock or RealClock()
        self._samples: deque[tuple[float, float, float]] = deque()
        self._last = self.clock.now()
        self._busy_until = 0.0
        self.count = 0
        self.last_offload: tuple[float, float] | None = None

    def record(self) -> None:
        """Note the tip/tilt's current position (call once per frame)."""
        now = self.clock.now()
        a, b = self.tiptilt.position
        self._samples.append((now, a, b))
        while self._samples and self._samples[0][0] < now - self.config.window:
            self._samples.popleft()

    def mean_pixels(self) -> np.ndarray:
        """The image shift (pixels) the tip/tilt's mean position makes."""
        if not self._samples:
            return np.zeros(2)
        arr = np.array([(a, b) for _, a, b in self._samples])
        mean = arr.mean(axis=0) - np.asarray(self.tiptilt.centre)
        return self.tt_cal.to_pixels(mean)

    def due(self) -> str | None:
        """Why an offload is due now (None: not due)."""
        now = self.clock.now()
        if now < self._busy_until:
            return None
        if range_fraction(self.tiptilt) >= self.config.threshold:
            return "near limit"
        if self.config.period and now - self._last >= self.config.period:
            if float(np.hypot(*self.mean_pixels())) >= self.config.min_pixels:
                return "periodic"
            self._last = now
        return None

    async def maybe_offload(self) -> bool:
        why = self.due()
        if why is None:
            return False
        await self.offload(why)
        return True

    async def offload(self, why: str = "requested") -> None:
        pix = self.mean_pixels()
        move = self.mount_cal.to_device(pix)
        now = self.clock.now()
        self._last = now
        self._busy_until = now + self.config.settle
        log.info(
            "offload (%s): tip/tilt mean %.2f, %.2f px -> mount %.3f, %.3f %s",
            why,
            pix[0],
            pix[1],
            move[0],
            move[1],
            self.mount.units,
        )
        await self.mount.move_by(float(move[0]), float(move[1]))
        self.count += 1
        self.last_offload = (float(move[0]), float(move[1]))
        self._samples.clear()
