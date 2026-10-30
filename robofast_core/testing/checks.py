"""Assertions over a :class:`Timeline`, shared by the unit and simulator tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from robofast_core.timeline import Entry, Timeline


async def wait_until(
    predicate: Callable[[], bool], timeout: float, every: float = 0.02, what: str = ""
) -> float:
    """Poll ``predicate`` until true; return the time it took."""
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out after {timeout} s waiting for {what or predicate}")
        await asyncio.sleep(every)
    return time.monotonic() - t0


async def await_until(
    probe: Callable[[], object], timeout: float, every: float = 0.1, what: str = ""
) -> float:
    """Like :func:`wait_until`, for a blocking probe (run in a thread)."""
    t0 = time.monotonic()
    while not await asyncio.to_thread(probe):
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out after {timeout} s waiting for {what or probe}")
        await asyncio.sleep(every)
    return time.monotonic() - t0


def _event(timeline: Timeline, what: str, source: str, **match) -> Entry:
    for e in timeline.events(what, source):
        if all(e.detail.get(k) == v for k, v in match.items()):
            return e
    raise AssertionError(f"no {what} event from {source} matching {match}\n{timeline.dump()}")


def assert_fenced_after_cancel(
    timeline: Timeline,
    sequencer: str,
    gen: int,
    devices: tuple[str, ...],
    allowed_after: tuple[str, ...] = (),
) -> float:
    """The legacy failure mode, as an assertion.

    After the sequencer asked generation ``gen`` to stop:

    1. nothing the observing task tried was *sent* after its grants were
       revoked (attempts are recorded as ``rejected``/``cancelled``);
    2. until the next generation starts, the only commands that reached
       ``devices`` came from this generation's cleanup (or a source listed
       in ``allowed_after``, e.g. the safety supervisor).

    Returns the cancel time.
    """
    cancel = _event(timeline, "cancel_requested", sequencer, gen=gen)
    observe = f"{sequencer}/observe#{gen}"
    cleanup = f"{sequencer}/cleanup#{gen}"
    revoked = [e.t for e in timeline.events("grant_revoked", observe)]
    assert revoked, f"{observe} grants were never revoked"
    fence = max(revoked)

    late = [e for e in timeline.commands(source_prefix=observe, outcome="sent") if e.t > fence]
    assert not late, f"{observe} sent commands after it was fenced: {late}"

    next_start = [e.t for e in timeline.events("observe_start", sequencer) if e.detail["gen"] > gen]
    horizon = min(next_start) if next_start else float("inf")
    stray = [
        e
        for e in timeline.commands(outcome="sent")
        if e.device in devices
        and cancel.t < e.t < horizon
        and e.source != cleanup
        and e.source not in allowed_after
        and not (e.source == observe and e.t <= fence)
    ]
    assert not stray, "commands from other components after cancellation:\n" + "\n".join(
        map(str, stray)
    )
    return cancel.t


def assert_generations_disjoint(timeline: Timeline, sequencer: str) -> None:
    """Generation n+1 starts only after generation n finished and cleaned up,
    and no grant of generation n sends anything after n+1 starts."""
    starts = {e.detail["gen"]: e.t for e in timeline.events("observe_start", sequencer)}
    finished = {e.detail["gen"]: e.t for e in timeline.events("observe_finished", sequencer)}
    cleaned = {e.detail["gen"]: e.t for e in timeline.events("cleanup_done", sequencer)}
    for gen, t_start in starts.items():
        nxt = starts.get(gen + 1)
        if nxt is None:
            continue
        assert gen in finished and finished[gen] < nxt, f"gen {gen} not finished before {gen + 1}"
        assert gen in cleaned and cleaned[gen] < nxt, f"gen {gen} not cleaned before {gen + 1}"
        for prefix in (f"{sequencer}/observe#{gen}", f"{sequencer}/cleanup#{gen}"):
            late = timeline.commands(source_prefix=prefix, outcome="sent", since=nxt)
            assert not late, f"{prefix} sent after generation {gen + 1} started: {late}"
        assert t_start < nxt
