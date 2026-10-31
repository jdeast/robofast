"""A sequencer's view of a supervisor in another process.

:class:`RemoteSupervisor` has the one method the sequencer calls on a
:class:`~robofast_core.safety.SafetySupervisor`, ``request_open(holder)``,
and returns a :class:`RemoteLease`, which *is* an
:class:`~robofast_core.safety.OpenLease` (same ``wait_open``, ``renew``,
``release``, ``revoked`` event and ``revoke_reason``). So the sequencer does
not know or care which one it has. The API is described in
:mod:`robofast_core.supervisor_service`.

The rule: **anything that is not a clear "your lease is live" is a
revocation.** An HTTP error, an unknown lease (the supervisor restarted), a
timeout (the supervisor is hung) or a refused connection (it is gone) all
revoke the lease locally, so the sequencer stops and cleans up exactly as it
does for bad weather. Meanwhile the enclosure's hardware heartbeat closes it.

Each lease runs two background tasks: a long-poll that follows the lease's
state (so "opened" and "revoked" arrive within a round trip), and a renewer
that sends a renewal each time the sequencer calls :meth:`RemoteLease.renew`
-- and only then, so a sequencer that stops renewing still lets the lease
expire. ``request_open`` itself retries until it gets a lease: a sequencer
with no supervisor to talk to simply waits, closed.

For startup after a crash, :meth:`RemoteSupervisor.lease_status` and
:meth:`RemoteSupervisor.recover` are the remote forms of the supervisor's own
(see :mod:`robofast_core.sequencer`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from robofast_core.safety import OpenLease, RecoveryRefused

log = logging.getLogger(__name__)


class SupervisorAPIError(Exception):
    """The supervisor's answer was anything but a plain success."""


class RemoteSupervisor:
    """Leases from the supervisor service at ``url``.

    ``timeout`` bounds every request beyond its own long-poll ``poll`` time,
    so a hung supervisor is noticed within ``poll + timeout`` seconds;
    ``retry`` is the pause between attempts to obtain a lease.
    """

    def __init__(
        self, url: str, *, timeout: float = 5.0, poll: float = 5.0, retry: float = 2.0
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.poll = poll
        self.retry = retry
        self._http = httpx.AsyncClient(base_url=url, trust_env=False)
        self._tasks: set[asyncio.Task] = set()
        self._tokens: dict[int, str] = {}

    async def _call(self, method: str, path: str, wait: float = 0.0, **kw: Any) -> Any:
        r = await self._http.request(method, path, timeout=wait + self.timeout, **kw)
        if r.status_code != 200:
            raise SupervisorAPIError(f"{method} {path}: HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def request_open(self, holder: str) -> RemoteLease:
        """Wait until the supervisor grants a lease (retrying through errors)."""
        request = uuid.uuid4().hex
        body = {"holder": holder, "wait": self.poll, "request": request}
        while True:
            try:
                answer = await self._call("POST", "/leases", self.poll, json=body)
            except (httpx.HTTPError, SupervisorAPIError, ValueError) as exc:
                log.warning("%s: no lease from %s: %r", holder, self.url, exc)
                await asyncio.sleep(self.retry)
                continue
            if answer.get("granted"):
                return RemoteLease(self, answer["lease"])

    async def status(self) -> dict[str, Any]:
        return await self._call("GET", "/status")

    async def lease_status(self) -> dict[str, Any] | None:
        """The outstanding lease's state, or None (retrying through errors)."""
        while True:
            try:
                lease = (await self.status())["lease"]
            except (httpx.HTTPError, SupervisorAPIError, ValueError, KeyError) as exc:
                log.warning("no status from %s: %r", self.url, exc)
                await asyncio.sleep(self.retry)
                continue
            if lease is None or lease["released"]:
                return None
            self._tokens[lease["id"]] = lease["token"]
            return lease

    async def recover(self, lease_id: int, holder: str) -> bool:
        """Release a dead lease of ``holder`` (one :meth:`lease_status` saw).

        True if released now, False if there was nothing to release (already
        released, or the supervisor restarted since); anything else raises
        :class:`RecoveryRefused`, meaning "not now".
        """
        path = f"/leases/{self._tokens[lease_id]}/recover"
        try:
            r = await self._http.post(path, json={"holder": holder}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise RecoveryRefused(f"supervisor unreachable: {exc!r}") from exc
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise RecoveryRefused(f"HTTP {r.status_code}: {r.text[:200]}")
        return bool(r.json()["released_now"])

    async def aclose(self) -> None:
        """Cancel background work (pending releases get a bounded chance)."""
        pending = list(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=self.timeout)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._http.aclose()


class RemoteLease(OpenLease):
    def __init__(self, client: RemoteSupervisor, state: dict[str, Any]) -> None:
        super().__init__(state["id"], state["holder"], state["ttl"])
        self.token: str = state["token"]
        self._client = client
        self._renew_wanted = asyncio.Event()
        self._apply(state)
        self._workers = [
            client._spawn(self._follow(), f"lease-{self.token}-follow"),
            client._spawn(self._renewer(), f"lease-{self.token}-renew"),
        ]

    def _apply(self, state: dict[str, Any]) -> None:
        if state.get("opened"):
            self.opened.set()
        if not state.get("live"):
            self._revoke(state.get("reason") or "lease no longer live at the supervisor")

    def _lost(self, exc: BaseException) -> None:
        if self._revoke(f"supervisor unreachable or refused: {exc!r}"):
            log.error("lease %s: %s", self.token, self.revoke_reason)

    def _stop_workers(self) -> None:
        for t in self._workers:
            if t is not asyncio.current_task():
                t.cancel()

    async def _follow(self) -> None:
        path = f"/leases/{self.token}"
        try:
            while self.live:
                wait = self._client.poll
                self._apply(await self._client._call("GET", path, wait, params={"wait": wait}))
        except (httpx.HTTPError, SupervisorAPIError, ValueError) as exc:
            if not self.released.is_set():
                self._lost(exc)
        finally:
            if self.revoked.is_set():
                self._stop_workers()

    async def _renewer(self) -> None:
        path = f"/leases/{self.token}/renew"
        while True:
            await self._renew_wanted.wait()
            self._renew_wanted.clear()
            try:
                self._apply(await self._client._call("POST", path))
            except (httpx.HTTPError, SupervisorAPIError, ValueError) as exc:
                self._lost(exc)
                return

    def renew(self) -> None:
        """Raise :class:`LeaseRevoked` if revoked, else ask for a renewal."""
        super().renew()
        self._renew_wanted.set()

    def quarantine(self) -> None:
        super().quarantine()
        self._client._spawn(self._send("quarantine"), f"lease-{self.token}-quarantine")

    def release(self) -> None:
        if self.released.is_set():
            return
        super().release()
        self._stop_workers()
        self._client._spawn(self._send("release"), f"lease-{self.token}-release")

    async def _send(self, action: str, attempts: int = 3) -> None:
        for attempt in range(attempts):
            try:
                await self._client._call("POST", f"/leases/{self.token}/{action}")
                return
            except (httpx.HTTPError, SupervisorAPIError, ValueError) as exc:
                # Harmless if the supervisor is gone or restarted (the lease
                # died with it); otherwise an undelivered release fails closed
                # (no new lease until it arrives), and an undelivered
                # quarantine still leaves the lease blocking new ones.
                log.warning("lease %s: %s not delivered: %r", self.token, action, exc)
                if isinstance(exc, SupervisorAPIError) and "HTTP 404" in str(exc):
                    return
            if attempt + 1 < attempts:
                await asyncio.sleep(self._client.retry)
