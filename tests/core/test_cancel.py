"""Lost cancellations (robofast_core.cancel): a ``task.cancel()`` landing
inside an httpx request can be lost, the request returning normally. The
fakes here lose the first cancel they see, the way httpx does."""

from __future__ import annotations

import asyncio

import pytest

from robofast_core.cancel import isolated, stop_task
from robofast_core.lease_client import RemoteLease, RemoteSupervisor


def cancel_loser(delay: float = 0.1):
    """An async "request" that loses the first cancel it sees."""
    lost: list[int] = []

    async def request(*_a, **_k):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if lost:
                raise
            lost.append(1)
        return "answer"

    return request, lost


async def test_isolated_delivers_the_callers_cancel():
    request, lost = cancel_loser()

    async def loop():
        while True:
            await isolated(request())

    task = asyncio.create_task(loop())
    await asyncio.sleep(0.05)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1)
    assert done and task.cancelled(), "the caller's cancel was lost with the request's"
    await asyncio.sleep(0.01)
    assert lost, "the request itself was not cancelled"


async def test_isolated_passes_results_and_errors_through():
    async def ok():
        return 42

    async def bad():
        raise ValueError("no")

    assert await isolated(ok()) == 42
    with pytest.raises(ValueError):
        await isolated(bad())


async def test_stop_task_is_bounded_and_passes_the_stoppers_cancel_on():
    request, _ = cancel_loser()
    stop = False

    async def loop():
        while not stop:
            await request()

    task = asyncio.create_task(loop())
    await asyncio.sleep(0.05)
    # The cancel is lost and nothing tells the loop to stop: bounded, False.
    assert await asyncio.wait_for(stop_task(task, 0.2), 1) is False
    stop = True
    await asyncio.wait_for(asyncio.wait({task}), 1)

    stop = False

    async def deaf():
        while not stop:
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(deaf())
    await asyncio.sleep(0)
    stopper = asyncio.create_task(stop_task(task, 5))
    await asyncio.sleep(0.05)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopper, 1)
    stop = True
    await asyncio.wait_for(asyncio.wait({task}), 1)


async def test_remote_lease_workers_end_when_their_cancels_are_lost():
    """A released lease's follow and renew workers end, and aclose() returns
    (bounded), even when their cancels are lost inside a request."""
    client = RemoteSupervisor("http://127.0.0.1:9", timeout=0.3, poll=0.1)
    state = {"id": 1, "holder": "t", "ttl": 5.0, "token": "x", "live": True, "opened": True}
    get, lost_get = cancel_loser()
    post, lost_post = cancel_loser()

    async def call(method, path, wait=0.0, **kw):
        await (get if method == "GET" else post)()
        return dict(state)

    client._call = call  # type: ignore[method-assign]
    lease = RemoteLease(client, state)
    lease.renew()
    await asyncio.sleep(0.05)  # both workers are inside a "request"
    workers = list(lease._workers)
    lease.release()
    done, _ = await asyncio.wait({asyncio.create_task(client.aclose())}, timeout=2)
    assert done, "aclose() hangs on lease workers whose cancels were lost"
    assert lost_get and lost_post
    assert all(w.done() for w in workers), "a lease worker outlived aclose()"
