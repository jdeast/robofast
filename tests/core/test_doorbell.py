"""The drop box's doorbell: a dropped file is taken in at once, not on a poll.

The intake-latency test follows Castle639's PR #110.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

from robofast import doorbell
from robofast.dispatch import DispatchConfig
from robofast.doorbell import Doorbell, ring
from tests.core.test_dispatch import group, make, too


async def _until(pred, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        await asyncio.sleep(0.005)
    return None


def _drop(tmp_path, box, data, name="x.json"):
    tmp = tmp_path / name
    tmp.write_text(json.dumps(data))
    tmp.rename(box / name)


async def test_a_ring_wakes_the_bell_and_close_unpublishes(tmp_path):
    assert await ring(tmp_path) is False  # no scheduler yet: nothing to wake
    bell = await Doorbell.open(tmp_path)
    try:
        waiter = asyncio.create_task(bell.wait())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        assert await ring(tmp_path) is True
        await asyncio.wait_for(waiter, 1.0)
    finally:
        bell.close()
    assert not (tmp_path / doorbell.BELL_FILE).exists()


async def test_a_wrong_token_is_not_heard(tmp_path, caplog):
    bell = await Doorbell.open(tmp_path)
    try:
        path = tmp_path / doorbell.BELL_FILE
        good = json.loads(path.read_text())
        path.write_text(json.dumps({**good, "token": "0" * 32}))
        with caplog.at_level(logging.ERROR, logger="robofast.doorbell"):
            assert await ring(tmp_path) is False
        assert "not heard" in caplog.text
        path.write_text(json.dumps(good))
        assert await ring(tmp_path) is True
    finally:
        bell.close()


async def test_a_dead_doorbell_is_logged(tmp_path, caplog):
    bell = await Doorbell.open(tmp_path)
    saved = (tmp_path / doorbell.BELL_FILE).read_text()
    bell.close()
    (tmp_path / doorbell.BELL_FILE).write_text(saved)  # left behind by a crash
    with caplog.at_level(logging.ERROR, logger="robofast.doorbell"):
        assert await ring(tmp_path) is False
    assert "ringing 127.0.0.1" in caplog.text


async def test_doorbell_listen_on_a_wildcard_publishes_the_host_name(tmp_path):
    bell = await Doorbell.open(tmp_path, "0.0.0.0:0")
    try:
        published = json.loads((tmp_path / doorbell.BELL_FILE).read_text())
        assert published["host"] == socket.gethostname() and published["port"] == bell.port
    finally:
        bell.close()


async def test_a_dropped_too_is_taken_in_at_once(tmp_path):
    box = tmp_path / "too"
    box.mkdir()
    # too_poll an hour: only the doorbell can take it in.
    d = make([group("a")], too_dropbox=box, config=DispatchConfig(too_poll=3600.0))
    task = asyncio.create_task(d.serve())
    try:
        assert await _until(lambda: (box / doorbell.BELL_FILE).exists(), 2.0) is not None
        _drop(tmp_path, box, group("burst", too=too("next")))
        assert await ring(box)
        took = await _until(lambda: "burst-r" in d.requests, 2.0)
        assert took is not None and took < 0.5, took
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not (box / doorbell.BELL_FILE).exists()


async def test_files_dropped_before_the_start_are_taken_in(tmp_path):
    box = tmp_path / "too"
    box.mkdir()
    _drop(tmp_path, box, group("early"))
    d = make([group("a")], too_dropbox=box, config=DispatchConfig(too_poll=3600.0))
    task = asyncio.create_task(d.serve())
    try:
        assert await _until(lambda: "early-r" in d.requests, 2.0) is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
