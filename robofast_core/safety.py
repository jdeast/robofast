"""The safety supervisor: the only component allowed to open or close.

Policy, in one paragraph: the enclosure is open only while (a) the weather
has been *continuously* safe for ``reopen_hold`` seconds, and (b) a live
sequencer holds an unexpired, unrevoked :class:`OpenLease`. Anything else
-- unsafe weather, a weather reading that errors or times out, weather data
older than ``stale_after``, no successful read for ``stale_after``, a lease
that was not renewed, or no lease at all -- means "close". The supervisor
never asks anyone before closing; it revokes the lease (which tells the
sequencer to stop and clean up) and closes in the same step.

Its loops run under one ``TaskGroup``:

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
  :mod:`robofast_core.heartbeat_watchdog`). ``heartbeat_interval`` must be
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

from robofast_core.owner import DeviceError, DeviceOwner, Grant
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: Dome members only the safety supervisor may call (``Action:Name`` is one
#: Alpaca action; ``ClearFaults`` is for an operator, through the supervisor).
ENCLOSURE_MEMBERS = frozenset({"OpenShutter", "CloseShutter", "Action:ClearFaults"})

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
    #: No lease (so no opening) unless an operator is on duty (module doc).
    #: ``None``: true exactly when there is no hardware heartbeat device
    #: (``heartbeat_interval`` unset). Setting it false without a heartbeat
    #: runs an enclosure unattended with nothing behind the supervisor.
    attended_only: bool | None = None
    #: An operator's duty expires (critical alarm) this long after its last
    #: renewal, and the supervisor closes ``duty_grace`` after that.
    duty_ttl: float = 300.0
    duty_grace: float = 300.0

    def __post_init__(self) -> None:
        if self.attended_only is None:
            self.attended_only = self.heartbeat_interval is None
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
    ) -> None:
        self.name = name
        self.timeline = timeline
        self.config = config or SafetyConfig()
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
        if not self.config.attended_only and self._hb is None:
            log.warning(
                "%s: attended_only is off and there is no hardware heartbeat:"
                " nothing closes this enclosure if the supervisor dies",
                name,
            )

        self._safe = False
        self._reason = "startup (no weather read yet)"
        self._safe_since: float | None = None
        self._last_good_read: float | None = None
        self._lease: OpenLease | None = None
        self._lease_ids = itertools.count(1)
        self._wake = asyncio.Event()
        self._closing_since: float | None = None
        self._opening_since: float | None = None
        self._parked_for_close = False
        #: Set when the enclosure loop commits to closing (right before
        #: CloseShutter), cleared once the shutter reads CLOSED: no lease is
        #: granted in between, so no sequencer unparks a telescope under a
        #: closing roof (request_open).
        self._close_committed = False
        self._enforced_at: float | None = None
        self._pinged_at: float | None = None
        self.shutter: int | None = None
        self.alarms: deque[str] = deque(maxlen=100)

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

    def open_allowed(self, now: float | None = None, *, keep: bool = False) -> bool:
        """May the enclosure open (``keep``: stay open; an expired duty's
        grace counts then)?"""
        now = time.monotonic() if now is None else now
        return (
            self.attended(now, keep=keep)
            and self._safe
            and self._fault is None
            and (self._shutter_hold_until is None or now >= self._shutter_hold_until)
            and self._safe_since is not None
            and now - self._safe_since >= self.config.reopen_hold
        )

    @property
    def fault(self) -> str | None:
        """The enclosure fault (from ``fault_action``), if any."""
        return self._fault

    def critical(self) -> list[str]:
        """Conditions that need an operator now (the console raises them as
        critical alarms until they are gone)."""
        return [c for c in (self._estop, self._auto_clear_stopped, self._duty_expired) if c]

    # -- operator duty (attended_only) ---------------------------------------

    def attended(self, now: float | None = None, *, keep: bool = False) -> bool:
        """True unless ``attended_only`` and no operator's duty is current
        (``keep``: or within its grace, for an enclosure already open)."""
        cfg = self.config
        if not cfg.attended_only:
            return True
        duty = self._duty
        now = time.monotonic() if now is None else now
        limit = cfg.duty_ttl + (cfg.duty_grace if keep else 0.0)
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
        if self.config.attended_only:
            self._revoke_lease(f"operator {operator} went off duty (attended only)")
        self._wake.set()
        return self.duty_status()

    def duty_status(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        duty = self._duty
        cfg = self.config
        out: dict[str, Any] = {
            "attended_only": bool(cfg.attended_only),
            "on_duty": None if duty is None else duty["operator"],
            "attended": self.attended(now),
            "ttl": cfg.duty_ttl,
            "grace": cfg.duty_grace,
        }
        if duty is not None:
            age = now - duty["renewed"]
            out.update(
                since_s=now - duty["since"],
                renewed_ago=age,
                expires_in=cfg.duty_ttl - age,
                closes_in=None if not cfg.attended_only else cfg.duty_ttl + cfg.duty_grace - age,
            )
        return out

    def _check_duty(self, now: float) -> None:
        """Duty expiry: a critical alarm after ``duty_ttl``; after
        ``duty_grace`` more, the lease is revoked (so the enclosure closes)."""
        cfg = self.config
        duty = self._duty
        if not cfg.attended_only or duty is None:
            return
        age = now - duty["renewed"]
        if age <= cfg.duty_ttl:
            return
        if self._duty_expired is None:
            self._duty_expired = (
                f"operator duty ({duty['operator']}) not renewed for {cfg.duty_ttl:g} s:"
                f" this enclosure is attended only; closing in {cfg.duty_grace:g} s"
                " unless the console renews it"
            )
            self._alarm(self._duty_expired, "critical")
            self.timeline.event(self.name, "duty_expired", operator=duty["operator"])
        if age > cfg.duty_ttl + cfg.duty_grace:
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
            "fault_auto_clear": None
            if not self.config.fault_auto_clear
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
        while True:
            # The (awaited) park check first, then the synchronous ones with
            # no await between them and the grant: _ensure_closed commits to
            # a close (``_close_committed``) synchronously too, so a grant and
            # a close can never both go ahead.
            parked = await self._telescopes_parked()
            if (
                parked
                and not self._close_committed
                and self.open_allowed()
                and self.outstanding() is None
            ):
                break
            await asyncio.sleep(min(self.config.poll_interval, 1.0))
        lease = OpenLease(next(self._lease_ids), holder, self.config.lease_ttl)
        self._lease = lease
        self.timeline.event(self.name, "lease_granted", lease=lease.id, holder=holder)
        self._wake.set()
        return lease

    def _revoke_lease(self, reason: str) -> None:
        lease = self._lease
        if lease is not None and lease._revoke(reason):
            self.timeline.event(self.name, "lease_revoked", lease=lease.id, reason=reason)

    def operator_close(self, operator: str, reason: str) -> bool:
        """An operator asks for the enclosure to close (the observer console):
        the current lease is revoked, so the sequencer stops and cleans up
        and the enclosure closes, as for any revocation. True if a live
        lease was revoked; False if there was none (it is closed, or closing,
        already). There is no operator *open*: only this supervisor's policy
        opens."""
        lease = self._lease
        self.timeline.event(self.name, "operator_close", operator=operator, reason=reason)
        log.warning("%s: close requested by %s: %s", self.name, operator, reason)
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
            return False, f"SafetyMonitor.IsSafe={is_safe!r}", True
        if age is not None and not (0 <= float(age) <= self.config.stale_after):
            return False, f"weather data stale ({float(age):.1f} s old)", True
        return True, "safe", True

    async def _weather_loop(self) -> None:
        while True:
            try:
                safe, reason, ok = await self._read_weather()
                if ok:
                    self._last_good_read = time.monotonic()
                self._set_safety(safe, reason)
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
        if cfg.fault_auto_clear:
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
                "ClearFaults",
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
                "Action", "ClearFaults", note, timeout=self.config.dome_timeout
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
        if self._want_open(now):
            await self._ensure_open()
        else:
            await self._ensure_closed()

    async def _ensure_closed(self) -> None:
        cfg = self.config
        self._opening_since = None
        status = await self._shutter_status()
        now = time.monotonic()
        if status == Shutter.CLOSED:
            if self._closing_since is not None:
                self.timeline.event(self.name, "enclosure_closed", took=now - self._closing_since)
            self._closing_since = None
            self._parked_for_close = False
            self._close_committed = False
            self._fault_escalated = False
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

    async def _ensure_open(self) -> None:
        cfg = self.config
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
        while True:
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
        while True:
            enforced = self._enforced_at
            age = None if enforced is None else time.monotonic() - enforced
            if age is not None and age <= cfg.heartbeat_max_loop_age:
                try:
                    await self._hb.call("Action", "Heartbeat", "", timeout=cfg.dome_timeout)
                    self._pinged_at = time.monotonic()
                except DeviceError as exc:
                    self._alarm(f"heartbeat ping failed: {exc}")
            elif age is not None:
                self._alarm(
                    f"enclosure loop stuck > {cfg.heartbeat_max_loop_age} s; heartbeat withheld"
                )
            await asyncio.sleep(cfg.heartbeat_interval)

    async def run(self) -> None:
        """Run until cancelled. On the way out, try (bounded) to close."""
        self.timeline.event(self.name, "supervisor_start", config=vars(self.config))
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._weather_loop(), name=f"{self.name}-weather")
                tg.create_task(self._enclosure_loop(), name=f"{self.name}-enclosure")
                if self._hb is not None:
                    tg.create_task(self._heartbeat_loop(), name=f"{self.name}-heartbeat")
        finally:
            self._safe = False
            self._revoke_lease("supervisor stopping")
            try:
                await asyncio.wait_for(
                    self._dome.call("CloseShutter", timeout=self.config.dome_timeout),
                    self.config.dome_timeout + 1,
                )
            except BaseException as exc:
                log.error("final CloseShutter failed: %r", exc)
            self.timeline.event(self.name, "supervisor_stop")
