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
  :mod:`robofast_core.heartbeat_watchdog`).

The supervisor holds the dome owner's single *privileged* grant: the owner
refuses ``OpenShutter``/``CloseShutter`` from anyone else.
"""

from __future__ import annotations

import asyncio
import enum
import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any

from robofast_core.owner import DeviceError, DeviceOwner
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: Dome members only the safety supervisor may call.
ENCLOSURE_MEMBERS = frozenset({"OpenShutter", "CloseShutter"})


class Shutter(enum.IntEnum):
    """ASCOM ShutterState."""

    OPEN = 0
    CLOSED = 1
    OPENING = 2
    CLOSING = 3
    ERROR = 4


class LeaseRevoked(Exception):
    pass


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
    #: Roll-off roofs: wait for the telescope to be parked (or park it
    #: ourselves) before closing. Domes close immediately.
    park_before_close: bool = False
    park_wait: float = 60.0
    #: Seconds between hardware heartbeat pings; ``None`` when the enclosure
    #: has no heartbeat device. Keep it well below the device's own timeout.
    heartbeat_interval: float | None = None
    #: Ping only while the enclosure loop finished a pass within this long.
    heartbeat_max_loop_age: float = 15.0


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

    def _revoke(self, reason: str) -> bool:
        if self.revoked.is_set():
            return False
        self.revoke_reason = reason
        self.revoked.set()
        return True

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
        telescope: DeviceOwner | None = None,
        heartbeat: DeviceOwner | None = None,
        config: SafetyConfig | None = None,
    ) -> None:
        self.name = name
        self.timeline = timeline
        self.config = config or SafetyConfig()
        self._dome = dome.grant(name, privileged=True)
        self._sm = safety_monitor.grant(name)
        self._oc = observing_conditions.grant(name) if observing_conditions else None
        self._tel = telescope.grant(name) if telescope else None
        if self.config.park_before_close and self._tel is None:
            raise ValueError("park_before_close needs a telescope owner")
        if (heartbeat is None) != (self.config.heartbeat_interval is None):
            raise ValueError("a heartbeat device and heartbeat_interval go together")
        self._hb = heartbeat.grant(f"{name}/heartbeat") if heartbeat else None

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
        self._enforced_at: float | None = None
        self._pinged_at: float | None = None
        self.shutter: int | None = None
        self.alarms: list[str] = []

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

    def open_allowed(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (
            self._safe
            and self._safe_since is not None
            and now - self._safe_since >= self.config.reopen_hold
        )

    def status(self) -> dict[str, Any]:
        lease = self._lease
        now = time.monotonic()
        return {
            "safe": self._safe,
            "reason": self._reason,
            "open_allowed": self.open_allowed(),
            "shutter": None if self.shutter is None else Shutter(self.shutter).name,
            "lease": repr(lease) if lease else None,
            "alarms": list(self.alarms),
            "enforced_ago": None if self._enforced_at is None else now - self._enforced_at,
            "pinged_ago": None if self._pinged_at is None else now - self._pinged_at,
        }

    # -- leases -----------------------------------------------------------

    def _can_grant(self) -> bool:
        prev = self._lease
        return self.open_allowed() and (prev is None or prev.released.is_set())

    async def request_open(self, holder: str) -> OpenLease:
        """Wait until opening is allowed and no earlier lease is outstanding."""
        while not self._can_grant():
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

    def _alarm(self, msg: str) -> None:
        if not self.alarms or self.alarms[-1] != msg:
            self.alarms.append(msg)
            self.timeline.event(self.name, "alarm", msg=msg)
            log.error("%s: %s", self.name, msg)

    def _want_open(self, now: float) -> bool:
        lease = self._lease
        return self.open_allowed(now) and lease is not None and lease.live

    async def _enforce_once(self) -> None:
        now = time.monotonic()
        cfg = self.config
        if self._safe and (
            self._last_good_read is None or now - self._last_good_read > cfg.stale_after
        ):
            self._set_safety(False, f"no successful weather read for > {cfg.stale_after} s")
        lease = self._lease
        if lease is not None and lease.live and lease.expired(now):
            self._revoke_lease(f"lease not renewed within {lease.ttl} s")
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
            return
        if status == Shutter.CLOSING:
            if self._closing_since is None:
                self._closing_since = now
            if now - self._closing_since < cfg.shutter_timeout:
                return
            self._alarm(f"shutter still closing after {cfg.shutter_timeout} s; re-commanding")
            self._closing_since = now
        if cfg.park_before_close and not self._parked_for_close:
            await self._park_before_close()
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
        park, then park ourselves, then close regardless."""
        cfg = self.config
        assert self._tel is not None
        lease = self._lease
        if lease is not None and not lease.released.is_set():
            try:
                await asyncio.wait_for(lease.released.wait(), cfg.park_wait / 2)
            except TimeoutError:
                self._alarm("sequencer did not finish cleanup before close")
        try:
            if not await self._tel.get("AtPark"):
                await self._tel.call("Park", timeout=cfg.park_wait / 2)
                async with asyncio.timeout(cfg.park_wait / 2):
                    while not await self._tel.get("AtPark"):
                        await asyncio.sleep(0.2)
        except (DeviceError, TimeoutError) as exc:
            self._alarm(f"telescope not confirmed parked before close: {exc!r}")
        self._parked_for_close = True

    async def _ensure_open(self) -> None:
        cfg = self.config
        self._closing_since = None
        lease = self._lease
        assert lease is not None
        status = await self._shutter_status()
        now = time.monotonic()
        if status == Shutter.OPEN:
            self._opening_since = None
            if not lease.opened.is_set():
                lease.opened.set()
                self.timeline.event(self.name, "enclosure_open", lease=lease.id)
            return
        if status is None or status == Shutter.ERROR:
            self._alarm(f"not opening: shutter status {status!r}")
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
