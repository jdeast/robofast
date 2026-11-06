"""The safety supervisor: the only component allowed to open or close.

Policy, in one paragraph: the enclosure is open only while (a) the weather
has been *continuously* safe for ``reopen_hold`` seconds, confirmed by a
reading *taken* after the hold ended (so an open never goes out on a
verdict older than a weather change; it waits for the next poll), and (b) a live
sequencer holds an unexpired, unrevoked :class:`OpenLease`. Anything else
-- unsafe weather, a weather reading that errors or times out, weather data
older than ``stale_after``, no successful read for ``stale_after``, a lease
that was not renewed, or no lease at all -- means "close". The supervisor
never asks anyone before closing; it revokes the lease (which tells the
sequencer to stop and clean up) and closes in the same step.

Its loops run as tasks of :meth:`SafetySupervisor.run`:

* the weather loop reads the SafetyMonitor (and, if configured, the
  ObservingConditions ``TimeSinceLastUpdate``) every ``poll_interval``;
* the enclosure loop enforces the decision every ``poll_interval`` (and
  immediately on a transition to unsafe). It also performs its own staleness
  check, so a weather loop that is stuck for any reason still fails closed;
* the heartbeat loop (if a heartbeat device is configured) pings the
  enclosure hardware every ``heartbeat_interval``, but only while the
  enclosure loop has completed a pass within ``heartbeat_max_loop_age``. If
  the pings stop -- the supervisor crashed, hung, or its enclosure loop is
  stuck -- the hardware closes by itself (the device contract is in
  :mod:`robofast.heartbeat_watchdog`). ``heartbeat_interval`` must be
  at most a quarter of the device's ``heartbeat_timeout`` (four pings per
  timeout, as the legacy MINERVA code ensured); a config that breaks this
  is rejected.

Enclosure faults are a separate axis from the shutter position. With
``fault_action`` set, each enclosure pass first reads the dome's read-only
fault channel (``Action(fault_action)``, JSON with booleans ``fault`` and
``error``; the Aqawan server's ``"Faults"``) through a non-privileged grant.
Any fault, or a failed read, alarms (once per change), revokes the lease and
blocks opening. If the enclosure is not closed, that is the dangerous case:
a second, louder alarm, and it closes (the Aqawan also reports
``shutterError`` then). A fault with the enclosure closed only alarms; the
close is not re-sent. :meth:`SafetySupervisor.clear_faults` is for an
operator (logged with their name), and goes out on the supervisor's
privileged grant, since ``Action:ClearFaults`` is in
:data:`ENCLOSURE_MEMBERS`.

Automatic fault clearing (``fault_auto_clear``; MINERVA's Aqawan, JDE
2026-10-31) is the supervisor's policy, since ``ClearFaults`` is privileged:

* only while both shutters are confirmed closed (the fault channel's
  ``shutters`` *and* ``ShutterStatus``), never while open or moving;
* never an E-stop (the channel's ``estop``, or fault text naming one, see
  :func:`mentions_estop`): a critical alarm instead, and an operator must
  reset it; never an emergency-close latch (an operator's own close), and
  not before the fault text has arrived (so an E-stop can be recognised);
* at most once per ``fault_auto_clear_interval`` (1 h), alarming every time;
* if the fault is back (or never went) within that interval of an automatic
  clear, automatic clearing stops (a critical alarm) until an operator
  clears faults (the console's ``clear_faults``).

The supervisor holds the dome owner's single *privileged* grant: the owner
refuses ``OpenShutter``/``CloseShutter`` from anyone else.

Attended-only enclosures
------------------------
An enclosure without a hardware heartbeat device has nothing that closes it
if this process dies or hangs, so it must not run unattended (JDE
2026-10-31; the FLWO 60-inch dome has no watchdog). ``attended_only``
(default: true exactly when no heartbeat device is configured) makes the
supervisor grant no lease, and so never open, unless an operator is **on
duty**: :meth:`SafetySupervisor.duty_on` (the observer console's ``duty``
action, audited), renewed by :meth:`SafetySupervisor.duty_renew` (the
console page renews it while it is open). A duty not renewed for
``duty_ttl`` raises a critical alarm; ``duty_grace`` later the supervisor
revokes the lease and closes, as for any revocation. Going off duty
(:meth:`SafetySupervisor.duty_off`) with the enclosure open closes it at
once. An enclosure with a heartbeat (MINERVA's Aqawan) is unaffected.

Unattended without a watchdog
-----------------------------
Whether to run such an enclosure unattended anyway is the site's choice
(JDE 2026-11-02: "some may decide it's worth the dome not closing (with a
loud warning) to operate unattended"). ``allow_unattended_without_watchdog``
(default false) lifts the floor: ``attended_only`` defaults to false, so
every mode, ``robotic`` included, is offered. Without it, ``attended_only``
false with no heartbeat is refused. While it is set the supervisor warns,
loudly and repeatedly: a critical log line and an ``unwatched`` timeline
event when it starts and on every switch into a mode that may open with
nobody on duty (:func:`robofast.modes.unattended`), the warning in the
switch notice, ``unwatched`` in its mode status (so on the console's
enclosure panel, in the sequencer's snapshot and the request API's status)
and an ``unwatched`` event every ``unwatched_warning_interval`` seconds
while it runs that way (the morning report lists them).

Operating modes
---------------
The enclosure's mode (:mod:`robofast.modes`, docs/modes.md) is held
here: the supervisor is the one authority every other process reads it
from (``GET /status``) and the one place it is switched
(:meth:`SafetySupervisor.set_mode`: the console's ``mode`` action and
``python -m robofast.modes set``, logged with who and why, a
``mode`` timeline event at warning level and a notice on the console). The
mode decides whether a lease may be granted at all (``open``), the duty
checks above (``attended_only``, ``duty_expiry``, ``duty_ttl``,
``duty_grace``; ``attended_only`` in ``safety`` is the enclosure's floor,
which no mode lowers) and automatic fault clearing. A mode whose
``enclosure`` is ``watch`` (``shadow``) commands nothing: the enclosure loop
reads the shutter and records what it would do (``shadow_would`` events,
``would_open`` in the status); a shutter found open then is a critical
alarm, not a close -- unless the mode's ``intervene`` is set (``shadow``'s is) and
an enforced close condition is active (:meth:`SafetySupervisor.active_risk`:
the safety monitor's verdict unsafe, or an enclosure fault read from the
fault channel): then it closes at once, as the robotic mode would
(``park_before_close`` included), with a critical alarm and a
``shadow_intervened`` event, and goes back to watching once closed. Not
knowing (no weather read yet, a stale feed, an unreadable fault channel)
is not an active risk: a restarted supervisor does not close what another
system has open.

A switch never opens by itself: a mode that may open grants a lease only
through :meth:`request_open`, with every check above. A switch to a mode
under which the live lease may not go on (it may not open, it observes
differently -- :func:`robofast.modes.lease_survives` -- or its duty
check fails now) revokes the lease at once: the sequencer stops and parks
and the enclosure closes, as for any revocation; a switch into a ``watch``
mode finishes that close before it stops commanding. The mode is saved
(:class:`~robofast.modes.ModeStore`) and reloaded at start; a missing
or unreadable file means the config's default mode, with an alarm.

Closing on unparked telescopes
------------------------------
On a roll-off roof, ``park_before_close`` gives the sequencer's cleanup half
of ``park_wait`` to park, then parks every telescope itself (in parallel,
bounded by the other half), then closes -- parked or not: rain on the optics
is worse than an unlikely roof contact. Some roofs can hit some telescopes,
though, so ``require_park_before_close`` (per enclosure, default off) adds
an interlock (JDE 2026-11-02): a **routine** close waits until every
telescope under the roof reports ``AtPark``. Routine is every close that is
not an emergency: a lease released (the night or an idle period over), a
lease not renewed, an operator's close (the console's enclosure ``stop``),
a mode switch, an operator's duty expiring. While held back the roof stays
open, the supervisor parks the telescopes again on every pass, alarms (a
``close_blocked`` timeline event, ``close_interlock`` in the status) and,
after ``park_interlock_escalate``, raises a critical alarm; it never closes
on an unparked telescope for a routine reason. An **emergency** close goes
ahead after ``park_wait`` as before, parked or not (:meth:`emergency`):
weather unsafe or unknown (no read yet, a stale feed, a failed read), an
enclosure fault, a shutter error, an operator's emergency close
(:meth:`operator_close` with ``emergency=True``: the console's EMERGENCY
CLOSE), and the supervisor stopping (its final close; on an enclosure with
a heartbeat the hardware would close by itself anyway).

Recovering from a sequencer crash
---------------------------------
A sequencer that dies holding a lease leaves it revoked (unrenewed) but never
released, and no new lease is granted while one is outstanding. Only two
things clear such an *orphan*, and only once it is no longer live and the
enclosure is confirmed closed:

* :meth:`SafetySupervisor.recover` -- the restarted sequencer (same
  ``holder``), after it has aborted the exposure and parked;
* :meth:`SafetySupervisor.clear` -- an operator, logged with their name.

The supervisor never clears an orphan by itself: it cannot know that the
telescope is safe. After ``orphan_timeout`` it alarms instead. A lease the
sequencer *quarantined* (its task ignored cancellation) is never recovered,
only cleared. And no lease is granted until every configured telescope
reports ``AtPark`` (an unreadable one counts as unparked, and alarms), so
nothing reopens on a telescope that a crash or a failed cleanup left
unparked.
"""

from __future__ import annotations

import asyncio
import enum
import itertools
import json
import logging
import re
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from robofast.cancel import stop_tasks
from robofast.modes import (
    ModePolicy,
    ModeSet,
    ModeStore,
    lease_survives,
    modes_from_config,
    now_iso,
    unattended,
)
from robofast.owner import DeviceError, DeviceOwner, Grant
from robofast.timeline import Timeline

log = logging.getLogger(__name__)

#: Dome members only the safety supervisor may call (``Action:Name`` is one
#: Alpaca action; ``ClearFaults`` is for an operator, through the supervisor).
ENCLOSURE_MEMBERS = frozenset({"OpenShutter", "CloseShutter", "Action:ClearFaults"})


def enclosure_members(config: SafetyConfig) -> frozenset[str]:
    """:data:`ENCLOSURE_MEMBERS` with the configured clear-faults action."""
    return frozenset({"OpenShutter", "CloseShutter", f"Action:{config.clear_faults_action}"})


#: How long a mode switch stays a notice in the status (s).
MODE_NOTICE_FOR = 3600.0

#: A ``ClearFaults`` note starting with this is the supervisor's automatic
#: clear (the Aqawan server then also refuses an E-stop or a latch itself).
AUTO_CLEAR_PREFIX = "auto:"

_ESTOP = re.compile(r"\be[\s_-]?stop|\bemergency[\s_-]?stop", re.IGNORECASE)


def mentions_estop(text: object) -> bool:
    """True if a controller's fault text names an emergency stop ("Estop
    active", "E-STOP", "emergency stop"): never cleared automatically."""
    return isinstance(text, str) and _ESTOP.search(text) is not None


class Shutter(enum.IntEnum):
    """ASCOM ShutterState."""

    OPEN = 0
    CLOSED = 1
    OPENING = 2
    CLOSING = 3
    ERROR = 4


class LeaseRevoked(Exception):
    pass


class RecoveryRefused(Exception):
    """The supervisor will not (yet) release this lease; the message says why."""


@dataclass
class SafetyConfig:
    poll_interval: float = 5.0
    #: Per-read timeout for the weather devices.
    read_timeout: float = 5.0
    #: Weather data (TimeSinceLastUpdate) or the last successful read older
    #: than this is treated as unsafe.
    stale_after: float = 60.0
    #: Continuous-safe time required before (re)opening. Rockit uses ~20 min.
    reopen_hold: float = 1200.0
    #: A lease not renewed for this long is revoked and the enclosure closed.
    lease_ttl: float = 60.0
    #: How long a shutter move may take before it is re-commanded and alarmed.
    shutter_timeout: float = 120.0
    #: Per-command timeout for the dome.
    dome_timeout: float = 5.0
    #: Roll-off roofs: wait for the telescopes to be parked (or park them
    #: ourselves, all at once) before closing. Domes close immediately.
    park_before_close: bool = False
    park_wait: float = 60.0
    #: The park-before-close interlock (module doc, "Closing on unparked
    #: telescopes"): a roof that can hit a telescope closes routinely only
    #: once every telescope under it reports ``AtPark``. An emergency close
    #: (unsafe or unknown weather, an enclosure fault, a shutter error, an
    #: operator's emergency close, the supervisor stopping) still goes ahead
    #: after ``park_wait``. Needs ``park_before_close``.
    require_park_before_close: bool = False
    #: A routine close blocked by the interlock this long becomes a critical
    #: alarm (the enclosure stays open; an operator parks the telescopes or
    #: closes as an emergency).
    park_interlock_escalate: float = 300.0
    #: Seconds between hardware heartbeat pings; ``None`` when the enclosure
    #: has no heartbeat device. At most ``heartbeat_timeout / 4``.
    heartbeat_interval: float | None = None
    #: The heartbeat device's own timeout (the Aqawan PAC's is 60 s);
    #: required with ``heartbeat_interval``, so the 4-pings rule is checked.
    heartbeat_timeout: float | None = None
    #: Ping only while the enclosure loop finished a pass within this long.
    heartbeat_max_loop_age: float = 15.0
    #: Alarm when a revoked lease is still unreleased this long after its
    #: revocation (its sequencer died and has not been restarted).
    orphan_timeout: float = 600.0
    #: The dome's read-only fault channel, an Alpaca action answering JSON
    #: with booleans ``fault`` and ``error`` (the Aqawan server's
    #: ``"Faults"``); ``None`` for a dome without one.
    fault_action: str | None = None
    #: The dome's Alpaca action that clears its faults (privileged: only
    #: the supervisor sends it), and the heartbeat device's ping action.
    #: The defaults are robofast's own servers' names (the Aqawan server,
    #: the heartbeat watchdog).
    clear_faults_action: str = "ClearFaults"
    heartbeat_action: str = "Heartbeat"
    #: Clear a fault automatically (module doc): only with both shutters
    #: confirmed closed, never an E-stop, at most once per
    #: ``fault_auto_clear_interval``; a recurrence within that interval stops
    #: it until an operator clears faults. Needs ``fault_action``.
    fault_auto_clear: bool = False
    fault_auto_clear_interval: float = 3600.0
    #: A fault still reported this long after an automatic clear has not
    #: cleared (the controller may take a moment to drop its flags).
    fault_auto_clear_settle: float = 15.0
    #: While the enclosure should be open, a shutter reporting ``ERROR``
    #: (immediately) or unreadable for this long revokes the lease, closes,
    #: and blocks reopening for ``shutter_error_backoff`` seconds, so a
    #: broken shutter is not cycled every reopen hold.
    shutter_unreadable_grace: float = 15.0
    shutter_error_backoff: float = 1800.0
    #: The enclosure must not run unattended: no lease (so no opening)
    #: unless an operator is on duty, in every mode (module doc; a floor the
    #: modes cannot lower). ``None``: true exactly when there is no hardware
    #: heartbeat device (``heartbeat_interval`` unset). Setting it false
    #: without a heartbeat runs an enclosure unattended with nothing behind
    #: the supervisor, and is refused unless the site opts in with
    #: ``allow_unattended_without_watchdog``.
    attended_only: bool | None = None
    #: The site's explicit choice to run an enclosure with no hardware
    #: heartbeat unattended (module doc, "Unattended without a watchdog"):
    #: ``attended_only`` then defaults to false and the supervisor warns
    #: loudly while it runs unattended. With a heartbeat it changes nothing;
    #: with ``attended_only`` true the floor stays.
    allow_unattended_without_watchdog: bool = False
    #: While unattended with no watchdog, repeat the warning this often (s).
    unwatched_warning_interval: float = 900.0
    #: An operator's duty expires (critical alarm) this long after its last
    #: renewal, and the supervisor closes ``duty_grace`` after that (a mode
    #: may set its own).
    duty_ttl: float = 300.0
    duty_grace: float = 300.0

    def __post_init__(self) -> None:
        for key in ("clear_faults_action", "heartbeat_action"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key):
                raise ValueError(f"{key}: an Alpaca action name")
        no_hb = self.heartbeat_interval is None
        if not isinstance(self.allow_unattended_without_watchdog, bool):
            raise ValueError("allow_unattended_without_watchdog: true or false")
        if self.attended_only is None:
            self.attended_only = no_hb and not self.allow_unattended_without_watchdog
        elif no_hb and not self.attended_only and not self.allow_unattended_without_watchdog:
            raise ValueError(
                "attended_only false with no hardware heartbeat: nothing closes this"
                " enclosure if the supervisor dies. To run it unattended anyway, set"
                " allow_unattended_without_watchdog: true (docs/modes.md)"
            )
        if not isinstance(self.require_park_before_close, bool):
            raise ValueError("require_park_before_close: true or false")
        if self.require_park_before_close and not self.park_before_close:
            raise ValueError(
                "require_park_before_close needs park_before_close (the supervisor parks"
                " the telescopes before every close)"
            )
        if not self.park_interlock_escalate >= 0:
            raise ValueError("park_interlock_escalate must be >= 0")
        if not self.unwatched_warning_interval > 0:
            raise ValueError("unwatched_warning_interval must be > 0")
        if self.duty_ttl <= 0 or self.duty_grace < 0:
            raise ValueError("duty_ttl must be > 0 and duty_grace >= 0")
        if self.fault_auto_clear and not self.fault_action:
            raise ValueError("fault_auto_clear needs fault_action (the dome's fault channel)")
        if self.fault_auto_clear_interval <= 0:
            raise ValueError("fault_auto_clear_interval must be > 0")
        hb, timeout = self.heartbeat_interval, self.heartbeat_timeout
        if hb is not None:
            if timeout is None:
                raise ValueError(
                    "heartbeat_interval needs heartbeat_timeout (the device's own timeout)"
                )
            if not 0 < hb <= timeout / 4:
                raise ValueError(
                    f"heartbeat_interval {hb:g} s must be > 0 and <= heartbeat_timeout/4"
                    f" ({timeout / 4:g} s): at least 4 pings per device timeout"
                )

    @property
    def unwatched(self) -> bool:
        """The site lets this enclosure run unattended with no hardware
        watchdog (module doc)."""
        return self.heartbeat_interval is None and not self.attended_only


#: The warning shown wherever an enclosure runs unattended with no watchdog.
UNWATCHED_TEXT = (
    "NO WATCHDOG: this enclosure may open with nobody on duty, and nothing closes it"
    " if the safety supervisor dies or hangs (safety.allow_unattended_without_watchdog)"
)


class OpenLease:
    """A sequencer's request to keep the enclosure open.

    The holder must :meth:`renew` it at least every ``ttl`` seconds and must
    :meth:`release` it once its observing task has finished *and* cleanup is
    done. The supervisor does not grant a new lease while an old one is
    unreleased, so a stale task can never overlap a fresh one.
    """

    def __init__(self, lease_id: int, holder: str, ttl: float) -> None:
        self.id = lease_id
        self.holder = holder
        self.ttl = ttl
        self.granted_at = time.monotonic()
        self.renewed_at = self.granted_at
        self.revoke_reason: str | None = None
        self.revoked_at: float | None = None
        self.quarantined = False
        self.revoked = asyncio.Event()
        self.opened = asyncio.Event()
        self.released = asyncio.Event()

    @property
    def live(self) -> bool:
        return not self.revoked.is_set() and not self.released.is_set()

    def renew(self) -> None:
        if self.revoked.is_set():
            raise LeaseRevoked(self.revoke_reason)
        self.renewed_at = time.monotonic()

    def expired(self, now: float) -> bool:
        return now - self.renewed_at > self.ttl

    def release(self) -> None:
        self.released.set()

    def quarantine(self) -> None:
        """Hold the lease on purpose: it may then only be cleared by an operator."""
        self.quarantined = True

    def _revoke(self, reason: str) -> bool:
        if self.revoked.is_set():
            return False
        self.revoke_reason = reason
        self.revoked_at = time.monotonic()
        self.revoked.set()
        return True

    def state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "holder": self.holder,
            "ttl": self.ttl,
            "live": self.live,
            "opened": self.opened.is_set(),
            "revoked": self.revoked.is_set(),
            "reason": self.revoke_reason,
            "released": self.released.is_set(),
            "quarantined": self.quarantined,
        }

    async def wait_open(self) -> None:
        """Return once the supervisor has confirmed the enclosure is open."""
        if self.revoked.is_set():
            raise LeaseRevoked(self.revoke_reason)
        opened = asyncio.ensure_future(self.opened.wait())
        revoked = asyncio.ensure_future(self.revoked.wait())
        try:
            await asyncio.wait({opened, revoked}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            opened.cancel()
            revoked.cancel()
        if self.revoked.is_set():
            raise LeaseRevoked(self.revoke_reason)

    def __repr__(self) -> str:
        return f"<OpenLease #{self.id} {self.holder} live={self.live}>"


class SafetySupervisor:
    def __init__(
        self,
        name: str,
        *,
        dome: DeviceOwner,
        safety_monitor: DeviceOwner,
        timeline: Timeline,
        observing_conditions: DeviceOwner | None = None,
        telescopes: Sequence[DeviceOwner] = (),
        heartbeat: DeviceOwner | None = None,
        config: SafetyConfig | None = None,
        modes: ModeSet | None = None,
        mode_store: ModeStore | None = None,
    ) -> None:
        self.name = name
        self.timeline = timeline
        self.config = config or SafetyConfig()
        #: The enclosure's modes (module doc); without any, the one that
        #: matches the behavior before modes (``robotic``, or ``attended``
        #: on an attended-only enclosure).
        self.modes = modes or modes_from_config(
            {},
            attended_floor=bool(self.config.attended_only),
            fault_action=bool(self.config.fault_action),
        )
        self._mode_store = mode_store or ModeStore(None)
        self.mode, self._mode_record, why = self._mode_store.load(self.modes)
        self._mode_since = time.monotonic()
        self._mode_notice: tuple[float, str] | None = None
        #: Why the mode is the default, not the saved one (None: it was saved).
        self._mode_fallback = why
        #: A switch into a ``watch`` mode finishes closing first.
        self._finish_close = False
        #: The last "would open" of a mode that may not open (shadow).
        self._would_open: bool | None = None
        #: A watched enclosure found not closed (critical; module doc).
        self._watch_alarm: str | None = None
        #: A watch mode is closing the enclosure on an active risk (why).
        self._intervening: str | None = None
        #: The safety monitor's last successful verdict, if unsafe.
        self._unsafe_verdict: str | None = None
        self._dome = dome.grant(name, privileged=True)
        self._sm = safety_monitor.grant(name)
        self._oc = observing_conditions.grant(name) if observing_conditions else None
        self._tels = [t.grant(name) for t in telescopes]
        if self.config.park_before_close and not self._tels:
            raise ValueError("park_before_close needs the telescope owners")
        if (heartbeat is None) != (self.config.heartbeat_interval is None):
            raise ValueError("a heartbeat device and heartbeat_interval go together")
        self._hb = heartbeat.grant(f"{name}/heartbeat") if heartbeat else None
        # Read-only, so not privileged.
        self._faults = dome.grant(f"{name}/faults") if self.config.fault_action else None
        #: What is wrong with the enclosure (``None``: nothing, or no fault
        #: channel). Unknown until the first good read.
        self._fault: str | None = "not read yet" if self._faults else None
        self._fault_kind: object = "not read yet"  # (fault, error), or why unknown
        self._fault_escalated = False
        #: The enclosure reports an emergency stop (never cleared automatically).
        self._estop: str | None = None
        #: Automatic clearing (``fault_auto_clear``): when, how many, whether
        #: the fault went away after the last one, and why it was stopped.
        self._auto_cleared_at: float | None = None
        self._auto_clears = 0
        self._auto_clear_took = False
        self._auto_clear_stopped: str | None = None
        self._shutter_bad_since: float | None = None
        self._shutter_hold_until: float | None = None
        #: The operator on duty (``attended_only``): name, since, last renewal.
        self._duty: dict[str, Any] | None = None
        self._duty_expired: str | None = None
        #: When the no-watchdog warning was last repeated (module doc).
        self._unwatched_warned: float | None = None

        self._safe = False
        self._reason = "startup (no weather read yet)"
        self._safe_since: float | None = None
        self._last_good_read: float | None = None
        #: When the latest safe reading was *taken* (its read started).
        self._safe_read_at: float | None = None
        self._lease: OpenLease | None = None
        self._lease_ids = itertools.count(1)
        self._wake = asyncio.Event()
        self._closing_since: float | None = None
        self._opening_since: float | None = None
        self._parked_for_close = False
        #: The park-before-close interlock (``require_park_before_close``):
        #: an operator's emergency close (why; cleared once closed), and a
        #: routine close held back by unparked telescopes (why, since when,
        #: and the critical alarm once it has lasted ``park_interlock_escalate``).
        self._emergency_close: str | None = None
        self._close_blocked: str | None = None
        self._close_blocked_since: float | None = None
        self._close_blocked_critical: str | None = None
        #: Set when the enclosure loop commits to closing (right before
        #: CloseShutter), cleared once the shutter reads CLOSED: no lease is
        #: granted in between, so no sequencer unparks a telescope under a
        #: closing roof (request_open).
        self._close_committed = False
        self._enforced_at: float | None = None
        self._pinged_at: float | None = None
        #: Set as :meth:`run` stops: the loops end on it even if their cancel
        #: was lost inside an httpx request (:mod:`robofast.cancel`), and
        #: nothing may open or stay open from then on.
        self._stopping = False
        self.shutter: int | None = None
        self.alarms: deque[str] = deque(maxlen=100)
        if why is not None and why.startswith("no mode file"):
            log.warning("%s: %s: the config's default mode %s", name, why, self.mode)
            timeline.event(name, "mode", mode=self.mode, why=why, level="warning")
        elif why is not None:
            self._alarm(f"{why}: starting in the config's default mode {self.mode!r}")
        elif not self.modes.configured:
            log.info("%s: no 'mode' in the config: mode %s (as before modes)", name, self.mode)
        log.warning("%s: mode %s", name, self.mode)
        if self.config.unwatched:
            self._warn_unwatched("start", time.monotonic())

    # -- the operating mode (module doc) -----------------------------------------

    @property
    def policy(self) -> ModePolicy:
        """The active mode's policy."""
        return self.modes.get(self.mode)

    def _attended_only(self) -> bool:
        return self.policy.attended_only

    def _duty_ttl(self) -> float:
        p = self.policy
        return self.config.duty_ttl if p.duty_ttl is None else p.duty_ttl

    def _duty_grace(self) -> float:
        p = self.policy
        return self.config.duty_grace if p.duty_grace is None else p.duty_grace

    def _fault_auto_clear(self) -> bool:
        p = self.policy
        if p.enclosure == "watch":
            return False
        return self.config.fault_auto_clear if p.fault_auto_clear is None else p.fault_auto_clear

    def _unwatched_active(self) -> bool:
        """Running unattended with no watchdog now: the site's opt-in and a
        mode that may open with nobody on duty."""
        return self.config.unwatched and unattended(self.policy)

    def _warn_unwatched(self, why: str, now: float) -> None:
        """The loud no-watchdog warning (module doc): a critical log line
        and an ``unwatched`` timeline event."""
        self._unwatched_warned = now
        active = self._unwatched_active()
        self.timeline.event(
            self.name,
            "unwatched",
            why=why,
            mode=self.mode,
            active=active,
            level="critical" if active else "warning",
        )
        if active:
            log.critical("%s: mode %s: %s", self.name, self.mode, UNWATCHED_TEXT)
        else:
            log.warning(
                "%s: mode %s needs an operator on duty, but this site lets the enclosure"
                " run unattended with no watchdog in another mode (%s)",
                self.name,
                self.mode,
                ", ".join(n for n, p in self.modes.modes.items() if unattended(p)) or "none",
            )

    def unwatched_status(self) -> dict[str, Any] | None:
        """``unwatched`` in the mode status: None unless the site opted in."""
        if not self.config.unwatched:
            return None
        return {"active": self._unwatched_active(), "text": UNWATCHED_TEXT}

    def _watching(self) -> bool:
        """The enclosure is only watched (a ``watch`` mode, its close done)."""
        return self.policy.enclosure == "watch" and not self._finish_close

    def set_mode(self, mode: str, operator: str, reason: str) -> dict[str, Any]:
        """Switch the mode (module doc); logged with who and why. A switch to
        a safer mode takes effect at once (the lease is revoked if it may not
        go on); a switch to a less safe one only through :meth:`request_open`.
        ValueError for an unknown mode or a missing name or reason."""
        operator, reason = str(operator).strip(), str(reason).strip()
        if not operator:
            raise ValueError("a mode switch needs an operator name")
        if not reason:
            raise ValueError("a mode switch needs a reason")
        new = self.modes.get(mode)
        old_name, old = self.mode, self.policy
        if mode == old_name:
            return {**self.mode_status(), "changed": False}
        record = {
            "mode": mode,
            "since": now_iso(),
            "by": operator,
            "reason": reason,
            "was": old_name,
        }
        saved = self._mode_store.save(record)
        now = time.monotonic()
        self.mode, self._mode_record, self._mode_since = mode, record, now
        self._mode_fallback = None
        self._would_open = None
        text = f"mode {old_name} -> {mode} by {operator}: {reason}"
        if self.config.unwatched and unattended(new):
            text += f" -- {UNWATCHED_TEXT}"
        self._mode_notice = (now, text)
        self.timeline.event(
            self.name,
            "mode",
            mode=mode,
            was=old_name,
            operator=operator,
            reason=reason,
            saved=saved,
            level="warning",
        )
        log.warning("%s: %s", self.name, text)
        if not saved:
            self._alarm(
                f"mode {mode} not saved ({self._mode_store.error}): a restart would start"
                f" in {self.modes.default}"
            )
        if self.config.unwatched and unattended(new) != unattended(old):
            self._warn_unwatched(f"switch from {old_name}", now)
        if self._duty_expired is not None and not new.attended_only:
            self._duty_expired = None
        lease = self._lease
        if lease is not None and lease.live:
            if not lease_survives(old, new):
                self._revoke_lease(f"mode {mode} (by {operator}: {reason})")
            elif not self.attended(now, keep=True):
                self._revoke_lease(f"mode {mode} needs an operator on duty (by {operator})")
        self._watch_alarm, self._finish_close, self._intervening = None, False, None
        if new.enclosure == "watch" and (
            self.outstanding() is not None or self.shutter != Shutter.CLOSED
        ):
            self._finish_close = True  # the normal close first, then only watch
        self._wake.set()
        return {**self.mode_status(now), "changed": True}

    def mode_status(self, now: float | None = None) -> dict[str, Any]:
        """The active mode, its policy and record, and the modes defined."""
        now = time.monotonic() if now is None else now
        notice = self._mode_notice
        p = self.policy
        return {
            "mode": self.mode,
            "description": p.description,
            "policy": p.as_json(),
            "since": self._mode_record.get("since"),
            "by": self._mode_record.get("by"),
            "reason": self._mode_record.get("reason"),
            "for_s": now - self._mode_since,
            "default": self.modes.default,
            "fallback": self._mode_fallback,
            "modes": {n: m.description for n, m in self.modes.modes.items()},
            "notice": None if notice is None or now - notice[0] > MODE_NOTICE_FOR else notice[1],
            "saved_error": self._mode_store.error,
            "would_open": None if p.open else self.open_allowed(now, ignore_mode=True),
            "finishing_close": self._finish_close,
            "intervening": self._intervening,
            "unwatched": self.unwatched_status(),
        }

    # -- public state -----------------------------------------------------

    @property
    def safe(self) -> bool:
        return self._safe

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def lease(self) -> OpenLease | None:
        """The current (or last) lease."""
        return self._lease

    def open_allowed(
        self, now: float | None = None, *, keep: bool = False, ignore_mode: bool = False
    ) -> bool:
        """May the enclosure open (``keep``: stay open; an expired duty's
        grace counts then)? ``ignore_mode``: would it, if the mode let it
        (shadow's "would open")."""
        now = time.monotonic() if now is None else now
        p = self.policy
        return (
            not self._stopping
            and (ignore_mode or (p.open and p.enclosure == "command"))
            and (ignore_mode or self.attended(now, keep=keep))
            and self._safe
            and self._fault is None
            and (self._shutter_hold_until is None or now >= self._shutter_hold_until)
            and self._safe_since is not None
            and now - self._safe_since >= self.config.reopen_hold
            # A reading taken after the hold ended, not the one before it.
            and self._safe_read_at is not None
            and self._safe_read_at >= self._safe_since + self.config.reopen_hold
        )

    @property
    def fault(self) -> str | None:
        """The enclosure fault (from ``fault_action``), if any."""
        return self._fault

    def critical(self) -> list[str]:
        """Conditions that need an operator now (the console raises them as
        critical alarms until they are gone)."""
        return [
            c
            for c in (
                self._estop,
                self._auto_clear_stopped,
                self._duty_expired,
                self._watch_alarm,
                self._close_blocked_critical,
            )
            if c
        ]

    # -- operator duty (attended_only) ---------------------------------------

    def attended(self, now: float | None = None, *, keep: bool = False) -> bool:
        """True unless the mode is ``attended_only`` and no operator's duty is
        current (``keep``: or within its grace, for an enclosure already
        open; with ``duty_expiry`` ``alarm``, an expired duty keeps it open)."""
        if not self._attended_only():
            return True
        duty = self._duty
        if keep and duty is not None and self.policy.duty_expiry == "alarm":
            return True
        now = time.monotonic() if now is None else now
        limit = self._duty_ttl() + (self._duty_grace() if keep else 0.0)
        return duty is not None and now - duty["renewed"] <= limit

    def duty_on(self, operator: str, reason: str = "") -> dict[str, Any]:
        """An operator goes on duty (or takes over from another); logged."""
        operator = operator.strip()
        if not operator:
            raise ValueError("going on duty needs an operator name")
        now = time.monotonic()
        prev = self._duty
        self._duty = {"operator": operator, "since": now, "renewed": now}
        self._duty_expired = None
        self.timeline.event(
            self.name,
            "duty_on",
            operator=operator,
            reason=reason,
            took_over=None if prev is None or prev["operator"] == operator else prev["operator"],
        )
        log.warning("%s: %s on duty (%s)", self.name, operator, reason or "no reason given")
        self._wake.set()
        return self.duty_status(now)

    def duty_renew(self, operator: str) -> dict[str, Any]:
        """The console page's renewal; only the operator on duty renews it."""
        duty = self._duty
        if duty is None or duty["operator"] != operator.strip():
            raise RecoveryRefused(
                f"{operator!r} is not on duty"
                + ("" if duty is None else f" ({duty['operator']} is)")
            )
        now = time.monotonic()
        if self._duty_expired is not None:
            self.timeline.event(self.name, "duty_renewed_late", operator=duty["operator"])
            self._duty_expired = None
        duty["renewed"] = now
        return self.duty_status(now)

    def duty_off(self, operator: str, reason: str = "") -> dict[str, Any]:
        """The operator goes off duty. With ``attended_only``, an open
        enclosure closes at once (the lease is revoked)."""
        duty, self._duty, self._duty_expired = self._duty, None, None
        self.timeline.event(
            self.name,
            "duty_off",
            operator=operator,
            reason=reason,
            was=None if duty is None else duty["operator"],
        )
        log.warning("%s: %s off duty (%s)", self.name, operator, reason or "no reason given")
        if self._attended_only():
            self._revoke_lease(f"operator {operator} went off duty (attended only)")
        self._wake.set()
        return self.duty_status()

    def duty_status(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        duty = self._duty
        attended_only, ttl, grace = self._attended_only(), self._duty_ttl(), self._duty_grace()
        closes = attended_only and self.policy.duty_expiry == "close"
        out: dict[str, Any] = {
            "attended_only": attended_only,
            "floor": bool(self.config.attended_only),
            "expiry": self.policy.duty_expiry,
            "on_duty": None if duty is None else duty["operator"],
            "attended": self.attended(now),
            "ttl": ttl,
            "grace": grace,
        }
        if duty is not None:
            age = now - duty["renewed"]
            out.update(
                since_s=now - duty["since"],
                renewed_ago=age,
                expires_in=ttl - age,
                closes_in=ttl + grace - age if closes else None,
            )
        return out

    def _check_duty(self, now: float) -> None:
        """Duty expiry: a critical alarm after ``duty_ttl``; after
        ``duty_grace`` more, the lease is revoked (so the enclosure closes),
        unless the mode's ``duty_expiry`` is ``alarm``."""
        duty = self._duty
        if not self._attended_only() or duty is None:
            return
        ttl, grace = self._duty_ttl(), self._duty_grace()
        closes = self.policy.duty_expiry == "close"
        age = now - duty["renewed"]
        if age <= ttl:
            return
        if self._duty_expired is None:
            then = (
                f"closing in {grace:g} s unless the console renews it"
                if closes
                else f"mode {self.mode} does not close for it (duty_expiry alarm)"
            )
            self._duty_expired = (
                f"operator duty ({duty['operator']}) not renewed for {ttl:g} s:"
                f" this enclosure is attended only; {then}"
            )
            self._alarm(self._duty_expired, "critical")
            self.timeline.event(self.name, "duty_expired", operator=duty["operator"])
        if closes and age > ttl + grace:
            lease = self._lease
            if lease is not None and lease.live:
                self._alarm(
                    f"operator duty ({duty['operator']}) expired; closing (attended only)",
                    "critical",
                )
                self._revoke_lease(f"operator duty ({duty['operator']}) expired")

    def status(self) -> dict[str, Any]:
        lease = self._lease
        now = time.monotonic()
        return {
            "safe": self._safe,
            "reason": self._reason,
            "open_allowed": self.open_allowed(),
            "fault": self._fault,
            "critical": self.critical(),
            "duty": self.duty_status(now),
            "mode": self.mode_status(now),
            "fault_auto_clear": None
            if not self._fault_auto_clear()
            else {
                "clears": self._auto_clears,
                "last_ago": None if self._auto_cleared_at is None else now - self._auto_cleared_at,
                "stopped": self._auto_clear_stopped,
            },
            "shutter_hold_for": (
                None
                if self._shutter_hold_until is None or now >= self._shutter_hold_until
                else self._shutter_hold_until - now
            ),
            "shutter": None if self.shutter is None else Shutter(self.shutter).name,
            "close_interlock": None
            if not self.config.require_park_before_close
            else {
                "blocked": self._close_blocked,
                "blocked_for": None
                if self._close_blocked_since is None
                else now - self._close_blocked_since,
                "emergency_close": self._emergency_close,
            },
            "lease": repr(lease) if lease else None,
            "orphaned_for": self._orphan_age(now),
            "alarms": list(self.alarms),
            "enforced_ago": None if self._enforced_at is None else now - self._enforced_at,
            "pinged_ago": None if self._pinged_at is None else now - self._pinged_at,
        }

    # -- leases -----------------------------------------------------------

    def outstanding(self) -> OpenLease | None:
        """The lease that blocks new grants (granted, not yet released), if any."""
        lease = self._lease
        return None if lease is None or lease.released.is_set() else lease

    async def lease_status(self) -> dict[str, Any] | None:
        lease = self.outstanding()
        return None if lease is None else lease.state()

    async def _telescopes_parked(self) -> bool:
        """True if every configured telescope reports AtPark (read-only, all
        read at once); otherwise alarm, naming the ones that are not, or
        cannot be read."""
        if not self._tels:
            return True

        async def parked(tel: Grant) -> bool | DeviceError:
            try:
                return bool(await tel.get("AtPark", timeout=self.config.read_timeout))
            except DeviceError as exc:
                return exc

        states = await asyncio.gather(*(parked(t) for t in self._tels))
        unparked = [t.owner.name for t, s in zip(self._tels, states, strict=True) if s is False]
        unknown = [
            f"{t.owner.name} ({s})"
            for t, s in zip(self._tels, states, strict=True)
            if isinstance(s, DeviceError)
        ]
        problems = ([f"not parked: {', '.join(unparked)}"] if unparked else []) + (
            [f"cannot confirm parked: {', '.join(unknown)}"] if unknown else []
        )
        if problems:
            self._alarm(f"telescope {'; '.join(problems)}; no lease is granted until all are")
        return not problems

    async def request_open(self, holder: str) -> OpenLease:
        """Wait until opening is allowed, no earlier lease is outstanding, and
        every configured telescope is parked."""
        while not (self._grantable() and await self._telescopes_parked() and self._grantable()):
            # The second check, after the awaited park check and with no await
            # between it and the grant: _ensure_closed commits to a close
            # (``_close_committed``) synchronously too, so a grant and a close
            # can never both go ahead. (The first spares the telescopes the
            # AtPark reads while nothing could be granted anyway.)
            await asyncio.sleep(min(self.config.poll_interval, 1.0))
        lease = OpenLease(next(self._lease_ids), holder, self.config.lease_ttl)
        self._lease = lease
        self.timeline.event(self.name, "lease_granted", lease=lease.id, holder=holder)
        self._wake.set()
        return lease

    def _grantable(self) -> bool:
        """Everything but the park check that a grant needs."""
        return not self._close_committed and self.open_allowed() and self.outstanding() is None

    def _revoke_lease(self, reason: str) -> None:
        lease = self._lease
        if lease is not None and lease._revoke(reason):
            self.timeline.event(self.name, "lease_revoked", lease=lease.id, reason=reason)

    def operator_close(self, operator: str, reason: str, *, emergency: bool = False) -> bool:
        """An operator asks for the enclosure to close (the observer console):
        the current lease is revoked, so the sequencer stops and cleans up
        and the enclosure closes, as for any revocation. True if a live
        lease was revoked; False if there was none (it is closed, or closing,
        already). There is no operator *open*: only this supervisor's policy
        opens. ``emergency`` (the console's EMERGENCY CLOSE) overrides the
        park-before-close interlock until the enclosure is closed (module
        doc); otherwise it is a routine close."""
        lease = self._lease
        self.timeline.event(
            self.name, "operator_close", operator=operator, reason=reason, emergency=emergency
        )
        log.warning(
            "%s: %sclose requested by %s: %s",
            self.name,
            "EMERGENCY " if emergency else "",
            operator,
            reason,
        )
        if emergency:
            self._emergency_close = f"operator {operator}: {reason}"
            self._wake.set()
        if lease is None or not lease.live:
            return False
        self._revoke_lease(f"operator {operator}: {reason}")
        self._wake.set()
        return True

    async def recover(self, lease_id: int, holder: str) -> bool:
        """A restarted sequencer releases its predecessor's lease, after cleanup.

        Refused (:class:`RecoveryRefused`) for another holder's lease, a live
        or quarantined lease, or while the enclosure is not confirmed closed.
        Idempotent: True if released now, False if it already was.
        """
        lease = self.outstanding()
        if lease is not None and lease.id == lease_id:
            if lease.holder != holder:
                raise RecoveryRefused(f"lease #{lease_id} belongs to {lease.holder!r}")
            if lease.quarantined:
                raise RecoveryRefused(
                    f"lease #{lease_id} is quarantined; an operator must clear it"
                )
        return await self._release_stale(lease_id, "lease_recovered")

    async def clear(self, lease_id: int, operator: str, reason: str) -> bool:
        """An operator releases a dead (or quarantined) lease, having made sure
        the telescope is safe. Otherwise the same conditions as :meth:`recover`."""
        released = await self._release_stale(
            lease_id, "lease_cleared", operator=operator, reason=reason
        )
        if released:
            log.warning("%s: lease #%d cleared by %s: %s", self.name, lease_id, operator, reason)
        return released

    async def _release_stale(self, lease_id: int, event: str, **detail: Any) -> bool:
        lease = self._lease
        if lease is None or lease.id < lease_id:
            raise RecoveryRefused(f"no lease #{lease_id}")
        if lease.id > lease_id or lease.released.is_set():
            return False  # released already (a newer lease implies it)
        if lease.live:
            raise RecoveryRefused(f"lease #{lease_id} is live")
        if await self._shutter_status() != Shutter.CLOSED:
            raise RecoveryRefused("enclosure not confirmed closed yet")
        if lease.released.is_set():
            return False  # a concurrent recover/clear won the race during the read
        lease.release()
        self.timeline.event(self.name, event, lease=lease_id, holder=lease.holder, **detail)
        return True

    def _orphan_age(self, now: float) -> float | None:
        """How long the outstanding lease has been revoked, if it has been."""
        lease = self.outstanding()
        if lease is None or lease.revoked_at is None:
            return None
        return now - lease.revoked_at

    # -- safety state -----------------------------------------------------

    def _set_safety(self, safe: bool, reason: str) -> None:
        now = time.monotonic()
        if safe:
            if not self._safe:
                self._safe = True
                self._safe_since = now
                self.timeline.event(self.name, "weather_safe", hold=self.config.reopen_hold)
        else:
            changed = self._safe or reason != self._reason
            self._safe = False
            self._safe_since = None
            if changed:
                self.timeline.event(self.name, "unsafe", reason=reason)
            self._revoke_lease(f"unsafe: {reason}")
            self._wake.set()
        self._reason = reason

    async def _read_weather(self) -> tuple[bool, str, bool]:
        """Return (safe, reason, read_succeeded)."""
        to = self.config.read_timeout
        try:
            is_safe = await self._sm.get("IsSafe", timeout=to)
            age = None
            if self._oc is not None:
                age = await self._oc.call("TimeSinceLastUpdate", "", timeout=to)
        except DeviceError as exc:
            return False, f"weather read failed: {exc}", False
        if is_safe is not True:
            self._unsafe_verdict = f"SafetyMonitor.IsSafe={is_safe!r}"
            return False, self._unsafe_verdict, True
        self._unsafe_verdict = None
        if age is not None and not (0 <= float(age) <= self.config.stale_after):
            return False, f"weather data stale ({float(age):.1f} s old)", True
        return True, "safe", True

    async def _weather_loop(self) -> None:
        while not self._stopping:
            try:
                taken = time.monotonic()
                safe, reason, ok = await self._read_weather()
                if self._stopping:
                    return
                if ok:
                    self._last_good_read = time.monotonic()
                self._set_safety(safe, reason)
                if safe:
                    self._safe_read_at = taken
            except Exception as exc:  # never let the loop die
                log.exception("weather loop")
                self._set_safety(False, f"weather loop error: {exc!r}")
            await asyncio.sleep(self.config.poll_interval)

    # -- enclosure --------------------------------------------------------

    async def _shutter_status(self) -> int | None:
        try:
            status = await self._dome.get("ShutterStatus", timeout=self.config.dome_timeout)
        except DeviceError as exc:
            self._alarm(f"cannot read shutter: {exc}")
            return None
        self.shutter = int(status)
        return self.shutter

    def _alarm(self, msg: str, level: str = "alarm") -> None:
        if not self.alarms or self.alarms[-1] != msg:
            self.alarms.append(msg)
            self.timeline.event(self.name, "alarm", msg=msg, level=level)
            (log.critical if level == "critical" else log.error)("%s: %s", self.name, msg)

    def _want_open(self, now: float) -> bool:
        lease = self._lease
        return self.open_allowed(now, keep=True) and lease is not None and lease.live

    async def _check_faults(self) -> None:
        """Read the dome's fault channel; alarm when a fault appears or changes.

        A fault (or a failed read: unknown counts as faulted) revokes the
        lease and stops opening, so the enclosure closes unless it already
        is. A fault while closed only alarms (once).
        """
        if self._faults is None:
            return
        cfg = self.config
        data: dict[str, Any] | None = None
        try:
            raw = await self._faults.call("Action", cfg.fault_action, "", timeout=cfg.dome_timeout)
            data = json.loads(raw)
            fault, error = data["fault"], data["error"]
            if not (isinstance(fault, bool) and isinstance(error, bool)):
                raise ValueError(f"fault/error not booleans in {raw!r}")
        except Exception as exc:  # anything: never skip the enforcement below
            kind: object = "unreadable"
            problem: str | None = f"cannot read enclosure faults: {exc}"
        else:
            kind = (fault, error)
            parts = [
                name + (f" ({data.get(text)})" if data.get(text) else "")
                for flag, name, text in ((fault, "Fault", "faults"), (error, "Error", "errors"))
                if flag
            ]
            problem = f"enclosure reports {', '.join(parts)}" if parts else None
        previous = self._fault
        if kind == self._fault_kind:
            self._fault = problem  # same condition; its text may have arrived
        else:
            self._fault, self._fault_kind = problem, kind
            if problem is None:
                self._fault_escalated = False
                self.timeline.event(self.name, "enclosure_fault_cleared", was=previous)
                log.warning("%s: enclosure fault cleared (was: %s)", self.name, previous)
            else:
                self._alarm(f"{problem}; not opening until it clears")
                self._revoke_lease(problem)
                self._wake.set()
        faulted = data if problem is not None and kind != "unreadable" else None
        self._note_estop(faulted)
        if self._fault_auto_clear():
            await self._auto_clear(faulted)

    def _note_estop(self, data: dict[str, Any] | None) -> None:
        """An emergency stop on the fault channel: a critical alarm, once."""
        estop = data is not None and (
            data.get("estop") is True
            or mentions_estop(data.get("faults"))
            or mentions_estop(data.get("errors"))
        )
        if not estop:
            self._estop = None
            return
        if self._estop is None:
            self._estop = (
                f"E-STOP reported by the enclosure ({self._fault}): never cleared"
                " automatically; an operator must reset it and clear faults"
            )
            self._alarm(self._estop, "critical")

    async def _auto_clear(self, data: dict[str, Any] | None) -> None:
        """``fault_auto_clear``: clear a fault ourselves, by the module's rules.

        ``data`` is the fault channel's reply while it reports a fault (``None``
        when it reports none, or cannot be read).
        """
        cfg = self.config
        now = time.monotonic()
        if data is None:
            if self._fault is None and self._auto_cleared_at is not None:
                self._auto_clear_took = True  # it went away after our clear
            return
        if self._estop is not None or self._auto_clear_stopped is not None:
            return
        if data.get("emergency_close"):
            return  # an operator's latch: only an operator releases it
        if (data.get("fault") and not data.get("faults")) or (
            data.get("error") and not data.get("errors")
        ):
            return  # wait for the fault text, so an E-stop can be recognised
        last = self._auto_cleared_at
        if last is not None and now - last < cfg.fault_auto_clear_interval:
            if not self._auto_clear_took and now - last < cfg.fault_auto_clear_settle:
                return  # the controller may not have dropped its flags yet
            how = "is back" if self._auto_clear_took else "did not clear"
            self._stop_auto_clear(
                f"enclosure fault {how} {now - last:.0f} s after automatic clear"
                f" #{self._auto_clears} ({self._fault})"
            )
            return
        if data.get("shutters") != ["CLOSED", "CLOSED"]:
            return  # never while open or moving; the enclosure loop closes it
        if await self._shutter_status() != Shutter.CLOSED:
            return
        self._auto_clears += 1
        self._auto_cleared_at, self._auto_clear_took = now, False
        n, fault = self._auto_clears, self._fault
        self._alarm(
            f"automatic fault clear #{n}, enclosure closed: {fault}"
            f" (at most one per {cfg.fault_auto_clear_interval:g} s)"
        )
        self.timeline.event(self.name, "fault_auto_clear", n=n, fault=fault)
        try:
            reply = await self._dome.call(
                "Action",
                cfg.clear_faults_action,
                f"{AUTO_CLEAR_PREFIX} {self.name} automatic clear #{n}: {fault}",
                timeout=cfg.dome_timeout,
            )
        except DeviceError as exc:
            self._stop_auto_clear(f"automatic clear #{n} failed: {exc}")
            return
        self.timeline.event(self.name, "faults_cleared", operator=None, reply=reply, n=n)
        self._wake.set()

    def _stop_auto_clear(self, why: str) -> None:
        self._auto_clear_stopped = (
            f"{why}; automatic fault clearing stopped: an operator must clear faults"
            " (console clear_faults)"
        )
        self._alarm(self._auto_clear_stopped, "critical")
        self.timeline.event(self.name, "fault_auto_clear_stopped", why=why)

    async def clear_faults(self, operator: str, reason: str) -> str:
        """An operator clears the enclosure's faults (logged with their name).

        The dome refuses unless it is closed. A successful clear also restarts
        automatic clearing (``fault_auto_clear``) if a recurrence stopped it.
        """
        note = f"{operator}: {reason}"
        self.timeline.event(self.name, "clear_faults", operator=operator, reason=reason)
        log.warning("%s: ClearFaults by %s", self.name, note)
        try:
            reply = await self._dome.call(
                "Action", self.config.clear_faults_action, note, timeout=self.config.dome_timeout
            )
        except DeviceError as exc:
            self._alarm(f"ClearFaults by {operator} failed: {exc}")
            raise
        self.timeline.event(self.name, "faults_cleared", operator=operator, reply=reply)
        if self._auto_clear_stopped is not None:
            log.warning("%s: automatic fault clearing restarted by %s", self.name, operator)
        self._auto_clear_stopped = None
        self._auto_cleared_at, self._auto_clear_took = None, False
        self._wake.set()
        return str(reply)

    async def _enforce_once(self) -> None:
        await self._check_faults()
        now = time.monotonic()
        cfg = self.config
        self._check_duty(now)
        if (
            self._unwatched_active()
            and self._unwatched_warned is not None
            and now - self._unwatched_warned >= cfg.unwatched_warning_interval
        ):
            self._warn_unwatched("still unattended with no watchdog", now)
        if self._safe and (
            self._last_good_read is None or now - self._last_good_read > cfg.stale_after
        ):
            self._set_safety(False, f"no successful weather read for > {cfg.stale_after} s")
        lease = self._lease
        if lease is not None and lease.live and lease.expired(now):
            self._revoke_lease(f"lease not renewed within {lease.ttl} s")
        orphaned_for = self._orphan_age(now)
        if lease is not None and orphaned_for is not None and orphaned_for > cfg.orphan_timeout:
            self._alarm(
                f"lease #{lease.id} ({lease.holder}) orphaned: unreleased"
                f" {cfg.orphan_timeout:g} s after revocation; restart its sequencer"
                " or have an operator clear it"
            )
        self._note_would_open(now)
        if self._watching():
            await self._watch()
        elif self._want_open(now):
            await self._ensure_open()
        else:
            await self._ensure_closed()

    def _note_would_open(self, now: float) -> None:
        """A mode that may not open: record when it *would* open or close
        (``shadow_would`` events), on each change."""
        if self.policy.open:
            self._would_open = None
            return
        would = self.open_allowed(now, ignore_mode=True)
        if would != self._would_open:
            self._would_open = would
            self.timeline.event(
                self.name,
                "shadow_would",
                mode=self.mode,
                open=would,
                reason=self._reason if not would else "safe, held, no fault",
            )
            log.info("%s: mode %s: would %s", self.name, self.mode, "open" if would else "close")

    def active_risk(self) -> str | None:
        """An enforced close condition active now, which a ``watch`` mode
        with ``intervene`` acts on (module doc): the safety monitor's
        last successful verdict unsafe (a read failure keeps it), or a fault
        read from the enclosure's fault channel. None: none known."""
        if self._unsafe_verdict is not None:
            return f"weather unsafe ({self._unsafe_verdict})"
        if self._fault is not None and isinstance(self._fault_kind, tuple):
            return f"enclosure fault ({self._fault})"
        return None

    async def _watch(self) -> None:
        """A ``watch`` mode: read the shutter, command nothing -- except
        close on an active risk when the mode's ``intervene`` is set (module doc)."""
        self._opening_since = None
        status = await self._shutter_status()
        risk = self.active_risk() if self.policy.intervene else None
        if risk is not None and status != Shutter.CLOSED:
            if self._intervening is None:
                self._intervening = risk
                shutter = "unreadable" if status is None else Shutter(status).name
                self._alarm(
                    f"mode {self.mode} intervened: closing the enclosure (shutter {shutter})"
                    f" on {risk}",
                    "critical",
                )
                self.timeline.event(
                    self.name, "shadow_intervened", mode=self.mode, risk=risk, shutter=shutter
                )
            self._watch_alarm = None
            await self._ensure_closed()
            return
        self._unblock_close(f"mode {self.mode} watches only")
        if self._intervening is not None:
            if status == Shutter.CLOSED:
                await self._ensure_closed()  # its bookkeeping: enclosure_closed
                self.timeline.event(self.name, "watching", mode=self.mode, after=self._intervening)
            self._intervening = None
        self._closing_since = None
        if status is None or status == Shutter.CLOSED:
            self._watch_alarm = None
            return
        if self._watch_alarm is None:
            self._watch_alarm = (
                f"enclosure {Shutter(status).name} in mode {self.mode}, which commands nothing:"
                " robofast will not close it"
                + (" unless a close condition becomes active" if self.policy.intervene else "")
                + " (whoever opened it must, or switch modes)"
            )
            self._alarm(self._watch_alarm, "critical")

    async def _ensure_closed(self) -> None:
        cfg = self.config
        self._opening_since = None
        status = await self._shutter_status()
        now = time.monotonic()
        if status == Shutter.CLOSED:
            if self._finish_close and self.outstanding() is None:
                self._finish_close = False  # closed: from now on only watched
                self.timeline.event(self.name, "watching", mode=self.mode)
            if self._closing_since is not None:
                self.timeline.event(self.name, "enclosure_closed", took=now - self._closing_since)
            self._closing_since = None
            self._parked_for_close = False
            self._close_committed = False
            self._fault_escalated = False
            self._emergency_close = None
            self._unblock_close("closed")
            return
        if self._fault is not None and not self._fault_escalated:
            # The dangerous case: a fault with the enclosure not closed.
            self._fault_escalated = True
            name = "unknown" if status is None else Shutter(status).name
            self._alarm(f"FAULT WITH ENCLOSURE NOT CLOSED (shutter {name}): {self._fault}; closing")
        if status == Shutter.CLOSING:
            self._close_committed = True  # however it began: no grant until CLOSED
            if self._closing_since is None:
                self._closing_since = now
            if now - self._closing_since < cfg.shutter_timeout:
                return
            self._alarm(f"shutter still closing after {cfg.shutter_timeout} s; re-commanding")
            self._closing_since = now
        if cfg.park_before_close and not self._parked_for_close:
            await self._park_before_close()
        if (
            cfg.require_park_before_close
            and not self._close_committed
            and not await self._close_interlock_clear()
        ):
            self._parked_for_close = False  # park again on the next pass
            return
        if not self._close_committed and self._want_open(time.monotonic()):
            # A new lease was granted while we read the shutter or parked
            # (an idle release followed at once by more work): opening is
            # wanted again, so do not close on it; the next pass keeps it open.
            self._parked_for_close = False
            return
        # Committed: from here until the shutter reads CLOSED no lease is
        # granted (request_open). Set with no await since the check above,
        # so a lease granted before now was seen there, and none after.
        self._close_committed = True
        try:
            await self._dome.call("CloseShutter", timeout=cfg.dome_timeout)
        except DeviceError as exc:
            self._alarm(f"CloseShutter failed: {exc}")
        if self._closing_since is None:
            self._closing_since = time.monotonic()
            self.timeline.event(
                self.name,
                "close_commanded",
                shutter=None if status is None else Shutter(status).name,
            )

    async def _park_before_close(self) -> None:
        """Roll-off roofs: give the sequencer's cleanup a bounded chance to
        park, then park every telescope ourselves (in parallel), then close
        regardless."""
        cfg = self.config
        lease = self._lease
        if lease is not None and not lease.released.is_set():
            try:
                await asyncio.wait_for(lease.released.wait(), cfg.park_wait / 2)
            except TimeoutError:
                self._alarm("sequencer did not finish cleanup before close")

        async def park(tel: Grant) -> None:
            try:
                async with asyncio.timeout(cfg.park_wait / 2):
                    if self._want_open(time.monotonic()):
                        return  # a new live lease: its sequencer has the telescopes
                    if not await tel.get("AtPark"):
                        await tel.call("Park", timeout=cfg.park_wait / 2)
                        while not await tel.get("AtPark"):
                            await asyncio.sleep(0.2)
            except (DeviceError, TimeoutError) as exc:
                self._alarm(f"{tel.owner.name} not confirmed parked before close: {exc!r}")

        await asyncio.gather(*(park(t) for t in self._tels))
        self._parked_for_close = True

    def emergency(self) -> str | None:
        """Why a close now would be an emergency (None: a routine close),
        for the park-before-close interlock (module doc)."""
        if self._stopping:
            return "supervisor stopping"
        if self._emergency_close is not None:
            return f"emergency close ({self._emergency_close})"
        if not self._safe:
            return f"weather: {self._reason}"
        if self._fault is not None:
            return f"enclosure fault: {self._fault}"
        hold = self._shutter_hold_until
        if hold is not None and time.monotonic() < hold:
            return "shutter error"
        return None

    async def _close_interlock_clear(self) -> bool:
        """``require_park_before_close``: may the enclosure close now? Yes
        for an emergency, or once every telescope reports ``AtPark`` (an
        unreadable one counts as unparked). Otherwise the close is held
        back: alarm, and escalate after ``park_interlock_escalate``."""
        why = self.emergency()
        if why is not None:
            if self._close_blocked is not None:
                self._alarm(
                    f"closing with telescopes possibly unparked: {why} overrides the"
                    " park-before-close interlock",
                    "critical",
                )
                self._unblock_close(f"overridden: {why}")
            return True

        async def parked(tel: Grant) -> str | None:
            try:
                if await tel.get("AtPark", timeout=self.config.read_timeout):
                    return None
                return f"{tel.owner.name} not parked"
            except DeviceError as exc:
                return f"{tel.owner.name} unreadable ({exc})"

        problems = [p for p in await asyncio.gather(*(parked(t) for t in self._tels)) if p]
        if self.emergency() is not None:
            return await self._close_interlock_clear()  # it became one meanwhile
        if not problems:
            self._unblock_close("all parked")
            return True
        now = time.monotonic()
        blocked = "; ".join(problems)
        if self._close_blocked_since is None:
            self._close_blocked_since = now
            self.timeline.event(self.name, "close_blocked", why=blocked, level="warning")
        if blocked != self._close_blocked:
            self._alarm(
                f"routine close held back by the park-before-close interlock ({blocked});"
                " the enclosure stays open until every telescope is parked"
            )
        self._close_blocked = blocked
        waited = now - self._close_blocked_since
        if self._close_blocked_critical is None and waited >= self.config.park_interlock_escalate:
            self._close_blocked_critical = (
                f"enclosure NOT CLOSED for {waited:.0f} s: the park-before-close interlock"
                f" holds a routine close ({blocked}); park the telescopes, or close as"
                " an emergency"
            )
            self._alarm(self._close_blocked_critical, "critical")
            self.timeline.event(self.name, "close_blocked_escalated", why=blocked, waited=waited)
        return False

    def _unblock_close(self, how: str) -> None:
        if self._close_blocked_since is None:
            return
        self.timeline.event(
            self.name,
            "close_unblocked",
            how=how,
            waited=time.monotonic() - self._close_blocked_since,
        )
        self._close_blocked = self._close_blocked_since = self._close_blocked_critical = None

    async def _ensure_open(self) -> None:
        cfg = self.config
        self._unblock_close("open wanted again")
        self._closing_since = None
        lease = self._lease
        assert lease is not None
        status = await self._shutter_status()
        now = time.monotonic()
        if status is None or status == Shutter.ERROR:
            self._shutter_failed(status, now)
            return
        self._shutter_bad_since = None
        if status == Shutter.OPEN:
            self._opening_since = None
            if not lease.opened.is_set():
                lease.opened.set()
                self.timeline.event(self.name, "enclosure_open", lease=lease.id)
            return
        if status == Shutter.CLOSING:
            return  # let it finish; reopen on a later tick if still wanted
        if status == Shutter.OPENING and self._opening_since is not None:
            if now - self._opening_since < cfg.shutter_timeout:
                return
            self._alarm(f"shutter still opening after {cfg.shutter_timeout} s")
        # Re-check immediately before the command: the status read awaited.
        if not self._want_open(time.monotonic()):
            return
        try:
            await self._dome.call("OpenShutter", timeout=cfg.dome_timeout)
        except DeviceError as exc:
            self._alarm(f"OpenShutter failed: {exc}")
            return
        self._opening_since = time.monotonic()
        self.timeline.event(self.name, "open_commanded", lease=lease.id)

    def _shutter_failed(self, status: int | None, now: float) -> None:
        """The shutter is in error, or unreadable, while it should be open.

        Fail closed: an error revokes the lease at once; an unreadable shutter
        after ``shutter_unreadable_grace`` (one failed read is not enough).
        Either way reopening is blocked for ``shutter_error_backoff``.
        """
        cfg = self.config
        if self._shutter_bad_since is None:
            self._shutter_bad_since = now
        if status is None and now - self._shutter_bad_since < cfg.shutter_unreadable_grace:
            return
        if status is None:
            reason = f"shutter unreadable for {now - self._shutter_bad_since:.0f} s"
        else:
            reason = "shutter in error"
        self._alarm(
            f"{reason} while open was wanted; closing, no reopen for"
            f" {cfg.shutter_error_backoff:g} s"
        )
        self._shutter_hold_until = now + cfg.shutter_error_backoff
        self._shutter_bad_since = None
        self._revoke_lease(reason)
        self._wake.set()

    async def _enclosure_loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                await self._enforce_once()
            except Exception as exc:  # never let the loop die
                log.exception("enclosure loop")
                self._alarm(f"enclosure loop error: {exc!r}")
            else:
                self._enforced_at = time.monotonic()
            try:
                await asyncio.wait_for(self._wake.wait(), self.config.poll_interval)
            except TimeoutError:
                pass

    # -- heartbeat ----------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Ping the enclosure hardware while the enclosure loop is healthy.

        Healthy means "a pass of the enclosure loop completed recently", so the
        pings also stop when that loop is stuck, not only when the process is
        gone.
        """
        cfg = self.config
        assert self._hb is not None and cfg.heartbeat_interval is not None
        # No ping once run() stops, even if this task's cancel was lost: the
        # pings would keep the hardware from closing by itself.
        while not self._stopping:
            enforced = self._enforced_at
            age = None if enforced is None else time.monotonic() - enforced
            if age is not None and age <= cfg.heartbeat_max_loop_age:
                try:
                    await self._hb.call(
                        "Action", cfg.heartbeat_action, "", timeout=cfg.dome_timeout
                    )
                    self._pinged_at = time.monotonic()
                except DeviceError as exc:
                    self._alarm(f"heartbeat ping failed: {exc}")
            elif age is not None:
                self._alarm(
                    f"enclosure loop stuck > {cfg.heartbeat_max_loop_age} s; heartbeat withheld"
                )
            await asyncio.sleep(cfg.heartbeat_interval)

    async def run(self) -> None:
        """Run until cancelled. On the way out, try (bounded) to close.

        Stopping never hangs: the loops end on ``_stopping`` even if their
        cancel is lost (:mod:`robofast.cancel`), the wait for them is
        bounded, and the final close goes out either way, even if this task
        is cancelled again meanwhile (that cancellation is passed on after).
        A loop that ends by itself (it should not) stops the supervisor too,
        closing.
        """
        self.timeline.event(self.name, "supervisor_start", config=vars(self.config))
        self._stopping = False
        loops = [
            asyncio.create_task(self._weather_loop(), name=f"{self.name}-weather"),
            asyncio.create_task(self._enclosure_loop(), name=f"{self.name}-enclosure"),
        ]
        if self._hb is not None:
            loops.append(asyncio.create_task(self._heartbeat_loop(), name=f"{self.name}-heartbeat"))
        try:
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()
            raise RuntimeError(f"{self.name}: a supervisor loop ended by itself")
        finally:
            self._stopping = True
            self._safe = False
            self._revoke_lease("supervisor stopping")
            again: asyncio.CancelledError | None = None
            try:
                await stop_tasks(loops, self.config.dome_timeout)
            except asyncio.CancelledError as exc:  # cancelled again: close anyway
                again = exc
            try:
                if self._watching():
                    log.warning(
                        "%s: mode %s commands nothing: no final close", self.name, self.mode
                    )
                else:
                    await asyncio.wait_for(
                        self._dome.call("CloseShutter", timeout=self.config.dome_timeout),
                        self.config.dome_timeout + 1,
                    )
            except asyncio.CancelledError as exc:
                log.error("final CloseShutter cancelled")
                again = exc
            except Exception as exc:
                log.error("final CloseShutter failed: %r", exc)
            self.timeline.event(self.name, "supervisor_stop")
            if again is not None:
                raise again
