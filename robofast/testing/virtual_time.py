"""Virtual time for in-process tests: a loop whose clock jumps to the next timer.

A test module opts in with ``pytestmark = pytest.mark.virtual_time``
(``tests/core/conftest.py`` wires it). Its event loop is a
:class:`VirtualTimeLoop`: whenever every task is waiting on a timer (an
``asyncio.sleep``, a timeout, a poll interval) and no I/O is ready, the loop's
clock jumps to the next timer instead of sleeping. ``time.monotonic`` follows
the same clock while the test runs, so robofast's own stamps (safety holds,
lease ages, staleness, the timeline) agree with ``asyncio.sleep`` and
``asyncio.timeout``. A supervisor's 20-minute reopen hold or a 300 s error
backoff then costs microseconds, and a test can run the shipped timings
instead of shortened ones.

Real I/O still happens in real time: while a connected socket is registered
(a loopback HTTP request, the supervisor's lease API; not the loop's own
self-pipe, nor a listening socket, which only accepts) the loop waits
``IO_GRACE`` real seconds for it before jumping, and it
never jumps while a worker thread of the default executor is busy. What it is
not for: tests whose devices run in other threads or processes on their own
clocks (simulators behind sockets, child processes); they stay in real time.

``time.monotonic`` itself is patched (the ``time`` module's attribute,
process-wide) for as long as a marked test runs, and restored after it. So
every thread sees the virtual clock, not only the loop's: code that a test
runs in an executor thread (``asyncio.to_thread``, ``run_in_executor``) stamps
virtual time too, and agrees with the loop. (Code that bound the function
early, ``from time import monotonic``, would keep the real clock; robofast
calls ``time.monotonic()``.)

Each read of ``time.monotonic`` advances the clock by 1 us, so stamps taken
one after another stay strictly ordered, as they are in real time (tests
compare them with ``<``).
"""

from __future__ import annotations

import asyncio
import selectors
import time
from typing import Any

__all__ = ["VirtualTimeLoop", "install_monotonic", "new_loop", "restore_monotonic"]

_real_monotonic = time.monotonic
_current: VirtualTimeLoop | None = None

#: Real seconds to wait for socket I/O before a jump.
IO_GRACE = 0.002
#: Real seconds to wait while an executor thread is busy.
THREAD_GRACE = 0.02
#: Virtual seconds added by each read of ``time.monotonic``.
TICK = 1e-6


class _Selector:
    """The loop's selector, jumping the clock when only timers are pending."""

    def __init__(self, real: selectors.BaseSelector, loop: VirtualTimeLoop) -> None:
        self._real = real
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = self._real.select(0)
        if ready:
            return ready
        if timeout is None:
            return self._real.select(None)  # no timer: truly waiting on I/O
        if timeout <= 0:
            return []
        busy = self._loop._executor_busy()
        if busy or self._connected():
            ready = self._real.select(min(timeout, THREAD_GRACE if busy else IO_GRACE))
            if ready or busy:
                return ready
        self._loop.virtual_time += timeout
        self._loop.jumps += 1
        return []

    def _connected(self) -> bool:
        """Is anything registered but the loop's self-pipe and listening
        servers? (A listening socket only accepts; the connection it accepts
        is registered on its own.) asyncio registers a server's socket by its
        fd, with ``_accept_connection`` as the reader."""
        own = self._loop._ssock.fileno()  # type: ignore[attr-defined]
        for key in self._real.get_map().values():
            if key.fd == own:
                continue
            reader, writer = key.data if isinstance(key.data, tuple) else (None, None)
            cb = getattr(reader, "_callback", None)
            if writer is None and getattr(cb, "__name__", "") == "_accept_connection":
                continue
            return True
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A selector event loop on a virtual clock (module doc)."""

    def __init__(self) -> None:
        #: The clock, seconds; starts at the real monotonic time.
        self.virtual_time = _real_monotonic()
        #: How many times the clock jumped.
        self.jumps = 0
        super().__init__(selectors.DefaultSelector())
        self._selector = _Selector(self._selector, self)  # type: ignore[assignment]

    def time(self) -> float:
        return self.virtual_time

    def _executor_busy(self) -> bool:
        ex = self._default_executor  # type: ignore[attr-defined]
        if ex is None:
            return False
        threads = len(getattr(ex, "_threads", ()))
        idle = getattr(ex, "_idle_semaphore", None)
        free = idle._value if idle is not None else threads
        queue = getattr(ex, "_work_queue", None)
        return (queue is not None and not queue.empty()) or free < threads


def new_loop() -> VirtualTimeLoop:
    """A new virtual-time loop, the one ``time.monotonic`` follows."""
    global _current
    _current = VirtualTimeLoop()
    return _current


def _monotonic() -> float:
    loop = _current
    if loop is None or loop.is_closed():
        return _real_monotonic()
    loop.virtual_time += TICK
    return loop.virtual_time


def install_monotonic() -> None:
    """Point ``time.monotonic`` at the current virtual loop (real time while
    there is none)."""
    time.monotonic = _monotonic


def restore_monotonic() -> None:
    """Give ``time.monotonic`` back and forget the loop."""
    global _current
    time.monotonic = _real_monotonic
    _current = None
