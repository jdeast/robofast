"""The supervisor and the sequencer in separate processes (fast tier, fakes).

The fake devices live in this test process behind a FakeAlpacaServer, so
they outlive whichever process a test kills; ground truth comes straight
from the fakes. The dome has a hardware heartbeat (``HB_TIMEOUT``).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from robofast_core import DeviceOwner, Observation, Sequencer, SequencerConfig, Timeline
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.checks import wait_until
from robofast_core.testing.fakes import (
    FakeCamera,
    FakeDome,
    FakeObservingConditions,
    FakeSafetyMonitor,
    FakeTelescope,
    FakeWeather,
)
from robofast_core.testing.processes import Service, SupervisorProcess

from .conftest import FAST_REMOTE, FAST_SAFETY, FAST_SEQ

HB_TIMEOUT = 1.0  # the dome's own heartbeat timeout
TRAVEL = 0.2  # shutter travel
SAFETY = {**FAST_SAFETY, "heartbeat_interval": 0.1, "heartbeat_max_loop_age": 0.5}
MARGIN = 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Site:
    """Fake devices served over Alpaca, plus the processes a test started."""

    workdir: Path
    weather: FakeWeather = field(default_factory=FakeWeather)
    dome: FakeDome = field(default_factory=lambda: FakeDome(TRAVEL, HB_TIMEOUT))
    tel: FakeTelescope = field(default_factory=FakeTelescope)
    cam: FakeCamera = field(default_factory=FakeCamera)
    services: list[Service] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    remotes: list[RemoteSupervisor] = field(default_factory=list)

    def __post_init__(self) -> None:
        devices = {
            "dome": self.dome,
            "telescope": self.tel,
            "camera": self.cam,
            "safetymonitor": FakeSafetyMonitor(self.weather),
            "observingconditions": FakeObservingConditions(self.weather),
        }
        self.server = FakeAlpacaServer(devices).start()

    def supervisor(self, port: int = 0, tag: str = "supervisor") -> SupervisorProcess:
        a = self.server.address
        config = {
            "listen": f"127.0.0.1:{port}",
            "devices": {"dome": a, "safetymonitor": a, "observingconditions": a},
            "safety": SAFETY,
        }
        proc = SupervisorProcess(config, self.workdir, tag)
        self.services.append(proc)
        return proc

    def sequencer_process(self, url: str) -> Service:
        proc = Service(
            "robofast_core.testing.run_sequencer",
            *("--supervisor", url, "--devices", self.server.address, "--exptime", "30"),
            *("--config", json.dumps(FAST_SEQ)),
            *("--remote", json.dumps(FAST_REMOTE)),
            log_path=self.workdir / "sequencer.log",
        )
        self.services.append(proc)
        return proc

    def sequencer(self, url: str) -> Sequencer:
        """A sequencer in this process (fakes driven directly), leasing over HTTP."""
        tl = Timeline()
        remote = RemoteSupervisor(url, **FAST_REMOTE)
        self.remotes.append(remote)
        seq = Sequencer(
            "seq",
            supervisor=remote,
            telescope=DeviceOwner("telescope", lambda: self.tel, tl, default_timeout=0.5),
            camera=DeviceOwner("camera", lambda: self.cam, tl, default_timeout=0.5),
            plan=lambda: Observation("target", exptime=30.0, count=1000),
            timeline=tl,
            config=SequencerConfig(**FAST_SEQ),
        )
        self.tasks.append(asyncio.create_task(seq.run(), name="seq"))
        return seq

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for r in self.remotes:
            await r.aclose()
        for s in self.services:
            s.stop()
        self.server.stop()

    # ground truth
    def closed(self) -> bool:
        return self.dome.state() == 1

    def observing(self) -> bool:
        return self.dome.state() == 0 and self.cam.CameraState == 2

    def safe_state(self) -> bool:
        return self.closed() and self.tel.AtPark and self.cam.CameraState == 0


@pytest.fixture
async def site(tmp_path):
    s = Site(tmp_path)
    yield s
    await s.close()


def _get(url: str, path: str, method: str = "GET") -> httpx.Response:
    return httpx.request(method, url + path, trust_env=False, timeout=5)


async def test_sequencer_process_killed_lease_expires(site):
    sup = site.supervisor()
    seq = site.sequencer_process(sup.url)
    await wait_until(site.observing, 20, what="open and exposing")
    seq.signal(signal.SIGKILL)
    ttl = SAFETY["lease_ttl"]
    dt = await wait_until(site.closed, ttl + MARGIN, what="closed after the lease expired")
    lease = _get(sup.url, "/status").json()["lease"]
    assert lease["revoked"] and "not renewed" in lease["reason"]
    assert dt >= ttl - FAST_SEQ["lease_renew_interval"]
    assert site.dome.watchdog_trips == 0  # the supervisor closed it, not the hardware
    # Nobody is left to park or abort: the cost of a sequencer crash.
    assert not site.tel.AtPark
    print(f"sequencer SIGKILL: closed {dt:.2f} s later (lease_ttl {ttl} s)")


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGSTOP], ids=["killed", "hung"])
async def test_supervisor_dies_heartbeat_closes(site, sig):
    sup = site.supervisor()
    seq = site.sequencer(sup.url)
    await wait_until(site.observing, 10, what="open and exposing")
    t0 = time.monotonic()
    os.kill(sup.pid, sig)
    closed = await wait_until(site.closed, HB_TIMEOUT + TRAVEL + MARGIN, what="hardware close")
    await wait_until(site.safe_state, 5, what="closed, parked, camera idle")
    safe = time.monotonic() - t0
    assert site.dome.watchdog_trips == 1
    noticed = seq.timeline.first_event("cancel_requested").t - t0
    reason = seq.timeline.first_event("cancel_requested").detail["reason"]
    assert "unreachable" in reason, reason
    await wait_until(lambda: seq.cleanups, 5)
    report = seq.cleanups[0]
    assert report.aborted and report.parked and not report.errors, report
    assert noticed < FAST_REMOTE["poll"] + FAST_REMOTE["timeout"] + 0.5
    assert closed < HB_TIMEOUT + TRAVEL + 0.5
    print(
        f"supervisor {signal.Signals(sig).name}: sequencer noticed {noticed:.2f} s,"
        f" hardware closed {closed:.2f} s, safe state {safe:.2f} s"
        f" (heartbeat timeout {HB_TIMEOUT} s)"
    )


async def test_supervisor_restart(site):
    """A restarted supervisor starts closed, rejects the old leases, and the
    sequencer gets a new one only after the reopen hold."""
    port = _free_port()
    first = site.supervisor(port, "first")
    seq = site.sequencer(first.url)
    await wait_until(site.observing, 10, what="open and exposing")
    token = _get(first.url, "/status").json()["lease"]["token"]
    first.signal(signal.SIGKILL)
    await wait_until(site.safe_state, HB_TIMEOUT + 3, what="closed, parked, camera idle")

    second = site.supervisor(port, "second")
    t_ready = time.monotonic()
    assert second.url == first.url and second.epoch != first.epoch
    assert _get(second.url, f"/leases/{token}").status_code == 404
    assert _get(second.url, f"/leases/{token}/renew", "POST").status_code == 404
    assert site.closed()
    hold = SAFETY["reopen_hold"]
    opened = await wait_until(lambda: site.dome.state() == 0, hold + 5, what="reopen")
    starts = seq.timeline.events("observe_start")
    assert [e.detail["gen"] for e in starts] == [1, 2]
    assert starts[1].t - t_ready >= hold
    assert opened >= hold
    print(
        f"supervisor restart: up in {second.startup:.2f} s; reopened {opened:.2f} s"
        f" after it was ready (reopen_hold {hold} s)"
    )


async def test_lease_request_is_idempotent(make_rig):
    """Repeating a lease request (e.g. after a lost answer) returns the same
    lease instead of stranding it."""
    from robofast_core.supervisor_service import SupervisorServer

    rig = make_rig(safety=dict(reopen_hold=0.1))
    await rig.start(sequencer=False)
    server = await SupervisorServer(rig.supervisor).start()
    body = {"holder": "x", "wait": 3, "request": "r1"}
    try:
        async with httpx.AsyncClient(base_url=server.url, trust_env=False) as http:
            a = (await http.post("/leases", json=body, timeout=5)).json()
            b = (await http.post("/leases", json=body, timeout=5)).json()
            assert a["granted"] and a["lease"]["token"] == b["lease"]["token"]
            c = await http.post("/leases", json={**body, "request": "r2", "wait": 0.3}, timeout=5)
            assert c.json() == {"granted": False}  # the first is still outstanding
    finally:
        await server.stop()


async def test_supervisor_sigterm_closes_before_exiting(site):
    sup = site.supervisor()
    seq = site.sequencer(sup.url)
    await wait_until(site.observing, 10, what="open and exposing")
    sup.signal(signal.SIGTERM)
    await asyncio.to_thread(sup.proc.wait, 10)
    assert site.dome.state() in (1, 3)  # closed or closing, commanded by the supervisor
    await wait_until(site.safe_state, 5, what="closed, parked, camera idle")
    assert site.dome.watchdog_trips == 0
    assert "supervisor stopping" in seq.timeline.first_event("cancel_requested").detail["reason"]
