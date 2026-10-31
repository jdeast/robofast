"""Guide camera backends (:class:`~robofast_core.guider.base.Camera`).

The guider runs on the computer the camera is plugged into, so frames never
cross the network in the loop: only centroids and corrections leave it.

* :class:`~robofast_core.guider.sim.SimCamera` -- the simulator
  (:mod:`robofast_core.guider.sim`).
* :class:`AlpacaCamera` -- any Alpaca camera, through robofast's own client.
  The **slow path**: every frame is an HTTP round trip and an image
  download, so it is for tests (OmniSim), commissioning, and as a fallback;
  fine for a mount-offset loop at ~1 Hz, not for tip/tilt.
* :class:`ZWOCamera` -- ZWO ASI cameras (the MINERVA FAU cameras) through
  the ZWO SDK's Python binding ``zwoasi`` (optional import), in video mode
  for a high frame rate. Written against the ``zwoasi`` API; **not yet run
  on a camera** (docs/guider.md, commissioning).
* :class:`FrameGrabberCamera` -- a hook for the analog guide cameras on
  some FLWO hardware (a video frame grabber); not implemented.

Every backend that reaches hardware (USB, a non-loopback Alpaca address, a
frame grabber) is refused unless hardware use is allowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.guider.base import (
    ROI,
    Clock,
    Frame,
    GuiderError,
    RealClock,
    check_address,
    check_hardware,
)

log = logging.getLogger(__name__)


class AlpacaCamera:
    """An Alpaca camera (module doc). ``driver`` has ``async get/set/call``
    (an :class:`~robofast_core.alpaca_client.AlpacaDevice`, a ``Grant`` or
    a fake). ROI by ``StartX``/``StartY``/``NumX``/``NumY`` at binning 1."""

    def __init__(
        self,
        driver: Any,
        *,
        name: str = "alpaca",
        timeout: float = 10.0,
        poll: float = 0.005,
        clock: Clock | None = None,
    ) -> None:
        self.driver = driver
        self.name = name
        self.timeout = timeout
        self.poll = poll
        self.clock = clock or RealClock()
        self._sensor = (0, 0)
        self._roi: ROI | None = None
        self._gain: float | None = None
        self._index = 0

    @classmethod
    def at(cls, address: str, number: int = 0, *, allow_hardware: bool = False, **kw: Any):
        from robofast_core.alpaca_client import AlpacaDevice

        check_address(address, "Alpaca camera", allowed=allow_hardware)
        return cls(AlpacaDevice(address, "camera", number), **kw)

    @property
    def sensor(self) -> tuple[int, int]:
        return self._sensor

    async def open(self) -> None:
        d = self.driver
        connect = getattr(d, "connect", None)
        if connect is not None:
            await connect(timeout=self.timeout)
        w = int(await d.get("CameraXSize", timeout=self.timeout))
        h = int(await d.get("CameraYSize", timeout=self.timeout))
        self._sensor = (w, h)
        for member in ("BinX", "BinY"):
            with contextlib.suppress(Exception):
                await d.set(member, 1, timeout=self.timeout)

    async def close(self) -> None:
        aclose = getattr(self.driver, "aclose", None)
        if aclose is not None:
            await aclose()

    async def _apply(self, roi: ROI, gain: float | None) -> None:
        d, t = self.driver, self.timeout
        if roi != self._roi:
            await d.set("NumX", roi.width, timeout=t)
            await d.set("NumY", roi.height, timeout=t)
            await d.set("StartX", roi.x, timeout=t)
            await d.set("StartY", roi.y, timeout=t)
            self._roi = roi
        if gain is not None and gain != self._gain:
            await d.set("Gain", int(gain), timeout=t)
            self._gain = gain

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        roi = roi or ROI(0, 0, *self._sensor)
        await self._apply(roi, gain)
        d = self.driver
        t_start = self.clock.now()
        await d.call("StartExposure", exposure, True, timeout=self.timeout)
        deadline = t_start + exposure + self.timeout
        while not await d.get("ImageReady", timeout=self.timeout):
            if self.clock.now() > deadline:
                raise GuiderError(f"{self.name}: no image within {exposure + self.timeout:.1f} s")
            await self.clock.sleep(self.poll)
        t_ready = self.clock.now()
        image = await d.get("ImageArray", timeout=self.timeout)
        data = getattr(image, "data", image)
        arr = np.asarray(data)
        if arr.ndim != 2:
            raise GuiderError(f"{self.name}: image of shape {arr.shape}")
        # Alpaca images are [x][y]; frames are [row = y][column = x].
        arr = arr.T
        self._index += 1
        return Frame(
            arr,
            ROI(roi.x, roi.y, arr.shape[1], arr.shape[0]),
            exposure,
            t_start,
            # The exposure ended no later than ImageReady and no earlier than
            # start + exposure; take the earlier (readout is not latency the
            # loop can remove).
            min(t_start + exposure, t_ready),
            index=self._index,
            utc_end=datetime.now(UTC),
            t_ready=self.clock.now(),
        )


class ZWOCamera:
    """ZWO ASI through ``zwoasi`` (module doc). Video mode: the camera
    streams frames at the exposure time and :meth:`expose` takes the next
    one, so readout overlaps the next exposure. Changing the exposure, gain
    or ROI restarts the stream. ``library`` is the path to the ASI SDK's
    ``libASICamera2``."""

    def __init__(
        self,
        *,
        index: int = 0,
        library: str | None = None,
        name: str = "zwo",
        allow_hardware: bool = False,
        clock: Clock | None = None,
    ) -> None:
        check_hardware("ZWO ASI camera (USB)", allowed=allow_hardware)
        self.index = index
        self.library = library
        self.name = name
        self.clock = clock or RealClock()
        self._asi: Any = None
        self._cam: Any = None
        self._sensor = (0, 0)
        self._mode: tuple[ROI, float, float | None] | None = None
        self._index = 0

    @property
    def sensor(self) -> tuple[int, int]:
        return self._sensor

    async def open(self) -> None:
        try:
            import zwoasi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GuiderError("zwoasi is not installed (pip install zwoasi)") from exc
        self._asi = zwoasi

        def _open() -> Any:
            if self.library:
                zwoasi.init(self.library)
            cam = zwoasi.Camera(self.index)
            info = cam.get_camera_property()
            cam.set_image_type(zwoasi.ASI_IMG_RAW16)
            return cam, (int(info["MaxWidth"]), int(info["MaxHeight"]))

        self._cam, self._sensor = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._cam is not None:
            cam = self._cam
            self._cam = None

            def _close() -> None:
                with contextlib.suppress(Exception):
                    cam.stop_video_capture()
                cam.close()

            await asyncio.to_thread(_close)

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        asi, cam = self._asi, self._cam
        if cam is None:
            raise GuiderError(f"{self.name}: not open")
        roi = roi or ROI(0, 0, *self._sensor)
        # The SDK wants the width a multiple of 8 and the height of 2.
        roi = ROI(
            roi.x, roi.y, max(8, roi.width - roi.width % 8), max(2, roi.height - roi.height % 2)
        )
        mode = (roi, exposure, gain)
        if mode != self._mode:

            def _configure() -> None:
                with contextlib.suppress(Exception):
                    cam.stop_video_capture()
                cam.set_roi(roi.x, roi.y, roi.width, roi.height)
                cam.set_control_value(asi.ASI_EXPOSURE, int(exposure * 1e6))
                if gain is not None:
                    cam.set_control_value(asi.ASI_GAIN, int(gain))
                cam.start_video_capture()

            await asyncio.to_thread(_configure)
            self._mode = mode
        timeout_ms = int(2000 * exposure + 500)
        data = await asyncio.to_thread(cam.capture_video_frame, timeout=timeout_ms)
        t_end = self.clock.now()
        arr = np.asarray(data).reshape(roi.height, roi.width)
        self._index += 1
        return Frame(
            arr,
            roi,
            exposure,
            t_end - exposure,
            t_end,
            index=self._index,
            utc_end=datetime.now(UTC),
            t_ready=t_end,
        )


class FrameGrabberCamera:
    """Hook for an analog guide camera behind a video frame grabber (some
    FLWO hardware). Not implemented: the frame grabber, its driver and how
    frames are time-stamped are still to be chosen (docs/guider.md)."""

    def __init__(
        self, *, device: str = "", name: str = "framegrabber", allow_hardware: bool = False
    ):
        check_hardware(f"frame grabber {device}", allowed=allow_hardware)
        self.device = device
        self.name = name

    @property
    def sensor(self) -> tuple[int, int]:
        return (0, 0)

    async def open(self) -> None:
        raise NotImplementedError("frame-grabber cameras are not implemented yet (docs/guider.md)")

    async def close(self) -> None:
        pass

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        raise NotImplementedError("frame-grabber cameras are not implemented yet")
