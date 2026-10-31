"""One asyncio sequencer per enclosure, driving one or more telescopes.

Each "generation" of observing runs like this::

    lease = await supervisor.request_open(...)     # waits for safe + hold
    grants for this generation only, per telescope (telescope, camera)
    task  = observe every telescope                # one asyncio task; inside
                                                   # it, a TaskGroup with one
                                                   # task per telescope
    wait for: task finishes  OR  lease revoked
    on revoke: 1. revoke this generation's grants   (fence: nothing more is sent)
               2. task.cancel()
               3. wait (bounded) for the task to acknowledge by finishing;
                  if it does not, quarantine: never release the lease, so the
                  supervisor will not reopen and no new generation starts
    cleanup with *fresh* grants: abort every exposure, park every telescope
            (bounded, all in parallel)
    lease.release()                                # only now can a new lease exist

The sequencer never touches the enclosure: it has no dome grant at all. It
only learns "open" from ``lease.wait_open()`` and "stop" from
``lease.revoked``. Its ``supervisor`` is a :class:`LeaseSource`: the
in-process :class:`SafetySupervisor`, or a
:class:`~robofast_core.lease_client.RemoteSupervisor` for one in another
process (whose leases revoke themselves if the supervisor cannot be reached).

Several telescopes (design.md 3.5)
----------------------------------
An enclosure has one lease and one sequencer; each of its telescopes is a
:class:`TelescopeUnit` (a telescope and its camera) with its own grants and
its own task. Each telescope asks the :class:`~robofast_core.plan.PlanProvider`
for its next observation whenever it is free, so every choice is made from
scratch (never resume a stale action).

**One telescope's error is not the others' problem.** A telescope task
catches its own errors (a device error, a timeout, a refused slew, a bug):
it aborts its exposure and parks (bounded), then backs off for
``error_backoff`` while the others keep observing, and after
``max_consecutive_errors`` in a row it gives up for the night (an alarm) and
the rest carry on. Only when no telescope is left working does the
generation end, so the lease is released and the enclosure closes; a
telescope still waiting out a backoff then waits a full ``error_backoff``
with the enclosure closed before the next lease (so a dead camera cannot
cycle the enclosure open and shut). The night ends when every telescope has
finished its plan or given up.

Before any slew the telescope task asks the :class:`SlewGuard` (default:
allow everything). MEarth North's collision checker will be one; a refused
slew counts as that telescope's error. Parking is never vetted: it is the
safe move, and cleanup must always be able to make it.

Starting up after a crash
-------------------------
The sequencer's ``name`` is its lease holder identity: stable across
restarts, one per enclosure. Before its first lease, :meth:`Sequencer.run`

1. waits while a *live* lease of this holder is outstanding (a crashed
   predecessor's, until it lapses; or a twin's -- see below), since cleaning
   up under it would disturb it;
2. cleans up every telescope (abort the exposure, park), as after any
   generation -- always, because even with no lease outstanding a crash can
   leave a telescope unparked, and the supervisor grants nothing until all
   are parked;
3. if the predecessor's lease is still outstanding (revoked, unreleased),
   asks the supervisor to release it (``recover``); the supervisor refuses
   until the enclosure is confirmed closed, so this retries.

Two processes with the same name (a twin) are a deployment error that the
process manager prevents (one systemd unit per enclosure); the protocol stays
safe if it happens: a live lease is never recovered, recovery is idempotent
(the second ``recover`` is a no-op), and the supervisor's one-lease rule
means the twins take turns, each generation ending with its own cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from robofast_core.owner import DeviceError, DeviceOwner, Grant, GrantRevoked
from robofast_core.plan import Observation, PlanProvider, as_plan
from robofast_core.safety import LeaseRevoked, OpenLease, RecoveryRefused, SafetySupervisor
from robofast_core.timeline import Timeline

__all__ = [
    "AllowAllSlews",
    "CleanupReport",
    "LeaseSource",
    "Observation",
    "Sequencer",
    "SequencerConfig",
    "SlewDenied",
    "SlewGuard",
    "TelescopeUnit",
]

log = logging.getLogger(__name__)

CAMERA_IDLE = 0  # ASCOM CameraState.cameraIdle


class LeaseSource(Protocol):
    """Where open leases come from (a supervisor, local or remote)."""

    async def request_open(self, holder: str) -> OpenLease: ...

    async def lease_status(self) -> dict[str, Any] | None:
        """The outstanding lease's :meth:`OpenLease.state`, or None."""
        ...

    async def recover(self, lease_id: int, holder: str) -> bool:
        """Release a dead lease of ``holder``; raise :class:`RecoveryRefused`."""
        ...


class SlewDenied(Exception):
    """A :class:`SlewGuard` refused a slew; the message says why."""


class SlewGuard(Protocol):
    """Consulted before every slew (not before parking).

    MEarth North's collision checker will implement this: vet the target and
    the path against the other telescopes' positions, and raise
    :class:`SlewDenied` if they could collide. It may hold its own read-only
    grants on the telescopes to read their positions.
    """

    async def check_slew(self, telescope: str, ra_hours: float, dec_deg: float) -> None:
        """Return to allow the slew; raise :class:`SlewDenied` to refuse it."""
        ...


class AllowAllSlews:
    """The default :class:`SlewGuard`: telescopes that cannot collide."""

    async def check_slew(self, telescope: str, ra_hours: float, dec_deg: float) -> None:
        return None


@dataclass(eq=False)
class TelescopeUnit:
    """One telescope and its camera, as the sequencer drives them.

    The fields after ``camera`` are the sequencer's per-night state.
    """

    name: str
    telescope: DeviceOwner
    camera: DeviceOwner
    consecutive_errors: int = field(default=0, init=False)
    #: Gave up for the night after too many errors in a row.
    gave_up: bool = field(default=False, init=False)
    #: The plan has nothing more for it tonight.
    finished: bool = field(default=False, init=False)
    #: ``time.monotonic()`` before which it waits out an error backoff.
    retry_at: float = field(default=0.0, init=False)

    @property
    def active(self) -> bool:
        return not (self.gave_up or self.finished)


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
    #: After a telescope's error, wait this long before its next try; give up
    #: on that telescope for the night after this many errors in a row (see
    #: the module doc).
    error_backoff: float = 300.0
    max_consecutive_errors: int = 3
    #: Bound on one call to the plan provider's ``next_observation``.
    plan_timeout: float = 30.0
    #: At startup, how often to re-check a predecessor's lease or retry its
    #: recovery.
    recover_poll: float = 1.0


@dataclass
class CleanupReport:
    """What a cleanup achieved, by telescope name."""

    generation: int
    telescopes: list[str] = field(default_factory=list)
    #: Telescopes whose camera was mid-exposure and accepted the abort.
    aborted: list[str] = field(default_factory=list)
    #: Telescopes confirmed parked.
    parked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration: float = 0.0

    @property
    def all_parked(self) -> bool:
        return set(self.parked) == set(self.telescopes)


@dataclass(eq=False)
class _Generation:
    """Shared state of one generation's telescope tasks."""

    gen: int
    lease: OpenLease
    #: Telescopes observing (not finished, given up, or waiting out a backoff).
    working: set[str]
    #: Set once ``working`` is empty: the generation is over.
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    #: Telescopes that left the generation in the middle of a backoff.
    backing_off: set[str] = field(default_factory=set)
    errors: int = 0
    revoked: bool = False

    def leave(self, telescope: str) -> None:
        self.working.discard(telescope)
        if not self.working:
            self.idle.set()


class Sequencer:
    """The sequencer of one enclosure.

    ``telescopes`` lists its :class:`TelescopeUnit` objects; ``telescope``
    and ``camera`` are a shorthand for a single unit named after the
    telescope owner. ``plan`` is a :class:`~robofast_core.plan.PlanProvider`,
    or a function ``() -> Observation | None`` (called for each
    observation).
    """

    def __init__(
        self,
        name: str,
        *,
        supervisor: SafetySupervisor | LeaseSource,
        plan: PlanProvider | Callable[..., Observation | None],
        timeline: Timeline,
        telescopes: Sequence[TelescopeUnit] = (),
        telescope: DeviceOwner | None = None,
        camera: DeviceOwner | None = None,
        config: SequencerConfig | None = None,
        slew_guard: SlewGuard | None = None,
    ) -> None:
        units = list(telescopes)
        if telescope is not None or camera is not None:
            if telescope is None or camera is None or units:
                raise ValueError("give telescopes=[...], or both telescope= and camera=")
            units = [TelescopeUnit(telescope.name, telescope, camera)]
        if not units:
            raise ValueError("a sequencer needs at least one telescope")
        self.units = {u.name: u for u in units}
        if len(self.units) != len(units):
            raise ValueError("telescope names must be unique")
        self.name = name
        self.supervisor = supervisor
        self.plan = as_plan(plan)
        self.timeline = timeline
        self.config = config or SequencerConfig()
        self.slew_guard: SlewGuard = slew_guard or AllowAllSlews()
        if self.config.abort_policy not in ("abort", "stop"):
            raise ValueError(f"abort_policy {self.config.abort_policy!r}")
        self.generation = 0
        self.quarantined = False
        #: Every telescope gave up for the night.
        self.gave_up = False
        self.frames: list[dict[str, Any]] = []
        self.cleanups: list[CleanupReport] = []
        self.startup_cleanup: CleanupReport | None = None

    def _event(self, what: str, **detail: Any) -> None:
        self.timeline.event(self.name, what, **detail)

    @property
    def needs_operator(self) -> bool:
        """Quarantined, or some telescope gave up: an operator should look."""
        return self.quarantined or any(u.gave_up for u in self.units.values())

    # -- main loop --------------------------------------------------------

    async def run(self) -> None:
        await self._recover()
        while await self._wait_for_a_telescope():
            lease = await self.supervisor.request_open(self.name)
            self.generation += 1
            gen = None
            try:
                gen = await self._run_generation(self.generation, lease)
            finally:
                if self.quarantined:
                    lease.quarantine()
                    self._event("lease_held_quarantined", lease=lease.id)
                else:
                    lease.release()
                    self._event("lease_released", lease=lease.id)
            if self.quarantined:
                return
            # The enclosure stays closed for a full backoff (module doc).
            for name in gen.backing_off:
                unit = self.units[name]
                unit.retry_at = max(unit.retry_at, time.monotonic() + self.config.error_backoff)

    async def _wait_for_a_telescope(self) -> bool:
        """Wait until some telescope may observe; False when none ever will."""
        active = [u for u in self.units.values() if u.active]
        if not active:
            if all(u.gave_up for u in self.units.values()):
                self.gave_up = True
                self._event("night_over", why="every telescope gave up", level="critical")
            else:
                self._event("plan_exhausted")
            return False
        wait = min(u.retry_at for u in active) - time.monotonic()
        if wait > 0:
            self._event("error_backoff", seconds=wait)
            await asyncio.sleep(wait)
        return True

    async def _recover(self) -> None:
        """Clean up after a crashed predecessor before the first lease (see
        the module doc)."""
        poll = self.config.recover_poll
        while True:
            state = await self.supervisor.lease_status()
            mine = state is not None and state["holder"] == self.name
            if mine and state["live"]:
                await asyncio.sleep(poll)  # not lapsed yet, or a twin observing
            elif self.startup_cleanup is None:
                self.startup_cleanup = await self._cleanup(0, "startup_cleanup")
            elif not mine:
                return
            elif state["quarantined"]:
                self._event("predecessor_quarantined", lease=state["id"], level="critical")
                return  # request_open waits until an operator clears it
            else:
                try:
                    if await self.supervisor.recover(state["id"], self.name):
                        self._event("lease_recovered", lease=state["id"])
                    return
                except RecoveryRefused as exc:
                    log.info("%s: recovery of lease #%d: %s", self.name, state["id"], exc)
                    await asyncio.sleep(poll)

    async def _renew(self, lease: OpenLease) -> None:
        while True:
            await asyncio.sleep(self.config.lease_renew_interval)
            lease.renew()

    async def _run_generation(self, gen: int, lease: OpenLease) -> _Generation:
        units = [u for u in self.units.values() if u.active]
        holder = f"{self.name}/observe#{gen}"
        grants = {u.name: (u.telescope.grant(holder), u.camera.grant(holder)) for u in units}
        fence = tuple(g for pair in grants.values() for g in pair)
        state = _Generation(gen, lease, {u.name for u in units})
        self._event("observe_start", gen=gen, telescopes=[u.name for u in units])
        task = asyncio.create_task(
            self._observe_all(state, units, grants), name=f"{self.name}-observe-{gen}"
        )
        renew = asyncio.create_task(self._renew(lease), name=f"{self.name}-renew-{gen}")
        revoked = asyncio.create_task(lease.revoked.wait())
        try:
            await asyncio.wait({task, revoked}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                await self._cancel_observe(gen, task, fence, lease.revoke_reason)
            else:
                exc = None if task.cancelled() else task.exception()
                if exc is not None:
                    outcome = "error"
                elif state.revoked:
                    outcome = "revoked"  # stopped at lease.wait_open()
                else:
                    outcome = "error" if state.errors else "complete"
                self._event(
                    "observe_finished", gen=gen, outcome=outcome, error=repr(exc) if exc else None
                )
        finally:
            for t in (renew, revoked):
                t.cancel()
            await asyncio.gather(renew, revoked, return_exceptions=True)
            if not task.done():  # we ourselves are being cancelled
                await self._cancel_observe(gen, task, fence, "sequencer stopping")
            for g in fence:
                g.revoke("generation over")
            report = await self._cleanup(gen)
            self.cleanups.append(report)
        return state

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

    async def _observe_all(
        self,
        state: _Generation,
        units: list[TelescopeUnit],
        grants: dict[str, tuple[Grant, Grant]],
    ) -> None:
        async with asyncio.TaskGroup() as tg:
            for u in units:
                tel, cam = grants[u.name]
                tg.create_task(
                    self._telescope(state, u, tel, cam), name=f"{self.name}-{u.name}-{state.gen}"
                )

    async def _telescope(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, cam: Grant
    ) -> None:
        """One telescope's share of a generation. Never raises (but for
        cancellation): its errors are its own (module doc)."""
        try:
            while await self._wait_out_backoff(state, unit):
                try:
                    obs = await asyncio.wait_for(
                        self.plan.next_observation(unit.name), self.config.plan_timeout
                    )
                except Exception as exc:
                    await self._telescope_failed(state, unit, tel, cam, f"planning: {exc!r}")
                    continue
                if obs is None:
                    unit.finished = True
                    self._event("telescope_done", gen=state.gen, telescope=unit.name)
                    return
                taken: list[int] = []
                try:
                    await self._observe(state, unit, tel, cam, obs, taken)
                except asyncio.CancelledError:
                    self._report(unit, obs, "interrupted", taken)
                    raise
                except (LeaseRevoked, GrantRevoked):
                    # Stopped from outside; the generation is ending.
                    self._report(unit, obs, "interrupted", taken)
                    state.revoked = True
                    return
                except Exception as exc:
                    self._report(unit, obs, "failed", taken)
                    await self._telescope_failed(state, unit, tel, cam, repr(exc))
                else:
                    self._report(unit, obs, "complete", taken)
                    unit.consecutive_errors = 0
        finally:
            state.leave(unit.name)

    def _report(self, unit: TelescopeUnit, obs: Observation, outcome: str, taken: list) -> None:
        try:
            self.plan.report(unit.name, obs, outcome, len(taken))
        except Exception:  # the plan's bug must not end this telescope's night
            log.exception("%s: plan.report(%s, %s)", self.name, unit.name, obs.name)

    async def _wait_out_backoff(self, state: _Generation, unit: TelescopeUnit) -> bool:
        """Wait out ``unit``'s error backoff, unless every other telescope
        stops working first (then return False: the generation is over, and
        the enclosure should close while this one waits)."""
        if not unit.active:
            return False
        delay = unit.retry_at - time.monotonic()
        if delay <= 0:
            return True
        state.leave(unit.name)
        try:
            await asyncio.wait_for(state.idle.wait(), delay)
        except TimeoutError:
            state.working.add(unit.name)
            return True
        state.backing_off.add(unit.name)
        return False

    async def _telescope_failed(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, cam: Grant, error: str
    ) -> None:
        cfg = self.config
        unit.consecutive_errors += 1
        state.errors += 1
        self._event(
            "telescope_error",
            gen=state.gen,
            telescope=unit.name,
            error=error,
            errors=unit.consecutive_errors,
        )
        log.error("%s: %s: %s", self.name, unit.name, error)
        # Make this telescope safe now, with this generation's grants (so the
        # fence still covers it); the generation's cleanup repeats it.
        report = CleanupReport(state.gen, [unit.name])
        try:
            await self._cleanup_unit(unit.name, tel, cam, report)
        except GrantRevoked:
            return  # fenced: the generation is ending and its cleanup takes over
        self._event(
            "telescope_safed",
            gen=state.gen,
            telescope=unit.name,
            aborted=bool(report.aborted),
            parked=bool(report.parked),
            errors=report.errors,
        )
        if unit.consecutive_errors >= cfg.max_consecutive_errors:
            unit.gave_up = True
            self._event(
                "giving_up",
                telescope=unit.name,
                errors=unit.consecutive_errors,
                level="critical",
            )
        else:
            unit.retry_at = time.monotonic() + cfg.error_backoff
            self._event("telescope_backoff", telescope=unit.name, seconds=cfg.error_backoff)

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
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[int],
    ) -> None:
        cfg = self.config
        gen = state.gen
        self._event("target_start", gen=gen, telescope=unit.name, target=obs.name)
        if obs.light:
            if await tel.get("AtPark"):
                await tel.call("Unpark")
            await tel.set("Tracking", True)
            await state.lease.wait_open()
            ra, dec = obs.ra_hours, obs.dec_deg
            if ra is None or dec is None:
                lst = await tel.get("SiderealTime")
                lat = await tel.get("SiteLatitude")
                ra, dec = (lst - 0.5) % 24.0, max(-89.0, min(89.0, lat - 10.0))
            await self.slew_guard.check_slew(unit.name, ra, dec)
            await tel.call("SlewToCoordinatesAsync", ra, dec)
            await self._wait_for(lambda: tel.get("Slewing"), lambda s: not s, cfg.slew_timeout)
            self._event("on_target", gen=gen, telescope=unit.name, ra=ra, dec=dec)
        for i in range(obs.count):
            await cam.call("StartExposure", obs.exptime, obs.light)
            self._event(
                "exposure_started", gen=gen, telescope=unit.name, index=i, exptime=obs.exptime
            )
            await self._wait_for(
                lambda: cam.get("ImageReady"), bool, obs.exptime + cfg.readout_margin
            )
            taken.append(i)
            self.frames.append(
                {
                    "gen": gen,
                    "telescope": unit.name,
                    "index": i,
                    "target": obs.name,
                    "t": time.monotonic(),
                }
            )
            self._event("exposure_complete", gen=gen, telescope=unit.name, index=i)

    # -- cleanup ----------------------------------------------------------

    async def _cleanup(self, gen: int, what: str = "cleanup") -> CleanupReport:
        """Abort every exposure and park every telescope, in parallel."""
        report = CleanupReport(gen, list(self.units))
        t0 = time.monotonic()
        self._event(f"{what}_start", gen=gen)
        holder = f"{self.name}/cleanup#{gen}"
        grants = {
            u.name: (u.telescope.grant(holder), u.camera.grant(holder)) for u in self.units.values()
        }
        try:
            await asyncio.gather(
                *(self._cleanup_unit(name, tel, cam, report) for name, (tel, cam) in grants.items())
            )
        finally:
            for pair in grants.values():
                for g in pair:
                    g.revoke("cleanup done")
        report.duration = time.monotonic() - t0
        self._event(
            f"{what}_done",
            gen=gen,
            aborted=report.aborted,
            parked=report.parked,
            errors=report.errors,
            duration=report.duration,
        )
        for err in report.errors:
            log.error("%s cleanup #%d: %s", self.name, gen, err)
        return report

    async def _cleanup_unit(self, name: str, tel: Grant, cam: Grant, report: CleanupReport) -> None:
        await asyncio.gather(
            self._cleanup_camera(name, cam, report), self._cleanup_telescope(name, tel, report)
        )

    async def _cleanup_camera(self, name: str, cam: Grant, report: CleanupReport) -> None:
        try:
            state = await cam.get("CameraState")
        except DeviceError as exc:
            report.errors.append(f"{name}: camera state: {exc}")
            state = None  # unknown: abort anyway
        if state == CAMERA_IDLE:
            return
        member = "AbortExposure" if self.config.abort_policy == "abort" else "StopExposure"
        try:
            await cam.call(member)
            report.aborted.append(name)
        except DeviceError as exc:
            report.errors.append(f"{name}: camera {member}: {exc}")

    async def _cleanup_telescope(self, name: str, tel: Grant, report: CleanupReport) -> None:
        cfg = self.config
        try:
            if await tel.get("Slewing"):
                await tel.call("AbortSlew")
        except DeviceError as exc:
            report.errors.append(f"{name}: telescope abort slew: {exc}")
        try:
            if not await tel.get("AtPark"):
                await tel.call("Park", timeout=cfg.park_timeout)
                await self._wait_for(lambda: tel.get("AtPark"), bool, cfg.park_timeout)
            report.parked.append(name)
        except (DeviceError, TimeoutError) as exc:
            report.errors.append(f"{name}: telescope park: {exc!r}")
