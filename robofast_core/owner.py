"""Single owner per device.

A :class:`DeviceOwner` is the only path to one physical device. It:

* serializes every command on the device's own single worker thread (so one
  hung device can never starve the thread pool used by another);
* bounds every command with a timeout, after which it abandons that worker
  (and its HTTP session) and builds a fresh one for the next command;
* checks, on the worker thread, *immediately before the command is sent*,
  that the :class:`Grant` it was issued under has not been revoked;
* refuses "privileged" members (e.g. ``CloseShutter``) to anyone but the one
  privileged grant (the safety supervisor);
* records every command and its outcome in the shared :class:`Timeline`.

Components never hold the device object itself, only a :class:`Grant`.
Revoking a grant is how a cancelled task is fenced off: any command it still
has queued or tries later is rejected before it reaches the wire.

Why a dedicated executor rather than ``asyncio.to_thread``: ``to_thread`` uses
the loop's shared default pool. A timed-out call keeps its thread until the
underlying HTTP timeout fires, so a few hung devices could exhaust the shared
pool and stall *unrelated* devices, including the safety supervisor's dome.
One single-thread executor per device keeps that failure local and also gives
in-order execution for free. It is the same mechanism (a coroutine awaiting a
blocking call on a worker thread) with a pool we control.
"""

from __future__ import annotations

import asyncio
import enum
import itertools
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)


class DeviceError(Exception):
    """The device (or the transport to it) reported an error."""


class DeviceTimeout(DeviceError):
    """A command did not complete within its timeout."""


class GrantRevoked(Exception):
    """The command's grant was revoked before it could be sent."""


class NotAuthorized(Exception):
    """The grant may not call this member (e.g. only safety may close)."""


@dataclass
class _Worker:
    executor: ThreadPoolExecutor
    generation: int
    driver: Any = None


class Grant:
    """Permission to send commands to one device, until revoked.

    Revocation is checked under a lock on the worker thread right before a
    command goes out, so any command whose send-decision comes after
    :meth:`revoke` returns is rejected. ``revoked_at`` is a
    ``time.monotonic()`` stamp taken under the same lock.
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


def _plain(value: Any) -> Any:
    """Turn alpyca's IntEnums (ShutterState, CameraStates, ...) into ints."""
    if isinstance(value, enum.Enum):
        return value.value
    return value


class DeviceOwner:
    """The single owner of one device.

    ``factory`` builds and connects the driver object (an alpyca device or a
    test fake). It runs on the worker thread, lazily, and again after a
    timeout has forced the old worker to be abandoned.
    """

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        timeline: Timeline,
        *,
        default_timeout: float = 5.0,
        privileged_members: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.name = name
        self.timeline = timeline
        self.default_timeout = default_timeout
        self.privileged_members = frozenset(privileged_members)
        self._factory = factory
        self._lock = asyncio.Lock()
        self._worker: _Worker | None = None
        self._generation = 0
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

    # -- execution --------------------------------------------------------

    def _ensure_worker(self) -> _Worker:
        if self._worker is None:
            self._generation += 1
            self._worker = _Worker(
                ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"dev-{self.name}-g{self._generation}",
                ),
                self._generation,
            )
        return self._worker

    def _abandon_worker(self, worker: _Worker, why: str) -> None:
        """Drop a worker whose thread is stuck; the next call gets a new one."""
        if self._worker is worker:
            self._worker = None
        worker.executor.shutdown(wait=False, cancel_futures=True)
        self.timeline.event(self.name, "worker_abandoned", generation=worker.generation, why=why)

    def _invoke(
        self, grant: Grant, op: str, member: str, args: tuple, worker: _Worker, timeout: float
    ) -> Any:
        # Runs on the device's worker thread.
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
            generation=worker.generation,
        )
        try:
            if worker.driver is None:
                worker.driver = self._factory()
            driver = worker.driver
            if hasattr(type(driver), "_robofast_http_timeout"):
                # transport timeout == this call's timeout (see robofast_core.alpaca)
                driver._robofast_http_timeout = timeout
            if op == "get":
                result = getattr(driver, member)
            elif op == "set":
                setattr(driver, member, args[0])
                result = None
            else:
                result = getattr(driver, member)(*args)
        except Exception as exc:
            self.timeline.command(
                grant.holder,
                self.name,
                member,
                "error",
                error=f"{type(exc).__name__}: {exc}",
                generation=worker.generation,
            )
            raise DeviceError(f"{self.name}.{member}: {type(exc).__name__}: {exc}") from exc
        result = _plain(result)
        self.timeline.command(
            grant.holder,
            self.name,
            member,
            "ok",
            dt=time.monotonic() - t_sent,
            generation=worker.generation,
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
        if member in self.privileged_members and not grant.privileged:
            self.timeline.command(grant.holder, self.name, member, "not_authorized")
            raise NotAuthorized(f"{grant.holder} may not call {self.name}.{member} (privileged)")
        timeout = self.default_timeout if timeout is None else timeout
        async with self._lock:
            worker = self._ensure_worker()
            loop = asyncio.get_running_loop()
            try:
                fut = loop.run_in_executor(
                    worker.executor, self._invoke, grant, op, member, args, worker, timeout
                )
            except RuntimeError as exc:  # executor already shut down
                self._abandon_worker(worker, f"executor unusable: {exc}")
                raise DeviceError(f"{self.name}: worker unusable") from exc
            try:
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                self.timeline.command(grant.holder, self.name, member, "timeout", timeout=timeout)
                self._abandon_worker(worker, f"{member} exceeded {timeout} s")
                raise DeviceTimeout(
                    f"{self.name}.{member} did not complete within {timeout} s"
                ) from None
            except asyncio.CancelledError:
                # The caller was cancelled. The command may still complete on
                # the worker; anything queued behind it runs after it, in order.
                self.timeline.command(grant.holder, self.name, member, "cancelled")
                raise

    def close(self) -> None:
        if self._worker is not None:
            self._worker.executor.shutdown(wait=False, cancel_futures=True)
            self._worker = None
