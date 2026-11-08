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

**Offset guiding** (:meth:`Guider.set_offset`; docs/offset_guiding.md):
the guider holds a guide star at the fiber plus the guide star's offset
from the science target (:class:`~robofast.guider.offset.OffsetGuide`),
so the target sits on the fiber; the setpoint is recomputed every
``update`` seconds (proper motion, differential refraction, field
rotation) without resetting the controller, and the centroider is a plain
one (the guide star is clear of the fiber hole).

Telemetry (:meth:`Guider.status`): the last frame, and over the last
``window`` frames the residual rms (pixels, and arcsec with a plate scale),
loop rate, latency (median, 95th percentile, max), flux, FWHM, tip/tilt
position and how much of its range is used. It is published, at most once
per ``publish_interval``, to a value registry (``<scope>.guide_*``, with
FITS keywords; design.md 3.8) and optionally to a Redis channel in
tres-guider's ``tracking_data`` format (:mod:`~robofast.guider.redis_pub`).

**Fiber flux** (``fiber_flux``; docs/kiwispec.md, "Flux from the FAU guide
frames"): every guide frame's background-subtracted counts inside the
fiber's footprint (:func:`fiber_counts`, a ``fiber_aperture`` radius around
the fiber pixel, the background from an annulus) go into the frame's record
(``fiber_counts``) and into :attr:`Guider.flux_frames`, which the guider
service reports to the spectrograph service for the spectrum's
flux-weighted midpoint. Behind a beamsplitter (MINERVA's FAU) this is the
light reaching the fiber's position, not the light coupled into it: a
relative flux.

Alarms: ``star_lost``, ``tiptilt_limit``, ``residual_high``,
``acquisition_failed``, ``not_converging``, ``device_error``; repeated
alarms within ``alarm_repeat`` seconds are counted, not duplicated.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

import numpy as np

from robofast.cancel import stop_task
from robofast.guider.base import (
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
from robofast.guider.calibration import Calibration
from robofast.guider.centroid import WindowedCOM, find_stars, make_centroider
from robofast.guider.controller import Controller, Gains
from robofast.guider.offload import Offloader

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
    #: Acquisition picks the star nearest the target ("nearest"), the
    #: brightest ("brightest"), or the one at the target's catalog position
    #: in a plate solve of the acquisition frame ("catalog":
    #: :mod:`robofast.guider.identify`; with no solution, the brightest
    #: only if the field is unambiguous, else none). A star farther than
    #: ``max_star_distance`` pixels is not taken.
    select: str = "nearest"
    #: "catalog": the detected star must be within this many pixels of the
    #: predicted position.
    identify_tolerance: float = 10.0
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
    #: Measure the counts inside the fiber's footprint on every guide frame
    #: (module doc): an aperture of ``fiber_aperture`` pixels (default
    #: ``fiber_radius``) around the fiber pixel, less the median of the
    #: annulus ``fiber_annulus`` (inner, outer radius; default 2x and 3x
    #: the aperture).
    fiber_flux: bool = False
    fiber_aperture: float | None = None
    fiber_annulus: tuple[float, float] | None = None

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
        if c.select not in ("nearest", "brightest", "catalog"):
            raise ValueError(f"select {c.select!r}: nearest, brightest or catalog")
        if not (0 < c.exposure <= 60 and 0 < c.acq_exposure <= 600):
            raise ValueError("exposure times out of range")
        if not 8 <= c.roi_size <= 4096:
            raise ValueError("roi_size out of range")
        if c.fiber_annulus is not None:
            c.fiber_annulus = (float(c.fiber_annulus[0]), float(c.fiber_annulus[1]))
            if not 0 < c.fiber_annulus[0] < c.fiber_annulus[1]:
                raise ValueError("fiber_annulus: 0 < inner < outer")
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
    #: Background-subtracted counts inside the fiber's footprint (fiber_flux).
    fiber_counts: float = math.nan


def fiber_counts(
    frame: Frame, x: float, y: float, radius: float, annulus: tuple[float, float] | None = None
) -> float | None:
    """Counts inside a ``radius``-pixel circle around detector pixel
    ``(x, y)``, less the median of the ``annulus`` (default 2x..3x the
    radius) times the aperture's pixel count. None when the annulus is not
    wholly inside the frame (the fiber is outside the window)."""
    r_in, r_out = annulus or (2 * radius, 3 * radius)
    data = frame.data
    cx, cy = x - frame.roi.x, y - frame.roi.y
    ny, nx = data.shape
    if cx - r_out < 0 or cy - r_out < 0 or cx + r_out > nx - 1 or cy + r_out > ny - 1:
        return None
    x0, x1 = math.floor(cx - r_out), math.ceil(cx + r_out) + 1
    y0, y1 = math.floor(cy - r_out), math.ceil(cy + r_out) + 1
    cut = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - cx, yy - cy)
    ap = d <= radius
    ring = (d >= r_in) & (d <= r_out)
    if not ap.any() or not ring.any():
        return None
    return float(cut[ap].sum() - np.median(cut[ring]) * ap.sum())


class Backlight:
    """Turns the fiber backlight on/off (``set(on)``); built by a backend in
    :data:`robofast.guider.hardware.BACKLIGHTS`."""

    def __init__(self, set_fn: Callable[[bool], Awaitable[None]]) -> None:
        self._set = set_fn

    async def set(self, on: bool) -> None:
        await self._set(on)


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
        #: Fiber flux not yet reported: (t_start, t_end, counts), clock time.
        self.flux_frames: deque[tuple[float, float, float]] = deque(maxlen=20000)
        self.alarms: deque[Alarm] = deque(maxlen=50)
        self.last: Measurement | None = None
        self.frames = 0
        self.reacquisitions = 0
        self.acquired_at: float | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_publish = -math.inf
        self.offset: Any = None
        self._centroider = self._make_centroider()
        self._ref_flux: float | None = None
        self._roi: ROI | None = None
        self.on_state: Callable[[str, str], None] | None = None
        #: For ``select: "catalog"``: the target's pixel from a plate solve
        #: of a frame (:func:`~robofast.guider.identify.catalog_identifier`),
        #: and whether brightest-star acquisition is safe without one.
        self.identify: Callable[[Frame], tuple[float, float] | None] | None = None
        self.unambiguous = False
        #: The acquisition cascade for this start (docs/acquisition.md;
        #: :class:`~robofast.guider.acquire.Acquirer`): when set, it alone
        #: decides which star is the target, whatever ``select`` says.
        self.acquirer: Any = None
        #: Where the last acquisition frame put the target (None: unsolved).
        self.identified: tuple[float, float] | None = None
        self._why = ""
        #: Offset guiding (:meth:`set_offset`): the setpoint model, the
        #: fiber pixel it is measured from, clock time -> UTC, the last
        #: solution and when the setpoint is next recomputed.
        self.fiber: tuple[float, float] | None = None
        self._utc_at: Callable[[float], datetime] | None = None
        self.offset_info: dict[str, Any] | None = None
        self._offset_due = math.inf

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
        spec = dict(c.centroider or {"kind": "com"})
        if self.offset is not None:
            # The guide star is clear of the fiber: no hole to model.
            return WindowedCOM() if spec.get("kind") == "annulus" else make_centroider(spec)
        fiber = None if c.target is None else (c.target[0], c.target[1], c.fiber_radius)
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

    # -- offset guiding ----------------------------------------------------------------

    def set_offset(self, offset: Any, utc_at: Callable[[float], datetime]) -> dict[str, Any]:
        """Guide on an offset star (module doc): ``offset`` an
        :class:`~robofast.guider.offset.OffsetGuide`, ``utc_at`` maps
        the guider's clock to UTC. The current target pixel is the fiber.
        Returns the setpoint's solution."""
        if self.running:
            raise GuiderError("stop guiding before changing the guide star")
        fiber = self.fiber if self.fiber is not None else self.config.target
        if fiber is None:
            raise GuiderError("no fiber pixel: configure it or locate the fiber")
        self.fiber = (float(fiber[0]), float(fiber[1]))
        self.offset = offset
        self._utc_at = utc_at
        self._offset_due = -math.inf
        return self._update_offset(force=True)

    def clear_offset(self) -> None:
        """Back to guiding on the target itself, at the fiber."""
        fiber, self.fiber = self.fiber, None
        self.offset = self._utc_at = self.offset_info = None
        self._offset_due = math.inf
        if fiber is not None:
            self.set_target(*fiber)

    def _update_offset(self, force: bool = False) -> dict[str, Any]:
        """Recompute the offset setpoint if due (keeps the controller)."""
        now = self.clock.now()
        if self.offset is None or (not force and now < self._offset_due):
            return self.offset_info or {}
        assert self.fiber is not None and self._utc_at is not None
        sol = self.offset.solve(self._utc_at(now))
        x = self.fiber[0] + sol["pixels"][0]
        y = self.fiber[1] + sol["pixels"][1]
        sol["setpoint"] = [round(x, 3), round(y, 3)]
        prev = self.config.target
        self.set_target(x, y)
        self.offset_info = sol
        self._offset_due = now + max(float(self.offset.update), 0.1)
        if force or prev is None or math.hypot(x - prev[0], y - prev[1]) > 0.05:
            log.info(
                'guider %s: offset star at %+.1f" E %+.1f" N of the target -> setpoint'
                " %.2f, %.2f (refraction %s arcsec, PA %.2f)",
                self.config.scope,
                sol["east"],
                sol["north"],
                x,
                y,
                sol.get("refraction", "not computed"),
                sol["pa"],
            )
        return sol

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
        # Bounded, passing our own cancellation on (robofast.cancel).
        await stop_task(task, timeout)
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
        self.clear_offset()
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

    def _select(self, frame: Frame, hint: tuple[float, float] | None = None) -> Measurement | None:
        c = self.config
        assert c.target is not None
        tx, ty = c.target
        mode = c.select
        if self.acquirer is not None:
            if hint is None:
                last = self.acquirer.last or {}
                self._why = f"target not identified ({last.get('method')}: {last.get('reason')})"
                return None
            mode = "hint"
        elif mode == "catalog":
            if hint is not None:
                mode = "hint"
            elif self.unambiguous:
                mode = "brightest"
            else:
                self._why = "target not identified (no plate solution; field ambiguous)"
                return None
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
            if mode == "hint":
                assert hint is not None
                dh = math.hypot(m.x - hint[0], m.y - hint[1])
                if dh > c.identify_tolerance:
                    continue
                key = dh
            else:
                key = d if mode == "nearest" else -m.flux
            if best is None or key < best[0]:
                best = (key, m)
        if best is None and mode == "hint":
            self._why = "no star at the target's catalog position"
        return None if best is None else best[1]

    async def _acquire(self) -> Measurement | None:
        c = self.config
        assert c.target is not None
        self._set_state("acquiring")
        prev: float | None = None
        worse = 0
        exposure = c.acq_exposure
        retried = False
        for i in range(c.coarse_iterations + 1):
            if self._stop.is_set():
                return None
            frame = await self.camera.expose(exposure, self._acq_roi(), c.gain)
            hint = None
            if self.acquirer is not None:
                if isinstance(self.clock, RealClock):
                    hint = await asyncio.to_thread(self.acquirer, frame)
                else:
                    hint = self.acquirer(frame)
                self.identified = hint
                longer = self.acquirer.config.retry_exposure
                if hint is None and longer and not retried:
                    retried = True
                    exposure = float(longer)
                    log.info("acquisition: retrying with a %.1f s exposure", exposure)
                    continue
            elif c.select == "catalog" and self.identify is not None:
                if isinstance(self.clock, RealClock):
                    hint = await asyncio.to_thread(self.identify, frame)
                else:  # simulated time: compute costs none (VirtualClock)
                    hint = self.identify(frame)
                self.identified = hint
            self._why = "no star found"
            if isinstance(self.clock, RealClock):
                # Star finding on a full acquisition frame: off the event
                # loop, which must keep answering the service's HTTP API.
                m = await asyncio.to_thread(self._select, frame, hint)
            else:
                m = self._select(frame, hint)
            if m is None:
                self.reason = self._why
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
            if self.offset is not None:
                self._update_offset()  # proper motion, refraction, field rotation
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
        counts = math.nan
        fib = self.fiber or self.config.target
        if self.config.fiber_flux and fib is not None:
            r = self.config.fiber_aperture or self.config.fiber_radius
            got = fiber_counts(frame, fib[0], fib[1], r, self.config.fiber_annulus)
            if got is not None:
                counts = got
                self.flux_frames.append((frame.t_start, frame.t_end, max(0.0, got)))
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
                counts,
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
            # The live seeing (docs/seeing.md): a whole star's FWHM; on the
            # fiber (annulus) the width learned from a star clear of the
            # hole, never the fit of the spill around it.
            if getattr(self._centroider, "name", "") == "annulus":
                px = getattr(self._centroider, "seeing", None)
            else:
                px = out["fwhm"]
            if px is not None and math.isfinite(px) and px > 0:
                out["seeing_arcsec"] = px * ps
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
            "acquisition": None if self.acquirer is None else self.acquirer.summary(),
            "mode": c.mode,
            "target": list(c.target) if c.target else None,
            "guide_mode": "offset" if self.offset is not None else "target",
            "fiber": list(self.fiber) if self.fiber else (list(c.target) if c.target else None),
            "offset_guide": None
            if self.offset is None
            else {**self.offset.describe(), "now": self.offset_info},
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

    def status_offset(self) -> dict[str, Any] | None:
        """The guide mode and star for the FITS headers (:func:`publish_values`)."""
        if self.offset is None:
            return {"guide_mode": "target"}
        return {"guide_mode": "offset", **self.offset.describe(), "now": self.offset_info}

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
                    target=self.fiber or self.config.target,
                    exposure=self.config.exposure,
                    tiptilt_position=None if self.tiptilt is None else self.tiptilt.position,
                    offset=self.status_offset(),
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
    "guide_seeing": ("GUIDSEE", "arcsec", "Seeing FWHM from the guide camera"),
    "guide_rate": ("GUIDRATE", "Hz", "Guide loop rate"),
    "guide_latency": ("GUIDLAT", "s", "Guide latency, exposure end to command (median)"),
    "guide_exptime": ("GUIDEXP", "s", "Guide exposure time"),
    "fiber_x": ("XSCIFIB", "pix", "X pixel of the science fiber (guide camera)"),
    "fiber_y": ("YSCIFIB", "pix", "Y pixel of the science fiber (guide camera)"),
    "tiptilt_a": ("TTPOSA", "", "Tip/tilt position, axis A"),
    "tiptilt_b": ("TTPOSB", "", "Tip/tilt position, axis B"),
    "guide_mode": ("GUIDMODE", "", "Guided on: target (annulus) or offset star"),
    "gstar_source": ("GSSOURCE", "", "Offset guide star: offset, coordinates, catalog"),
    "gstar_ra": ("GSRA", "deg", "Offset guide star RA (ICRS, epoch of obs)"),
    "gstar_dec": ("GSDEC", "deg", "Offset guide star Dec (ICRS, epoch of obs)"),
    "gstar_g": ("GSMAG", "mag", "Offset guide star magnitude (Gaia G)"),
    "gstar_id": ("GSID", "", "Offset guide star catalog source_id"),
    "gstar_east": ("GSOFFE", "arcsec", "Guide star minus target, east"),
    "gstar_north": ("GSOFFN", "arcsec", "Guide star minus target, north"),
    "gstar_x": ("GSPIXX", "pix", "Guide star setpoint X (guide camera)"),
    "gstar_y": ("GSPIXY", "pix", "Guide star setpoint Y (guide camera)"),
    "gstar_dar": ("GSDAR", "arcsec", "Differential refraction across the offset"),
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
    offset: dict[str, Any] | None = None,
) -> None:
    """Publish the guider's state to a value registry (design.md 3.8):
    ``<scope>.guide_*`` with FITS keywords (:data:`VALUES`). Used by the
    service for its own registry and by
    :class:`~robofast.guider.service.GuiderRemote` for the
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
            ("guide_seeing", "seeing_arcsec"),
            ("guide_rate", "loop_rate"),
            ("guide_latency", "latency_median"),
        ):
            v = summary.get(key)
            if isinstance(v, (int, float)) and math.isfinite(v):
                vals[name] = round(float(v), 6)
    if tiptilt_position is not None:
        vals["tiptilt_a"], vals["tiptilt_b"] = (round(float(p), 6) for p in tiptilt_position)
    if offset is not None:
        vals.update(_offset_values(offset))
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


def _offset_values(offset: dict[str, Any]) -> dict[str, Any]:
    """The ``guide_mode`` and ``gstar_*`` values of a status's offset entry."""
    vals: dict[str, Any] = {"guide_mode": offset.get("guide_mode", "target")}
    if vals["guide_mode"] != "offset":
        return vals
    star = offset.get("star") or {}
    now = offset.get("now") or {}
    vals["gstar_source"] = star.get("source", "?")
    for name, key in (("gstar_ra", "ra"), ("gstar_dec", "dec"), ("gstar_g", "g")):
        if star.get(key) is not None:
            vals[name] = round(float(star[key]), 7)
    if star.get("source_id") is not None:
        vals["gstar_id"] = str(star["source_id"])
    for name, key in (("gstar_east", "east"), ("gstar_north", "north")):
        if now.get(key) is not None:
            vals[name] = round(float(now[key]), 3)
    sp = now.get("setpoint")
    if sp:
        vals["gstar_x"], vals["gstar_y"] = (round(float(v), 3) for v in sp)
    dar = now.get("refraction")
    if dar:
        vals["gstar_dar"] = round(math.hypot(float(dar[0]), float(dar[1])), 4)
    return vals
