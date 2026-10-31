"""KiwiSpec service: sharing the one spectrograph (the claim queue), the
night label and clock, and the iodine stage's positions read from the live
server (docs/kiwispec.md). Against the spectrograph-PC simulator, on
loopback."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from robofast_core.clock import ScaledClock
from robofast_core.devices.kiwispec_client import KiwiSpecBusy
from robofast_core.devices.kiwispec_service import KiwiSpecRemote, KiwiSpecServer

from .test_kiwispec import http, run_job, service, service_config, sim  # noqa: F401


async def _status(c):
    return (await c.get("/status")).json()


async def test_a_second_client_waits_in_the_queue_then_gets_it(http, sim):  # noqa: F811
    r = await http.post("/expose", json={"kind": "dark", "exptime": 0.8, "client": "aqawan1"})
    assert r.status_code == 202
    first = r.json()["id"]
    t0 = time.monotonic()
    waiting = asyncio.create_task(
        http.post("/expose", json={"kind": "bias", "wait": 10, "client": "aqawan2"})
    )
    await asyncio.sleep(0.2)
    st = await _status(http)
    assert [q["client"] for q in st["queue"]] == ["aqawan2"]
    # Nobody jumps the queue: a request without a wait is refused at once.
    r = await http.post("/expose", json={"kind": "bias"})
    assert r.status_code == 409 and r.json()["busy"] is True
    r = await waiting
    assert r.status_code == 202, r.text
    assert r.json()["client"] == "aqawan2"
    assert time.monotonic() - t0 > 0.5  # it waited for the first job
    done = (await http.get(f"/jobs/{first}")).json()
    assert done["state"] == "done"
    job = (await http.get(f"/jobs/{r.json()['id']}", params={"wait": 10})).json()
    assert job["state"] == "done"
    assert (await _status(http))["queue"] == []


async def test_the_wait_is_bounded_and_answers_busy(sim):  # noqa: F811
    server = await KiwiSpecServer(service_config(sim, max_queue=1)).start(status_loop=False)
    try:
        a = KiwiSpecRemote(server.url, client="aqawan1")
        b = KiwiSpecRemote(server.url, client="aqawan2", queue_wait=0.3)
        await a.expose(kind="dark", exptime=1.5)
        t0 = time.monotonic()
        with pytest.raises(KiwiSpecBusy):
            await b.expose(kind="bias")
        assert 0.25 < time.monotonic() - t0 < 1.5
        # The queue is bounded: with one waiting, the next is refused at once.
        slow = asyncio.create_task(KiwiSpecRemote(server.url, queue_wait=5).expose(kind="bias"))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        with pytest.raises(KiwiSpecBusy):
            await b.expose(kind="bias")
        assert time.monotonic() - t0 < 0.25
        assert (await slow)["state"] in ("queued", "running")
        assert await a.abort("test")
    finally:
        await server.stop()


async def test_a_waiter_that_goes_away_does_not_keep_the_spectrograph(http, sim):  # noqa: F811
    r = await http.post("/expose", json={"kind": "dark", "exptime": 0.5})
    assert r.status_code == 202
    async with httpx.AsyncClient(base_url=str(http.base_url), timeout=0.2) as impatient:
        with pytest.raises(httpx.TimeoutException):
            await impatient.post("/expose", json={"kind": "bias", "wait": 30})
    job = (await http.get(f"/jobs/{r.json()['id']}", params={"wait": 10})).json()
    assert job["state"] == "done"
    # The abandoned request may have been handed the spectrograph and run
    # its bias; either way nothing is left holding it.
    st = await _status(http)
    for _ in range(50):
        st = await _status(http)
        if st["busy"] is None:
            break
        await asyncio.sleep(0.1)
    assert st["busy"] is None and st["queue"] == [], st


async def test_the_sequencer_names_the_night(http, sim):  # noqa: F811
    job = await run_job(http, "/expose", {"kind": "bias", "night": "n20261109"})
    assert job["results"][0]["file"].startswith("n20261109.Bias.")
    job = await run_job(
        http, "/calibrate", {"frames": [{"kind": "bias", "n": 2}], "night": "n20261109"}
    )
    assert all(r["file"].startswith("n20261109.") for r in job["results"])
    r = await http.post("/expose", json={"kind": "bias", "night": "20261109"})
    assert r.status_code == 400


async def test_an_injected_clock_stamps_the_exposures(sim, tmp_path):  # noqa: F811
    from datetime import UTC, datetime

    path = tmp_path / "clock.json"
    ScaledClock(datetime(2026, 11, 10, 3, 0, tzinfo=UTC), 1.0).write(path)
    server = await KiwiSpecServer(service_config(sim, clock={"file": str(path)})).start(
        status_loop=False
    )
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            job = await run_job(c, "/expose", {"kind": "bias"})
        r = job["results"][0]
        assert r["start_utc"].startswith("2026-11-10T03:00")
        assert r["file"].startswith("n20261110.")  # legacy label: the UTC date
    finally:
        await server.stop()


# -- the iodine stage's positions: the live server is authoritative ----------------------


async def test_stage_positions_are_learned_from_the_live_server(http, sim):  # noqa: F811
    r = await http.post("/configure", json={"iodine": "out"})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == {"mm": 102.0, "name": "out"}
    st = await _status(http)
    assert st["stage_positions_live"] == {"out": 102.0}
    assert not any("stage" in a for a in st["alarms"])


async def test_flipped_positions_follow_the_server_not_a_hard_coded_table(sim):  # noqa: F811
    # The stage's motor and sensor were swapped once, flipping the
    # positions: whatever the live server's ini says is where "in" is.
    sim.config.stage_positions = {"in": 145.0, "flat": 88.0, "out": 55.0}
    server = await KiwiSpecServer(
        service_config(sim, stage_positions={"in": 11.0, "out": 102.0}, stage_timeout=3.0)
    ).start(status_loop=False)
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            r = await c.post("/configure", json={"iodine": "in"})
            assert r.status_code == 200, r.text
            assert r.json()["stage"] == {"mm": 145.0, "name": "in"}
            st = (await c.get("/status")).json()
        assert st["stage_positions_live"]["in"] == 145.0
        # The stale configured cross-check raises an alarm.
        assert any("authoritative" in a and "'in'" in a for a in st["alarms"]), st["alarms"]
    finally:
        await server.stop()
