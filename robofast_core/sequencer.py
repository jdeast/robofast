"""One asyncio sequencer per enclosure.

Each "generation" of observing runs like this::

    lease = await supervisor.request_open(...)     # waits for safe + hold
    obs   = plan()                                 # re-plan from scratch
    grants for this generation only (telescope, camera)
    task  = observe(obs)                           # one asyncio task
    wait for: task finishes  OR  lease revoked
    on revoke: 1. revoke this generation's grants   (fence: nothing more is sent)
               2. task.cancel()
               3. wait (bounded) for the task to acknowledge by finishing;
                  if it does not, quarantine: never release the lease, so the
                  supervisor will not reopen and no new generation starts
    cleanup with *fresh* grants: abort the exposure, park (bounded, parallel)
    lease.release()                                # only now can a new lease exist

The sequencer never touches the enclosure: it has no dome grant at all. It
only learns "open" from ``lease.wait_open()`` and "stop" from
``lease.revoked``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from robofast_core.owner import DeviceError, DeviceOwner, Grant
from robofast_core.safety import LeaseRevoked, OpenLease, SafetySupervisor
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

CAMERA_IDLE = 0  # ASCOM CameraState.cameraIdle


@dataclass
class Observation:
    name: str
    #: Target; ``None`` means "half an hour east of the meridian, 10 deg
    #: south of zenith", computed from the mount (handy for simulators).
    ra_hours: float | None = None
    dec_deg: float | None = None
    exptime: float = 10.0
    count: int = 1


@dataclass
class SequencerConfig:
    poll_interval: float = 0.5
    slew_timeout: float = 120.0
    #: Allowance on top of the exposure time for readout before giving up.
    readout_margin: float = 60.0
    lease_renew_interval: float = 10.0
    #: How long a cancelled observing task gets to finish before quarantine.
    cancel_ack_timeout: float = 10.0
    park_timeout: float = 120.0
    #: Per-device abort policy: "abort" discards the frame, "stop" keeps it.
    abort_policy: str = "abort"
    #: After a generation fails on its own (a device error, not weather), wait
    #: this long, with the lease released (so the enclosure closes), before
    #: asking to open again; give up after this many failures in a row. This
    #: stops a dead camera from cycling the enclosure open and shut.
    error_backoff: float = 300.0
    max_consecutive_errors: int = 3


@dataclass
class CleanupReport:
    generation: int
    aborted: bool = False
    parked: bool = False
    errors: list[str] = field(default_factory=list)
    duration: float = 0.0


class Sequencer:
    def __init__(
        self,
        name: str,
        *,
        supervisor: SafetySupervisor,
        telescope: DeviceOwner,
        camera: DeviceOwner,
        plan: Callable[[], Observation | None],
        timeline: Timeline,
        config: SequencerConfig | None = None,
    ) -> None:
        self.name = name
        self.supervisor = supervisor
        self.telescope = telescope
        self.camera = camera
        self.plan = plan
        self.timeline = timeline
        self.config = config or SequencerConfig()
        if self.config.abort_policy not in ("abort", "stop"):
            raise ValueError(f"abort_policy {self.config.abort_policy!r}")
        self.generation = 0
        self.quarantined = False
        self.frames: list[dict[str, Any]] = []
        self.cleanups: list[CleanupReport] = []
        self.consecutive_errors = 0
        self.gave_up = False
        self._outcome: str | None = None

    def _event(self, what: str, **detail: Any) -> None:
        self.timeline.event(self.name, what, **detail)

    # -- main loop --------------------------------------------------------

    async def run(self) -> None:
        while True:
            lease = await self.supervisor.request_open(self.name)
            self._outcome = None
            try:
                obs = self.plan()
                if obs is None:
                    self._event("plan_exhausted")
                    return
                self.generation += 1
                await self._run_generation(self.generation, lease, obs)
            finally:
                if self.quarantined:
                    self._event("lease_held_quarantined", lease=lease.id)
                else:
                    lease.release()
                    self._event("lease_released", lease=lease.id)
            if self.quarantined:
                return
            if self._outcome == "error":
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.config.max_consecutive_errors:
                    self.gave_up = True
                    self._event("giving_up", errors=self.consecutive_errors, level="critical")
                    return
                self._event("error_backoff", seconds=self.config.error_backoff)
                await asyncio.sleep(self.config.error_backoff)
            else:
                self.consecutive_errors = 0

    async def _renew(self, lease: OpenLease) -> None:
        while True:
            await asyncio.sleep(self.config.lease_renew_interval)
            lease.renew()

    async def _run_generation(self, gen: int, lease: OpenLease, obs: Observation) -> None:
        tel = self.telescope.grant(f"{self.name}/observe#{gen}")
        cam = self.camera.grant(f"{self.name}/observe#{gen}")
        self._event("observe_start", gen=gen, target=obs.name)
        task = asyncio.create_task(
            self._observe(gen, tel, cam, lease, obs), name=f"{self.name}-observe-{gen}"
        )
        renew = asyncio.create_task(self._renew(lease), name=f"{self.name}-renew-{gen}")
        revoked = asyncio.create_task(lease.revoked.wait())
        try:
            await asyncio.wait({task, revoked}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                await self._cancel_observe(gen, task, (tel, cam), lease.revoke_reason)
            else:
                exc = None if task.cancelled() else task.exception()
                if exc is None:
                    outcome = "complete"
                elif isinstance(exc, LeaseRevoked):
                    outcome = "revoked"  # stopped at lease.wait_open()
                else:
                    outcome = "error"
                self._outcome = outcome
                self._event(
                    "observe_finished", gen=gen, outcome=outcome, error=repr(exc) if exc else None
                )
        finally:
            for t in (renew, revoked):
                t.cancel()
            await asyncio.gather(renew, revoked, return_exceptions=True)
            if not task.done():  # we ourselves are being cancelled
                await self._cancel_observe(gen, task, (tel, cam), "sequencer stopping")
            for g in (tel, cam):
                g.revoke("generation over")
            report = await self._cleanup(gen)
            self.cleanups.append(report)

    async def _cancel_observe(
        self, gen: int, task: asyncio.Task, grants: tuple[Grant, ...], reason: str | None
    ) -> None:
        self._event("cancel_requested", gen=gen, reason=reason)
        for g in grants:  # fence first, so nothing slips out while cancelling
            g.revoke(f"cancelled: {reason}")
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self.config.cancel_ack_timeout)
        if not done:
            self.quarantined = True
            self._event("cancel_not_acknowledged", gen=gen, level="critical")
            log.critical("%s: observe task %d ignored cancellation", self.name, gen)
            return
        if not task.cancelled() and task.exception() is not None:
            outcome = f"error during cancel: {task.exception()!r}"
        else:
            outcome = "cancelled"
        self._event("observe_finished", gen=gen, outcome=outcome)

    # -- observing --------------------------------------------------------

    async def _wait_for(
        self, probe: Callable[[], Awaitable[Any]], done: Callable[[Any], bool], timeout: float
    ) -> Any:
        async with asyncio.timeout(timeout):
            while True:
                value = await probe()
                if done(value):
                    return value
                await asyncio.sleep(self.config.poll_interval)

    async def _observe(
        self, gen: int, tel: Grant, cam: Grant, lease: OpenLease, obs: Observation
    ) -> None:
        cfg = self.config
        if await tel.get("AtPark"):
            await tel.call("Unpark")
        await tel.set("Tracking", True)
        await lease.wait_open()
        ra, dec = obs.ra_hours, obs.dec_deg
        if ra is None or dec is None:
            lst = await tel.get("SiderealTime")
            lat = await tel.get("SiteLatitude")
            ra, dec = (lst - 0.5) % 24.0, max(-89.0, min(89.0, lat - 10.0))
        await tel.call("SlewToCoordinatesAsync", ra, dec)
        await self._wait_for(lambda: tel.get("Slewing"), lambda s: not s, cfg.slew_timeout)
        self._event("on_target", gen=gen, ra=ra, dec=dec)
        for i in range(obs.count):
            await cam.call("StartExposure", obs.exptime, True)
            self._event("exposure_started", gen=gen, index=i, exptime=obs.exptime)
            await self._wait_for(
                lambda: cam.get("ImageReady"), bool, obs.exptime + cfg.readout_margin
            )
            self.frames.append({"gen": gen, "index": i, "target": obs.name, "t": time.monotonic()})
            self._event("exposure_complete", gen=gen, index=i)

    # -- cleanup ----------------------------------------------------------

    async def _cleanup(self, gen: int) -> CleanupReport:
        report = CleanupReport(gen)
        t0 = time.monotonic()
        self._event("cleanup_start", gen=gen)
        cam = self.camera.grant(f"{self.name}/cleanup#{gen}")
        tel = self.telescope.grant(f"{self.name}/cleanup#{gen}")
        try:
            await asyncio.gather(
                self._cleanup_camera(cam, report), self._cleanup_telescope(tel, report)
            )
        finally:
            cam.revoke("cleanup done")
            tel.revoke("cleanup done")
        report.duration = time.monotonic() - t0
        self._event(
            "cleanup_done",
            gen=gen,
            aborted=report.aborted,
            parked=report.parked,
            errors=report.errors,
            duration=report.duration,
        )
        for err in report.errors:
            log.error("%s cleanup #%d: %s", self.name, gen, err)
        return report

    async def _cleanup_camera(self, cam: Grant, report: CleanupReport) -> None:
        try:
            state = await cam.get("CameraState")
        except DeviceError as exc:
            report.errors.append(f"camera state: {exc}")
            state = None  # unknown: abort anyway
        if state == CAMERA_IDLE:
            return
        member = "AbortExposure" if self.config.abort_policy == "abort" else "StopExposure"
        try:
            await cam.call(member)
            report.aborted = True
        except DeviceError as exc:
            report.errors.append(f"camera {member}: {exc}")

    async def _cleanup_telescope(self, tel: Grant, report: CleanupReport) -> None:
        cfg = self.config
        try:
            if await tel.get("Slewing"):
                await tel.call("AbortSlew")
        except DeviceError as exc:
            report.errors.append(f"telescope abort slew: {exc}")
        try:
            if not await tel.get("AtPark"):
                await tel.call("Park", timeout=cfg.park_timeout)
                await self._wait_for(lambda: tel.get("AtPark"), bool, cfg.park_timeout)
            report.parked = True
        except (DeviceError, TimeoutError) as exc:
            report.errors.append(f"telescope park: {exc!r}")
