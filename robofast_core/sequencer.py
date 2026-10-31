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

Preempting one telescope (targets of opportunity, design.md 3.7.1)
-------------------------------------------------------------------
:meth:`Sequencer.preempt` asks one telescope to drop its current
observation so that its next one (asked of the plan as usual) can be a ToO.
The plan calls it (``set_preempt_callback``); an operator API could too. It
touches only that telescope: no other telescope, no lease, no fence. Each
observation runs as its own child task of the telescope task, so it can be
cancelled alone. The policies:

* ``next`` -- nothing is interrupted; an idle telescope (one waiting out an
  :class:`~robofast_core.plan.Idle`) is woken to ask the plan again;
* ``now-after-readout`` -- stop at the next exposure boundary: an exposure
  in progress finishes and is read out (and counted), then the observation
  stops; if no exposure is in progress (slewing, settling), it stops at once;
* ``now`` -- stop at once: cancel the observation task, then abort the
  exposure (if the camera is not idle) and any slew.

A stopped observation is reported ``interrupted``, so the plan re-queues it.
The telescope is never parked by a preemption: the next target is a slew
away. The stop is bounded: if the observation task does not finish within
``cancel_ack_timeout`` of being cancelled, that telescope's grants are
revoked (fencing it alone) and it gives up for the night (an alarm).

**Safety wins.** A preemption is refused unless a generation is running
with its lease unrevoked. If the lease is revoked while a preemption is
being carried out, the generation's fence makes the preemption's own abort
commands fail with :class:`GrantRevoked`, the generation cancel reaches the
telescope task, and the generation's cleanup (abort and park, with fresh
grants) runs exactly as without the preemption.

Idle telescopes release the enclosure
-------------------------------------
A telescope whose plan answers :class:`~robofast_core.plan.Idle` stops
tracking and stays where it is, with the enclosure open: for a short wait
(a target about to rise, a gap between visits) that is cheaper than closing
and reopening, and an idle telescope cannot track into a limit. Once *every*
telescope still working in the generation has been idle for
``idle_release_after`` (default 15 min), the generation ends cleanly: the
telescopes leave it, the usual cleanup parks them, the lease is released
and the supervisor closes. The sequencer then waits in its closed phase.

The closed phase (no lease)
---------------------------
Before every lease the sequencer asks the plan what each telescope would do
now -- ``plan.peek(telescope)`` if the plan has it (no state changes), else
``next_observation``, whose answer is then held as that telescope's next
observation -- and:

* if any telescope has light work, it requests a lease (the supervisor's
  reopen hold applies as usual);
* else, if some telescope has dark frames (``light=False``: biases, darks),
  it takes them with the enclosure closed, in a *closed generation*: grants
  and telescope tasks as usual, but no lease; a telescope leaves it as soon
  as its next observation is not a dark frame, and the cleanup at its end
  is the usual one;
* else, if every telescope is idle, it waits (the shortest ``Idle``, or
  until :meth:`Sequencer.preempt` wakes a telescope, e.g. for a ToO) and asks
  again;
* when no telescope has anything more tonight, :meth:`Sequencer.run`
  returns.

Dark frames that a plan hands out during an open generation are taken
there (the enclosure is open for another telescope's light work anyway).

Filters and M3 ports
--------------------
An observation may name a ``filter`` and a ``port``. The filter is looked up
in the telescope's filter wheel's ``Names`` and the wheel moved to it (bounded
by ``filter_timeout``); the port is selected with the telescope's Alpaca
``Action(port_action, port)`` (PWI4-based CDK700s; bounded by
``port_timeout``). A telescope without a filter wheel, or without a
``port_action``, fails an observation that names one (an ordinary error of
that telescope), so nothing is ever observed through the wrong filter or
port.

Saving images
-------------
With an :class:`~robofast_core.imaging.Imaging` (``imaging=``), every frame
is saved: telescope telemetry is read into the value registry right after
``StartExposure``; once ``ImageReady``, the camera's state is read and the
image downloaded (still counted as "exposing", so a ``now-after-readout``
preemption waits for the download too), then queued for the telescope's
background writer, which overlaps the next exposure. An observation is
reported ``complete`` only once its frames are on disk, with their paths
(the plan's ``report(..., files=...)``). If a frame cannot be written, that
telescope gives up for the night (an alarm); the others observe on. See
:mod:`robofast_core.imaging`.

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
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from robofast_core.imaging import FrameWriteFailed, Imaging, PendingFrame
from robofast_core.owner import DeviceError, DeviceOwner, Grant, GrantRevoked
from robofast_core.plan import Idle, Observation, PlanProvider, as_plan
from robofast_core.safety import LeaseRevoked, OpenLease, RecoveryRefused, SafetySupervisor
from robofast_core.timeline import Timeline

__all__ = [
    "AllowAllSlews",
    "CleanupReport",
    "GuiderHook",
    "LeaseSource",
    "Observation",
    "Sequencer",
    "SequencerConfig",
    "SlewDenied",
    "SlewGuard",
    "TelescopeUnit",
    "Unsupported",
]

log = logging.getLogger(__name__)

CAMERA_IDLE = 0  # ASCOM CameraState.cameraIdle

#: Preemption policies, weakest first (module doc).
PREEMPT_POLICIES = ("next", "now-after-readout", "now")


class Preempted(Exception):
    """An observation was stopped by :meth:`Sequencer.preempt`."""


class _PreemptStuck(Exception):
    """A preempted observation did not stop in time; its telescope is fenced."""


class _PreemptAbortFailed(DeviceError):
    """A preempted observation stopped, but its abort commands failed."""


class Unsupported(DeviceError):
    """An observation needs a filter or M3 port this telescope cannot select."""


class LeaseSource(Protocol):
    """Where open leases come from (a supervisor, local or remote)."""

    async def request_open(self, holder: str) -> OpenLease: ...

    async def lease_status(self) -> dict[str, Any] | None:
        """The outstanding lease's :meth:`OpenLease.state`, or None."""
        ...

    async def recover(self, lease_id: int, holder: str) -> bool:
        """Release a dead lease of ``holder``; raise :class:`RecoveryRefused`."""
        ...


class GuiderHook(Protocol):
    """A telescope's guider (:class:`~robofast_core.guider.service.GuiderRemote`):
    started on target for an observation with ``guide``, checked before each
    exposure, stopped when the observation ends and, first, in every
    cleanup (before parking)."""

    async def start(self, **params: Any) -> Any:
        """Acquire and start guiding; raise if it cannot (bounded)."""
        ...

    async def check(self) -> None:
        """Raise if the guider has failed since :meth:`start`."""
        ...

    async def stop(self, reason: str) -> bool:
        """Stop guiding; bounded, never raises."""
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

    ``filterwheel`` is optional; ``port_action`` is the telescope's Alpaca
    ``Action`` that selects an M3 port (None: it cannot). The fields after
    them are the sequencer's per-night state.
    """

    name: str
    telescope: DeviceOwner
    camera: DeviceOwner
    filterwheel: DeviceOwner | None = None
    port_action: str | None = None
    #: Started for observations with ``guide`` (design.md 3.4, docs/guider.md).
    guider: GuiderHook | None = None
    consecutive_errors: int = field(default=0, init=False)
    #: Gave up for the night after too many errors in a row.
    gave_up: bool = field(default=False, init=False)
    #: The plan has nothing more for it tonight.
    finished: bool = field(default=False, init=False)
    #: ``time.monotonic()`` before which it waits out an error backoff.
    retry_at: float = field(default=0.0, init=False)
    #: A pending preemption (the strongest asked for since the telescope
    #: last asked the plan), when it was asked for, and why.
    preempt_policy: str | None = field(default=None, init=False)
    preempt_t: float = field(default=0.0, init=False)
    preempt_reason: str = field(default="", init=False)
    #: Set by :meth:`Sequencer.preempt`: wakes the telescope task.
    wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    #: An exposure has been started and not yet read out.
    exposing: bool = field(default=False, init=False)
    #: Waiting out an :class:`~robofast_core.plan.Idle`.
    idle: bool = field(default=False, init=False)
    #: ``time.monotonic()`` since when the plan has answered only Idle (None:
    #: it is not idle).
    idle_since: float | None = field(default=None, init=False)

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
    #: When every telescope of the generation has been idle this long, end
    #: the generation (park, release the lease) and wait without a lease
    #: (module doc); None: never.
    idle_release_after: float | None = 900.0
    #: Bounds on a filter wheel move and on an M3 port selection.
    filter_timeout: float = 60.0
    port_timeout: float = 60.0


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
    #: None for a closed generation (dark frames, no lease).
    lease: OpenLease | None
    #: Telescopes observing (not finished, given up, or waiting out a backoff).
    working: set[str]
    #: Set once ``working`` is empty: the generation is over.
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when every working telescope has been idle for
    #: ``idle_release_after``: they all leave the generation.
    idle_release: asyncio.Event = field(default_factory=asyncio.Event)
    #: Filter wheel grants, for the telescopes that have one.
    filterwheels: dict[str, Grant] = field(default_factory=dict)
    #: Telescopes that left the generation in the middle of a backoff.
    backing_off: set[str] = field(default_factory=set)
    errors: int = 0
    revoked: bool = False

    @property
    def closed(self) -> bool:
        return self.lease is None

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
        imaging: Imaging | None = None,
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
        #: The frame pipeline (module doc), or None: frames are not saved.
        self.imaging = imaging
        self._report_files = _accepts_files(self.plan.report)
        if self.config.abort_policy not in ("abort", "stop"):
            raise ValueError(f"abort_policy {self.config.abort_policy!r}")
        self.generation = 0
        self.quarantined = False
        #: Every telescope gave up for the night.
        self.gave_up = False
        self.frames: list[dict[str, Any]] = []
        self.cleanups: list[CleanupReport] = []
        self.startup_cleanup: CleanupReport | None = None
        #: Every preemption carried out: telescope, policy, latency (s from
        #: ``preempt()`` to the observation stopped and the camera safe).
        self.preemptions: list[dict[str, Any]] = []
        self._gen: _Generation | None = None
        #: Observation tasks that ignored cancellation (fenced; kept referenced).
        self._orphans: set[asyncio.Task] = set()
        hook = getattr(self.plan, "set_preempt_callback", None)
        if hook is not None:
            hook(self.preempt)

    def preempt(self, telescope: str, policy: str, reason: str = "") -> bool:
        """Ask ``telescope`` to stop its current observation (module doc).

        Returns whether the request was accepted: refused (False, and a
        ``preempt_refused`` event) unless a generation is running with its
        lease unrevoked and the telescope is working in it. Raises
        :class:`ValueError` for an unknown telescope or policy. Non-blocking;
        the stop happens in the telescope's task. A stronger policy upgrades a
        pending weaker one.
        """
        unit = self.units.get(telescope)
        gen = self._gen
        if unit is None or policy not in PREEMPT_POLICIES:
            raise ValueError(f"preempt({telescope!r}, {policy!r})")
        why = None
        if self.quarantined:
            why = "quarantined"
        elif gen is None:
            why = "no generation (enclosure closed)"
            # In the closed phase: make the telescope ask its plan again now.
            unit.wake.set()
        elif gen.revoked or (gen.lease is not None and gen.lease.revoked.is_set()):
            why = "lease revoked"
        elif telescope not in gen.working:
            why = "telescope not working (backoff, done or gave up)"
        if why is not None:
            self._event("preempt_refused", telescope=telescope, policy=policy, why=why)
            return False
        old = unit.preempt_policy
        if old is None or PREEMPT_POLICIES.index(policy) > PREEMPT_POLICIES.index(old):
            unit.preempt_policy = policy
            unit.preempt_reason = reason
            if old is None:
                unit.preempt_t = time.monotonic()
        self._event(
            "preempt_requested",
            gen=gen.gen,
            telescope=telescope,
            policy=unit.preempt_policy,
            reason=reason,
        )
        unit.wake.set()
        return True

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
            if not await self._closed_phase():
                if self.quarantined:
                    return
                continue
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

    # -- the closed phase ---------------------------------------------------

    async def _closed_phase(self) -> bool:
        """Ask the plan, without a lease, what each telescope would do now
        (module doc). True: some telescope has light work; request a lease.
        False: dark frames were taken, telescopes finished or went into a
        backoff, or an idle wait ended; the caller re-evaluates."""
        now = time.monotonic()
        ready = [u for u in self.units.values() if u.active and u.retry_at <= now]
        if not ready:
            return False
        for u in ready:
            u.wake.clear()
        answers: dict[str, Observation | Idle] = {}
        for u in ready:
            answer = await self._peek(u)
            if answer is not None:
                answers[u.name] = answer
        if any(isinstance(a, Observation) and a.light for a in answers.values()):
            return True
        darks = [self.units[n] for n, a in answers.items() if isinstance(a, Observation)]
        if darks:
            self.generation += 1
            await self._run_generation(self.generation, None, darks)
            return False
        idle = [a.seconds for a in answers.values() if isinstance(a, Idle)]
        if idle:
            seconds = max(0.0, min(idle))
            self._event("closed_idle", seconds=seconds, telescopes=sorted(answers))
            await _wait_any([self.units[n].wake for n in answers], seconds)
        return False

    async def _peek(self, unit: TelescopeUnit) -> Observation | Idle | None:
        """What ``unit``'s plan would do now, with no generation running.
        None: nothing more tonight (the unit is finished), or a plan error
        (the unit backs off, or gives up)."""
        try:
            answer, taken = await asyncio.wait_for(self._ask_peek(unit), self.config.plan_timeout)
        except Exception as exc:
            self._closed_error(unit, f"planning: {exc!r}")
            return None
        if taken and isinstance(answer, Observation):
            self._give_back(unit, answer)
        if answer is None:
            unit.finished = True
            self._event("telescope_done", gen=None, telescope=unit.name)
        return answer

    async def _ask_peek(self, unit: TelescopeUnit) -> tuple[Observation | Idle | None, bool]:
        """``plan.peek``, or, for a plan without it, ``next_observation``.
        Also returns whether the answer was taken from the plan (then an
        observation must be observed or given back)."""
        peek = getattr(self.plan, "peek", None)
        if peek is not None:
            return await peek(unit.name), False
        return await self.plan.next_observation(unit.name), True

    def _give_back(self, unit: TelescopeUnit, obs: Observation) -> None:
        """Return an observation taken but not started (reported
        ``interrupted`` with no frames, so the plan re-queues it): the next
        generation asks the plan afresh rather than act on a stale answer."""
        self._event("observation_returned", telescope=unit.name, target=obs.name)
        self._report(unit, obs, "interrupted", [])

    def _closed_error(self, unit: TelescopeUnit, error: str) -> None:
        """A plan error with no generation running: nothing to make safe."""
        cfg = self.config
        unit.consecutive_errors += 1
        self._event(
            "telescope_error",
            gen=None,
            telescope=unit.name,
            error=error,
            errors=unit.consecutive_errors,
        )
        log.error("%s: %s: %s", self.name, unit.name, error)
        if unit.consecutive_errors >= cfg.max_consecutive_errors:
            unit.gave_up = True
            self._event(
                "giving_up", telescope=unit.name, errors=unit.consecutive_errors, level="critical"
            )
        else:
            unit.retry_at = time.monotonic() + cfg.error_backoff
            self._event("telescope_backoff", telescope=unit.name, seconds=cfg.error_backoff)

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

    async def _run_generation(
        self, gen: int, lease: OpenLease | None, units: list[TelescopeUnit] | None = None
    ) -> _Generation:
        """One generation: under ``lease``, or, with None, a closed one (dark
        frames only, for ``units``)."""
        if units is None:
            units = [u for u in self.units.values() if u.active]
        holder = f"{self.name}/{'closed' if lease is None else 'observe'}#{gen}"
        grants = {u.name: (u.telescope.grant(holder), u.camera.grant(holder)) for u in units}
        state = _Generation(gen, lease, {u.name for u in units})
        state.filterwheels = {
            u.name: u.filterwheel.grant(holder) for u in units if u.filterwheel is not None
        }
        fence = (*(g for pair in grants.values() for g in pair), *state.filterwheels.values())
        self._gen = state
        what = "closed_start" if lease is None else "observe_start"
        self._event(what, gen=gen, telescopes=[u.name for u in units])
        task = asyncio.create_task(
            self._observe_all(state, units, grants), name=f"{self.name}-observe-{gen}"
        )
        waits: set[asyncio.Future] = {task}
        renew = revoked = None
        if lease is not None:
            renew = asyncio.create_task(self._renew(lease), name=f"{self.name}-renew-{gen}")
            revoked = asyncio.create_task(lease.revoked.wait())
            waits.add(revoked)
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                assert lease is not None
                await self._cancel_observe(gen, task, fence, lease.revoke_reason)
            else:
                exc = None if task.cancelled() else task.exception()
                if exc is not None:
                    outcome = "error"
                elif state.revoked:
                    outcome = "revoked"  # stopped at lease.wait_open()
                elif state.idle_release.is_set():
                    outcome = "idle"
                else:
                    outcome = "error" if state.errors else "complete"
                self._event(
                    "observe_finished", gen=gen, outcome=outcome, error=repr(exc) if exc else None
                )
        finally:
            self._gen = None  # no preemption from here on
            helpers = [t for t in (renew, revoked) if t is not None]
            for t in helpers:
                t.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
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
                if state.idle_release.is_set():
                    return  # every telescope idle: the generation is ending
                # A preemption asked for before this point is served by the
                # plan's answer (a ToO is already queued there).
                unit.preempt_policy = None
                unit.wake.clear()
                try:
                    obs = None
                    if state.closed:
                        # Dark frames only: leave as soon as the next is not one.
                        nxt, taken = await asyncio.wait_for(
                            self._ask_peek(unit), self.config.plan_timeout
                        )
                        if not (isinstance(nxt, Observation) and not nxt.light):
                            if taken and isinstance(nxt, Observation):
                                self._give_back(unit, nxt)
                            return
                        if taken:
                            obs = nxt
                    if obs is None:
                        obs = await asyncio.wait_for(
                            self.plan.next_observation(unit.name), self.config.plan_timeout
                        )
                except Exception as exc:
                    await self._telescope_failed(state, unit, tel, cam, f"planning: {exc!r}")
                    continue
                if obs is None:
                    unit.finished = True
                    unit.idle_since = None
                    self._event("telescope_done", gen=state.gen, telescope=unit.name)
                    return
                if isinstance(obs, Idle):
                    if state.closed:
                        return  # the plan changed its mind since the peek
                    try:
                        if await self._idle(state, unit, tel, obs):
                            return  # every telescope idle: release the enclosure
                    except (LeaseRevoked, GrantRevoked):
                        state.revoked = True
                        return
                    except Exception as exc:
                        await self._telescope_failed(state, unit, tel, cam, f"idle: {exc!r}")
                    continue
                unit.idle_since = None
                if state.closed and obs.light:
                    self._give_back(unit, obs)  # the plan changed its mind
                    return
                taken: list[Any] = []
                try:
                    await self._observe_preemptible(state, unit, tel, cam, obs, taken)
                except asyncio.CancelledError:
                    self._report(unit, obs, "interrupted", taken)
                    raise
                except (LeaseRevoked, GrantRevoked):
                    # Stopped from outside; the generation is ending.
                    self._report(unit, obs, "interrupted", taken)
                    state.revoked = True
                    return
                except Preempted:
                    self._report(unit, obs, "interrupted", taken)
                except _PreemptAbortFailed as exc:
                    self._report(unit, obs, "interrupted", taken)
                    await self._telescope_failed(state, unit, tel, cam, f"preemption: {exc}")
                except _PreemptStuck as exc:
                    self._report(unit, obs, "interrupted", taken)
                    unit.gave_up = True
                    self._event("giving_up", telescope=unit.name, error=str(exc), level="critical")
                    return
                except FrameWriteFailed as exc:
                    # Never observe on while frames are being lost (module doc).
                    self._report(unit, obs, "failed", taken)
                    await self._telescope_failed(state, unit, tel, cam, f"frame writing: {exc}")
                    if not unit.gave_up:
                        unit.gave_up = True
                        self._event(
                            "giving_up", telescope=unit.name, error=str(exc), level="critical"
                        )
                except Exception as exc:
                    self._report(unit, obs, "failed", taken)
                    await self._telescope_failed(state, unit, tel, cam, repr(exc))
                else:
                    self._report(unit, obs, "complete", taken)
                    unit.consecutive_errors = 0
        finally:
            unit.idle = unit.exposing = False
            state.leave(unit.name)

    async def _idle(self, state: _Generation, unit: TelescopeUnit, tel: Grant, idle: Idle) -> bool:
        """Wait out an :class:`Idle`: stop tracking (so an idle telescope
        cannot track into a limit), stay put, until woken or timed out.
        True if every telescope has now been idle for ``idle_release_after``
        (module doc): the telescope leaves the generation."""
        if unit.idle_since is None:
            unit.idle_since = time.monotonic()
        self._event("telescope_idle", gen=state.gen, telescope=unit.name, seconds=idle.seconds)
        if not await tel.get("AtPark") and await tel.get("Tracking"):
            await tel.set("Tracking", False)
        wait = max(0.0, idle.seconds)
        after = self.config.idle_release_after
        if after is not None:
            release_in = unit.idle_since + after - time.monotonic()
            if release_in > 0:
                wait = min(wait, release_in)
        unit.idle = True
        try:
            await _wait_any([unit.wake, state.idle_release], wait)
        finally:
            unit.idle = False
        if state.idle_release.is_set():
            return True
        if not self._idle_release_due(state, unit):
            return False
        now = time.monotonic()
        idle_for = {n: round(now - (self.units[n].idle_since or now), 1) for n in state.working}
        self._event("idle_release", gen=state.gen, idle_for=idle_for)
        log.info("%s: every telescope idle %s: releasing the enclosure", self.name, idle_for)
        state.idle_release.set()
        return True

    def _idle_release_due(self, state: _Generation, me: TelescopeUnit) -> bool:
        """Every working telescope has been idle for ``idle_release_after``:
        ``me`` (just back from its wait) and every other one (waiting now)."""
        after = self.config.idle_release_after
        if after is None or state.closed:
            return False
        now = time.monotonic()
        for name in state.working:
            u = self.units[name]
            if u is not me and not u.idle:
                return False
            if u.idle_since is None or now - u.idle_since < after:
                return False
        return True

    async def _observe_preemptible(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """:meth:`_observe` as a child task that :meth:`preempt` can stop
        (module doc). Raises what the observation raised, or
        :class:`Preempted`."""
        task = asyncio.create_task(
            self._observe(state, unit, tel, cam, obs, taken),
            name=f"{self.name}-{unit.name}-obs-{state.gen}",
        )
        try:
            while True:
                wake = asyncio.ensure_future(unit.wake.wait())
                try:
                    await asyncio.wait({task, wake}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
                if task.done():
                    return task.result()
                unit.wake.clear()
                policy = unit.preempt_policy
                if policy == "now" or (policy == "now-after-readout" and not unit.exposing):
                    await self._stop_observation(state, unit, tel, cam, task, policy)
                # Otherwise: "next" needs nothing here, and _observe stops
                # itself at the readout boundary.
        finally:
            if not task.done() and task not in self._orphans:
                task.cancel()
                await asyncio.wait({task})
            if task.done() and not task.cancelled():
                task.exception()  # retrieved: the caller already has its outcome
            if obs.guide and unit.guider is not None:
                await unit.guider.stop("observation ended")  # bounded, never raises

    async def _stop_observation(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        task: asyncio.Task,
        policy: str,
    ) -> None:
        """Cancel the observation task (bounded) and make the camera and the
        mount ready for the next target: abort the exposure and any slew, no
        park. Raises :class:`Preempted` (or returns, if the task finished on
        its own meanwhile: the caller then takes its result)."""
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self.config.cancel_ack_timeout)
        if not done:
            for g in (tel, cam):
                g.revoke("preempted observation ignored cancellation")
            self._orphans.add(task)
            task.add_done_callback(self._orphan_done)
            self._event(
                "preempt_not_acknowledged", gen=state.gen, telescope=unit.name, level="critical"
            )
            log.critical("%s: %s: preempted observation ignored cancellation", self.name, unit.name)
            raise _PreemptStuck(f"{unit.name}: preempted observation ignored cancellation")
        if not task.cancelled():
            return  # it finished (or failed) by itself first
        unit.exposing = False
        report = CleanupReport(state.gen, [unit.name])
        await asyncio.gather(
            self._cleanup_camera(unit.name, cam, report), self._abort_slew(unit.name, tel, report)
        )
        self._record_preemption(state, unit, policy, bool(report.aborted), report.errors)
        if report.errors:
            # The camera or mount did not take the abort: an ordinary error
            # of this telescope (abort, park, back off).
            raise _PreemptAbortFailed("; ".join(report.errors))
        raise Preempted(policy)

    def _orphan_done(self, task: asyncio.Task) -> None:
        self._orphans.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("%s: orphaned observation ended: %r", self.name, task.exception())

    def _record_preemption(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        policy: str,
        aborted: bool,
        errors: list[str] | None = None,
    ) -> None:
        latency = time.monotonic() - unit.preempt_t
        self.preemptions.append(
            {
                "gen": state.gen,
                "telescope": unit.name,
                "policy": policy,
                "reason": unit.preempt_reason,
                "aborted": aborted,
                "latency": latency,
            }
        )
        self._event(
            "preempted",
            gen=state.gen,
            telescope=unit.name,
            policy=policy,
            aborted=aborted,
            errors=errors or [],
            latency=latency,
        )

    def _report(self, unit: TelescopeUnit, obs: Observation, outcome: str, taken: list) -> None:
        files = [f.path for f in taken if isinstance(f, PendingFrame) and f.path is not None]
        try:
            if self._report_files and self.imaging is not None:
                self.plan.report(unit.name, obs, outcome, len(taken), files=files)
            else:
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
        taken: list[Any],
    ) -> None:
        cfg = self.config
        gen = state.gen
        imaging = self.imaging
        self._event("target_start", gen=gen, telescope=unit.name, target=obs.name)
        if obs.light and state.lease is None:
            raise RuntimeError(f"{obs.name}: light frames in a closed generation")
        if obs.light and await tel.get("AtPark"):
            await tel.call("Unpark")
        if obs.port is not None:
            await self._select_port(state, unit, tel, obs.port)
        if obs.filter is not None:
            await self._select_filter(state, unit, obs.filter)
        if obs.light:
            assert state.lease is not None
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
            if obs.guide:
                # Stopped in _observe_preemptible's finally and in cleanup.
                if unit.guider is None:
                    raise Unsupported(f"{unit.name} has no guider ({obs.name} asks to guide)")
                await unit.guider.start(dec=dec)
                self._event("guiding", gen=gen, telescope=unit.name)
        for i in range(obs.count):
            if imaging is not None:
                imaging.check(unit.name)  # a lost frame stops the telescope first
            if obs.guide and obs.light and unit.guider is not None:
                await unit.guider.check()  # a failed guider is this telescope's error
            # From here until it is read out (downloaded, with imaging), a
            # "now-after-readout" preemption waits for this exposure.
            unit.exposing = True
            try:
                host_start = datetime.now(UTC)
                await cam.call("StartExposure", obs.exptime, obs.light)
                self._event(
                    "exposure_started", gen=gen, telescope=unit.name, index=i, exptime=obs.exptime
                )
                exposure = None
                if imaging is not None:
                    exposure = await imaging.exposure_started(
                        unit.name,
                        tel,
                        obs,
                        index=i,
                        host_start=host_start,
                        sequencer=self.name,
                        generation=gen,
                        ids=self._ids(unit.name),
                    )
                await self._wait_for(
                    lambda: cam.get("ImageReady"), bool, obs.exptime + cfg.readout_margin
                )
                frame: Any = i
                if exposure is not None:
                    assert imaging is not None
                    frame = await imaging.exposure_done(unit.name, cam, exposure)
            finally:
                unit.exposing = False
            taken.append(frame)
            self.frames.append(
                {
                    "gen": gen,
                    "telescope": unit.name,
                    "index": i,
                    "target": obs.name,
                    "t": time.monotonic(),
                    **({"image": frame} if isinstance(frame, PendingFrame) else {}),
                }
            )
            self._event("exposure_complete", gen=gen, telescope=unit.name, index=i)
            policy = unit.preempt_policy
            if policy in ("now-after-readout", "now") and i + 1 < obs.count:
                self._record_preemption(state, unit, policy, aborted=False)
                raise Preempted(policy)
        if imaging is not None and taken:
            # Complete means on disk: the plan records the paths.
            await imaging.flush(unit.name)

    def _ids(self, telescope: str) -> dict[str, Any]:
        """The plan's request ids for what ``telescope`` is observing (the
        dispatch scheduler's ``current()``), for the FITS header."""
        fn = getattr(self.plan, "current", None)
        try:
            cur = fn().get(telescope) if fn is not None else None
        except Exception:
            log.exception("%s: plan.current()", self.name)
            cur = None
        if not cur:
            return {}
        return {k: cur.get(k) for k in ("group", "request", "configuration")}

    async def _select_port(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, port: str
    ) -> None:
        if unit.port_action is None:
            raise Unsupported(f"{unit.name} cannot select M3 port {port!r} (no port_action)")
        await tel.call("Action", unit.port_action, port, timeout=self.config.port_timeout)
        self._event("port_selected", gen=state.gen, telescope=unit.name, port=port)
        if self.imaging is not None:
            self.imaging.port_selected(unit.name, port)

    async def _select_filter(self, state: _Generation, unit: TelescopeUnit, name: str) -> None:
        fw = state.filterwheels.get(unit.name)
        if fw is None:
            raise Unsupported(f"{unit.name} has no filter wheel (filter {name!r})")
        names = list(await fw.get("Names"))
        if name not in names:
            raise Unsupported(f"{unit.name}: no filter {name!r} in {names}")
        position = names.index(name)
        if await fw.get("Position") != position:
            await fw.set("Position", position)
            # Position reads -1 while the wheel moves.
            await self._wait_for(
                lambda: fw.get("Position"), lambda p: p == position, self.config.filter_timeout
            )
        self._event("filter_selected", gen=state.gen, telescope=unit.name, filter=name)
        if self.imaging is not None:
            self.imaging.filter_selected(unit.name, name, position)

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

    async def _abort_slew(self, name: str, tel: Grant, report: CleanupReport) -> None:
        try:
            if await tel.get("Slewing"):
                await tel.call("AbortSlew")
        except DeviceError as exc:
            report.errors.append(f"{name}: telescope abort slew: {exc}")

    async def _cleanup_telescope(self, name: str, tel: Grant, report: CleanupReport) -> None:
        cfg = self.config
        unit = self.units.get(name)
        if unit is not None and unit.guider is not None:
            # The guide loop moves the mount: stop it before parking.
            if not await unit.guider.stop("cleanup"):
                report.errors.append(f"{name}: guider did not confirm stop")
        await self._abort_slew(name, tel, report)
        try:
            if not await tel.get("AtPark"):
                await tel.call("Park", timeout=cfg.park_timeout)
                await self._wait_for(lambda: tel.get("AtPark"), bool, cfg.park_timeout)
            report.parked.append(name)
        except (DeviceError, TimeoutError) as exc:
            report.errors.append(f"{name}: telescope park: {exc!r}")


def _accepts_files(report: Callable[..., Any]) -> bool:
    """Whether a plan's ``report`` takes the ``files`` keyword."""
    try:
        params = inspect.signature(report).parameters
    except (TypeError, ValueError):
        return False
    return "files" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


async def _wait_any(events: Sequence[asyncio.Event], timeout: float) -> None:
    """Wait until any of ``events`` is set, or for ``timeout`` seconds."""
    if any(e.is_set() for e in events):
        return
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
