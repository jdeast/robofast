"""The guide loop: acquire the star onto the fiber, then hold it there.

:class:`Guider` ties the parts together (docs/guider.md):

1. **Fiber.** The target pixel is configured, or located from a backlit
   fiber image (:meth:`Guider.locate_fiber`: the backlight on, one frame,
   the brightest blob's centroid, the backlight off).
2. **Acquire** (state ``acquiring``). A wide frame; the star nearest the
   fiber (or the brightest) is found. If it is farther than
   ``capture_radius`` pixels, the mount is offset to bring it in (through
   the mount's calibration), waiting ``coarse_settle``; repeated up to
   ``coarse_iterations`` times. A distance that grows instead of shrinking
   is the legacy "acquisition not converging" alarm (usually a wrong
   calibration or rotator angle) and fails the acquisition.
3. **Guide** (state ``guiding``). A ``roi_size`` window around the target,
   ``exposure`` seconds per frame: centroid, controller, calibration,
   tip/tilt move, as fast as the camera delivers. Every frame records the
   **latency** from the end of its exposure to the moment its correction
   went on the wire. The offloader hands the tip/tilt's mean to the mount.
4. **Lost.** No valid star for ``lost_frames`` frames in a row, or its
   flux below ``lost_flux_fraction`` of the running reference, or the star
   leaving the window: state ``lost`` and an alarm, then back to
   acquisition, up to ``max_reacquire`` times before ``failed``.

``mode`` ``"open"`` measures and reports without moving anything (for
commissioning and for checking a calibration); ``"closed"`` guides.

Telemetry (:meth:`Guider.status`): the last frame, and over the last
``window`` frames the residual rms (pixels, and arcsec with a plate scale),
loop rate, latency (median, 95th percentile, max), flux, FWHM, tip/tilt
position and how much of its range is used. It is published, at most once
per ``publish_interval``, to a value registry (``<scope>.guide_*``, with
FITS keywords; design.md 3.8) and optionally to a Redis channel in
tres-guider's ``tracking_data`` format (:mod:`~robofast_core.guider.redis_pub`).

Alarms: ``star_lost``, ``tiptilt_limit``, ``residual_high``,
``acquisition_failed``, ``not_converging``, ``device_error``; repeated
alarms within ``alarm_repeat`` seconds are counted, not duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import statistics
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast_core.guider.base import (
    ROI,
    Alarm,
    Camera,
    Clock,
    Frame,
    GuiderError,
    Measurement,
    RealClock,
    TipTilt,
    range_fraction,
)
from robofast_core.guider.calibration import Calibration
from robofast_core.guider.centroid import WindowedCOM, find_stars, make_centroider
from robofast_core.guider.controller import Controller, Gains
from robofast_core.guider.offload import Offloader

log = logging.getLogger(__name__)

STATES = ("idle", "locating", "acquiring", "guiding", "lost", "failed", "stopped")


@dataclass
class GuideConfig:
    #: Guide exposure (s) and camera gain (None: leave it).
    exposure: float = 0.02
    gain: float | None = None
    roi_size: int = 48
    #: Acquisition exposure and window (None: full frame).
    acq_exposure: float = 0.5
    acq_roi: tuple[int, int, int, int] | None = None
    #: The target pixel (the fiber); None until located.
    target: tuple[float, float] | None = None
    fiber_radius: float = 3.0
    #: The guide star's FWHM (pixels) for the annulus centroider's fit until
    #: it has measured it on a star clear of the hole (None: unknown).
    fwhm: float | None = None
    #: The guide-loop centroider (centroid.make_centroider); "annulus" uses
    #: the fiber position and radius.
    centroider: dict[str, Any] = field(default_factory=lambda: {"kind": "com"})
    gains: dict[str, float] = field(default_factory=dict)
    mode: str = "closed"
    #: Within this many pixels the fine loop takes over from the mount.
    capture_radius: float = 8.0
    coarse_iterations: int = 6
    coarse_settle: float = 2.0
    #: Acquisition picks the star nearest the target ("nearest") or the
    #: brightest ("brightest"); a star farther than ``max_star_distance``
    #: pixels is not taken.
    select: str = "nearest"
    max_star_distance: float = 400.0
    lost_frames: int = 10
    lost_flux_fraction: float = 0.2
    max_reacquire: int = 3
    #: Telemetry window (frames), alarm threshold on the residual rms
    #: (pixels), and the tip/tilt range fraction that raises a limit alarm.
    window: int = 200
    residual_alarm: float = 1.5
    limit_alarm: float = 0.95
    alarm_repeat: float = 60.0
    publish_interval: float = 1.0
    #: Arcsec per pixel, for telemetry in arcsec (optional).
    plate_scale: float | None = None
    #: Value registry scope (the telescope's name: "t2").
    scope: str = "guider"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuideConfig:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"guide config: unknown keys {sorted(unknown)}")
        c = cls(**d)
        if c.target is not None:
            c.target = (float(c.target[0]), float(c.target[1]))
        if c.acq_roi is not None:
            c.acq_roi = tuple(int(v) for v in c.acq_roi)  # type: ignore[assignment]
        if c.mode not in ("closed", "open"):
            raise ValueError(f"mode {c.mode!r}: closed or open")
        if c.select not in ("nearest", "brightest"):
            raise ValueError(f"select {c.select!r}: nearest or brightest")
        if not (0 < c.exposure <= 60 and 0 < c.acq_exposure <= 600):
            raise ValueError("exposure times out of range")
        if not 8 <= c.roi_size <= 4096:
            raise ValueError("roi_size out of range")
        Gains.from_dict(c.gains)
        return c


@dataclass
class FrameRecord:
    t: float
    x: float
    y: float
    flux: float
    fwhm: float
    ex: float
    ey: float
    ux: float
    uy: float
    tt_a: float
    tt_b: float
    latency: float
    valid: bool = True
    #: Frame delivered -> command sent (the guider's own share).
    processing: float = math.nan


class Backlight:
    """Turns the fiber backlight on/off (``set(on)``)."""

    def __init__(self, set_fn: Callable[[bool], Awaitable[None]]) -> None:
        self._set = set_fn

    async def set(self, on: bool) -> None:
        await self._set(on)


def kiwispec_backlight(url: str, timeout: float = 15.0) -> Backlight:
    """The KiwiSpec service's ``POST /backlight`` (docs/kiwispec.md)."""

    async def _set(on: bool) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.post(url.rstrip("/") + "/backlight", json={"on": on})
        if r.status_code >= 400:
            raise GuiderError(f"backlight {on}: {r.status_code} {r.text[:200]}")

    return Backlight(_set)


class Guider:
    """One guide camera and what it drives (module doc)."""

    def __init__(
        self,
        camera: Camera,
        config: GuideConfig,
        *,
        tiptilt: TipTilt | None = None,
        tt_cal: Calibration | None = None,
        mount: TipTilt | None = None,
        mount_cal: Calibration | None = None,
        offloader: Offloader | None = None,
        backlight: Backlight | None = None,
        clock: Clock | None = None,
        registry: Any = None,
        publisher: Any = None,
    ) -> None:
        self.camera = camera
        self.config = config
        self.tiptilt = tiptilt
        self.tt_cal = tt_cal
        self.mount = mount
        self.mount_cal = mount_cal
        self.offloader = offloader
        self.backlight = backlight
        self.clock = clock or RealClock()
        self.registry = registry
        self.publisher = publisher
        self.controller = Controller(Gains.from_dict(config.gains))
        if config.target is not None:
            self.controller.set_target(*config.target)
        self.state = "idle"
        self.reason = ""
        self.records: deque[FrameRecord] = deque(maxlen=max(config.window, 10))
        self.alarms: deque[Alarm] = deque(maxlen=50)
        self.last: Measurement | None = None
        self.frames = 0
        self.reacquisitions = 0
        self.acquired_at: float | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_publish = -math.inf
        self._centroider = self._make_centroider()
        self._ref_flux: float | None = None
        self._roi: ROI | None = None
        self.on_state: Callable[[str, str], None] | None = None

    # -- configuration --------------------------------------------------------------

    @property
    def fine(self) -> TipTilt | None:
        """What the fine loop moves: the tip/tilt, else the mount."""
        return self.tiptilt if self.tiptilt is not None else self.mount

    @property
    def fine_cal(self) -> Calibration | None:
        return self.tt_cal if self.tiptilt is not None else self.mount_cal

    def _make_centroider(self) -> Any:
        c = self.config
        fiber = None if c.target is None else (c.target[0], c.target[1], c.fiber_radius)
        spec = dict(c.centroider or {"kind": "com"})
        if spec.get("kind") == "annulus":
            if fiber is None:
                return WindowedCOM()
            if c.fwhm is not None:
                spec.setdefault("fwhm", c.fwhm)
        return make_centroider(spec, fiber)

    def set_target(self, x: float, y: float) -> None:
        """Move the target (setpoint) without resetting the controller."""
        self.config.target = (float(x), float(y))
        self.controller.set_target(x, y)
        self._centroider = self._make_centroider()
        if self._roi is not None and not self._roi.contains(x, y, margin=4):
            self._roi = ROI.centred(x, y, self.config.roi_size, self.camera.sensor)

    def set_gains(self, gains: dict[str, float]) -> None:
        merged = {**self.controller.gains.as_json(), **gains}
        merged = {k: (math.inf if v is None else v) for k, v in merged.items()}
        self.controller.set_gains(Gains.from_dict(merged))
        self.config.gains = {k: v for k, v in merged.items() if math.isfinite(v)}

    def set_mode(self, mode: str) -> None:
        if mode not in ("closed", "open"):
            raise ValueError(f"mode {mode!r}: closed or open")
        self.config.mode = mode

    def set_roi_size(self, size: int) -> None:
        if not 8 <= size <= 4096:
            raise ValueError("roi_size out of range")
        self.config.roi_size = int(size)
        if self._roi is not None and self.config.target is not None:
            self._roi = ROI.centred(*self.config.target, size, self.camera.sensor)

    # -- state and alarms -----------------------------------------------------------

    def _set_state(self, state: str, reason: str = "") -> None:
        if state != self.state:
            log.info("guider %s: %s -> %s %s", self.config.scope, self.state, state, reason)
        self.state = state
        self.reason = reason
        if self.on_state is not None:
            self.on_state(state, reason)
        self._publish(force=True)

    def alarm(self, kind: str, message: str, **extra: Any) -> None:
        now = self.clock.now()
        for a in reversed(self.alarms):
            if a.kind == kind and now - a.t < self.config.alarm_repeat:
                a.count += 1
                a.message = message
                return
        log.warning("guider %s alarm %s: %s", self.config.scope, kind, message)
        self.alarms.append(
            Alarm(kind, message, now, datetime.now(UTC).isoformat(timespec="seconds"), extra=extra)
        )

    # -- control --------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            raise GuiderError("already guiding")
        if self.config.target is None:
            raise GuiderError("no target pixel: configure it or locate the fiber")
        if self.fine is None:
            raise GuiderError("nothing to guide with (no tip/tilt and no mount)")
        if self.fine_cal is None and self.config.mode == "closed":
            raise GuiderError(f"{self.fine.name} is not calibrated")
        self._stop.clear()
        self.reacquisitions = 0
        self._set_state("acquiring", "starting")
        self._task = asyncio.create_task(self._run(), name=f"guider-{self.config.scope}")

    async def stop(self, reason: str = "requested", timeout: float = 5.0) -> bool:
        """Stop the loop; True once it has stopped (bounded)."""
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception, TimeoutError):
                async with asyncio.timeout(timeout):
                    await asyncio.shield(task)
        stopped = task is None or task.done()
        if self.state != "failed":
            self._set_state("stopped", reason)
        return stopped

    async def wait_state(self, states: tuple[str, ...], timeout: float) -> str:
        """Wait (on the guider's clock) until the state is one of ``states``."""
        deadline = self.clock.now() + timeout
        while self.state not in states:
            if self.clock.now() > deadline:
                raise TimeoutError(f"guider still {self.state} after {timeout} s")
            await self.clock.sleep(0.01)
        return self.state

    # -- fiber ------------------------------------------------------------------------

    async def locate_fiber(self) -> tuple[float, float]:
        """Find the fiber in a backlit frame and make it the target."""
        if self.running:
            raise GuiderError("stop guiding before locating the fiber")
        if self.backlight is None:
            raise GuiderError("no backlight configured")
        self._set_state("locating")
        try:
            await self.backlight.set(True)
            try:
                frame = await self.camera.expose(self.config.acq_exposure, self._acq_roi(), None)
            finally:
                await self.backlight.set(False)
            stars = find_stars(frame.data, nsigma=10.0, max_stars=1)
            if not stars:
                raise GuiderError("no backlit fiber in the frame")
            c, r, _ = stars[0]
            m = WindowedCOM(radius=max(3 * self.config.fiber_radius, 4.0)).measure(
                frame, (c + frame.roi.x, r + frame.roi.y)
            )
            if not m.valid:
                raise GuiderError(f"backlit fiber: {m.reason}")
        except Exception as exc:
            self._set_state("idle", f"fiber not located: {exc}")
            raise
        self.set_target(m.x, m.y)
        self._set_state("idle", f"fiber at {m.x:.2f}, {m.y:.2f}")
        return (m.x, m.y)

    # -- the loop ---------------------------------------------------------------------

    def _acq_roi(self) -> ROI | None:
        a = self.config.acq_roi
        return ROI(*a) if a is not None else None

    async def _run(self) -> None:
        try:
            if self.tiptilt is not None and self.config.mode == "closed":
                await self.tiptilt.home()
            attempts = 0
            while not self._stop.is_set():
                star = await self._acquire()
                if self._stop.is_set():
                    return
                if star is None:
                    attempts += 1
                    if attempts > self.config.max_reacquire:
                        self.alarm("acquisition_failed", self.reason or "no star")
                        self._set_state("failed", f"acquisition failed: {self.reason}")
                        return
                    continue
                why = await self._guide(star)
                if why is None:
                    return
                self.alarm("star_lost", why)
                self._set_state("lost", why)
                self.reacquisitions += 1
                attempts += 1
                if attempts > self.config.max_reacquire:
                    self._set_state("failed", f"star lost {attempts} times: {why}")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("guider %s", self.config.scope)
            self.alarm("device_error", f"{type(exc).__name__}: {exc}")
            self._set_state("failed", f"{type(exc).__name__}: {exc}")

    def _select(self, frame: Frame) -> Measurement | None:
        c = self.config
        assert c.target is not None
        tx, ty = c.target
        cands = find_stars(frame.data, nsigma=5.0, max_stars=20)
        best: tuple[float, Measurement] | None = None
        near_hole = c.fiber_radius * 2 + 6
        for col, row, _peak in cands:
            x, y = col + frame.roi.x, row + frame.roi.y
            if math.hypot(x - tx, y - ty) < near_hole and self._centroider.name == "annulus":
                m = self._centroider.measure(frame, (x, y))
            else:
                m = WindowedCOM().measure(frame, (x, y))
            if not m.valid:
                continue
            learn = getattr(self._centroider, "learn", None)
            if learn is not None:
                learn(m)  # the seeing, from a star clear of the fiber
            d = math.hypot(m.x - tx, m.y - ty)
            if d > c.max_star_distance:
                continue
            key = d if c.select == "nearest" else -m.flux
            if best is None or key < best[0]:
                best = (key, m)
        return None if best is None else best[1]

    async def _acquire(self) -> Measurement | None:
        c = self.config
        assert c.target is not None
        self._set_state("acquiring")
        prev: float | None = None
        worse = 0
        for i in range(c.coarse_iterations + 1):
            if self._stop.is_set():
                return None
            frame = await self.camera.expose(c.acq_exposure, self._acq_roi(), c.gain)
            m = self._select(frame)
            if m is None:
                self.reason = "no star found"
                await self.clock.sleep(0)
                continue
            off = np.array(c.target) - np.array([m.x, m.y])
            dist = float(np.hypot(*off))
            if dist <= c.capture_radius:
                self._ref_flux = None
                self.acquired_at = self.clock.now()
                return m
            if prev is not None and dist > 0.8 * prev:
                # Twice no real progress, or once clearly the wrong way.
                worse += 2 if dist > 1.5 * prev else 1
                if worse >= 2:
                    self.alarm(
                        "not_converging",
                        f"acquisition not converging ({prev:.1f} -> {dist:.1f} px):"
                        " check the mount calibration (rotator angle)",
                    )
                    self.reason = "not converging"
                    return None
            prev = dist
            if self.mount is None or self.mount_cal is None or c.mode != "closed":
                self.reason = f"star {dist:.1f} px from the target and no calibrated mount"
                return None
            move = self.mount_cal.to_device(off)
            log.info("acquire %d: star %.1f px away; mount %.2f, %.2f", i, dist, move[0], move[1])
            await self.mount.move_by(float(move[0]), float(move[1]))
            await self.clock.sleep(c.coarse_settle)
        self.reason = self.reason or "did not converge"
        return None

    async def _guide(self, star: Measurement) -> str | None:
        """Guide until stopped (None) or the star is lost (the reason)."""
        c = self.config
        assert c.target is not None
        self.controller.reset()
        self.controller.set_target(*c.target)
        self._roi = ROI.centred(c.target[0], c.target[1], c.roi_size, self.camera.sensor)
        self._set_state("guiding")
        guess: tuple[float, float] = (star.x, star.y)
        bad = 0
        last_t: float | None = None
        fine, cal = self.fine, self.fine_cal
        assert fine is not None
        while not self._stop.is_set():
            frame = await self.camera.expose(c.exposure, self._roi, c.gain)
            self.frames += 1
            m = self._centroider.measure(frame, guess)
            why = self._check(m)
            if why is not None:
                bad += 1
                self._record(frame, m, None, math.nan, valid=False)
                if bad >= c.lost_frames:
                    return why
                continue
            bad = 0
            guess = (m.x, m.y)
            dt = frame.t_end - last_t if last_t is not None else c.exposure
            last_t = frame.t_end
            u = np.zeros(2)
            latency = processing = math.nan
            if c.mode == "closed" and cal is not None:
                u = self.controller.update((m.x, m.y), dt)
                d = cal.to_device(u)
                res = await fine.move_by(float(d[0]), float(d[1]))
                latency = res.t_sent - frame.t_end
                if frame.t_ready is not None:
                    processing = res.t_sent - frame.t_ready
                if res.at_limit:
                    clipped_pix = cal.to_pixels(res.clipped)
                    self.controller.saturated((np.sign(clipped_pix[0]), np.sign(clipped_pix[1])))
                    self.alarm("tiptilt_limit", f"{fine.name} at its range limit")
                if self.offloader is not None and fine is self.tiptilt:
                    self.offloader.record()
                    await self.offloader.maybe_offload()
            self._record(frame, m, u, latency, processing=processing)
            self._check_alarms()
            self._publish()
        return None

    def _check(self, m: Measurement) -> str | None:
        c = self.config
        if not m.valid:
            return m.reason or "no star"
        if self._roi is not None and not self._roi.contains(m.x, m.y, margin=2):
            return "the star left the guide window"
        if self._ref_flux is not None and m.flux < c.lost_flux_fraction * self._ref_flux:
            return f"flux {m.flux:.0f} < {c.lost_flux_fraction} x reference {self._ref_flux:.0f}"
        # Reference flux: a slow running mean of good frames.
        self._ref_flux = m.flux if self._ref_flux is None else 0.98 * self._ref_flux + 0.02 * m.flux
        return None

    def _record(
        self,
        frame: Frame,
        m: Measurement,
        u: np.ndarray | None,
        latency: float,
        valid: bool = True,
        processing: float = math.nan,
    ) -> None:
        self.last = m
        tgt = self.config.target or (math.nan, math.nan)
        pos = self.fine.position if self.fine is not None else (math.nan, math.nan)
        u = u if u is not None else np.zeros(2)
        self.records.append(
            FrameRecord(
                frame.t_end,
                m.x,
                m.y,
                m.flux,
                m.fwhm,
                m.x - tgt[0],
                m.y - tgt[1],
                float(u[0]),
                float(u[1]),
                float(pos[0]),
                float(pos[1]),
                latency,
                valid,
                processing,
            )
        )

    # -- telemetry --------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        recs = [r for r in self.records if r.valid]
        out: dict[str, Any] = {"frames": self.frames, "window": len(self.records)}
        if not recs:
            return out
        ex = np.array([r.ex for r in recs])
        ey = np.array([r.ey for r in recs])
        rms = float(np.sqrt(np.mean(ex**2 + ey**2)))
        lat = [r.latency for r in recs if math.isfinite(r.latency)]
        ts = [r.t for r in self.records]
        rate = (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 1 and ts[-1] > ts[0] else 0.0
        last = recs[-1]
        out.update(
            residual_rms=rms,
            residual_x=float(np.sqrt(np.mean(ex**2))),
            residual_y=float(np.sqrt(np.mean(ey**2))),
            x=last.x,
            y=last.y,
            flux=float(np.mean([r.flux for r in recs])),
            fwhm=_nanmean([r.fwhm for r in recs]),
            correction=[last.ux, last.uy],
            position=[last.tt_a, last.tt_b],
            loop_rate=rate,
            valid_fraction=len(recs) / len(self.records),
        )
        if lat:
            lat_sorted = sorted(lat)
            out.update(
                latency_median=statistics.median(lat),
                latency_p95=lat_sorted[min(len(lat) - 1, int(0.95 * len(lat)))],
                latency_max=lat_sorted[-1],
            )
        proc = sorted(r.processing for r in recs if math.isfinite(r.processing))
        if proc:
            out.update(
                processing_median=statistics.median(proc),
                processing_p95=proc[min(len(proc) - 1, int(0.95 * len(proc)))],
            )
        if self.fine is not None:
            out["range_fraction"] = range_fraction(self.fine)
        ps = self.config.plate_scale
        if ps:
            out["residual_rms_arcsec"] = rms * ps
            if self.last is not None and math.isfinite(self.last.fwhm):
                out["fwhm_arcsec"] = out["fwhm"] * ps
        return out

    def _check_alarms(self) -> None:
        c = self.config
        if len(self.records) < (self.records.maxlen or 20) // 2:
            return
        s = self.summary()
        if s.get("residual_rms", 0.0) > c.residual_alarm:
            self.alarm("residual_high", f"residual rms {s['residual_rms']:.2f} px")
        if s.get("range_fraction", 0.0) >= c.limit_alarm:
            self.alarm("tiptilt_limit", f"{self.fine.name if self.fine else '?'} near its limit")

    def status(self) -> dict[str, Any]:
        c = self.config
        return {
            "state": self.state,
            "reason": self.reason,
            "mode": c.mode,
            "target": list(c.target) if c.target else None,
            "roi": self._roi.as_json() if self._roi else None,
            "exposure": c.exposure,
            "centroider": getattr(self._centroider, "name", "?"),
            "controller": self.controller.state(),
            "fine": None if self.fine is None else self.fine.name,
            "tiptilt": None
            if self.tiptilt is None
            else {
                "name": self.tiptilt.name,
                "units": self.tiptilt.units,
                "position": list(self.tiptilt.position),
                "centre": list(self.tiptilt.centre),
                "range_fraction": range_fraction(self.tiptilt),
            },
            "mount": None
            if self.mount is None
            else {"name": self.mount.name, "offset_total": list(self.mount.position)},
            "offloads": 0 if self.offloader is None else self.offloader.count,
            "reacquisitions": self.reacquisitions,
            "telemetry": self.summary(),
            "alarms": [asdict(a) for a in self.alarms],
        }

    def _publish(self, force: bool = False) -> None:
        now = self.clock.now()
        if not force and now - self._last_publish < self.config.publish_interval:
            return
        self._last_publish = now
        s = self.summary()
        if self.registry is not None:
            try:
                publish_values(
                    self.registry,
                    self.config.scope,
                    self.state,
                    s,
                    target=self.config.target,
                    exposure=self.config.exposure,
                    tiptilt_position=None if self.tiptilt is None else self.tiptilt.position,
                )
            except Exception:
                log.exception("guider: publishing values")
        if self.publisher is not None and self.records and self.state == "guiding":
            try:
                self.publisher.publish(self, s)
            except Exception as exc:
                log.warning("guider: redis publish failed: %r", exc)


def _nanmean(v: list[float]) -> float:
    vals = [x for x in v if math.isfinite(x)]
    return float(np.mean(vals)) if vals else math.nan


#: Registry names (``<scope>.<name>``), FITS keyword, unit, comment.
VALUES: dict[str, tuple[str, str, str]] = {
    "guide_state": ("GUIDSTAT", "", "Guider state"),
    "guide_rms": ("GUIDRMS", "pix", "Guide residual rms (pixels)"),
    "guide_rms_arcsec": ("GUIDRMSA", "arcsec", "Guide residual rms (arcsec)"),
    "guide_flux": ("GUIDFLUX", "adu", "Guide star flux per frame (ADU)"),
    "guide_fwhm": ("GUIDFWHM", "pix", "Guide star FWHM (pixels)"),
    "guide_rate": ("GUIDRATE", "Hz", "Guide loop rate"),
    "guide_latency": ("GUIDLAT", "s", "Guide latency, exposure end to command (median)"),
    "guide_exptime": ("GUIDEXP", "s", "Guide exposure time"),
    "fiber_x": ("XSCIFIB", "pix", "X pixel of the science fiber (guide camera)"),
    "fiber_y": ("YSCIFIB", "pix", "Y pixel of the science fiber (guide camera)"),
    "tiptilt_a": ("TTPOSA", "", "Tip/tilt position, axis A"),
    "tiptilt_b": ("TTPOSB", "", "Tip/tilt position, axis B"),
}


def publish_values(
    registry: Any,
    scope: str,
    state: str,
    summary: dict[str, Any],
    *,
    target: tuple[float, float] | list[float] | None,
    exposure: float | None,
    tiptilt_position: tuple[float, float] | list[float] | None,
    max_age: float = 10.0,
    measured: datetime | None = None,
) -> None:
    """Publish the guider's state to a value registry (design.md 3.8):
    ``<scope>.guide_*`` with FITS keywords (:data:`VALUES`). Used by the
    service for its own registry and by
    :class:`~robofast_core.guider.service.GuiderRemote` for the
    sequencer's."""
    vals: dict[str, Any] = {"guide_state": state}
    if exposure is not None:
        vals["guide_exptime"] = exposure
    if target is not None:
        vals["fiber_x"], vals["fiber_y"] = (round(float(v), 3) for v in target)
    if state == "guiding":
        for name, key in (
            ("guide_rms", "residual_rms"),
            ("guide_rms_arcsec", "residual_rms_arcsec"),
            ("guide_flux", "flux"),
            ("guide_fwhm", "fwhm"),
            ("guide_rate", "loop_rate"),
            ("guide_latency", "latency_median"),
        ):
            v = summary.get(key)
            if isinstance(v, (int, float)) and math.isfinite(v):
                vals[name] = round(float(v), 6)
    if tiptilt_position is not None:
        vals["tiptilt_a"], vals["tiptilt_b"] = (round(float(p), 6) for p in tiptilt_position)
    for name, value in vals.items():
        kw, unit, comment = VALUES[name]
        registry.publish(
            f"{scope}.{name}",
            value,
            max_age=max_age,
            measured=measured,
            unit=unit,
            keyword=kw,
            comment=comment,
        )
