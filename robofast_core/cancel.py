"""Cancellation that is never lost, and stops that never hang.

With httpx 0.28 / anyio 4.x, a ``task.cancel()`` that lands while the task
is inside an httpx request can be lost: the request returns normally and
the task never sees ``CancelledError`` (reproduced standalone: 1 to 6 in
100 cancels of a polling loop under load, none with :func:`isolated`;
PR #49 found it as a hung ``GuiderRemote.stop()``). A ``wait_for``/``asyncio.timeout`` around such a
request is a cancel too, and is lost the same way. Two rules follow:

* **Every httpx request runs through** :func:`isolated`. The request runs in
  a task of its own and the caller only waits for that task, so the caller's
  cancellation is delivered by asyncio itself, never through anyio. The
  request's own task is cancelled too; if that cancel is lost, it finishes
  its request (bounded by its httpx timeout) and ends.
* **A loop stopped by** ``task.cancel()`` **also checks a stop flag** (or "am
  I still the current task") on every pass, and whoever stops it uses
  :func:`stop_task`: cancel, wait with a bound, and pass the stopper's own
  cancellation on. Never ``suppress(BaseException)`` around an unbounded
  ``await task``: it hangs on a task that outlives its cancel, and it
  swallows the stopper's own cancellation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger(__name__)


def _retrieve(task: asyncio.Future[Any]) -> None:
    """Mark a finished task's exception as retrieved (no "never retrieved")."""
    if not task.cancelled():
        task.exception()


async def isolated[T](aw: Awaitable[T]) -> T:
    """Await ``aw`` (an httpx request) in its own task (module doc).

    The result or exception is the request's own; a cancellation of the
    caller always reaches the caller, and cancels the request too.
    """
    task = asyncio.ensure_future(aw)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_retrieve)
        raise


async def stop_task(task: asyncio.Task[Any] | None, timeout: float) -> bool:
    """Cancel ``task`` and wait for it, at most ``timeout`` seconds.

    True if it ended (however: an exception it ends with after the cancel
    is logged, not raised; one it had already ended with is not). False
    if it outlived its cancel for ``timeout`` (logged; it is left running,
    and should end by itself on its stop flag). The caller's own
    cancellation while waiting is passed on, never swallowed.
    """
    return await stop_tasks([] if task is None else [task], timeout)


async def stop_tasks(tasks: list[asyncio.Task[Any]], timeout: float) -> bool:
    """:func:`stop_task` for several at once, one bound for all; True if all
    ended."""
    for t in tasks:
        if t.done():
            _retrieve(t)  # already over: nothing to stop or report
    tasks = [t for t in tasks if not t.done() and t is not asyncio.current_task()]
    if not tasks:
        return True
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        for t in tasks:
            t.add_done_callback(_retrieve)
        raise
    ok = True
    for t in tasks:
        if not t.done():
            log.error("%s outlived its cancel for %g s; left to end by itself", t, timeout)
            t.add_done_callback(_retrieve)
            ok = False
        elif not t.cancelled() and (exc := t.exception()) is not None:
            log.warning("%s ended with %r", t.get_name(), exc)
    return ok
