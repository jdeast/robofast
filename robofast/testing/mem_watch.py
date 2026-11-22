"""Find what grows in a long-running process: periodic tracemalloc reports.

Night 2026-11-21 (night1121): the rehearsal process grew from ~0.3 GB to over
2 GB, about 0.4-1.1 GB/h, almost all of it in the main malloc heap; over a
13 h night that approaches the 16 GB of the gateway. The usual suspects
(the dispatch scheduler's astropy transforms, the simulated KiwiSpec PC's
frames, the imager frames, the mount guard's records) were ruled out, and an
all-simulated night does not grow, so the grower is in what only the
real-time rehearsal does over hours.

:class:`MemWatch` starts :mod:`tracemalloc` (one frame per allocation: low
overhead) and every ``every`` seconds appends to ``path`` the process's RSS
and the ``top`` source lines whose allocations grew most since the first
report. Opt-in (``rehearsal --memwatch SECONDS``): tracing costs memory and
time.
"""

from __future__ import annotations

import asyncio
import time
import tracemalloc
from pathlib import Path

__all__ = ["MemWatch"]


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


class MemWatch:
    def __init__(self, path: str | Path, every: float = 600.0, top: int = 15) -> None:
        if every <= 0:
            raise ValueError("every must be > 0")
        self.path = Path(path)
        self.every = every
        self.top = top
        self.reports = 0

    def report(self, base: tracemalloc.Snapshot) -> None:
        snap = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(False, tracemalloc.__file__)]
        )
        stats = snap.compare_to(base, "lineno")[: self.top]
        cur, peak = tracemalloc.get_traced_memory()
        rss = _rss_mb()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = [
            f"== {stamp} RSS {rss:.0f} MB; traced {cur / 1e6:.0f} MB (peak {peak / 1e6:.0f})"
            if rss is not None
            else f"== {stamp} traced {cur / 1e6:.0f} MB (peak {peak / 1e6:.0f})"
        ]
        for st in stats:
            frame = st.traceback[0]
            lines.append(
                f"{st.size_diff / 1e6:+9.2f} MB {st.count_diff:+9d} blocks"
                f"  {frame.filename}:{frame.lineno}"
            )
        with open(self.path, "a") as f:
            f.write("\n".join(lines) + "\n")
        self.reports += 1

    async def run(self) -> None:
        started = not tracemalloc.is_tracing()
        if started:
            tracemalloc.start(1)
        try:
            base = tracemalloc.take_snapshot()
            while True:
                await asyncio.sleep(self.every)
                await asyncio.to_thread(self.report, base)
        finally:
            if started:
                tracemalloc.stop()
