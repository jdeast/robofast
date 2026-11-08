"""Single owner per device.

A :class:`DeviceOwner` is the only path to one physical device. It:

* serializes every command to the device (one at a time, in order), while
  commands to *different* devices proceed independently;
* bounds every command with a timeout, after which it discards the driver
  (and its HTTP connections) and builds a fresh one for the next command;
* checks, *immediately before the command is sent*, that the :class:`Grant`
  it was issued under has not been revoked;
* refuses "privileged" members (e.g. ``CloseShutter``) to anyone but the one
  privileged grant (the safety supervisor); an entry ``"Action:Name"``
  makes the one Alpaca ``Action("Name", ...)`` privileged;
* records every command and its outcome in the shared :class:`Timeline`;
* runs its ``on_connect`` hook, if it has one, every time it connects the
  device afresh (the first command, after a timeout discarded the driver,
  after :meth:`DeviceOwner.reconnect`): what a device must have set again
  whenever it is (re)connected -- a camera's cooler (robofast/cooling.py)
  -- is set there, and only there.

Components never hold the device object itself, only a :class:`Grant`.
Revoking a grant is how a cancelled task is fenced off: any command it still
has queued or tries later is rejected before it reaches the wire.

The driver is asynchronous: an object with ``async get(member, *, timeout)``,
``async set(member, value, *, timeout)`` and ``async call(member, *args,
timeout)``, and optionally ``async connect(*, timeout)`` and ``async
aclose()``. :class:`robofast.alpaca_client.AlpacaDevice` is the real
one; :mod:`robofast.testing.fakes` provides stand-ins. No threads.

A command that has been sent is not abandoned half-way because its *caller*
was cancelled: it runs on (bounded by its own timeout), and the next command
to the device waits for it. Otherwise a cleanup's ``AbortExposure`` could
overtake the ``StartExposure`` it is meant to abort.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from robofast.timeline import Timeline

log = logging.getLogger(__name__)


class DeviceError(Exception):
    """The device (or the transport to it) reported an error."""


class DeviceTimeout(DeviceError):
    """A command did not complete within its timeout."""


class Unsupported(DeviceError):
    """The device (or the telescope) cannot do what was asked: an
    observation's filter or M3 port, a camera that cannot set its CCD
    temperature. The caller handles it (one path per feature: a device that
    cannot do it says so, nothing works around it)."""


class GrantRevoked(Exception):
    """The command's grant was revoked before it could be sent."""


class NotAuthorized(Exception):
    """The grant may not call this member (e.g. only safety may close)."""


class Grant:
    """Permission to send commands to one device, until revoked.

    Revocation is checked (under a lock, so :meth:`revoke` may be called from
    any thread) right before a command goes out, so any command whose
    send-decision comes after :meth:`revoke` returns is rejected.
    ``revoked_at`` is a ``time.monotonic()`` stamp taken under the same lock.
    """

    _ids = itertools.count(1)

    def __init__(self, owner: DeviceOwner, holder: str, privileged: bool) -> None:
        self.owner = owner
        self.holder = holder
        self.privileged = privileged
        self.id = next(self._ids)
        self._lock = threading.Lock()
        self.revoked_at: float | None = None
        self.revoke_reason: str | None = None

    def revoke(self, reason: str) -> None:
        with self._lock:
            if self.revoked_at is None:
                self.revoked_at = time.monotonic()
                self.revoke_reason = reason
        self.owner.timeline.event(
            self.holder, "grant_revoked", device=self.owner.name, reason=reason
        )

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def _stamp_if_live(self) -> float:
        with self._lock:
            if self.revoked_at is not None:
                raise GrantRevoked(
                    f"{self.holder}: grant on {self.owner.name} revoked ({self.revoke_reason})"
                )
            return time.monotonic()

    async def get(self, member: str, *, timeout: float | None = None) -> Any:
        return await self.owner._execute(self, "get", member, (), timeout)

    async def set(self, member: str, value: Any, *, timeout: float | None = None):
        return await self.owner._execute(self, "set", member, (value,), timeout)

    async def call(self, member: str, *args: Any, timeout: float | None = None):
        return await self.owner._execute(self, "call", member, args, timeout)

    def __repr__(self) -> str:
        state = f"revoked:{self.revoke_reason}" if self.revoked else "live"
        return f"<Grant {self.holder} on {self.owner.name} {state}>"


# Background driver shutdowns, referenced until they finish.
_closing: set[asyncio.Task] = set()


def _close_later(driver: Any) -> None:
    aclose = getattr(driver, "aclose", None)
    if aclose is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(aclose())
    except RuntimeError:  # no running loop: nothing left to close politely
        return
    _closing.add(task)
    task.add_done_callback(_closing.discard)


def _swallow(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # already recorded in the timeline


class DeviceOwner:
    """The single owner of one device.

    ``factory`` returns the driver (an
    :class:`~robofast.alpaca_client.AlpacaDevice` or a test fake). It is
    called lazily, and again after a timeout has discarded the old driver; if
    the driver has ``connect``, that runs (inside the command's timeout)
    before its first command.
    """

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        timeline: Timeline,
        *,
        default_timeout: float = 5.0,
        privileged_members: frozenset[str] | set[str] = frozenset(),
        on_connect: Callable[[Any, float], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        #: ``await on_connect(driver, timeout)`` after every fresh connection
        #: (module doc), before the command that made it: bounded by
        #: ``timeout`` on its own (it does not eat into the command's), and
        #: its failure is logged and recorded, never the command's.
        self.on_connect = on_connect
        self.timeline = timeline
        self.default_timeout = default_timeout
        self.privileged_members = frozenset(privileged_members)
        # "Action:Name" entries make one Alpaca Action privileged (names are
        # case-insensitive, as Alpaca action names are).
        self._privileged_actions = frozenset(
            m.split(":", 1)[1].lower() for m in self.privileged_members if m.startswith("Action:")
        )
        self._factory = factory
        self._lock = asyncio.Lock()
        self._driver: Any = None
        self._fresh = False
        self._generation = 0
        self._inflight: asyncio.Task | None = None
        self._privileged_grant: Grant | None = None

    # -- grants -----------------------------------------------------------

    def grant(self, holder: str, *, privileged: bool = False) -> Grant:
        if privileged:
            if self._privileged_grant is not None:
                raise NotAuthorized(
                    f"{self.name}: privileged grant already held by {self._privileged_grant.holder}"
                )
            g = Grant(self, holder, True)
            self._privileged_grant = g
            return g
        return Grant(self, holder, False)

    def _is_privileged(self, member: str, args: tuple) -> bool:
        if member in self.privileged_members:
            return True
        return (
            member == "Action" and bool(args) and str(args[0]).lower() in self._privileged_actions
        )

    # -- execution --------------------------------------------------------

    @property
    def connected(self) -> bool:
        """It holds a connected driver (the next command does not connect)."""
        return self._driver is not None

    def reconnect(self, why: str) -> None:
        """Drop the device's client; the next command connects afresh (the
        recovery ladder's first rung, robofast/recovery.py)."""
        self._discard_driver(why)

    def _discard_driver(self, why: str) -> None:
        """Drop a driver that timed out; the next command builds a new one."""
        driver, self._driver = self._driver, None
        if driver is not None:
            _close_later(driver)
            self.timeline.event(self.name, "driver_discarded", generation=self._generation, why=why)

    async def _get_driver(self, timeout: float) -> Any:
        if self._driver is None:
            driver = self._factory()
            self._generation += 1
            if hasattr(driver, "connect"):
                try:
                    await driver.connect(timeout=timeout)
                except BaseException:
                    _close_later(driver)
                    raise
            self._driver = driver
            self._fresh = True
        return self._driver

    async def _connected(self, driver: Any, timeout: float) -> None:
        """Run ``on_connect`` on a freshly connected driver (bounded; never raises)."""
        hook = self.on_connect
        if hook is None:
            return
        try:
            async with asyncio.timeout(timeout):
                await hook(driver, timeout)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            log.warning("%s: on-connect hook failed: %s", self.name, why)
            self.timeline.event(
                self.name, "on_connect_failed", generation=self._generation, error=why[:300]
            )

    async def _send(self, grant: Grant, op: str, member: str, args: tuple, timeout: float) -> Any:
        """Send one command and record its outcome (runs as its own task)."""
        t_sent = time.monotonic()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            async with asyncio.timeout_at(deadline):
                driver = await self._get_driver(timeout)
            if self._fresh:
                self._fresh = False
                t0 = loop.time()
                await self._connected(driver, timeout)
                deadline += loop.time() - t0  # the hook's time is not the command's
            async with asyncio.timeout_at(deadline):
                if op == "get":
                    result = await driver.get(member, timeout=timeout)
                elif op == "set":
                    result = await driver.set(member, args[0], timeout=timeout)
                else:
                    result = await driver.call(member, *args, timeout=timeout)
        except (TimeoutError, DeviceTimeout):
            self.timeline.command(grant.holder, self.name, member, "timeout", timeout=timeout)
            self._discard_driver(f"{member} exceeded {timeout} s")
            raise DeviceTimeout(
                f"{self.name}.{member} did not complete within {timeout} s"
            ) from None
        except Exception as exc:
            self.timeline.command(
                grant.holder,
                self.name,
                member,
                "error",
                error=f"{type(exc).__name__}: {exc}",
                generation=self._generation,
            )
            if isinstance(exc, DeviceError):
                if exc.args and isinstance(exc.args[0], str) and self.name not in exc.args[0]:
                    # Name the device (the recovery ladder finds the failed
                    # device by it: RecoveryLadder.device_for).
                    exc.args = (f"{self.name}.{member}: {exc.args[0]}", *exc.args[1:])
                raise
            raise DeviceError(f"{self.name}.{member}: {type(exc).__name__}: {exc}") from exc
        self.timeline.command(
            grant.holder,
            self.name,
            member,
            "ok",
            dt=time.monotonic() - t_sent,
            generation=self._generation,
        )
        return result

    async def _execute(
        self,
        grant: Grant,
        op: str,
        member: str,
        args: tuple,
        timeout: float | None,
    ) -> Any:
        if grant.owner is not self:
            raise NotAuthorized(f"{grant!r} is not a grant on {self.name}")
        if not grant.privileged and self._is_privileged(member, args):
            self.timeline.command(grant.holder, self.name, member, "not_authorized")
            raise NotAuthorized(f"{grant.holder} may not call {self.name}.{member} (privileged)")
        timeout = self.default_timeout if timeout is None else timeout
        async with self._lock:
            prior = self._inflight
            if prior is not None and not prior.done():
                # A cancelled caller's command is still running. It is bounded
                # by its own timeout; waiting for it counts against ours.
                t0 = time.monotonic()
                await asyncio.wait([prior], timeout=timeout)
                if not prior.done():
                    self.timeline.command(grant.holder, self.name, member, "timeout")
                    raise DeviceTimeout(
                        f"{self.name}.{member} did not complete within {timeout} s"
                        " (previous command still running)"
                    )
                timeout -= time.monotonic() - t0
            try:
                t_sent = grant._stamp_if_live()
            except GrantRevoked:
                self.timeline.command(grant.holder, self.name, member, "rejected")
                raise
            self.timeline.command(
                grant.holder,
                self.name,
                member,
                "sent",
                t=t_sent,
                op=op,
                args=args,
                generation=self._generation,
            )
            task = asyncio.ensure_future(self._send(grant, op, member, args, timeout))
            task.add_done_callback(_swallow)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The command runs on; the next one waits for it (module doc).
                if not task.done():
                    self._inflight = task
                self.timeline.command(grant.holder, self.name, member, "cancelled")
                raise

    def close(self) -> None:
        """Drop the driver (its connections close in the background)."""
        driver, self._driver = self._driver, None
        if driver is not None:
            _close_later(driver)
