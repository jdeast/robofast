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
:class:`~robofast.lease_client.RemoteSupervisor` for one in another
process (whose leases revoke themselves if the supervisor cannot be reached).

Several telescopes (design.md 3.5)
----------------------------------
An enclosure has one lease and one sequencer; each of its telescopes is a
:class:`TelescopeUnit` (a telescope and its camera) with its own grants and
its own task. Each telescope asks the :class:`~robofast.plan.PlanProvider`
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

**What a telescope cannot do is not its error.** An observation that needs
something this telescope lacks -- a moving target on a mount that cannot
set tracking rates, a filter, M3 port, lamp or guider it does not have --
raises :class:`Unsupported` (one path per feature: the device says so,
nothing works around it). That is a property of the (observation,
telescope) pair, not a fault: the observation is reported ``unsupported``
(with the reason, to a plan that has ``unsupported(telescope, obs,
reason)``), the telescope is made ready for its next target as after a
preemption (exposure, slew, guider, lamps, ADC stopped; no park), its
error count is untouched, there is no backoff, and it asks the plan for its
next observation at once. The plan must not hand the same observation to
the same telescope again (the dispatch scheduler offers it to the others,
and refuses it if none can); if it does, the repeat counts as an error of
that telescope, so a plan that ignores ``unsupported`` cannot spin. Where a
capability can be read from the device the plan is told before the first
observation (``set_capabilities(telescope, {...})``: ``tracking_rates``,
the mount's ``CanSetRightAscensionRate`` and ``CanSetDeclinationRate``), so
it can avoid asking at all; the runtime :class:`Unsupported` stays the
backstop.

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
  :class:`~robofast.plan.Idle`) is woken to ask the plan again;
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
A telescope whose plan answers :class:`~robofast.plan.Idle` stops
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
``port_action``, reports an observation that names one ``unsupported``
(not an error of that telescope: see "Several telescopes"), so nothing is
ever observed through the wrong filter or port.

Saving images
-------------
With an :class:`~robofast.imaging.Imaging` (``imaging=``), every frame
is saved: telescope telemetry is read into the value registry right after
``StartExposure``; once ``ImageReady``, the camera's state is read and the
image downloaded (still counted as "exposing", so a ``now-after-readout``
preemption waits for the download too), then queued for the telescope's
background writer, which overlaps the next exposure. An observation is
reported ``complete`` only once its frames are on disk, with their paths
(the plan's ``report(..., files=...)``). If a frame cannot be written, that
telescope gives up for the night (an alarm); the others observe on. See
:mod:`robofast.imaging`.

Camera cooling
--------------
A telescope with a ``cooling`` entry (:class:`~robofast.cooling.CameraCooling`;
docs/cooling.md) has its camera kept at its setpoint: the cooler is set
whenever the camera's owner connects it afresh (its ``on_connect`` hook:
startup, which touches each cooled camera once the mode lets the sequencer
command, a timeout, every recovery rung) and, found off before an exposure,
turned back on (rate-limited, an alarm). Light frames wait for the CCD to
settle, but never past ``settle_timeout`` after the cooler was set; then
they go ahead with a warning and ``COOLOK = F`` in the header.

Spectra (fiber-fed, several telescopes)
---------------------------------------
With a ``spectrograph`` (the enclosure's spectrograph service, a
:class:`SpectrographHook` built by its instrument type: e.g.
:class:`~robofast.devices.kiwispec_service.KiwiSpecRemote`), an
observation of ``kind="spectrum"`` is one or more spectra of its target,
fed by the telescopes of ``obs.spectrum["feed"]`` (docs/rv_night.md). Each
of them, in its own telescope task, slews to the target, locates the fiber
with the backlight (``locate_fiber``; one telescope at a time, since the
backlight lights the whole bundle; on failure it guides on the configured
fiber position, as legacy did) and starts guiding. The *leader* (the
telescope the plan gave the spectrum to; ``obs.spectrum["leader"]``, or the
first to start it) opens a session; the others (members) join it once they
are guiding. The leader waits until every expected member is guiding -- or
``spectrum_ready_timeout``, then starts with those that are (a member that
is late does not join a spectrum already exposing) -- and asks the
spectrograph for each frame (``expose``: the telescopes feeding it, and
the instrument's own parameters, ``obs.spectrum["params"]`` -- KiwiSpec's
``iodine``, and ``target_counts`` for exposure-meter termination with
``exptime`` the maximum). Never without guiding: a spectrum starts only
with at least one telescope guiding on the fiber.

While it exposes, every feeding telescope checks its guider and its mount
every ``spectrum_check_interval``. One that fails **drops out** (it is made
safe and backs off as after any error; a dropped leader keeps running the
exposures and its error is handled once the spectrum ends): the spectrum
goes on with the rest and its record says who dropped and why. If every
telescope drops out, the spectrograph job is aborted and the observation
fails. Only the leader's report changes the plan's request; a member's
report only ends its part.

Each spectrum's metadata -- the telescopes, each one's guide statistics,
the exposure meter's flux-weighted midpoint, the target's astrometry, the
site, and the BJD_TDB and barycentric correction at the midpoint -- goes to
a sidecar file (:class:`~robofast.spectra.SpectrumRecorder`); the raw
FITS file on the spectrograph PC is never rewritten.

``kind="spectrograph_calibration"`` observations (``light=False``: biases,
darks, and any lamp frames the spectrograph has; MINERVA's KiwiSpec has
none) are one ``POST /calibrate`` job, taken like dark frames in the closed
phase with the enclosure closed.

**Cleanup aborts the spectrum first.** Every cleanup (lease revoked,
generation over, sequencer stopping) aborts a spectrograph job this
sequencer started (bounded; :meth:`KiwiSpecRemote.abort` never raises),
aborts the cameras and stops the guiders and slews in parallel, and parks
only once the spectrograph's abort has ended or ``spectrograph_abort_wait``
has passed. The generation's fence also stops any new spectrograph job.
A preempted leader (policy ``now``) aborts its spectrum; a preempted member
just drops out.

Spectrograph health
-------------------
A spectrum the spectrograph cannot take is never the telescope's error (no
park, no backoff, no give-up count): :class:`SpectrographBusy` (another job
holds it -- the other enclosure's, after the service's bounded queue wait)
is reported ``interrupted`` and nothing is counted; :class:`SpectrographFault`
(refused, failed, overran, or not answering) is reported ``interrupted`` to
a plan that has ``set_spectrograph_available`` (else ``failed``) and
counted. After ``spectrograph_max_failures`` faults in a row the
spectrograph is marked down (``spectrograph_down``, critical; the status
snapshot's ``spectrograph.down``), the plan is told, and spectrum and
spectrograph-calibration observations fail at once without slewing; imaging
goes on. While down its status is checked every
``spectrograph_recovery_interval``; a passing check (with
``spectrograph_auto_recover``) or the operator's :meth:`Sequencer.clear_spectrograph`
marks it up. docs/rv_night.md.

Config-defined sequences
------------------------
An observation of ``kind="sequence"`` runs one of the enclosure config's
``sequences`` (``sequences=``, from
:func:`robofast.sequences.sequences_from_config`; docs/sequences.md):
checked against the devices first (reads only), then its steps through the
generation's grants, then its cleanup, always -- every lamp it switched on
off at once, the rest (the calibration stage out, ...) with grants of its
own, in the background after a cancellation, waited for by the
generation's cleanup before it parks. Its ``light`` is its definition's.

Operator park
-------------
:meth:`Sequencer.request_park` (the observer console's ``park``, through
:class:`~robofast.console.hold.OperatorHold`, which also holds the
telescope and stops its observation) parks one telescope as soon as it is
idle, with its own grant: guider stopped, slew aborted, ``Park``. Never the
roof, the lease or another telescope.

Night names and time stamps
---------------------------
``clock`` (:mod:`robofast.clock`; default the host's) is the time of
every night name and stamp the sequencer makes: ``DATE-CMD`` and the frame's
night, the frame pipeline's ``DATE`` and index, the spectrum's ``night``
(sent to the spectrograph, which names the raw file with it) and sidecar,
the autofocus results.

Starting up after a crash
-------------------------
The sequencer's ``name`` is its lease holder identity: stable across
restarts, one per enclosure. Before its first lease, :meth:`Sequencer.run`

1. waits while a *live* lease of this holder is outstanding (a crashed
   predecessor's, until it lapses; or a twin's -- see below), since cleaning
   up under it would disturb it;
2. cleans up every telescope (abort the exposure, park), as after any
   generation, and aborts a spectrum it finds running for its telescopes --
   always, because even with no lease outstanding a crash can
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
import functools
import inspect
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

import numpy as np

from robofast import pointmodel, sky
from robofast.adc import ADCSwitches
from robofast.alpaca_client import InvalidOperationException
from robofast.autofocus import (
    FOCUS_CONTEXT,
    Autofocus,
    AutofocusSetup,
    FocusStore,
    publish_result,
    term_source,
)
from robofast.camera_state import CAMERA_IDLE, image_ready
from robofast.cancel import stop_task, stop_tasks
from robofast.clock import Clock, NightRule, real_clock
from robofast.cooling import CameraCooling, CoolingStatus
from robofast.escalation import DEFAULT_SAFE, AgentResult, Incident, fault_signature, run_agent
from robofast.imagebytes import ImageArray, fits_order
from robofast.imaging import FrameWriteFailed, Imaging, PendingFrame
from robofast.instruments import SpectrographBusy, SpectrographError, SpectrographFault
from robofast.kinds import OBSERVATION_KINDS, KindRun
from robofast.lamps import LampSetup, lamps_off, switch_status
from robofast.modes import ModeGate
from robofast.orbit import Orbit
from robofast.owner import DeviceError, DeviceOwner, Grant, GrantRevoked, Unsupported
from robofast.plan import Idle, Observation, PlanProvider, as_plan
from robofast.pointmodel import PointingCorrector
from robofast.recovery import MountAccess, RecoveryLadder, RecoveryResult
from robofast.registry import ValueRegistry
from robofast.rotcal import RotatorSetup
from robofast.safety import LeaseRevoked, OpenLease, RecoveryRefused, SafetySupervisor
from robofast.skyflats import FlatConfig
from robofast.spectra import SpectrumRecorder
from robofast.timeline import Timeline

__all__ = [
    "AllowAllSlews",
    "CleanupReport",
    "GuiderHook",
    "LeaseSource",
    "Observation",
    "ObservationFailed",
    "Sequencer",
    "SequencerConfig",
    "SlewDenied",
    "SlewGuard",
    "SpectrographBusy",
    "SpectrographError",
    "SpectrographFault",
    "SpectrographHook",
    "SpectrumFailed",
    "TelescopeUnit",
    "Unsupported",
]

log = logging.getLogger(__name__)


def _follow_orbit(obs: Observation) -> tuple[Orbit, Any]:
    """A moving target's orbit and site (``obs.follow``)."""
    assert obs.follow is not None
    st = obs.follow["site"]
    return _parsed_follow(
        tuple(sorted(obs.follow["orbit"].items())),
        (float(st["latitude"]), float(st["longitude"]), float(st.get("elevation", 0.0))),
    )


@functools.lru_cache(maxsize=64)
def _parsed_follow(
    orbit: tuple[tuple[str, Any], ...], site: tuple[float, float, float]
) -> tuple[Orbit, Any]:
    return Orbit(**dict(orbit)), sky.Site(*site).location


def _follow_place(obs: Observation, when: datetime) -> tuple[float, float]:
    """A moving target's place at ``when``: (ra hours, dec deg)."""
    orbit, location = _follow_orbit(obs)
    ra, dec = orbit.place(when, location)
    return ra / 15.0, dec


def _follow_rate(obs: Observation, when: datetime) -> tuple[float, float]:
    """A moving target's motion at ``when``: (dRA cos Dec, dDec) arcsec/s."""
    orbit, location = _follow_orbit(obs)
    return orbit.rate(when, location)


#: A pointing run's settings (docs/pointing_model.md): the telescope's
#: ``pointing.run`` entry and an observation's ``pointing`` override them.
POINTING_RUN_DEFAULTS: dict[str, Any] = {
    "min_alt": 25.0,
    "max_alt": 85.0,
    "moon_distance": 15.0,
    "max_hour_angle": None,
    "min_dec": None,
    "max_dec": None,
    "settle": 2.0,
    "min_points": 12,
    "significance": 4.0,
    #: Fixed TPOINT terms (None: chosen by pointmodel.select_terms); a first
    #: calibration from a few points fixes the zero points (docs/pointing_model.md).
    "terms": None,
}

#: What a target's acquisition entry passes to the guider (docs/acquisition.md).
ACQ_KEYS = ("mode", "fallback", "companions", "tolerance")

#: Preemption policies, weakest first (module doc).
PREEMPT_POLICIES = ("next", "now-after-readout", "now")


class Preempted(Exception):
    """An observation was stopped by :meth:`Sequencer.preempt`."""


class _PreemptStuck(Exception):
    """A preempted observation did not stop in time; its telescope is fenced."""


class _PreemptAbortFailed(DeviceError):
    """A preempted observation stopped, but its abort commands failed."""


class ObservationFailed(Exception):
    """The observation failed but the telescope is fine (an autofocus that
    found no focus): reported ``failed``, with no error backoff."""


class LampOff(DeviceError):
    """The lamp of a lamp observation went off before its frames were taken
    (its switch's max-on limit, or switched off by hand)."""


class ManualRefused(Exception):
    """A manual command that cannot be carried out now (said to the operator)."""


class SpectrumFailed(Exception):
    """A spectrum could not be taken: no telescope of its feed was guiding
    when it was due to start, or every one dropped out during it."""


class _LeaderDropped(Exception):
    """The leading telescope failed while its spectrum went on without it:
    the spectrum's outcome is reported, then the telescope's error handled."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(repr(error))
        self.error = error


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
    """A telescope's guider (:class:`~robofast.guider.service.GuiderRemote`):
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

    # Optional, for the recovery ladder's ``guider`` device
    # (docs/recovery.md): ``async reconnect()`` (close and open the guide
    # camera again; raise if it cannot) and ``async status()``.


class SpectrographHook(Protocol):
    """The enclosure's spectrograph service: a client built by its
    instrument type (:mod:`robofast.instruments`; KiwiSpec's is
    :class:`~robofast.devices.kiwispec_service.KiwiSpecRemote`). Its
    methods raise :class:`SpectrographBusy` when another job holds the
    spectrograph and any other :class:`SpectrographError` when it fails (or
    subclasses of them: :mod:`robofast.instruments`). It may have
    ``record_keys``: keys of a frame's result copied to the top of each
    spectrum's sidecar record."""

    async def expose(self, **frame: Any) -> dict[str, Any]:
        """Start one frame; returns the job (``id``, ``state``)."""
        ...

    async def calibrate(
        self, frames: list[dict[str, Any]], night: str | None = None
    ) -> dict[str, Any]:
        """Start a calibration sequence (raw files named for ``night``);
        returns the job."""
        ...

    async def job(self, job_id: int, wait: float = 0.0) -> dict[str, Any]:
        """The job's state (``state``, ``results``, ``error``), waiting up to
        ``wait`` s for it to finish."""
        ...

    async def abort(self, reason: str, *, timeout: float | None = None) -> bool:
        """Abort the running job; bounded, never raises; True if nothing is
        left running."""
        ...

    async def status(self, refresh: bool = False) -> dict[str, Any]: ...


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
    """The default :class:`SlewGuard`: vets nothing, for telescopes the site has
    judged cannot collide."""

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
    #: The TRES front end's calibration stage: an Alpaca FilterWheel whose
    #: ``Names`` are its positions (docs/tres_frontend.md); optional.
    calstage: DeviceOwner | None = None
    #: The spectrograph's calibration lamps (docs/pulizzi.md): the lamp
    #: switch (robofast's lamp Switch server) and its setup; both needed for
    #: ``kind="arc"`` and ``kind="lamp_flat"`` observations.
    lamps: DeviceOwner | None = None
    lamp_setup: LampSetup | None = None
    #: Started for observations with ``guide`` (design.md 3.4, docs/guider.md).
    guider: GuiderHook | None = None
    #: The focuser the sequencer moves (autofocus), and the autofocus
    #: setup; both needed for ``kind="autofocus"`` observations.
    focuser: DeviceOwner | None = None
    autofocus: AutofocusSetup | None = None
    consecutive_errors: int = field(default=0, init=False)
    #: The observation it last reported ``unsupported`` (cleared by any other
    #: outcome): the same one again counts as an error (module doc).
    last_unsupported: str | None = field(default=None, init=False)
    #: Its capabilities have been read and told to the plan (or tried).
    capabilities_told: bool = field(default=False, init=False)
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
    #: Following a moving target: the mount's RA/Dec rates were set (they
    #: are set back to 0, sidereal, when the observation ends, and before
    #: any later slew if that failed).
    follow_rates: bool = field(default=False, init=False)
    #: Waiting out an :class:`~robofast.plan.Idle`.
    idle: bool = field(default=False, init=False)
    #: ``time.monotonic()`` since when the plan has answered only Idle (None:
    #: it is not idle).
    idle_since: float | None = field(default=None, init=False)
    #: An autofocus was cut short: the focuser position to return to before
    #: the next light observation (None: nothing to undo).
    focus_restore: float | None = field(default=None, init=False)
    #: Every autofocus result tonight (:class:`~robofast.autofocus.AutofocusResult`).
    autofocus_results: list[Any] = field(default_factory=list, init=False)
    #: The spectrum this telescope leads or feeds (its id), while it does.
    spectrum: str | None = field(default=None, init=False)
    #: An operator asked to park it now (why; :meth:`Sequencer.request_park`).
    park_requested: str | None = field(default=None, init=False)
    #: robofast's pointing model (docs/pointing_model.md): applied to every
    #: slew, measured on every solved frame, built by ``kind="pointing_model"``.
    pointing: PointingCorrector | None = None
    #: Alarms raised tonight (the status snapshot carries them to the console
    #: and the notifier).
    alarms: list[str] = field(default_factory=list, init=False)
    #: The generation in which the mount's own pointing model was switched off.
    vendor_off_gen: int | None = field(default=None, init=False)
    #: The rotator the sequencer may move, and the PA calibration setup
    #: (docs/rotcal.md): ``kind="rotator_calibration"``.
    rotator: DeviceOwner | None = None
    pa_calibration: RotatorSetup | None = None
    #: The recovery ladder (docs/recovery.md): what to try when one of its
    #: devices fails, before giving up for the night.
    recovery: RecoveryLadder | None = None
    #: Twilight sky flats (docs/skyflats.md): ``kind="sky_flat"``.
    flats: FlatConfig | None = None
    #: The atmospheric dispersion corrector's tracking switch (an Alpaca
    #: Switch, robofast's ADC server: docs/adc.md); optional. Tracking on
    #: once on target for light frames, off (prisms neutral) at cleanup.
    adc: DeviceOwner | None = None
    #: Its switches by name (robofast/adc.py; the telescope's ``adc``
    #: entry may rename them).
    adc_switches: ADCSwitches = field(default_factory=ADCSwitches)
    #: How the guider identified the last target (docs/acquisition.md).
    acquisition: dict[str, Any] | None = field(default=None, init=False)
    #: Keeps the camera cooled (docs/cooling.md); None: robofast leaves
    #: its cooler alone.
    cooling: CameraCooling | None = None
    #: The incident being escalated (an agent working on one of its devices,
    #: robofast/escalation.py): out of the plan until it ends.
    escalation: Incident | None = field(default=None, init=False)
    #: Every incident tonight, finished or not (the status snapshot carries
    #: the finished ones' summaries to the console and the notifier).
    incidents: list[Incident] = field(default_factory=list, init=False)
    #: Set when an escalation ends: wakes whatever waits for it to rejoin.
    rejoin: asyncio.Event = field(default_factory=asyncio.Event, init=False)

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
    #: Bound on a calibration-stage move (the legacy TRES window waits 60 s).
    calstage_timeout: float = 60.0
    #: How long to wait for the ADC to settle on a new target (docs/adc.md);
    #: past it the frames are taken anyway, with a warning.
    adc_settle_timeout: float = 60.0
    #: Spectra (module doc, "Spectra"): how long the leading telescope waits
    #: for the rest of the feed to be guiding before it starts with those
    #: that are; how often each feeding telescope checks its guider and its
    #: mount during a spectrum; the spectrograph job poll; the bound on one
    #: spectrum beyond its exposure time (readout, saving); locate the fiber
    #: with the backlight before guiding (bounded by ``locate_timeout``; on
    #: failure the guider's configured fiber position is used, as legacy
    #: did); and how long a cleanup waits for the spectrograph to abort
    #: before it parks anyway.
    spectrum_ready_timeout: float = 300.0
    spectrum_check_interval: float = 2.0
    spectrum_poll: float = 2.0
    spectrum_margin: float = 180.0
    locate_fiber: bool = True
    locate_timeout: float = 60.0
    spectrograph_abort_wait: float = 10.0
    #: Spectrograph health (module doc): after this many spectrograph
    #: faults in a row it is marked down (an alarm; no more spectra are
    #: scheduled, imaging goes on); while down, its status is checked every
    #: ``spectrograph_recovery_interval`` s and, with
    #: ``spectrograph_auto_recover``, it is marked up again once a check
    #: passes (else only the operator's clear does). How long a spectrum
    #: request waits in the spectrograph service's queue while another
    #: enclosure holds it (docs/kiwispec.md, "Sharing the spectrograph").
    spectrograph_max_failures: int = 3
    spectrograph_recovery_interval: float = 300.0
    spectrograph_auto_recover: bool = True
    spectrograph_queue_wait: float = 600.0
    #: Operating modes (docs/modes.md): while the enclosure's mode does not
    #: let the plan run (``manual``, ``shadow``), how long a telescope waits
    #: (Idle) before it asks again.
    mode_idle: float = 10.0
    #: Config-defined sequences (docs/sequences.md): how long a sequence's
    #: cleanup (lamps off, the calibration stage out, ...) may take.
    sequence_cleanup_timeout: float = 120.0


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
    #: The spectrograph's abort: None (nothing of ours was running), True
    #: (it ended), False (it did not confirm).
    spectrograph_aborted: bool | None = None
    #: Lamps switched off by the cleanup (``telescope:lamp``).
    lamps_off: list[str] = field(default_factory=list)

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
    #: Calibration-stage grants (the TRES front end), per telescope.
    calstages: dict[str, Grant] = field(default_factory=dict)
    #: Lamp-switch grants, per telescope with lamps.
    lamps: dict[str, Grant] = field(default_factory=dict)
    #: Focuser grants, for the telescopes whose focuser the sequencer moves.
    focusers: dict[str, Grant] = field(default_factory=dict)
    #: Rotator grants, for the telescopes whose rotator the sequencer moves.
    rotators: dict[str, Grant] = field(default_factory=dict)
    #: Telescopes that left the generation in the middle of a backoff.
    backing_off: set[str] = field(default_factory=set)
    errors: int = 0
    revoked: bool = False
    #: Spectra being taken, by id.
    spectra: dict[str, _Spectrum] = field(default_factory=dict)
    #: The spectrograph for this generation (None: the sequencer has none).
    spectrograph: _SpecGate | None = None
    #: Grants made during the generation (manual control), fenced with the rest.
    extra_grants: list[Grant] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.lease is None

    def leave(self, telescope: str) -> None:
        self.working.discard(telescope)
        if not self.working:
            self.idle.set()


class _SpecGate:
    """This generation's way to the spectrograph: like a grant, it is revoked
    when the generation is fenced, so nothing new is started after that."""

    def __init__(self, hook: SpectrographHook) -> None:
        self.hook = hook
        self.revoked: str | None = None

    def revoke(self, reason: str) -> None:
        if self.revoked is None:
            self.revoked = reason

    def _check(self) -> None:
        if self.revoked is not None:
            raise GrantRevoked(f"spectrograph: {self.revoked}")

    async def expose(self, **frame: Any) -> dict[str, Any]:
        self._check()
        return await self.hook.expose(**frame)

    async def calibrate(
        self, frames: list[dict[str, Any]], night: str | None = None
    ) -> dict[str, Any]:
        self._check()
        return await self.hook.calibrate(frames, night=night)


@dataclass
class SpectrumFrame:
    """One spectrum (or spectrograph calibration frame) as taken: the raw
    file's name on the spectrograph PC and the sidecar (relative to the data
    root; None without a :class:`~robofast.spectra.SpectrumRecorder`)."""

    file: str
    path: str | None
    record: dict[str, Any]


@dataclass(eq=False)
class _Spectrum:
    """One spectrum fed by several telescopes (module doc, "Spectra")."""

    id: str
    obs: Observation
    leader: str
    #: Telescopes the leader waits for (the feed, less those not working).
    expected: set[str]
    #: Guiding, ready to feed: telescope -> time.monotonic().
    ready: dict[str, float] = field(default_factory=dict)
    #: Dropped out: telescope -> why.
    dropped: dict[str, str] = field(default_factory=dict)
    #: The first exposure has started: nobody joins any more.
    started: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set (and replaced) on every change: a telescope ready or dropped, a
    #: frame begun or ended.
    tick: asyncio.Event = field(default_factory=asyncio.Event)

    def pulse(self) -> None:
        self.tick.set()
        self.tick = asyncio.Event()

    def active(self) -> list[str]:
        return sorted(t for t in self.ready if t not in self.dropped)

    def mark_ready(self, telescope: str) -> None:
        self.expected.add(telescope)
        self.ready[telescope] = time.monotonic()
        self.pulse()

    def drop(self, telescope: str, why: str) -> None:
        if telescope not in self.dropped:
            self.dropped[telescope] = why
            self.pulse()


class Sequencer:
    """The sequencer of one enclosure.

    ``telescopes`` lists its :class:`TelescopeUnit` objects; ``telescope``
    and ``camera`` are a shorthand for a single unit named after the
    telescope owner. ``plan`` is a :class:`~robofast.plan.PlanProvider`,
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
        spectrograph: SpectrographHook | None = None,
        spectrograph_names: dict[str, str] | None = None,
        spectra: SpectrumRecorder | None = None,
        clock: Clock | None = None,
        sequences: dict[str, Any] | None = None,
        night_rule: NightRule | None = None,
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
        #: The value registry (the frame pipeline's, else a private one):
        #: autofocus results and the focus model's inputs.
        self.registry = imaging.registry if imaging is not None else ValueRegistry()
        #: The enclosure's spectrograph (module doc, "Spectra"), the names its
        #: service knows the telescopes by (default: the same), and the
        #: spectrum sidecar writer (None: records are kept only in
        #: :attr:`spectrum_records`).
        self.spectrograph = spectrograph
        self.spectrograph_names = dict(spectrograph_names or {})
        #: The enclosure config's sequences, by name (docs/sequences.md;
        #: robofast.sequences.sequences_from_config), and their
        #: cleanups still running after a cancellation, per telescope.
        self.sequences: dict[str, Any] = dict(sequences or {})
        self._sequence_cleanups: dict[str, set[asyncio.Task]] = {}
        self.spectra = spectra
        #: The one clock every night name and time stamp comes from
        #: (robofast.clock): the frames, the sidecars, the spectrograph's
        #: file names, the autofocus results. Give the dispatch scheduler the
        #: same one (sequencer_service.build does). Default: the host's.
        self.clock: Clock = clock or real_clock
        if imaging is not None:
            imaging.set_clock(self.clock)
        if spectra is not None:
            spectra.clock = self.clock
        #: The site's night rule (robofast.clock; :attr:`night_rule`).
        self._night_rule = night_rule
        if night_rule is not None:
            if imaging is not None:
                imaging.store.rule = night_rule
            if spectra is not None:
                spectra.rule = night_rule
        #: Spectrograph health (module doc): faults in a row, and why it is
        #: down (None: up).
        self.spectrograph_failures = 0
        self.spectrograph_down: str | None = None
        self._spec_recovery: asyncio.Task | None = None
        #: Escalations running (an agent at work; docs/recovery.md), agent
        #: runs per (telescope, device) and the faults escalated tonight.
        self._escalations: set[asyncio.Task] = set()
        self._agent_runs: dict[tuple[str, str], int] = {}
        self._faults_escalated: dict[tuple[str, str], str] = {}
        #: Every spectrum and spectrograph calibration frame taken tonight.
        self.spectrum_records: list[dict[str, Any]] = []
        #: Pointing models built tonight (docs/pointing_model.md).
        self.pointing_models: list[Any] = []
        #: Sky flats taken tonight: (telescope, FlatFrame).
        self.flat_frames: list[Any] = []
        #: PA calibrations measured tonight (docs/rotcal.md).
        self.pa_calibrations: list[Any] = []
        #: The spectrograph job this sequencer started and has not seen end,
        #: and the telescope that started it.
        self._spec_job: int | None = None
        self._spec_job_owner: str | None = None
        #: One spectrograph job at a time from this sequencer; one fiber
        #: location (backlight) at a time.
        self._spec_lock = asyncio.Lock()
        self._locate_lock = asyncio.Lock()
        #: Spectra finished tonight, by id (a late member does not start one again).
        self._spectra_done: dict[str, str] = {}
        self._report_files = _accepts_files(self.plan.report)
        #: The enclosure's operating mode, as the plan is asked
        #: (robofast.modes.ModeGate; docs/modes.md).
        self.modes = ModeGate(
            self.plan, lambda: self.supervisor, self._event, idle=self.config.mode_idle
        )
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
        self._wire_pointing()
        hook = getattr(self.plan, "set_preempt_callback", None)
        if hook is not None:
            hook(self.preempt)
        hook = getattr(self.plan, "set_operator_hooks", None)
        if hook is not None:
            hook(park=self.request_park, clear_spectrograph=self.clear_spectrograph)

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

    def request_park(self, telescope: str, reason: str = "") -> bool:
        """Park ``telescope`` now (the observer console's ``park``, through
        :class:`~robofast.console.hold.OperatorHold`, which also holds
        it and stops its observation). Only that mount moves: never the
        roof, never the lease. Its telescope task parks it as soon as its
        observation has stopped, with its own grant (so the generation's
        fence covers it). A telescope that is not observing in a generation
        is parked already (by its error's cleanup or the enclosure's).
        Returns whether the request was taken."""
        unit = self.units.get(telescope)
        if unit is None:
            raise ValueError(f"request_park({telescope!r})")
        gen = self._gen
        if gen is None or gen.revoked or telescope not in gen.working:
            self._event(
                "park_requested",
                telescope=telescope,
                reason=reason,
                pending=False,
                why="not observing: parked by the last cleanup",
            )
            return True
        unit.park_requested = reason or "operator"
        self._event("park_requested", telescope=telescope, reason=reason, pending=True)
        unit.wake.set()
        return True

    def clear_spectrograph(self, operator: str = "", reason: str = "") -> bool:
        """The operator's clear of a spectrograph marked down (module doc)."""
        was = self.spectrograph_down
        self._spectrograph_up(f"cleared by {operator or 'an operator'}: {reason}")
        return was is not None

    # -- spectrograph health ------------------------------------------------

    def _spectrograph_ok(self) -> None:
        self.spectrograph_failures = 0

    def _spectrograph_fault(self, gen: int, telescope: str, error: str) -> None:
        """A spectrograph fault (not the telescope's): count it; mark the
        spectrograph down after ``spectrograph_max_failures`` in a row."""
        self.spectrograph_failures += 1
        self._event(
            "spectrograph_error",
            gen=gen,
            telescope=telescope,
            error=error,
            failures=self.spectrograph_failures,
            level="warning",
        )
        log.error("%s: spectrograph: %s", self.name, error)
        if (
            self.spectrograph_down is None
            and self.spectrograph_failures >= self.config.spectrograph_max_failures
        ):
            self._spectrograph_mark_down(
                f"{self.spectrograph_failures} faults in a row; last: {error}"
            )

    def _spectrograph_mark_down(self, why: str) -> None:
        self.spectrograph_down = why
        self._event("spectrograph_down", why=why, level="critical")
        log.critical("%s: spectrograph marked down: %s", self.name, why)
        self._tell_plan_spectrograph(False, why)
        if self._spec_recovery is None or self._spec_recovery.done():
            try:
                self._spec_recovery = asyncio.get_running_loop().create_task(
                    self._spectrograph_recovery(), name=f"{self.name}-spectrograph-recovery"
                )
            except RuntimeError:
                self._spec_recovery = None  # no loop (a synchronous test)

    def _spectrograph_up(self, why: str) -> None:
        if self.spectrograph_down is None:
            return
        self.spectrograph_down = None
        self.spectrograph_failures = 0
        self._event("spectrograph_up", why=why)
        log.warning("%s: spectrograph up again: %s", self.name, why)
        self._tell_plan_spectrograph(True, why)
        task = self._spec_recovery
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._spec_recovery = None
        for unit in self.units.values():
            unit.wake.set()  # an idle telescope may lead a spectrum again

    def _tell_plan_spectrograph(self, available: bool, why: str) -> None:
        hook = getattr(self.plan, "set_spectrograph_available", None)
        if hook is None:
            return
        try:
            hook(available, why)
        except Exception:
            log.exception("%s: plan.set_spectrograph_available", self.name)

    async def _spectrograph_recovery(self) -> None:
        """While the spectrograph is down: check it every
        ``spectrograph_recovery_interval`` (its status, read fresh, answers
        with no alarms and no job stuck); with ``spectrograph_auto_recover``
        a passing check marks it up."""
        cfg = self.config
        me = asyncio.current_task()
        # Also ends once it is no longer the current check, even if its
        # cancel was lost (robofast.cancel).
        while self.spectrograph_down is not None and self._spec_recovery is me:
            await asyncio.sleep(cfg.spectrograph_recovery_interval)
            if self.spectrograph is None or self._spec_recovery is not me:
                return
            try:
                st = await asyncio.wait_for(
                    self.spectrograph.status(refresh=True), cfg.spectrum_poll + 30.0
                )
            except Exception as exc:
                self._event("spectrograph_check", ok=False, error=repr(exc))
                continue
            alarms = list(st.get("alarms") or [])
            ok = not alarms
            self._event("spectrograph_check", ok=ok, alarms=alarms[:5])
            if ok and cfg.spectrograph_auto_recover:
                self._spectrograph_up("recovery check passed")
                return

    def _event(self, what: str, **detail: Any) -> None:
        self.timeline.event(self.name, what, **detail)

    def alarm(self, telescope: str, text: str) -> None:
        """Raise an alarm about ``telescope`` (status snapshot, console, notifier)."""
        unit = self.units.get(telescope)
        if unit is not None and text not in unit.alarms:
            unit.alarms.append(text)
        log.error("%s: %s", telescope, text)
        self._event("alarm", telescope=telescope, text=text, level="alarm")

    @property
    def needs_operator(self) -> bool:
        """Quarantined, or some telescope gave up: an operator should look."""
        return self.quarantined or any(u.gave_up for u in self.units.values())

    # -- main loop --------------------------------------------------------

    def _wire_pointing(self) -> None:
        """Pointing models: their alarms to :meth:`alarm`, their frames from
        the frame pipeline."""
        for u in self.units.values():
            if u.pointing is not None:
                u.pointing.on_alarm = lambda text, u=u: self.alarm(u.name, text)
                if self.imaging is not None:
                    self.imaging.pointing[u.name] = u.pointing
            if u.pa_calibration is not None and self.imaging is not None:
                self.imaging.pa_stores[u.name] = u.pa_calibration.store
            if u.recovery is not None:
                u.recovery.alarm = lambda text, u=u: self.alarm(u.name, text)
                u.recovery.event = lambda what, u=u, **kw: self._event(what, telescope=u.name, **kw)
            if u.cooling is not None:
                u.cooling.alarm = lambda text, u=u: self.alarm(u.name, text)
                u.cooling.event = lambda what, u=u, **kw: self._event(what, telescope=u.name, **kw)

    async def run(self) -> None:
        self._wire_pointing()
        try:
            await self._run()
        finally:
            task, self._spec_recovery = self._spec_recovery, None
            await stop_task(task, self.config.cancel_ack_timeout)
            await self._stop_escalations()

    async def _run(self) -> None:
        await self._shadow_start()
        self._hook_cooling()
        await self._recover()
        await self._start_cooling()
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

    async def _shadow_start(self) -> None:
        """While the mode commands nothing (``shadow``) or is not known yet,
        do not start: the startup cleanup would park the telescopes. Log what
        the plan would do meanwhile (docs/modes.md)."""
        told = False
        while (observe := await self.modes.update(force=True)) in ("shadow", "unknown"):
            if not told:
                self._event("startup_held", mode=self.modes.mode, observe=observe)
                told = True
            for u in self.units.values():
                await self.modes.shadow(u.name)
            await asyncio.sleep(self.config.mode_idle)

    def _hook_cooling(self) -> None:
        """Each cooled camera's owner sets its cooler on every fresh
        connection (its ``on_connect``; docs/cooling.md). Once the mode lets
        the sequencer command (not in shadow), before the startup cleanup."""
        for u in self.units.values():
            if u.cooling is not None:
                u.camera.on_connect = u.cooling.on_connect

    async def _start_cooling(self) -> None:
        """Touch each cooled camera the startup has not connected yet, so
        it starts cooling now rather than at its first frame
        (docs/cooling.md). A camera that does not answer is logged; its
        first command tries again."""
        units = [u for u in self.units.values() if u.cooling is not None]

        async def touch(u: TelescopeUnit) -> None:
            assert u.cooling is not None
            if u.cooling.applied_at is not None or u.cooling.unsupported is not None:
                return  # the startup cleanup connected it: the hook ran
            grant = u.camera.grant(f"{self.name}/cooling")
            try:
                if u.camera.connected:
                    await u.cooling.apply(grant, "startup")
                else:
                    await grant.get("CoolerOn")  # connects: the hook sets the cooler
            except (DeviceError, TimeoutError) as exc:
                log.warning("%s: %s: camera cooling at startup: %r", self.name, u.name, exc)
                self._event("cooling_start_failed", telescope=u.name, error=repr(exc)[:200])
            finally:
                grant.revoke("cooling started")

        if units:
            await asyncio.gather(*(touch(u) for u in units))

    async def _cooling_check(
        self, state: _Generation, unit: TelescopeUnit, cam: Grant, *, light: bool
    ) -> CoolingStatus | None:
        """Before an exposure: :meth:`CameraCooling.check` (a cooler found off
        back on; a light frame waits, bounded, for the CCD to settle); the
        result goes in the frame's header (``COOLOK``). Never fails the
        exposure: a camera that cannot answer fails at ``StartExposure``."""
        assert unit.cooling is not None
        try:
            status = await unit.cooling.check(cam, wait=light)
        except DeviceError as exc:
            log.warning("%s: %s: cooling check: %s", self.name, unit.name, exc)
            self._event(
                "cooling_check_failed", gen=state.gen, telescope=unit.name, error=str(exc)[:200]
            )
            return None
        if status is not None and self.imaging is not None:
            self.imaging.registry.publish(
                f"{unit.name}.camera.cool_ok",
                status.settled,
                max_age=1800.0,
                keyword="COOLOK",
                comment="CCD within tolerance of its setpoint",
            )
        return status

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
        for u in active:
            u.rejoin.clear()
        wait = min(u.retry_at for u in active) - time.monotonic()
        if wait > 0:
            self._event("error_backoff", seconds=wait)
            # An escalation that ends sooner (rejoins or drops) wakes it.
            await _wait_any([u.rejoin for u in active], wait)
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
        if getattr(self.plan, "peek", None) is not None:
            return self._prepare(await self.modes.peek(unit.name)), False
        return self._prepare(await self.modes.next_observation(unit.name)), True

    def _prepare(self, answer: Any) -> Any:
        """A plan's answer as the sequencer takes it: a config-defined
        sequence (docs/sequences.md) is light or dark as its definition
        says, whatever the plan set."""
        if isinstance(answer, Observation) and answer.kind == "sequence":
            try:
                seq = self._sequence_for(answer)
            except ValueError:
                seq = None  # refused when it runs, before anything moves
            if seq is not None and answer.light != seq.light:
                answer.light = seq.light
        return answer

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
        state.calstages = {
            u.name: u.calstage.grant(holder) for u in units if u.calstage is not None
        }
        state.lamps = {u.name: u.lamps.grant(holder) for u in units if u.lamps is not None}
        state.focusers = {u.name: u.focuser.grant(holder) for u in units if u.focuser is not None}
        state.rotators = {u.name: u.rotator.grant(holder) for u in units if u.rotator is not None}
        if self.spectrograph is not None:
            state.spectrograph = _SpecGate(self.spectrograph)
        fence = (
            *(g for pair in grants.values() for g in pair),
            *state.filterwheels.values(),
            *state.calstages.values(),
            *state.lamps.values(),
            *state.focusers.values(),
            *state.rotators.values(),
        )
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
                if state.spectrograph is not None:
                    state.spectrograph.revoke(f"cancelled: {lease.revoke_reason}")
                await self._cancel_observe(
                    gen, task, (*fence, *state.extra_grants), lease.revoke_reason
                )
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
                if state.spectrograph is not None:
                    state.spectrograph.revoke("sequencer stopping")
                await self._cancel_observe(
                    gen, task, (*fence, *state.extra_grants), "sequencer stopping"
                )
            for g in (*fence, *state.extra_grants):
                g.revoke("generation over")
            if state.spectrograph is not None:
                state.spectrograph.revoke("generation over")
            for spec in state.spectra.values():
                spec.done.set()
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
                if not unit.capabilities_told:
                    await self._tell_capabilities(state, unit, tel)
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
                        obs = self._prepare(
                            await asyncio.wait_for(
                                self.modes.next_observation(unit.name), self.config.plan_timeout
                            )
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
                except ObservationFailed as exc:
                    # Not the telescope's fault: no backoff, no park.
                    self._report(unit, obs, "failed", taken)
                    self._event(
                        "observation_failed", gen=state.gen, telescope=unit.name, error=str(exc)
                    )
                    unit.consecutive_errors = 0
                except _PreemptAbortFailed as exc:
                    self._report(unit, obs, "interrupted", taken)
                    await self._telescope_failed(state, unit, tel, cam, f"preemption: {exc}")
                except _LeaderDropped as exc:
                    # Its spectrum was taken without it (module doc, "Spectra").
                    self._report(unit, obs, "complete", taken)
                    await self._telescope_failed(state, unit, tel, cam, repr(exc.error))
                except SpectrographBusy as exc:
                    # Busy with another job: re-queued, nobody's fault.
                    self._report(unit, obs, "interrupted", taken)
                    self._event(
                        "spectrograph_busy", gen=state.gen, telescope=unit.name, error=str(exc)
                    )
                except SpectrographFault as exc:
                    # The spectrograph's fault, not the telescope's: no park,
                    # no backoff, no give-up count (module doc). Re-queued
                    # by a plan that is told when the spectrograph is down;
                    # failed for one that is not (so it moves on).
                    told = getattr(self.plan, "set_spectrograph_available", None)
                    self._report(unit, obs, "interrupted" if told else "failed", taken)
                    self._spectrograph_fault(state.gen, unit.name, str(exc))
                    if self.spectrograph_down is None:
                        await asyncio.sleep(self.config.poll_interval)
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
                except Unsupported as exc:
                    await self._unsupported(state, unit, tel, cam, obs, taken, exc)
                    continue
                except Exception as exc:
                    self._report(unit, obs, "failed", taken)
                    await self._telescope_failed(state, unit, tel, cam, repr(exc))
                else:
                    self._report(unit, obs, "complete", taken)
                    unit.consecutive_errors = 0
                unit.last_unsupported = None
        finally:
            unit.idle = unit.exposing = False
            state.leave(unit.name)

    async def _unsupported(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
        exc: Unsupported,
    ) -> None:
        """``obs`` cannot be done on this telescope (module doc, "Several
        telescopes"): not its fault. Tell the plan, report it
        ``unsupported``, make the telescope ready for its next target (no
        park, no backoff, no error count) and go on."""
        reason = str(exc)
        repeat = unit.last_unsupported == obs.name
        unit.last_unsupported = obs.name
        hook = getattr(self.plan, "unsupported", None)
        if hook is not None:
            try:
                hook(unit.name, obs, reason)
            except Exception:  # the plan's bug must not end this telescope's night
                log.exception("%s: plan.unsupported(%s, %s)", self.name, unit.name, obs.name)
        self._report(unit, obs, "unsupported", taken)
        self._event(
            "observation_unsupported",
            gen=state.gen,
            telescope=unit.name,
            target=obs.name,
            reason=reason,
            level="warning",
        )
        log.warning("%s: %s cannot do %s: %s", self.name, unit.name, obs.name, reason)
        if repeat:
            # The plan ignored the report and asked again: an error, so the
            # give-up bounds it (a plan must not spin a telescope).
            await self._telescope_failed(
                state, unit, tel, cam, f"{obs.name} handed out again after unsupported: {reason}"
            )
            return
        report = CleanupReport(state.gen, [unit.name])
        if self._spec_job is not None and self._spec_job_owner == unit.name:
            await self._abort_spectrograph(f"{unit.name}: {reason}", report)
        await self._make_ready(unit.name, tel, cam, report)
        if report.errors:
            # Making it ready failed: that is the telescope's error.
            await self._telescope_failed(
                state, unit, tel, cam, f"after unsupported: {'; '.join(report.errors)}"
            )

    async def _tell_capabilities(self, state: _Generation, unit: TelescopeUnit, tel: Grant) -> None:
        """Read what the plan can use to avoid an :class:`Unsupported`
        (module doc) and tell it, once a night per telescope. A read that
        fails is left out (the runtime check stays the backstop)."""
        unit.capabilities_told = True
        hook = getattr(self.plan, "set_capabilities", None)
        if hook is None:
            return
        caps: dict[str, bool] = {}
        try:
            async with asyncio.timeout(10.0):
                caps["tracking_rates"] = bool(
                    await tel.get("CanSetRightAscensionRate")
                    and await tel.get("CanSetDeclinationRate")
                )
        except (DeviceError, TimeoutError) as exc:
            log.warning("%s: %s: tracking-rate capability unread: %r", self.name, unit.name, exc)
        if not caps:
            return
        self._event("capabilities", gen=state.gen, telescope=unit.name, **caps)
        try:
            hook(unit.name, caps)
        except Exception:
            log.exception("%s: plan.set_capabilities(%s)", self.name, unit.name)

    async def _idle(self, state: _Generation, unit: TelescopeUnit, tel: Grant, idle: Idle) -> bool:
        """Wait out an :class:`Idle`: stop tracking (so an idle telescope
        cannot track into a limit), stay put, until woken or timed out.
        True if every telescope has now been idle for ``idle_release_after``
        (module doc): the telescope leaves the generation."""
        if unit.idle_since is None:
            unit.idle_since = time.monotonic()
        if unit.park_requested is not None:
            await self._operator_park(state, unit, tel)
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

    async def _operator_park(self, state: _Generation, unit: TelescopeUnit, tel: Grant) -> None:
        """Park this one telescope now (:meth:`request_park`): its guider is
        stopped and any slew aborted first. The roof is not touched."""
        why, unit.park_requested = unit.park_requested, None
        report = CleanupReport(state.gen, [unit.name])
        await self._stop_guider(unit.name, report)
        await self._abort_slew(unit.name, tel, report)
        await self._park(unit.name, tel, report)
        self._event(
            "operator_parked",
            gen=state.gen,
            telescope=unit.name,
            parked=unit.name in report.parked,
            errors=report.errors,
            reason=why,
            level="warning" if report.errors else "info",
        )

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
                self._event("guiding_stopped", gen=state.gen, telescope=unit.name)
            if obs.follow is not None:
                await self._follow_end(state, unit, tel)

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
        aborts = [
            self._cleanup_camera(unit.name, cam, report),
            self._abort_slew(unit.name, tel, report),
        ]
        if self._spec_job is not None and self._spec_job_owner == unit.name:
            aborts.append(self._abort_spectrograph(f"{unit.name} preempted ({policy})", report))
        await asyncio.gather(*aborts)
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
        files = [
            f.path
            for f in taken
            if isinstance(f, PendingFrame | SpectrumFrame) and f.path is not None
        ]
        try:
            if self._report_files and (self.imaging is not None or self.spectra is not None):
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
        while True:
            # An escalation ending (robofast/escalation.py) wakes it early.
            await _wait_any([state.idle, unit.rejoin], delay)
            if state.idle.is_set():
                if unit.active:
                    state.backing_off.add(unit.name)
                return False
            unit.rejoin.clear()
            if not unit.active:
                return False
            delay = unit.retry_at - time.monotonic()
            if delay <= 0:
                state.working.add(unit.name)
                return True

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
        if self._spec_job is not None and self._spec_job_owner == unit.name:
            await self._abort_spectrograph(f"{unit.name}: {error}", report)
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
        result = await self._recover_device(state, unit, tel, cam, error)
        if result is not None and result.exhausted:
            why = self._escalate(unit, result, error)
            if why is None:
                return  # an agent is at work: the telescope is out of the plan meanwhile
            unit.gave_up = True
            self._event(
                "giving_up",
                telescope=unit.name,
                errors=unit.consecutive_errors,
                reason=f"recovery ladder exhausted ({result.device}); {why}",
                level="critical",
            )
            return
        if result is not None and result.recovered:
            # The device is back: it gets another try (the ladder itself
            # gives up once every rung has been used).
            unit.consecutive_errors = min(unit.consecutive_errors, cfg.max_consecutive_errors - 1)
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

    def _devices(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, cam: Grant
    ) -> dict[str, tuple[DeviceOwner, Grant]]:
        out = {"telescope": (unit.telescope, tel), "camera": (unit.camera, cam)}
        for kind, owner, grants in (
            ("focuser", unit.focuser, state.focusers),
            ("rotator", unit.rotator, state.rotators),
            ("filterwheel", unit.filterwheel, state.filterwheels),
            ("calstage", unit.calstage, state.calstages),
        ):
            if owner is not None and unit.name in grants:
                out[kind] = (owner, grants[unit.name])
        return out

    async def _recover_device(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, cam: Grant, error: str
    ) -> RecoveryResult | None:
        """Climb the failed device's recovery ladder, if it has one (docs/recovery.md)."""
        ladder = unit.recovery
        if ladder is None:
            return None
        devices = self._devices(state, unit, tel, cam)
        kind = ladder.device_for(error, {k: o.name for k, (o, _) in devices.items()})
        if kind is None or (kind == "guider" and unit.guider is None):
            return None
        self._event("recovery_start", gen=state.gen, telescope=unit.name, device=kind)

        async def others(device: str, member: str) -> Any:
            if device not in devices:
                return None
            return await devices[device][1].get(member)

        tel_owner = unit.telescope
        mount = MountAccess(
            reconnect=lambda: tel_owner.reconnect("recovery"),
            probe=lambda member: tel.get(member),
            call=lambda member, *args: tel.call(member, *args),
        )
        call: Callable[..., Awaitable[Any]] | None = None
        if kind == "guider":
            reconnect, probe = self._guider_access(unit)
        else:
            owner, grant = devices[kind]
            reconnect = lambda: owner.reconnect("recovery")  # noqa: E731
            probe = lambda member: grant.get(member)  # noqa: E731
            call = lambda member, *args: grant.call(member, *args)  # noqa: E731
        try:
            result = await ladder.recover(
                kind,
                reconnect=reconnect,
                probe=probe,
                others=others,
                call=call,
                mount=None if kind == "telescope" else mount,
            )
        except GrantRevoked:
            return None  # fenced: the generation's cleanup takes over
        self._event(
            "recovery_done",
            gen=state.gen,
            telescope=unit.name,
            device=kind,
            recovered=result.recovered,
            tried=[f"{r}: {o}" for r, o in result.tried],
        )
        return result

    @staticmethod
    def _guider_access(unit: TelescopeUnit) -> tuple[Callable, Callable]:
        """The recovery ladder's view of a telescope's guider (docs/recovery.md):
        its ``reconnect`` (the service closes and opens its camera again)
        and its ``status``."""
        guider = unit.guider

        async def reconnect() -> None:
            fn = getattr(guider, "reconnect", None)
            if fn is None:
                raise RuntimeError(f"{unit.name}'s guider cannot reconnect its camera")
            await fn()

        async def probe(member: str) -> Any:
            fn = getattr(guider, member, None)
            return {} if fn is None else await fn()

        return reconnect, probe

    # -- escalation: the ladder's last step (robofast/escalation.py) ------------------------

    def _escalate(self, unit: TelescopeUnit, result: RecoveryResult, error: str) -> str | None:
        """``result``'s ladder is exhausted: open an incident. Start its agent
        in the background (None: the telescope is out of the plan until the
        agent ends) or, with no agent to run, end the incident at once and
        return why (the telescope gives up for the night)."""
        ladder = unit.recovery
        assert ladder is not None
        kind = result.device
        pol = ladder.escalation(kind)
        inc = Incident(unit.name, kind, len(unit.incidents) + 1, error, list(result.tried))
        unit.incidents.append(inc)
        key = (unit.name, kind)
        sig = (unit.name, fault_signature(kind, error))
        runs = self._agent_runs.get(key, 0)
        if pol.skipped:
            inc.agent = "skipped by the config (escalate.agent: false)"
        elif pol.agent is None:
            inc.agent = "no agent configured"
        elif runs >= pol.max_per_night:
            inc.agent = (
                f"not run: the {kind} was escalated {runs} time(s) tonight "
                f"(max_per_night {pol.max_per_night})"
            )
        elif sig in self._faults_escalated:
            inc.agent = (
                f"not run: the same fault was escalated at {self._faults_escalated[sig]} "
                "and came back"
            )
        else:
            self._agent_runs[key] = runs + 1
            self._faults_escalated[sig] = inc.started
            inc.agent = "running"
            unit.escalation = inc
            # Out of the plan until the agent ends (it wakes the waits sooner).
            unit.retry_at = (
                time.monotonic() + pol.timeout + ladder.policies[kind].verify_timeout + 60
            )
            self._event(
                "escalation_start",
                telescope=unit.name,
                device=kind,
                incident=inc.id,
                timeout=pol.timeout,
            )
            context = self._escalation_context(unit, inc, pol)
            task = asyncio.get_running_loop().create_task(
                self._run_escalation(unit, inc, pol, context),
                name=f"{self.name}-escalation-{inc.id}",
            )
            self._escalations.add(task)
            task.add_done_callback(self._escalations.discard)
            return None
        inc.finish("dropped", "the recovery ladder is exhausted")
        self._incident_done(unit, inc)
        return f"escalation: {inc.agent}"

    async def _stop_escalations(self) -> None:
        """The sequencer is stopping: kill every agent at work, and end its
        incident (dropped; the person is told)."""
        if self._escalations:
            await stop_tasks(list(self._escalations), self.config.cancel_ack_timeout + 20)
        for u in self.units.values():
            if u.escalation is not None:  # cancelled before it started
                u.escalation.agent = "interrupted: the sequencer stopped"
                self._end_escalation(u, u.escalation, "the sequencer stopped")

    def _escalation_context(self, unit: TelescopeUnit, inc: Incident, pol: Any) -> dict[str, Any]:
        """What the agent is given (docs/recovery.md, "The agent hook")."""
        name = unit.name
        entries = [
            e
            for e in self.timeline.recent(max(1000, pol.log_entries * 5), skip_sent=True)
            if str(e.get("source", "")).startswith(f"{name}.")
            or (e.get("detail") or {}).get("telescope") == name
        ][-pol.log_entries :]
        try:
            from robofast.sequencer_service import snapshot

            status = snapshot(self)
            status.pop("timeline", None)
        except Exception as exc:  # the context must not fail for want of a status
            status = {"error": repr(exc)}
        owner = self._owner(unit, inc.device)
        return {
            "schema": 1,
            "incident": inc.id,
            "time": inc.started,
            "sequencer": self.name,
            "telescope": name,
            "device": inc.device,
            "device_name": owner.name if owner is not None else None,
            "error": inc.error,
            "tried": [{"rung": r, "outcome": o} for r, o in inc.tried],
            "other_telescopes": sorted(n for n in self.units if n != name),
            "timeout": pol.timeout,
            "safe": [*DEFAULT_SAFE, *pol.safe],
            "contract": (
                "Exit 0 if the device should be retried (robofast verifies it "
                "itself), nonzero otherwise. Write a summary for a person -- "
                "diagnosis, what you did, the result -- to the file "
                "$ROBOFAST_ESCALATION_SUMMARY (or stdout)."
            ),
            "timeline": entries,
            "status": status,
        }

    async def _run_escalation(
        self, unit: TelescopeUnit, inc: Incident, pol: Any, context: dict[str, Any]
    ) -> None:
        """Run the agent, verify the device if it asks for a retry, end the
        incident (module doc of robofast/escalation.py)."""
        try:
            try:
                res = await run_agent(pol, context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                res = AgentResult(None, "", error=repr(exc))
        except asyncio.CancelledError:
            inc.agent = "interrupted: the sequencer stopped while the agent worked"
            self._end_escalation(unit, inc, "the sequencer stopped")
            raise
        inc.agent_summary = res.summary
        why: str | None
        if res.error is not None:
            inc.agent = f"could not run: {res.error}"
            why = "the agent could not run"
        elif res.timed_out:
            inc.agent = f"timed out after {pol.timeout:g} s (killed)"
            why = "the agent timed out"
        elif res.exit != 0:
            inc.agent = f"exited {res.exit} (could not fix it)"
            why = "the agent could not fix it"
        else:
            inc.agent = "exited 0 (asks for a retry)"
            try:
                await self._verify_device(unit, inc.device)
                why = None
            except Exception as exc:
                why = f"robofast's own check after the agent failed: {str(exc)[:200] or exc!r}"
        self._end_escalation(unit, inc, why)

    async def _verify_device(self, unit: TelescopeUnit, kind: str) -> None:
        """Robofast's own check that ``kind`` answers (and, a PWI telescope,
        is usable), never the agent's word: a fresh connection, read through
        a grant of its own."""
        if kind == "guider" and unit.recovery is not None:
            reconnect, probe = self._guider_access(unit)
            await unit.recovery.verify(kind, reconnect=reconnect, probe=probe)
            return
        owner = self._owner(unit, kind)
        if owner is None or unit.recovery is None:
            raise RuntimeError(f"no {kind} to verify")
        grant = owner.grant(f"{self.name}/escalation")
        try:
            owner.reconnect("escalation")
            await unit.recovery.verify(
                kind,
                reconnect=lambda: owner.reconnect("escalation"),
                probe=lambda member: grant.get(member),
                call=lambda member, *args: grant.call(member, *args),
            )
        finally:
            grant.revoke("escalation verified")

    @staticmethod
    def _owner(unit: TelescopeUnit, kind: str) -> DeviceOwner | None:
        return {
            "telescope": unit.telescope,
            "camera": unit.camera,
            "focuser": unit.focuser,
            "rotator": unit.rotator,
            "filterwheel": unit.filterwheel,
            "calstage": unit.calstage,
        }.get(kind)

    def _end_escalation(self, unit: TelescopeUnit, inc: Incident, why: str | None) -> None:
        """The agent is done: rejoin (``why`` None: the device was verified)
        or give up for the night; either way one summary for a person."""
        unit.escalation = None
        if why is None:
            assert unit.recovery is not None
            inc.finish("recovered", "the device answered robofast's own check")
            unit.recovery.reset(inc.device)
            unit.consecutive_errors = 0
            unit.retry_at = time.monotonic()
        else:
            inc.finish("dropped", why)
            unit.gave_up = True
            self._event(
                "giving_up",
                telescope=unit.name,
                errors=unit.consecutive_errors,
                reason=f"recovery ladder exhausted ({inc.device}); escalation: {inc.agent}",
                level="critical",
            )
        self._incident_done(unit, inc)
        unit.rejoin.set()

    def _incident_done(self, unit: TelescopeUnit, inc: Incident) -> None:
        """The incident's one summary for a person: the status snapshot
        carries it to the console and the notifier (docs/notifier.md)."""
        log.error("%s: %s\n%s", unit.name, inc.text, inc.summary())
        self._event(
            "incident",
            telescope=unit.name,
            device=inc.device,
            incident=inc.id,
            outcome=inc.outcome,
            agent=inc.agent,
            level=inc.level,
            summary=inc.summary()[:4000],
        )

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
        gen = state.gen
        if self._sequence_cleanups.get(unit.name):
            # A sequence cut short (a preemption) still cleaning up: let it
            # finish (bounded) before anything else uses its devices.
            report = CleanupReport(gen, [unit.name])
            await self._cleanup_sequences(unit.name, report)
            if report.errors:
                raise ObservationFailed(f"{obs.name}: {'; '.join(report.errors)}")
        self._event("target_start", gen=gen, telescope=unit.name, target=obs.name)
        if obs.light and state.lease is None:
            raise RuntimeError(f"{obs.name}: light frames in a closed generation")
        handler = OBSERVATION_KINDS[obs.kind]
        run = KindRun(self, state, unit, tel, cam, obs, taken)
        if handler.stage == "before_setup":
            await handler.run(run)
            return
        if (
            handler.spectrograph
            and self.spectrograph_down is not None
            and not (obs.spectrum and obs.spectrum.get("leader") not in (None, unit.name))
        ):
            raise SpectrographFault(f"the spectrograph is marked down: {self.spectrograph_down}")
        if obs.light and await tel.get("AtPark"):
            await tel.call("Unpark")
        if obs.port is not None:
            await self._select_port(state, unit, tel, obs.port)
        if obs.filter is not None:
            await self._select_filter(state, unit, obs.filter)
        if obs.calstage is not None:
            # Before any lamp is switched on (_lamp_frames), so no lamp
            # light reaches the fibers through the wrong path.
            await self._select_calstage(state, unit, obs.calstage)
        if obs.light and unit.focus_restore is not None:
            await self._restore_focus(state, unit)
        if obs.light:
            assert state.lease is not None
            await tel.set("Tracking", True)
            await state.lease.wait_open()
            if handler.stage == "before_slew":
                await handler.run(run)
                return
            ra, dec = obs.ra_hours, obs.dec_deg
            if obs.follow is not None:
                # One way to follow a moving target: the mount's rates. A
                # mount without them cannot (the PWI and TCS servers today).
                if not (
                    await tel.get("CanSetRightAscensionRate")
                    and await tel.get("CanSetDeclinationRate")
                ):
                    raise Unsupported(
                        f"{unit.name}: the mount cannot set tracking rates, so it cannot"
                        f" follow {obs.name}"
                    )
                ra, dec = _follow_place(obs, self.clock())
            if ra is None or dec is None:
                lst = await tel.get("SiderealTime")
                lat = await tel.get("SiteLatitude")
                ra, dec = (lst - 0.5) % 24.0, max(-89.0, min(89.0, lat - 10.0))
            await self._slew(state, unit, tel, obs, ra, dec)
            self._event("on_target", gen=gen, telescope=unit.name, ra=ra, dec=dec)
            if unit.adc is not None:
                await self._adc_track(state, unit)
            if obs.guide:
                # Stopped in _observe_preemptible's finally and in cleanup.
                if unit.guider is None:
                    raise Unsupported(f"{unit.name} has no guider ({obs.name} asks to guide)")
                if handler.fiber:
                    await self._locate_fiber(state, unit)
                acq = (obs.spectrum or {}).get("acquisition") or {}
                extra: dict[str, Any] = {}
                how = {k: acq[k] for k in ACQ_KEYS if k in acq}
                if how:
                    extra["acquisition"] = how
                sigma = self._pointing_sigma(unit, obs.port)
                if sigma is not None:
                    extra["pointing_sigma"] = sigma
                if obs.guide_offset is not None:
                    extra["offset_guide"] = obs.guide_offset
                st = await unit.guider.start(
                    dec=dec,
                    target_radec=[ra * 15.0, dec],
                    unambiguous=bool(acq.get("unambiguous", False)),
                    **extra,
                )
                done = (st or {}).get("acquisition") if isinstance(st, dict) else None
                self._acquired(unit, done)
                offset = (st or {}).get("offset_guide") if isinstance(st, dict) else None
                self._event(
                    "guiding",
                    gen=gen,
                    telescope=unit.name,
                    target=obs.name,
                    guide_mode="offset" if offset else "annulus",
                    **(
                        {}
                        if not offset
                        else {"guide_star": offset.get("star"), "guide_offset": offset.get("now")}
                    ),
                    **(
                        {}
                        if not done
                        else {
                            "acq_method": done.get("method"),
                            "acq_stars": done.get("stars"),
                            "acq_trace": done.get("trace"),
                        }
                    ),
                )
            run.ra, run.dec = ra, dec
        await handler.run(run)

    async def _slew(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        obs: Observation,
        ra: float,
        dec: float,
    ) -> None:
        """Slew to (``ra`` hours, ``dec`` deg) through the pointing model
        (:meth:`_goto`)."""
        cra, cdec = ra, dec
        if unit.pointing is not None:
            await self._vendor_model_off(state, unit, tel)
            lst = float(await tel.get("SiderealTime"))
            cra, cdec, applied = unit.pointing.command(
                ra, dec, lst, obs.port, self.clock(), guided=obs.guide
            )
            self._event(
                "pointing_applied",
                gen=state.gen,
                telescope=unit.name,
                ra=ra,
                dec=dec,
                commanded_ra=round(cra, 7),
                commanded_dec=round(cdec, 6),
                **applied,
            )
        await self._goto(state, unit, tel, cra, cdec)

    async def _goto(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, ra: float, dec: float
    ) -> None:
        """Every slew to coordinates: the slew guard, then the slew, waited
        out. Rates a moving target left set (their reset failed) are set back
        to 0 first, or the slew fails: nothing else is ever tracked at a
        comet's rates."""
        if unit.follow_rates:
            await tel.set("RightAscensionRate", 0.0)
            await tel.set("DeclinationRate", 0.0)
            unit.follow_rates = False
            self._event("follow_rates_reset", gen=state.gen, telescope=unit.name, late=True)
        await self.slew_guard.check_slew(unit.name, ra, dec)
        await tel.call("SlewToCoordinatesAsync", ra, dec)
        await self._wait_for(lambda: tel.get("Slewing"), lambda s: not s, self.config.slew_timeout)

    async def _follow(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, obs: Observation, index: int
    ) -> None:
        """Before exposure ``index`` of a moving target: set the mount's RA/Dec
        rates to its motion at the exposure's middle (docs/moving_targets.md).
        The mount was pointed at it when the observation started, and checked
        then to take rates (:meth:`_observe`)."""
        assert obs.follow is not None
        mid = self.clock() + timedelta(seconds=obs.exptime / 2.0)
        ra, dec = _follow_place(obs, mid)
        dra, ddec = _follow_rate(obs, mid)
        # ASCOM: RightAscensionRate in seconds of RA per sidereal second,
        # DeclinationRate in arcsec per SI second.
        ra_rate = dra / math.cos(math.radians(dec)) / 15.0 * sky.SIDEREAL
        unit.follow_rates = True  # before the set: a failure still resets
        await tel.set("RightAscensionRate", ra_rate)
        await tel.set("DeclinationRate", ddec)
        self._event(
            "follow",
            gen=state.gen,
            telescope=unit.name,
            index=index,
            ra=round(ra, 7),
            dec=round(dec, 6),
            rate_arcsec_s=[round(dra, 7), round(ddec, 7)],
        )

    async def _follow_end(self, state: _Generation, unit: TelescopeUnit, tel: Grant) -> None:
        """A moving target's observation ended: the mount's rates back to 0
        (sidereal), bounded, never raises; if that fails they stay marked
        set, and the next slew resets them before it moves (:meth:`_goto`)."""
        if not unit.follow_rates:
            return
        try:
            async with asyncio.timeout(10.0):
                await tel.set("RightAscensionRate", 0.0)
                await tel.set("DeclinationRate", 0.0)
            unit.follow_rates = False
            self._event("follow_rates_reset", gen=state.gen, telescope=unit.name)
        except Exception as exc:  # still set: the next slew resets them first (_goto)
            log.error("%s: resetting the tracking rates: %r", unit.name, exc)
            self._event(
                "follow_rates_reset_failed", gen=state.gen, telescope=unit.name, error=repr(exc)
            )

    async def _expose_frames(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
        check: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """``obs.count`` exposures where the telescope points now (saved,
        with imaging), stopping at a readout boundary when preempted.
        ``check`` runs before each exposure (and may raise to stop)."""
        cfg = self.config
        gen = state.gen
        imaging = self.imaging
        # A lamp frame takes no sky light but opens the shutter on the lamp.
        shutter = obs.light or OBSERVATION_KINDS[obs.kind].shutter
        for i in range(obs.count):
            if imaging is not None:
                imaging.check(unit.name)  # a lost frame stops the telescope first
            if check is not None:
                await check()
            if obs.guide and obs.light and unit.guider is not None:
                await unit.guider.check()  # a failed guider is this telescope's error
            if obs.follow is not None and obs.light:
                await self._follow(state, unit, tel, obs, i)
            cooled = None
            if unit.cooling is not None:
                cooled = await self._cooling_check(state, unit, cam, light=obs.light)
            # From here until it is read out (downloaded, with imaging), a
            # "now-after-readout" preemption waits for this exposure.
            unit.exposing = True
            try:
                host_start = self.clock()
                await cam.call("StartExposure", obs.exptime, shutter)
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
                    lambda: image_ready(cam.get), bool, obs.exptime + cfg.readout_margin
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
                    **({"ccd_settled": cooled.settled} if cooled is not None else {}),
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

    # -- calibration lamps (docs/pulizzi.md) ----------------------------------------

    async def _lamp_frames(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """An ARC or LAMP_FLAT: the calibration stage, the lamp on, its
        warm-up, the frames, the lamp off -- also on any error, abort,
        preemption or cancellation (and the cleanup switches every lamp off
        again)."""
        setup, grant, name = unit.lamp_setup, state.lamps.get(unit.name), obs.lamp
        if setup is None or grant is None or unit.lamps is None:
            raise Unsupported(f"{unit.name} has no lamps ({obs.kind} with lamp {name!r})")
        assert name is not None
        spec = setup.lamps.get(name)
        if spec is None:
            raise Unsupported(f"{unit.name}: no lamp {name!r} (it has {sorted(setup.lamps)})")
        timeout = setup.switch_timeout
        outlets = await switch_status(grant, timeout, setup.status_action)
        outlet = outlets.get(name)
        if outlet is None or not outlet.get("lamp"):
            raise Unsupported(f"{unit.name}: its lamp switch has no lamp {name!r}")
        need = setup.duration(name, obs.exptime, obs.count)
        if need > float(outlet["max_on"]):
            raise ObservationFailed(
                f"{obs.name}: {obs.count} x {obs.exptime:g} s keeps lamp {name} on for about"
                f" {need:.0f} s, more than its max_on {float(outlet['max_on']):g} s:"
                " split it into shorter configurations"
            )
        if obs.calstage is None and spec.calstage is not None:
            await self._select_calstage(state, unit, spec.calstage)
        for other, o in outlets.items():
            if o.get("lamp") and o.get("on") and other != name:
                # One lamp's light at a time.
                await grant.call("SetSwitch", o["id"], False, timeout=timeout)
                self._event(
                    "lamp_off", gen=state.gen, telescope=unit.name, lamp=other, why="another lamp"
                )
        # Switching a lamp off is never harmful, so it does not go through
        # the generation's grant: a fence (cancellation) must not stop it.
        off_grant = unit.lamps.grant(f"{self.name}/lamp-off#{state.gen}")

        async def still_on() -> None:
            now = (await switch_status(grant, timeout, setup.status_action)).get(name) or {}
            if not now.get("on"):
                raise LampOff(f"lamp {name} went off (its max_on, or switched off by hand)")

        try:
            await grant.call("SetSwitch", outlet["id"], True, timeout=timeout)
            self._event("lamp_on", gen=state.gen, telescope=unit.name, lamp=name)
            if spec.warmup > 0:
                self._event(
                    "lamp_warmup",
                    gen=state.gen,
                    telescope=unit.name,
                    lamp=name,
                    seconds=spec.warmup,
                )
                await asyncio.sleep(spec.warmup)
            await self._expose_frames(state, unit, tel, cam, obs, taken, check=still_on)
            await still_on()  # the last frame had its lamp throughout
        except BaseException:
            await self._lamp_off(state.gen, unit.name, off_grant, outlet["id"], name, timeout)
            raise
        if not await self._lamp_off(state.gen, unit.name, off_grant, outlet["id"], name, timeout):
            raise LampOff(f"lamp {name} did not confirm off (the cleanup tries again)")

    async def _lamp_off(
        self, gen: int, telescope: str, grant: Grant, sid: int, name: str, timeout: float
    ) -> bool:
        """Switch one lamp off; bounded, and not abandoned if the caller is
        cancelled again. False (and a critical event) if it did not confirm."""
        task = asyncio.ensure_future(grant.call("SetSwitch", sid, False, timeout=timeout))
        task.add_done_callback(lambda t: grant.revoke("lamp frames over"))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise
        except Exception as exc:
            self._event(
                "lamp_off_failed",
                gen=gen,
                telescope=telescope,
                lamp=name,
                error=repr(exc),
                level="critical",
            )
            log.critical("%s: %s: lamp %s did not switch off: %r", self.name, telescope, name, exc)
            return False
        self._event("lamp_off", gen=gen, telescope=telescope, lamp=name, why="frames over")
        return True

    # -- config-defined sequences (docs/sequences.md) ----------------------------------

    async def _sequence(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """Run the config-defined sequence ``obs.sequence``: check it against
        the devices, run its steps, and always its cleanup (module doc of
        :mod:`robofast.sequences`)."""
        from robofast.sequences import SEQUENCE_STEPS, SequenceRun

        try:
            seq = self._sequence_for(obs)
        except ValueError as exc:
            raise ObservationFailed(f"{obs.name}: {exc}") from None
        if seq is None:
            raise ObservationFailed(
                f"{obs.name}: no sequence {obs.sequence!r} in this enclosure's config"
                f" (it has {sorted(self.sequences)})"
            )
        if not seq.runs_on(unit.name):
            raise ObservationFailed(f"{obs.name}: sequence {seq.name} is not for {unit.name}")
        r = SequenceRun(self, state, unit, tel, cam, obs, taken, seq)
        if any(s.type == "lamp" for s in (*seq.steps, *seq.cleanup)):
            await self._sequence_preflight_lamps(r)
        for step in (*seq.steps, *seq.cleanup):
            await SEQUENCE_STEPS[step.type].preflight(step.params, r)
        r.event("sequence_start", steps=len(seq.steps))
        try:
            for i, step in enumerate(seq.steps):
                r.event("sequence_step", index=i, step=step.type)
                await SEQUENCE_STEPS[step.type].run(step.params, r)
        except asyncio.CancelledError:
            await self._sequence_cleanup(r, background=True)
            raise
        except BaseException:
            await self._sequence_cleanup(r, background=False)
            raise
        else:
            await self._sequence_cleanup(r, background=False, strict=True)
        finally:
            r.revoke("sequence over")
        r.event("sequence_done", frames=len(taken))

    def _sequence_for(self, obs: Observation) -> Any:
        """The definition ``obs`` names (None: none), made again from its
        template with ``obs.sequence_params`` (an ARC or LAMP_FLAT request's
        exposure time and count): checked as at load (the lamps' ``max_on``
        too); ValueError if it cannot be."""
        seq = self.sequences.get(obs.sequence or "")
        if seq is None or not obs.sequence_params:
            return seq
        if seq.rebuild is None:
            raise ValueError(
                f"sequence {seq.name} is not made from a template: no parameters to set"
                f" ({sorted(obs.sequence_params)})"
            )
        return seq.rebuild(dict(obs.sequence_params))

    async def _sequence_preflight_lamps(self, r: Any) -> None:
        """The lamp switch's outlets, and each lamp's time on in the
        sequence against the switch's own ``max_on``."""
        unit, setup = r.unit, r.unit.lamp_setup
        if unit.lamps is None or setup is None:
            raise Unsupported(f"{unit.name} has no lamps (sequence {r.sequence.name})")
        r.outlets = await switch_status(r.grant("lamps"), setup.switch_timeout, setup.status_action)
        for lamp, seconds in r.sequence.lamp_time.get(unit.name, {}).items():
            limit = (r.outlets.get(lamp) or {}).get("max_on")
            if limit is not None and seconds > float(limit):
                raise ObservationFailed(
                    f"{r.obs.name}: sequence {r.sequence.name} keeps lamp {lamp} on for about"
                    f" {seconds:.0f} s, more than its max_on {float(limit):g} s: split it"
                )

    def _lamps_still_on(self, r: Any) -> Callable[[], Awaitable[None]]:
        """Before each lamp frame: every lamp the sequence has on still is."""
        setup = r.unit.lamp_setup

        async def check() -> None:
            now = await switch_status(r.grant("lamps"), setup.switch_timeout, setup.status_action)
            for lamp in r.lamps_on:
                if not (now.get(lamp) or {}).get("on"):
                    raise LampOff(f"lamp {lamp} went off (its max_on, or switched off by hand)")

        return check

    async def _sequence_lamp_off(self, r: Any, lamp: str, sid: int) -> None:
        """One lamp off, with a grant of its own (never fenced: switching a
        lamp off is never harmful); not abandoned if cancelled again."""
        setup = r.unit.lamp_setup
        grant = r.unit.lamps.grant(f"{self.name}/lamp-off#{r.gen}:{r.sequence.name}")
        if not await self._lamp_off(r.gen, r.unit.name, grant, sid, lamp, setup.switch_timeout):
            raise LampOff(f"lamp {lamp} did not confirm off (the cleanup tries again)")
        r.lamps_on.pop(lamp, None)

    async def _sequence_cleanup(self, r: Any, *, background: bool, strict: bool = False) -> None:
        """Every lamp the sequence switched on off (now), then its cleanup
        steps with grants of their own: awaited, or -- cancelled --
        finished in the background and waited for by the generation's
        cleanup. ``strict``: a cleanup that fails fails the observation."""
        problems: list[str] = []
        for lamp, sid in list(r.lamps_on.items()):
            try:
                await self._sequence_lamp_off(r, lamp, sid)
            except LampOff as exc:
                problems.append(str(exc))
        task = asyncio.ensure_future(self._sequence_cleanup_steps(r))
        if background:
            tasks = self._sequence_cleanups.setdefault(r.unit.name, set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return
        try:
            problems += await asyncio.shield(task)
        except asyncio.CancelledError:
            tasks = self._sequence_cleanups.setdefault(r.unit.name, set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            raise
        if strict and problems:
            raise ObservationFailed(f"sequence {r.sequence.name}: cleanup: {'; '.join(problems)}")

    async def _sequence_cleanup_steps(self, r: Any) -> list[str]:
        from robofast.sequences import SEQUENCE_STEPS

        r.cleaning = True
        problems: list[str] = []
        try:
            for step in r.sequence.cleanup:
                try:
                    await asyncio.wait_for(
                        SEQUENCE_STEPS[step.type].run(step.params, r),
                        self.config.sequence_cleanup_timeout,
                    )
                except Exception as exc:
                    problems.append(f"{step.where}: {exc!r}")
                    r.event(
                        "sequence_cleanup_failed",
                        step=step.where,
                        error=repr(exc),
                        level="critical",
                    )
                    log.critical(
                        "%s: %s: sequence cleanup: %s: %r", self.name, r.unit.name, step.where, exc
                    )
        finally:
            r.revoke("sequence cleanup over")
        r.event("sequence_cleanup_done", ok=not problems)
        return problems

    async def _cleanup_sequences(self, name: str, report: CleanupReport) -> None:
        """A cancelled sequence's cleanup still running (the calibration
        stage on its way out): wait for it (bounded), then every lamp off."""
        tasks = list(self._sequence_cleanups.get(name, ()))
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self.config.sequence_cleanup_timeout)
            for t in done:
                if not t.cancelled() and t.exception() is None and t.result():
                    report.errors.append(f"{name}: sequence cleanup: {'; '.join(t.result())}")
            if pending:
                report.errors.append(f"{name}: a sequence cleanup still running after the cleanup")
        await self._cleanup_lamps(name, report)

    # -- the ADC (docs/adc.md) ------------------------------------------------------

    async def _adc_track(self, state: _Generation, unit: TelescopeUnit) -> None:
        """ADC tracking on (it re-reads the telescope), then wait, bounded,
        for it to report ``Settled``. A failure or a timeout is a warning
        and the frames go ahead: the ADC sharpens the spectrum, it does not
        make it possible."""
        assert unit.adc is not None
        cfg = self.config
        grant = unit.adc.grant(f"{self.name}/adc#{state.gen}")
        t0 = time.monotonic()
        try:
            sw = unit.adc_switches
            await grant.call("SetSwitch", await sw.id(grant, "tracking"), True)
            settled = await sw.id(grant, "settled")
            await self._wait_for(
                lambda: grant.call("GetSwitch", settled), bool, cfg.adc_settle_timeout
            )
        except (DeviceError, TimeoutError) as exc:
            why = "not settled in time" if isinstance(exc, TimeoutError) else repr(exc)
            self._event(
                "adc_not_settled",
                gen=state.gen,
                telescope=unit.name,
                error=why,
                level="warning",
            )
            log.warning("%s: %s: ADC %s", self.name, unit.name, why)
            return
        finally:
            grant.revoke("adc settled")
        self._event(
            "adc_settled",
            gen=state.gen,
            telescope=unit.name,
            seconds=round(time.monotonic() - t0, 1),
        )

    async def _cleanup_adc(self, name: str, report: CleanupReport) -> None:
        """ADC tracking off: the server turns the prisms to neutral. Bounded;
        an error is reported, not raised."""
        unit = self.units.get(name)
        if unit is None or unit.adc is None:
            return
        grant = unit.adc.grant(f"{self.name}/cleanup-adc#{report.generation}")
        try:
            await grant.call("SetSwitch", await unit.adc_switches.id(grant, "tracking"), False)
        except DeviceError as exc:
            report.errors.append(f"{name}: ADC tracking off: {exc!r}")
        finally:
            grant.revoke("cleanup done")

    async def _cleanup_lamps(self, name: str, report: CleanupReport) -> None:
        """Every lamp of telescope ``name`` off (the switch's ``LampsOff``),
        with a grant of its own; bounded."""
        unit = self.units.get(name)
        if unit is None or unit.lamps is None or unit.lamp_setup is None:
            return
        grant = unit.lamps.grant(f"{self.name}/cleanup-lamps#{report.generation}")
        try:
            off = await lamps_off(grant, unit.lamp_setup.switch_timeout, unit.lamp_setup.off_action)
        except Exception as exc:
            report.errors.append(f"{name}: lamps off: {exc!r}")
            self._event(
                "lamp_off_failed",
                gen=report.generation,
                telescope=name,
                error=repr(exc),
                level="critical",
            )
            return
        finally:
            grant.revoke("cleanup done")
        if off:
            report.lamps_off += [f"{name}:{x}" for x in off]
            self._event("lamps_off", gen=report.generation, telescope=name, lamps=off)

    # -- sky flats (docs/skyflats.md) ---------------------------------------------

    async def _sky_flats(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """Twilight flats opposite the Sun (``kind="sky_flat"``)."""
        from robofast import skyflats
        from robofast.sky import Site, sky_now

        if unit.flats is None:
            raise Unsupported(f"{unit.name} has no sky_flats settings")
        over = dict(obs.flats or {})
        if "filters" in over and isinstance(over["filters"], list):
            over["filters"] = {f: unit.flats.filters.get(f, 1.0) for f in over["filters"]}
        try:
            cfg = replace(unit.flats, **over)
        except TypeError as exc:
            raise ObservationFailed(f"sky flats: {exc}") from exc
        site = Site(cfg.latitude, cfg.longitude)
        now = self.clock()
        ra_h, dec, _ = await asyncio.to_thread(skyflats.flat_spot, cfg.latitude, cfg.longitude, now)
        sky0 = await asyncio.to_thread(sky_now, site, now)
        evening = not sky0.sun_rising
        current: dict[str, str | None] = {"filter": None}

        def sun_alt() -> float:
            return sky_now(site, self.clock()).sun_alt

        async def expose(flt: str, exptime: float, off: tuple[float, float]) -> float:
            if unit.filterwheel is not None and current["filter"] != flt:
                await self._select_filter(state, unit, flt)
                current["filter"] = flt
            cosd = max(math.cos(math.radians(dec)), 1e-3)
            ra = (ra_h + off[0] / 3600.0 / 15.0 / cosd) % 24.0
            d = dec + off[1] / 3600.0
            await self._goto(state, unit, tel, ra, d)
            flat = replace(obs, filter=flt, object=f"skyflat {flt}", name=f"skyflat {flt}")
            image = await self._take_image(state, unit, tel, cam, flat, exptime, taken, flat=True)
            return float(np.median(image))

        async def wait(seconds: float) -> None:
            await asyncio.sleep(seconds)

        frames = await skyflats.take_flats(
            cfg,
            evening=evening,
            sun_alt=sun_alt,
            expose=expose,
            wait=wait,
            event=lambda what, **kw: self._event(what, telescope=unit.name, **kw),
        )
        self.flat_frames += [(unit.name, f) for f in frames]
        if self.imaging is not None and taken:
            await self.imaging.flush(unit.name)
        kept = sum(f.kept for f in frames)
        self._event(
            "sky_flats_done", gen=state.gen, telescope=unit.name, kept=kept, tried=len(frames)
        )
        if not kept:
            raise ObservationFailed("sky flats: no frame at a usable level")

    # -- acquisition (docs/acquisition.md) ---------------------------------------

    @staticmethod
    def _pointing_sigma(unit: TelescopeUnit, port: str | None) -> float | None:
        """The pointing model's cross-validated rms (arcsec), if robofast's
        model is in use: how far from the fiber the target can be."""
        pc = unit.pointing
        built = None if pc is None or not pc.enabled else pc.model_for(port)
        if built is None or not math.isfinite(built.cv_rms):
            return None
        return float(built.cv_rms)

    def _acquired(self, unit: TelescopeUnit, done: dict[str, Any] | None) -> None:
        """How the guider identified the target: kept for the spectrum's
        record and the FITS headers (ACQMETH, ACQSTARS)."""
        unit.acquisition = done
        if not done:
            return
        for name, value, kw, comment in (
            ("acq.method", done.get("method"), "ACQMETH", "How the target was identified"),
            ("acq.stars", done.get("stars"), "ACQSTARS", "Stars in the acquisition frame"),
        ):
            if value is not None:
                self.registry.publish(
                    f"{unit.name}.{name}", value, max_age=None, keyword=kw, comment=comment
                )

    # -- pointing model (docs/pointing_model.md) --------------------------------

    async def _vendor_model_off(self, state: _Generation, unit: TelescopeUnit, tel: Grant) -> None:
        """Switch the mount software's own pointing model off, once a
        generation, where robofast's replaces it (``pointing.vendor_off``)."""
        pc = unit.pointing
        if pc is None or not pc.vendor_off or unit.vendor_off_gen == state.gen:
            return
        await tel.call("Action", pc.vendor_off["action"], pc.vendor_off.get("parameters", ""))
        unit.vendor_off_gen = state.gen
        self._event("vendor_model_off", gen=state.gen, telescope=unit.name)

    async def _take_image(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        exptime: float,
        taken: list[Any],
        **mark: Any,
    ) -> np.ndarray:
        """One light exposure, saved (with imaging) and returned in FITS
        order (rows = y): what a calibration routine measures."""
        imaging = self.imaging
        if imaging is not None:
            imaging.check(unit.name)
        i = len(taken)
        unit.exposing = True
        try:
            host_start = self.clock()
            await cam.call("StartExposure", exptime, True)
            self._event(
                "exposure_started", gen=state.gen, telescope=unit.name, index=i, exptime=exptime
            )
            exposure = None
            if imaging is not None:
                exposure = await imaging.exposure_started(
                    unit.name,
                    tel,
                    replace(obs, exptime=exptime),
                    index=i,
                    host_start=host_start,
                    sequencer=self.name,
                    generation=state.gen,
                    ids=self._ids(unit.name),
                )
            await self._wait_for(
                lambda: image_ready(cam.get), bool, exptime + self.config.readout_margin
            )
            if exposure is not None:
                assert imaging is not None
                frame: Any = await imaging.exposure_done(unit.name, cam, exposure, keep_image=True)
                image, frame.image = frame.image, None
            else:
                frame = i
                image = await cam.get("ImageArray", timeout=self.config.readout_margin)
        finally:
            unit.exposing = False
        taken.append(frame)
        self.frames.append(
            {
                "gen": state.gen,
                "telescope": unit.name,
                "index": i,
                "target": obs.name,
                "t": time.monotonic(),
                **mark,
                **({"image": frame} if isinstance(frame, PendingFrame) else {}),
            }
        )
        self._event("exposure_complete", gen=state.gen, telescope=unit.name, index=i)
        data = image.data if isinstance(image, ImageArray) else np.asarray(image)
        return fits_order(data)

    async def _pointing_run(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """A pointing run (``kind="pointing_model"``): ``obs.count`` points
        over the sky, each slewed to with robofast's model off, exposed,
        plate-solved and recorded; then a model is built, saved and used."""
        pc = unit.pointing
        if pc is None or pc.solver is None or pc.catalog is None:
            raise Unsupported(
                f"{unit.name} cannot build a pointing model (no pointing entry with"
                " solver and catalog)"
            )
        run = {**POINTING_RUN_DEFAULTS, **pc.run, **(obs.pointing or {})}
        unknown = set(run) - set(POINTING_RUN_DEFAULTS)
        if unknown:
            raise ObservationFailed(f"pointing run: unknown settings {sorted(unknown)}")
        if run["terms"] is not None:
            # Checked before the telescope moves, not after the run's points.
            from robofast.pointing import ALTAZ_TERMS, EQUATORIAL_TERMS

            known = ALTAZ_TERMS if pc.mount == "altaz" else EQUATORIAL_TERMS
            terms = run["terms"]
            # A config error: never retried (a static plan re-queues a failure).
            if isinstance(terms, str) or not terms or any(t not in known for t in terms):
                raise Unsupported(
                    f"pointing run: terms {terms!r} must be a list of {pc.mount}"
                    f" TPOINT terms ({', '.join(known)})"
                )
            if run["min_points"] < len(terms):
                raise Unsupported(
                    f"pointing run: min_points {run['min_points']} cannot carry {len(terms)} terms"
                )
        await self._vendor_model_off(state, unit, tel)
        lst0 = float(await tel.get("SiderealTime")) * 15.0
        avoid = []
        if run["moon_distance"]:
            try:
                from robofast.sky import Site, offline_iers, sky_now

                offline_iers()
                moon = sky_now(Site(pc.latitude, pc.longitude), self.clock()).moon
                avoid.append((float(moon.ra.deg), float(moon.dec.deg), float(run["moon_distance"])))
            except Exception:
                log.exception("%s: the Moon's position for the pointing run", unit.name)
        grid = pointmodel.sky_grid(
            obs.count,
            min_alt=run["min_alt"],
            max_alt=run["max_alt"],
            latitude=pc.latitude,
            lst=lst0,
            avoid=avoid,
            max_hour_angle=run["max_hour_angle"],
            min_dec=run["min_dec"],
            max_dec=run["max_dec"],
        )
        binning = int(await cam.get("BinX"))
        self._event(
            "pointing_run_start",
            gen=state.gen,
            telescope=unit.name,
            port=obs.port,
            points=len(grid),
        )
        samples: list[pointmodel.Sample] = []
        pc.running = True
        try:
            for i, (az, alt) in enumerate(grid):
                lst = float(await tel.get("SiderealTime")) * 15.0
                ra, dec = pointmodel.grid_radec(az, alt, pc.latitude, lst)
                try:
                    await self._goto(state, unit, tel, ra / 15.0, dec)
                except SlewDenied as exc:
                    self._event("pointing_point_skipped", telescope=unit.name, i=i, why=str(exc))
                    continue
                if run["settle"]:
                    await asyncio.sleep(run["settle"])
                mra = float(await tel.get("RightAscension")) * 15.0
                mdec = float(await tel.get("Declination"))
                lst = float(await tel.get("SiderealTime")) * 15.0
                when = self.clock()
                image = await self._take_image(
                    state, unit, tel, cam, obs, obs.exptime, taken, pointing=True
                )
                mid = lst + 0.5 * obs.exptime * pointmodel.SIDEREAL_DEG_PER_S
                try:
                    sample = await asyncio.to_thread(
                        pointmodel.solve_sample,
                        image,
                        pc,
                        mra,
                        mdec,
                        mid,
                        when,
                        obs.port,
                        binning,
                    )
                except Exception as exc:  # SolveError, CatalogUnavailable
                    self._event(
                        "pointing_point_unsolved", telescope=unit.name, i=i, error=str(exc)[:200]
                    )
                    continue
                pc.add_sample(sample)
                samples.append(sample)
                self._event(
                    "pointing_point",
                    telescope=unit.name,
                    i=i,
                    az=round(az, 2),
                    alt=round(alt, 2),
                    error_arcsec=round(sample.error_arcsec, 2),
                )
            if self.imaging is not None and taken:
                await self.imaging.flush(unit.name)
        finally:
            pc.running = False
        if len(samples) < run["min_points"]:
            raise ObservationFailed(
                f"pointing run: {len(samples)} of {len(grid)} points solved"
                f" (at least {run['min_points']} needed)"
            )
        built = await asyncio.to_thread(
            pointmodel.build,
            samples,
            mount=pc.mount,
            latitude=pc.latitude,
            telescope=unit.name,
            port=obs.port,
            significance=run["significance"],
            min_points=run["min_points"],
            terms=run["terms"],
            when=self.clock(),
        )
        built.note = f"pointing run {obs.name}"
        pc.set_model(built)
        self.pointing_models.append(built)
        self._event(
            "pointing_model_built",
            gen=state.gen,
            telescope=unit.name,
            port=obs.port,
            terms=built.model.terms,
            rms_before=built.rms_before,
            rms_after=built.rms_after,
            cv_rms=built.cv_rms,
            n=built.n,
        )

    # -- rotator / PA calibration (docs/rotcal.md) ------------------------------

    async def _rotator_calibration(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
        ra: float,
        dec: float,
    ) -> None:
        """Measure the port's PA and parity against the rotator's mechanical
        angle on the current field, save it, use it (``kind="rotator_calibration"``)."""
        from robofast import rotcal
        from robofast.astrometry import solve_near

        setup = unit.pa_calibration
        if setup is None or setup.solver is None or setup.catalog is None:
            raise Unsupported(
                f"{unit.name} cannot calibrate its PA (no pa_calibration with solver and catalog)"
            )
        opts = dict(obs.rotator or {})
        rot = state.rotators.get(unit.name)
        angles = [float(a) for a in opts.get("angles", setup.angles)]
        start = None
        if rot is None:
            angles = angles[:1] or [0.0]
        else:
            start = float(await rot.get("MechanicalPosition"))
        binning = int(await cam.get("BinX"))
        cfg = replace(setup.solver, pa=None, flip=None)
        when = self.clock()
        # Solve near where the telescope points: the target, or the mount.
        if unit.pointing is None:
            ra0, dec0 = (
                float(await tel.get("RightAscension")) * 15.0,
                float(await tel.get("Declination")),
            )
        else:
            ra0, dec0 = ra * 15.0, dec

        async def move(angle: float) -> None:
            assert rot is not None
            await rot.call("MoveMechanical", angle)
            await self._wait_for(lambda: rot.get("IsMoving"), lambda m: not m, setup.move_timeout)
            if setup.settle:
                await asyncio.sleep(setup.settle)
            self._event("rotator_moved", telescope=unit.name, mechanical=angle)

        async def expose() -> np.ndarray:
            return await self._take_image(
                state, unit, tel, cam, obs, obs.exptime, taken, calibration="pa"
            )

        def solve(image: np.ndarray) -> Any:
            return solve_near(image, setup.catalog, ra0, dec0, cfg, binning=binning, epoch=when)

        try:
            cal = await rotcal.calibrate(
                angles,
                move=move if rot is not None else None,
                expose=expose,
                solve=solve,
                telescope=unit.name,
                port=obs.port,
                sense=opts.get("sense", setup.sense),
                min_measurements=min(setup.min_measurements, len(angles)),
                event=lambda what, **kw: self._event(what, telescope=unit.name, **kw),
                source=f"rotator calibration {obs.name}",
            )
        except ValueError as exc:
            raise ObservationFailed(str(exc)) from exc
        finally:
            if rot is not None and start is not None:
                await move(start)
        setup.store.save(cal)
        self.pa_calibrations.append(cal)
        self._event(
            "pa_calibrated",
            gen=state.gen,
            telescope=unit.name,
            port=obs.port,
            offset=cal.offset,
            sense=cal.sense,
            flip=cal.flip,
            rms=cal.rms,
            n=cal.n,
        )
        if self.imaging is not None and taken:
            await self.imaging.flush(unit.name)

    # -- manual control (the observer console) -------------------------------

    async def _manual(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """An operator's manual session on this telescope (module doc,
        "Manual control"): run their commands, one at a time, until they
        release it. Its grants are this generation's, fenced like the rest:
        a revoked lease ends it at once."""
        hook = getattr(self.plan, "manual_session", None)
        session = hook(unit.name) if hook is not None else None
        if session is None:
            return  # released meanwhile
        holder = f"{self.name}/manual:{session.operator}#{state.gen}"
        mtel, mcam = unit.telescope.grant(holder), unit.camera.grant(holder)
        state.extra_grants += [mtel, mcam]
        self._event("manual_start", gen=state.gen, telescope=unit.name, operator=session.operator)
        session.started(state.gen)
        guiding = False
        released = False
        try:
            while True:
                cmd = await session.next()
                if cmd is None:
                    released = True
                    return
                op = cmd.get("op")
                try:
                    detail = await self._manual_command(state, unit, mtel, mcam, cmd, taken)
                except (LeaseRevoked, GrantRevoked) as exc:
                    session.result(cmd, False, f"interrupted: {exc}")
                    raise
                except asyncio.CancelledError:
                    session.result(cmd, False, "interrupted")
                    raise
                except Exception as exc:
                    session.result(cmd, False, f"{type(exc).__name__}: {exc}")
                    self._event(
                        "manual_command",
                        gen=state.gen,
                        telescope=unit.name,
                        op=op,
                        ok=False,
                        error=repr(exc),
                        level="warning",
                    )
                    continue
                guiding = op == "guide_start" or (guiding and op != "guide_stop")
                session.result(cmd, True, detail)
                self._event(
                    "manual_command", gen=state.gen, telescope=unit.name, op=op, ok=True, **detail
                )
        finally:
            lease = state.lease
            if released:
                why = "released"
            elif state.revoked or mtel.revoked or (lease is not None and lease.revoked.is_set()):
                why = "the lease was revoked"
            else:
                why = "interrupted"
            session.stopped(why)
            if guiding and unit.guider is not None:
                await unit.guider.stop("manual control ended")  # bounded, never raises
            self._event("manual_end", gen=state.gen, telescope=unit.name, why=why)

    async def _manual_command(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        cmd: dict[str, Any],
        taken: list[Any],
    ) -> dict[str, Any]:
        op = cmd.get("op")
        lease = state.lease
        sky = op in ("slew", "offset", "guide_start") or (op == "expose" and cmd.get("light", True))
        if sky:
            if lease is None or lease.revoked.is_set():
                raise LeaseRevoked("no live lease")
            if not lease.opened.is_set():
                raise ManualRefused("the enclosure is not open yet")
        if op in ("slew", "offset"):
            if await tel.get("AtPark"):
                await tel.call("Unpark")
            await tel.set("Tracking", True)
            if op == "offset":
                ra = float(await tel.get("RightAscension"))
                dec = float(await tel.get("Declination"))
                cosd = max(1e-3, math.cos(math.radians(dec)))
                ra = (ra + float(cmd.get("east_arcsec", 0)) / 3600.0 / 15.0 / cosd) % 24.0
                dec = max(-90.0, min(90.0, dec + float(cmd.get("north_arcsec", 0)) / 3600.0))
            else:
                ra, dec = float(cmd["ra_hours"]), float(cmd["dec_deg"])
                if unit.pointing is not None:
                    # A target goes through robofast's pointing model like any
                    # other slew (an offset moves the mount from where it is).
                    await self._vendor_model_off(state, unit, tel)
                    lst = float(await tel.get("SiderealTime"))
                    pc = unit.pointing
                    ra, dec, _ = pc.command(ra, dec, lst, pc.port, self.clock())
            await self._goto(state, unit, tel, ra, dec)
            return {"ra_hours": round(ra, 6), "dec_deg": round(dec, 5)}
        if op == "focus":
            if state.focusers.get(unit.name) is None:
                raise Unsupported(f"{unit.name}: the sequencer moves no focuser here")
            af = self._focus_runner(state, unit, None, None, None, exposer=_no_exposures)
            if cmd.get("position") is not None:
                target = float(cmd["position"])
            else:
                target = float(await state.focusers[unit.name].get("Position")) + float(
                    cmd.get("delta", 0)
                )
            await af.move(target)
            return {"position": target}
        if op == "expose":
            light = bool(cmd.get("light", True))
            obs = Observation(
                f"manual-{unit.name}",
                exptime=float(cmd["exptime"]),
                count=int(cmd.get("count", 1)),
                light=light,
                object=str(cmd.get("object") or "manual"),
            )
            before = len(taken)
            await self._expose_frames(state, unit, tel, cam, obs, taken)
            files = [
                f.path for f in taken[before:] if isinstance(f, PendingFrame) and f.path is not None
            ]
            return {"frames": len(taken) - before, "files": files}
        if op in ("guide_start", "guide_stop"):
            if unit.guider is None:
                raise Unsupported(f"{unit.name} has no guider")
            if op == "guide_stop":
                await unit.guider.stop("operator")
                return {}
            await unit.guider.start(dec=float(await tel.get("Declination")))
            return {}
        raise ManualRefused(f"unknown manual command {op!r}")

    # -- autofocus ----------------------------------------------------------

    def _focus_runner(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        cam: Grant | None,
        port: str | None,
        filter: str | None,
        extra: dict[str, Any] | None = None,
        exposer: Any = None,
    ) -> Autofocus:
        setup, fg = unit.autofocus, state.focusers.get(unit.name)
        if setup is None or fg is None:
            raise Unsupported(f"{unit.name} cannot autofocus (no focuser or autofocus config)")
        model = setup.model
        tel_name = unit.name

        def values() -> dict[str, float | None]:
            snap = self.registry.snapshot()
            names = {*model.terms, *FOCUS_CONTEXT}
            return {
                name: snap.current(term_source(name).replace("{tel}", tel_name)) for name in names
            }

        return Autofocus(
            unit.name,
            setup.config_for(port, filter, extra),
            fg,
            cam,
            exposer=exposer,
            store=setup.store,
            key=FocusStore.key(unit.name, port, filter),
            model=model,
            values=values,
            event=lambda what, **kw: self._event(what, gen=state.gen, **kw),
            clock=self.clock,
        )

    async def _read_altitude(self, unit: TelescopeUnit, tel: Grant) -> None:
        """The mount's altitude into the registry, for the focus model's
        gravity term (best effort, like the temperature)."""
        try:
            alt = float(await tel.get("Altitude"))
        except Exception as exc:
            self.registry.failed(f"{unit.name}.tel.alt", repr(exc))
            return
        self.registry.publish(
            f"{unit.name}.tel.alt",
            alt,
            max_age=300.0,
            unit="deg",
            keyword="ALT",
            comment="Altitude at exposure start",
        )

    async def _read_focuser(self, unit: TelescopeUnit, fg: Grant) -> None:
        """The focuser's temperature into the registry, for the focus model
        (best effort: a focuser without one just leaves the term out)."""
        try:
            temp = await fg.get("Temperature")
        except Exception as exc:
            self.registry.failed(f"{unit.name}.focuser.temperature", repr(exc))
            return
        self.registry.publish(
            f"{unit.name}.focuser.temperature",
            float(temp),
            max_age=300.0,
            unit="C",
            keyword="FOCTEMP",
            comment="Focuser temperature",
        )

    async def _autofocus(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """Focus ``unit`` on the current field (``obs.kind == "autofocus"``)."""
        extra = dict(obs.autofocus or {})
        calibrate = bool(extra.pop("calibrate", False))
        fg = state.focusers.get(unit.name)
        if fg is None or unit.autofocus is None:
            raise Unsupported(f"{unit.name} cannot autofocus (no focuser or autofocus config)")
        await self._read_focuser(unit, fg)
        await self._read_altitude(unit, tel)
        cfg = self.config
        imaging = self.imaging

        async def expose(exptime: float) -> np.ndarray:
            if imaging is not None:
                imaging.check(unit.name)
            i = len(taken)
            unit.exposing = True
            try:
                host_start = self.clock()
                await cam.call("StartExposure", exptime, True)
                self._event(
                    "exposure_started", gen=state.gen, telescope=unit.name, index=i, exptime=exptime
                )
                exposure = None
                if imaging is not None:
                    exposure = await imaging.exposure_started(
                        unit.name,
                        tel,
                        replace(obs, exptime=exptime),
                        index=i,
                        host_start=host_start,
                        sequencer=self.name,
                        generation=state.gen,
                        ids=self._ids(unit.name),
                    )
                await self._wait_for(
                    lambda: image_ready(cam.get), bool, exptime + cfg.readout_margin
                )
                if exposure is not None:
                    assert imaging is not None
                    frame: Any = await imaging.exposure_done(
                        unit.name, cam, exposure, keep_image=True
                    )
                    image, frame.image = frame.image, None
                else:
                    frame = i
                    image = await cam.get("ImageArray", timeout=af.config.download_timeout)
            finally:
                unit.exposing = False
            taken.append(frame)
            self.frames.append(
                {
                    "gen": state.gen,
                    "telescope": unit.name,
                    "index": i,
                    "target": obs.name,
                    "t": time.monotonic(),
                    "autofocus": True,
                    **({"image": frame} if isinstance(frame, PendingFrame) else {}),
                }
            )
            self._event("exposure_complete", gen=state.gen, telescope=unit.name, index=i)
            data = image.data if isinstance(image, ImageArray) else np.asarray(image)
            return fits_order(data)

        af = self._focus_runner(state, unit, cam, obs.port, obs.filter, extra, expose)
        unit.focus_restore = float(await fg.get("Position"))
        result = await (af.calibrate() if calibrate else af.run())
        # The run left the focuser at its new focus, or back where it started.
        unit.focus_restore = None
        unit.autofocus_results.append(result)
        publish_result(self.registry, unit.name, result)
        hook = getattr(self.plan, "autofocus_result", None)
        if hook is not None:
            try:
                hook(unit.name, obs, result.summary())
            except Exception:
                log.exception("%s: plan.autofocus_result(%s)", self.name, unit.name)
        if imaging is not None and taken:
            await imaging.flush(unit.name)
        if not result.success:
            raise ObservationFailed(f"autofocus: {result.message}")

    async def _restore_focus(self, state: _Generation, unit: TelescopeUnit) -> None:
        """Undo an autofocus that was cut short (the focuser was left defocused)."""
        target = unit.focus_restore
        if target is None or state.focusers.get(unit.name) is None:
            return
        af = self._focus_runner(state, unit, None, None, None, exposer=_no_exposures)
        await af.move(target)
        self._event("focus_restored", gen=state.gen, telescope=unit.name, position=target)
        unit.focus_restore = None

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

    async def _select_named(
        self, fw: Grant, unit: TelescopeUnit, name: str, what: str, timeout: float
    ) -> int:
        """Move an Alpaca FilterWheel (a filter wheel, or the calibration
        stage) to the position called ``name``; returns its index."""
        names = list(await fw.get("Names"))
        if name not in names:
            raise Unsupported(f"{unit.name}: no {what} {name!r} in {names}")
        position = names.index(name)
        try:
            current = await fw.get("Position")
        except InvalidOperationException:
            # The calibration stage rests between named positions (moved by
            # an absolute move): any named move starts from there.
            current = None
        if current != position:
            await fw.set("Position", position)
            # Position reads -1 while the wheel moves.
            await self._wait_for(lambda: fw.get("Position"), lambda p: p == position, timeout)
        return position

    async def _select_calstage(self, state: _Generation, unit: TelescopeUnit, name: str) -> None:
        stage = state.calstages.get(unit.name)
        if stage is None:
            raise Unsupported(f"{unit.name} has no calibration stage (calstage {name!r})")
        await self._select_named(
            stage, unit, name, "calstage position", self.config.calstage_timeout
        )
        self._event("calstage_selected", gen=state.gen, telescope=unit.name, calstage=name)

    async def _select_filter(self, state: _Generation, unit: TelescopeUnit, name: str) -> None:
        fw = state.filterwheels.get(unit.name)
        if fw is None:
            raise Unsupported(f"{unit.name} has no filter wheel (filter {name!r})")
        position = await self._select_named(fw, unit, name, "filter", self.config.filter_timeout)
        self._event("filter_selected", gen=state.gen, telescope=unit.name, filter=name)
        if self.imaging is not None:
            self.imaging.filter_selected(unit.name, name, position)

    # -- spectra --------------------------------------------------------------------

    def _held(self) -> set[str]:
        """Telescopes an operator holds now (the plan's ``held``, from
        :class:`~robofast.console.hold.OperatorHold`)."""
        try:
            return set(getattr(self.plan, "held", None) or ())
        except Exception:
            return set()

    @property
    def night_rule(self) -> NightRule | None:
        """The site's night rule (robofast.clock): every night name the
        sequencer writes or asks for -- the frames, the sidecars, the raw
        spectrum file names -- comes from this one rule and the clock. The
        one given (sequencer_service.build gives the site's), else the
        spectrum sidecars', else the frames'."""
        if self._night_rule is not None:
            return self._night_rule
        if self.spectra is not None:
            return self.spectra.rule
        if self.imaging is not None:
            return self.imaging.store.rule
        return None

    def _night_name(self, when: datetime) -> str | None:
        """The night ``when`` belongs to (``n<YYYYMMDD>``), by the one night
        rule the frames and the spectrum sidecars use; None without one."""
        return None if self.night_rule is None else self.night_rule.name(when)

    def _spec_name(self, telescope: str) -> str:
        return self.spectrograph_names.get(telescope, telescope)

    async def _locate_fiber(self, state: _Generation, unit: TelescopeUnit) -> None:
        """Find the fiber with the backlight (one telescope at a time: the
        backlight lights the whole fiber bundle); on failure, guide on the
        configured position (legacy did the same)."""
        locate = getattr(unit.guider, "locate", None)
        if not self.config.locate_fiber or locate is None:
            return
        async with self._locate_lock:
            try:
                x, y = await asyncio.wait_for(locate(), self.config.locate_timeout)
            except (GrantRevoked, LeaseRevoked):
                raise
            except Exception as exc:
                self._event(
                    "fiber_not_located",
                    gen=state.gen,
                    telescope=unit.name,
                    error=repr(exc),
                    level="warning",
                )
                log.warning("%s: %s: fiber not located (%r)", self.name, unit.name, exc)
                return
        self._event("fiber_located", gen=state.gen, telescope=unit.name, x=x, y=y)

    async def _spectrum(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        cam: Grant,
        obs: Observation,
        taken: list[Any],
    ) -> None:
        """``unit`` is on target and guiding: lead the spectrum, or feed the
        one another telescope leads (module doc, "Spectra")."""
        assert obs.spectrum is not None
        sid = str(obs.spectrum["id"])
        leader = obs.spectrum.get("leader") or unit.name
        if leader != unit.name or sid in state.spectra:
            await self._spectrum_member(state, unit, tel, obs, sid)
            return
        if sid in self._spectra_done:
            self._event("spectrum_already_taken", gen=state.gen, telescope=unit.name, spectrum=sid)
            return
        if state.spectrograph is None:
            raise Unsupported(f"{unit.name}: no spectrograph ({obs.name} is a spectrum)")
        feed = [t for t in obs.spectrum.get("feed") or [] if t in self.units]
        held = self._held()
        expected = {
            t
            for t in feed
            if t != unit.name
            and t in state.working
            and t not in held
            and self.units[t].guider is not None
        }
        session = _Spectrum(sid, obs, unit.name, expected)
        state.spectra[sid] = session
        unit.spectrum = sid
        session.mark_ready(unit.name)
        self._event(
            "spectrum_session",
            gen=state.gen,
            telescope=unit.name,
            spectrum=sid,
            feed=feed or [unit.name],
            waiting_for=sorted(expected),
        )
        watch = asyncio.create_task(
            self._spectrum_watch(state, unit, tel, session, cam),
            name=f"{self.name}-{unit.name}-spectrum-watch",
        )
        outcome = "failed"
        own_error: BaseException | None = None
        try:
            try:
                await self._spectrum_expose(state, unit, obs, session, taken, watch)
                outcome = "complete"
            finally:
                session.done.set()
                session.pulse()
                if not watch.done():
                    watch.cancel()
                await asyncio.wait({watch}, timeout=self.config.cancel_ack_timeout)
                if watch.done() and not watch.cancelled():
                    own_error = watch.exception()
        except Exception as exc:
            if own_error is not None and not isinstance(own_error, asyncio.CancelledError):
                raise own_error from exc  # the leader's own failure comes first
            raise
        finally:
            unit.spectrum = None
            state.spectra.pop(sid, None)
            self._spectra_done[sid] = outcome
        if own_error is not None:
            raise _LeaderDropped(own_error)

    async def _spectrum_member(
        self, state: _Generation, unit: TelescopeUnit, tel: Grant, obs: Observation, sid: str
    ) -> None:
        """Feed another telescope's spectrum: wait (bounded) for its leader to
        open it, join, and watch the guider and the mount until it ends."""
        cfg = self.config
        deadline = time.monotonic() + cfg.spectrum_ready_timeout
        session = state.spectra.get(sid)
        while session is None:
            if sid in self._spectra_done or time.monotonic() > deadline:
                self._event(
                    "spectrum_missed", gen=state.gen, telescope=unit.name, spectrum=sid, why="ended"
                )
                return
            await asyncio.sleep(cfg.poll_interval)
            session = state.spectra.get(sid)
        if session.started or session.done.is_set():
            self._event(
                "spectrum_missed", gen=state.gen, telescope=unit.name, spectrum=sid, why="started"
            )
            return
        unit.spectrum = sid
        try:
            session.mark_ready(unit.name)
            self._event(
                "spectrum_joined",
                gen=state.gen,
                telescope=unit.name,
                spectrum=sid,
                leader=session.leader,
            )
            await self._spectrum_watch(state, unit, tel, session)
        finally:
            unit.spectrum = None

    async def _spectrum_watch(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        tel: Grant,
        session: _Spectrum,
        cam: Grant | None = None,
    ) -> None:
        """While the spectrum lasts, check this telescope's guider and mount
        every ``spectrum_check_interval``; on a failure (or a preemption of a
        member) it drops out of the feed and the error is raised."""
        cfg = self.config
        guider = unit.guider
        assert guider is not None
        try:
            while True:
                tick = session.tick
                if session.done.is_set():
                    return
                policy = unit.preempt_policy
                if (
                    unit.name != session.leader
                    and policy in ("now-after-readout", "now")
                    and not unit.exposing
                ):
                    session.drop(unit.name, f"preempted ({policy})")
                    self._record_preemption(state, unit, policy, aborted=False)
                    raise Preempted(policy)
                await _wait_any([session.done, tick], cfg.spectrum_check_interval)
                if session.done.is_set():
                    return
                if tick.is_set():
                    continue  # a change: look again before the periodic check
                await guider.check()
                if not await tel.get("Tracking"):
                    raise DeviceError(f"{unit.name} stopped tracking")
        except asyncio.CancelledError:
            session.drop(unit.name, "cancelled")
            raise
        except Preempted:
            raise
        except Exception as exc:
            session.drop(unit.name, repr(exc))
            self._event(
                "spectrum_dropped",
                gen=state.gen,
                telescope=unit.name,
                spectrum=session.id,
                error=repr(exc),
                level="warning",
            )
            if unit.name == session.leader and cam is not None:
                # The spectrum goes on without it: make this telescope safe
                # now (its error is handled once the spectrum ends).
                report = CleanupReport(state.gen, [unit.name])
                try:
                    await self._cleanup_unit(unit.name, tel, cam, report)
                except GrantRevoked:
                    pass  # fenced: the generation's cleanup takes over
                self._event(
                    "telescope_safed",
                    gen=state.gen,
                    telescope=unit.name,
                    aborted=bool(report.aborted),
                    parked=bool(report.parked),
                    errors=report.errors,
                )
            raise

    async def _spectrum_expose(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        obs: Observation,
        session: _Spectrum,
        taken: list[Any],
        watch: asyncio.Task,
    ) -> None:
        """The leader's part: wait for the feed, then take ``obs.count``
        spectra while at least one telescope of the feed is guiding."""
        cfg = self.config
        assert obs.spectrum is not None and state.spectrograph is not None
        deadline = time.monotonic() + cfg.spectrum_ready_timeout
        while True:
            # A member an operator holds now will not come (docs/console.md).
            held = self._held() - set(session.ready)
            pending = session.expected - set(session.ready) - set(session.dropped) - held
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            await _wait_any([session.tick], min(remaining, cfg.spectrum_check_interval))
        for t in sorted(session.expected & (self._held() - set(session.ready))):
            session.expected.discard(t)
            self._event("spectrum_member_held", gen=state.gen, telescope=t, spectrum=session.id)
        missing = sorted(session.expected - set(session.ready) - set(session.dropped))
        if missing:
            self._event(
                "spectrum_feed_timeout",
                gen=state.gen,
                telescope=unit.name,
                spectrum=session.id,
                missing=missing,
                level="warning",
            )
        if not session.active():
            raise SpectrumFailed(f"{session.id}: no telescope of the feed is guiding")
        try:
            async with asyncio.timeout(cfg.spectrum_ready_timeout):
                await self._spec_lock.acquire()
        except TimeoutError as exc:
            raise SpectrographBusy("the spectrograph is busy with another job") from exc
        try:
            for i in range(obs.count):
                policy = unit.preempt_policy
                if i > 0 and policy in ("now-after-readout", "now"):
                    self._record_preemption(state, unit, policy, aborted=False)
                    raise Preempted(policy)
                taken.append(await self._spectrum_frame(state, unit, obs, session, i))
        finally:
            self._spec_lock.release()

    async def _spectrum_frame(
        self, state: _Generation, unit: TelescopeUnit, obs: Observation, session: _Spectrum, i: int
    ) -> SpectrumFrame:
        cfg = self.config
        spec = obs.spectrum
        assert spec is not None and state.spectrograph is not None
        feeding = session.active()
        if not feeding:
            raise SpectrumFailed(f"{session.id}: every telescope of the feed dropped out")
        frame: dict[str, Any] = {
            "kind": "science",
            "exptime": obs.exptime,
            "object": _spec_object(obs.object or obs.name),
            "telescopes": [self._spec_name(t) for t in feeding],
        }
        # The instrument's own parameters, as it takes them (kinds.Spectrum).
        frame.update(spec.get("params") or {})
        asked = self.clock()
        night = self._night_name(asked)
        if night is not None:
            frame["night"] = night
        session.started = True
        exposing = [self.units[t] for t in feeding]
        for u in exposing:
            u.exposing = True
        session.pulse()
        try:
            try:
                job = await state.spectrograph.expose(**frame)
            except SpectrographBusy as exc:
                raise SpectrographBusy(str(exc)) from exc
            except SpectrographError as exc:
                raise SpectrographFault(f"expose: {exc}") from exc
            job_id = int(job["id"])
            self._spec_job, self._spec_job_owner = job_id, unit.name
            self._event(
                "spectrum_started",
                gen=state.gen,
                telescope=unit.name,
                spectrum=session.id,
                index=i,
                job=job_id,
                telescopes=feeding,
                exptime=obs.exptime,
            )
            view = await self._wait_spectrograph_job(
                state, unit, job_id, obs.exptime + cfg.spectrum_margin, session
            )
        finally:
            for u in exposing:
                u.exposing = False
            session.pulse()
        self._spectrograph_ok()
        result = (view.get("results") or [{}])[-1]
        end = session.active()
        stats = await self._guide_stats(feeding)
        record = self._spectrum_record(state, unit, obs, session, result, feeding, end, stats)
        record["night"] = night
        record["sequencer_utc"] = asked.isoformat()
        path = self._write_spectrum(record)
        self.frames.append(
            {
                "gen": state.gen,
                "telescope": unit.name,
                "index": i,
                "target": obs.name,
                "t": time.monotonic(),
                "spectrum": result.get("file"),
            }
        )
        self._event(
            "spectrum_done",
            gen=state.gen,
            telescope=unit.name,
            spectrum=session.id,
            index=i,
            file=result.get("file"),
            sidecar=path,
            telescopes=feeding,
            telescopes_end=end,
            dropped=dict(session.dropped),
            ended_by=result.get("ended_by"),
            flux_midpoint_utc=result.get("flux_midpoint_utc"),
        )
        return SpectrumFrame(str(result.get("file")), path, record)

    async def _wait_spectrograph_job(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        job_id: int,
        bound: float,
        session: _Spectrum | None = None,
    ) -> dict[str, Any]:
        """Wait (bounded) for a spectrograph job; abort it if every
        telescope feeding it drops out, or if it overruns ``bound``."""
        cfg = self.config
        assert self.spectrograph is not None
        t0 = time.monotonic()
        while True:
            try:
                view = await self.spectrograph.job(job_id, wait=cfg.spectrum_poll)
            except SpectrographError as exc:
                if time.monotonic() - t0 <= bound:
                    log.warning("%s: spectrograph job %d: %s", self.name, job_id, exc)
                    await asyncio.sleep(cfg.poll_interval)
                    continue
                report = CleanupReport(state.gen, [unit.name])
                await self._abort_spectrograph(f"not answering: {exc}", report)
                raise SpectrographFault(f"job {job_id}: {exc}") from exc
            if view.get("state") in ("done", "failed", "aborted"):
                break
            why = None
            if session is not None and not session.active():
                why = "every telescope of the feed dropped out"
            elif time.monotonic() - t0 > bound:
                why = f"not finished within {bound:.0f} s"
            if why is not None:
                report = CleanupReport(state.gen, [unit.name])
                await self._abort_spectrograph(why, report)
                if session is not None and not session.active():
                    raise SpectrumFailed(f"{session.id}: {why}")
                raise SpectrographFault(f"job {job_id}: {why}")
        self._spec_job = self._spec_job_owner = None
        if view.get("state") == "aborted" and session is not None and not session.active():
            raise SpectrumFailed(f"{session.id}: every telescope of the feed dropped out")
        if view.get("state") != "done":
            raise SpectrographFault(f"job {job_id} {view.get('state')}: {view.get('error')}")
        return view

    async def _guide_stats(self, telescopes: list[str]) -> dict[str, Any]:
        """Each feeding telescope's guide telemetry now (residual rms, flux,
        ...), from its guider's status; failures are recorded, not raised."""
        out: dict[str, Any] = {}

        async def one(name: str) -> None:
            status = getattr(self.units[name].guider, "status", None)
            if status is None:
                out[name] = {"error": "guider has no status"}
                return
            try:
                st = await asyncio.wait_for(status(), 10.0)
            except Exception as exc:
                out[name] = {"error": repr(exc)}
                return
            out[name] = {
                "state": st.get("state"),
                "target": st.get("target"),
                "reacquisitions": st.get("reacquisitions"),
                "telemetry": st.get("telemetry") or {},
            }

        await asyncio.gather(*(one(t) for t in telescopes))
        return out

    def _spectrum_record(
        self,
        state: _Generation,
        unit: TelescopeUnit,
        obs: Observation,
        session: _Spectrum,
        result: dict[str, Any],
        feeding: list[str],
        end: list[str],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        spec = obs.spectrum or {}
        record: dict[str, Any] = {
            "version": 1,
            "kind": "science",
            "file": result.get("file"),
            "object": obs.object or obs.name,
            "observation": obs.name,
            "spectrum": session.id,
            "sequencer": self.name,
            "generation": state.gen,
            "leader": session.leader,
            "ids": self._ids(unit.name),
            "feed_requested": list(spec.get("feed") or []),
            "telescopes": feeding,
            "telescopes_spectrograph": [self._spec_name(t) for t in feeding],
            "telescopes_end": end,
            "dropped": dict(session.dropped),
            "guide": stats,
            "acquisition": {t: self.units[t].acquisition for t in feeding if t in self.units},
            "exptime": result.get("exptime"),
            "exptime_requested": result.get("exptime_requested", obs.exptime),
            "ended_by": result.get("ended_by"),
            "start_utc": result.get("start_utc"),
            "expmeter_counts": result.get("expmeter_counts"),
            "target_counts": result.get("target_counts"),
            "flux_midpoint_s": result.get("flux_midpoint_s"),
            "flux_midpoint_utc": result.get("flux_midpoint_utc"),
            "flux_midpoint_jd": result.get("flux_midpoint_jd"),
            **self._record_extra(result),
            "target": spec.get("target")
            or {"name": obs.object or obs.name, "ra_hours": obs.ra_hours, "dec_deg": obs.dec_deg},
            "spectrograph": result,
            "warnings": list(result.get("warnings") or []),
        }
        if self.spectra is not None:
            site = self.spectra.site
            record["site"] = {
                "name": self.spectra.site_name,
                "latitude": site.latitude,
                "longitude": site.longitude,
                "elevation": site.elevation,
            }
            record["barycentric"] = self.spectra.correct(record)
        return record

    def _record_extra(self, result: dict[str, Any]) -> dict[str, Any]:
        """The spectrograph's ``record_keys`` of a frame's result (SpectrographHook)."""
        keys = getattr(self.spectrograph, "record_keys", ())
        return {k: result.get(k) for k in keys}

    def _write_spectrum(self, record: dict[str, Any]) -> str | None:
        self.spectrum_records.append(record)
        if self.spectra is None:
            return None
        try:
            return str(self.spectra.write(record))
        except OSError as exc:
            # The raw frame is safe on the spectrograph PC; only robofast's
            # metadata is lost. Loud, but not a reason to stop observing.
            log.exception("%s: spectrum sidecar for %s", self.name, record.get("file"))
            self._event(
                "spectrum_sidecar_failed",
                file=record.get("file"),
                error=repr(exc),
                level="critical",
            )
            return None

    async def _spectrograph_calibration(
        self, state: _Generation, unit: TelescopeUnit, obs: Observation, taken: list[Any]
    ) -> None:
        """The spectrograph's calibration frames (``obs.calibration``) as one
        job; the telescope does nothing. Bounded by the frames' exposure
        times plus ``spectrum_margin`` per frame."""
        if state.spectrograph is None:
            raise Unsupported(f"{unit.name}: no spectrograph for {obs.name}")
        frames = [dict(f) for f in obs.calibration or []]
        n = sum(int(f.get("n", 1)) for f in frames)
        bound = sum(float(f.get("exptime", 0.0)) * int(f.get("n", 1)) for f in frames)
        bound += n * self.config.spectrum_margin
        try:
            async with asyncio.timeout(self.config.spectrum_ready_timeout):
                await self._spec_lock.acquire()
        except TimeoutError as exc:
            raise SpectrographBusy("the spectrograph is busy with another job") from exc
        unit.exposing = True
        night = self._night_name(self.clock())
        try:
            try:
                job = await state.spectrograph.calibrate(frames, night=night)
            except SpectrographBusy as exc:
                raise SpectrographBusy(str(exc)) from exc
            except SpectrographError as exc:
                raise SpectrographFault(f"calibrate: {exc}") from exc
            job_id = int(job["id"])
            self._spec_job, self._spec_job_owner = job_id, unit.name
            self._event(
                "spectrograph_calibration_started",
                gen=state.gen,
                telescope=unit.name,
                job=job_id,
                frames=frames,
            )
            view = await self._wait_spectrograph_job(state, unit, job_id, bound)
        finally:
            unit.exposing = False
            self._spec_lock.release()
        self._spectrograph_ok()
        for result in view.get("results") or []:
            record = {
                "version": 1,
                "kind": result.get("kind"),
                "file": result.get("file"),
                "object": result.get("object"),
                "observation": obs.name,
                "sequencer": self.name,
                "generation": state.gen,
                "dark": result.get("dark"),
                "exptime": result.get("exptime"),
                "start_utc": result.get("start_utc"),
                "night": night,
                **self._record_extra(result),
                "spectrograph": result,
                "warnings": list(result.get("warnings") or []),
            }
            path = self._write_spectrum(record)
            taken.append(SpectrumFrame(str(result.get("file")), path, record))
        self._event(
            "spectrograph_calibration_done",
            gen=state.gen,
            telescope=unit.name,
            job=job_id,
            files=[f.file for f in taken],
        )

    async def _abort_spectrograph(self, reason: str, report: CleanupReport) -> None:
        """Abort this sequencer's spectrograph job, if one is running
        (bounded by the hook's own timeout; never raises)."""
        if self.spectrograph is None or self._spec_job is None:
            return
        job = self._spec_job
        self._event("spectrograph_abort", gen=report.generation, job=job, reason=reason)
        ended = await self.spectrograph.abort(reason)
        report.spectrograph_aborted = ended
        self._event("spectrograph_aborted", gen=report.generation, job=job, ended=ended)
        if ended:
            if self._spec_job == job:
                self._spec_job = self._spec_job_owner = None
        else:
            report.errors.append(f"spectrograph: job {job} did not confirm the abort")

    async def _startup_spectrograph(self, report: CleanupReport) -> None:
        """At startup: abort a spectrum a crashed predecessor left running
        (a job whose telescopes are ours)."""
        if self.spectrograph is None:
            return
        try:
            st = await asyncio.wait_for(self.spectrograph.status(), 15.0)
        except Exception as exc:
            report.errors.append(f"spectrograph status: {exc!r}")
            return
        job = st.get("job") or {}
        if job.get("state") not in ("queued", "running"):
            return
        ours = {self._spec_name(t) for t in self.units}
        theirs = {t for f in job.get("frames") or [] for t in f.get("telescopes") or []}
        if theirs & ours:
            self._spec_job, self._spec_job_owner = int(job["id"]), None
            await self._abort_spectrograph("startup: left running by a predecessor", report)

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
        # The spectrograph first (bounded), alongside the cameras and the
        # guiders: nothing may still be exposing when the telescopes park.
        if what == "startup_cleanup":
            spec = asyncio.create_task(self._startup_spectrograph(report))
        else:
            spec = asyncio.create_task(self._abort_spectrograph(f"cleanup #{gen}", report))
        try:
            await asyncio.gather(
                *(
                    self._cleanup_unit(name, tel, cam, report, spec)
                    for name, (tel, cam) in grants.items()
                )
            )
            await asyncio.wait({spec}, timeout=self.config.spectrograph_abort_wait)
        finally:
            if not spec.done():
                report.errors.append("spectrograph: abort still running after the cleanup")
                self._orphans.add(spec)
                spec.add_done_callback(self._orphans.discard)
            for pair in grants.values():
                for g in pair:
                    g.revoke("cleanup done")
        report.duration = time.monotonic() - t0
        for unit in self.units.values():
            if unit.park_requested is not None and unit.name in report.parked:
                unit.park_requested = None  # parked by the cleanup
        self._event(
            f"{what}_done",
            gen=gen,
            aborted=report.aborted,
            parked=report.parked,
            errors=report.errors,
            duration=report.duration,
            spectrograph_aborted=report.spectrograph_aborted,
            lamps_off=report.lamps_off,
        )
        for err in report.errors:
            log.error("%s cleanup #%d: %s", self.name, gen, err)
        return report

    async def _cleanup_unit(
        self,
        name: str,
        tel: Grant,
        cam: Grant,
        report: CleanupReport,
        spectrograph: asyncio.Task | None = None,
    ) -> None:
        """Abort the exposure, stop the guider and any slew, and switch its
        lamps off (in parallel), then -- once the spectrograph's abort has ended, or after
        ``spectrograph_abort_wait`` -- park. Every step is bounded."""
        await self._make_ready(name, tel, cam, report)
        if spectrograph is not None and not spectrograph.done():
            await asyncio.wait({spectrograph}, timeout=self.config.spectrograph_abort_wait)
        await self._park(name, tel, report)

    async def _make_ready(self, name: str, tel: Grant, cam: Grant, report: CleanupReport) -> None:
        """Everything of the cleanup but the park, in parallel and bounded:
        abort the exposure, stop the guider and any slew, lamps and ADC
        off, tracking rates back to 0."""
        await asyncio.gather(
            self._cleanup_camera(name, cam, report),
            self._stop_guider(name, report),
            self._abort_slew(name, tel, report),
            self._cleanup_sequences(name, report),
            self._cleanup_adc(name, report),
            self._cleanup_rates(name, tel, report),
        )

    async def _cleanup_rates(self, name: str, tel: Grant, report: CleanupReport) -> None:
        """A moving target's tracking rates back to 0 before the park (bounded;
        if it fails they stay marked set, and the next slew resets them first)."""
        unit = self.units.get(name)
        if unit is None or not unit.follow_rates:
            return
        try:
            async with asyncio.timeout(10.0):
                await tel.set("RightAscensionRate", 0.0)
                await tel.set("DeclinationRate", 0.0)
            unit.follow_rates = False
            self._event("follow_rates_reset", gen=report.generation, telescope=name)
        except (DeviceError, TimeoutError) as exc:
            report.errors.append(f"{name}: tracking rates reset: {exc!r}")

    async def _stop_guider(self, name: str, report: CleanupReport) -> None:
        unit = self.units.get(name)
        if unit is None or unit.guider is None:
            return
        # The guide loop moves the mount: stop it before parking.
        if not await unit.guider.stop("cleanup"):
            report.errors.append(f"{name}: guider did not confirm stop")
        self._event("guiding_stopped", gen=report.generation, telescope=name, why="cleanup")

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

    async def _park(self, name: str, tel: Grant, report: CleanupReport) -> None:
        cfg = self.config
        try:
            if not await tel.get("AtPark"):
                await tel.call("Park", timeout=cfg.park_timeout)
                await self._wait_for(lambda: tel.get("AtPark"), bool, cfg.park_timeout)
            report.parked.append(name)
        except (DeviceError, TimeoutError) as exc:
            report.errors.append(f"{name}: telescope park: {exc!r}")


def _spec_object(name: str) -> str:
    """An object name the spectrograph service accepts (its file names)."""
    return re.sub(r"[^A-Za-z0-9_+-]", "_", name)[:60] or "object"


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


async def _no_exposures(exptime: float) -> np.ndarray:
    raise RuntimeError("no exposures here")
