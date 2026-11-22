"""MemWatch (robofast/testing/mem_watch.py): night 2026-11-21, the rehearsal
process grew 0.4-1.1 GB/h and nothing in its logs said what grew."""

from __future__ import annotations

import asyncio

import pytest

from robofast.testing.mem_watch import MemWatch

_kept: list[bytes] = []


def _a_growing_cache_for_the_test() -> None:
    _kept.append(bytes(200_000))


async def test_the_report_names_the_line_that_grows(tmp_path):
    watch = MemWatch(tmp_path / "memwatch.txt", every=0.2, top=5)
    task = asyncio.create_task(watch.run())
    try:
        for _ in range(30):
            _a_growing_cache_for_the_test()
            await asyncio.sleep(0.02)
        while watch.reports < 2:
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _kept.clear()
    text = (tmp_path / "memwatch.txt").read_text()
    last = text.split("== ")[-1].splitlines()
    assert "RSS" in last[0] or "traced" in last[0]
    assert "test_mem_watch.py" in last[1]  # the top grower: the cache above
    assert float(last[1].split()[0]) >= 2.0  # MB (allocations after the baseline)


def test_every_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        MemWatch(tmp_path / "x", every=0)
