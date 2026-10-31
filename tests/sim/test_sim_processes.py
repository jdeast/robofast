"""The supervisor and the sequencer as separate processes, against OmniSim.

OmniSim's dome has no heartbeat, so a :class:`HeartbeatWatchdog` (in this
test process) stands in for the enclosure controller: the supervisor process
pings it, and it closes the OmniSim dome when the pings stop.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import httpx
import pytest

from robofast_core import Observation, Sequencer, SequencerConfig, Timeline
from robofast_core.alpaca import alpaca_owner
from robofast_core.heartbeat_watchdog import HeartbeatWatchdog
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.testing.checks import await_until, wait_until
from robofast_core.testing.processes import SequencerProcess, Service, SupervisorProcess

from .conftest import BOUND, COMMAND_TIMEOUT, SIM_SAFETY, SIM_SEQ

pytestmark = pytest.mark.alpaca_sim

HB_TIMEOUT = 3.0
SAFETY = {
    **SIM_SAFETY,
    "lease_ttl": 3.0,
    "heartbeat_interval": 0.5,
    "heartbeat_timeout": HB_TIMEOUT,
    "heartbeat_max_loop_age": 5.0,
}
SEQ = {**SIM_SEQ, "lease_renew_interval": 0.5}
REMOTE = dict(timeout=2.0, poll=1.0, retry=0.5)


@pytest.fixture
def watchdog(omnisim):
    wd = HeartbeatWatchdog(omnisim, HB_TIMEOUT).start()
    yield wd
    wd.stop()


@pytest.fixture
def services(request):
    started: list[Service] = []
    yield started
    for s in started:
        s.stop()
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:  # show what the processes said
        for s in started:
            lines = s.log_path.read_text().splitlines()
            text = "\n".join(line for line in lines if "HTTP Request" not in line)
            print(f"\n--- {s.log_path.name} ---\n{text}")


@pytest.fixture
def supervisor(omnisim, weather, watchdog, services, truth, tmp_path):
    _, server = weather
    config = {
        "listen": "127.0.0.1:0",
        "devices": {
            "dome": omnisim,
            "safetymonitor": server.address,
            "observingconditions": server.address,
            "heartbeat": watchdog.address,
        },
        "telescopes": {"t1": {"telescope": omnisim, "camera": omnisim}},
        "safety": SAFETY,
    }
    proc = SupervisorProcess(config, tmp_path)
    services.append(proc)
    return proc


@pytest.fixture
async def sequencer(omnisim, supervisor):
    tl = Timeline()
    remote = RemoteSupervisor(supervisor.url, **REMOTE)
    seq = Sequencer(
        "seq",
        supervisor=remote,
        telescope=alpaca_owner("telescope", "telescope", omnisim, tl, default_timeout=3.0),
        camera=alpaca_owner("camera", "camera", omnisim, tl, default_timeout=COMMAND_TIMEOUT),
        plan=lambda: Observation("field", exptime=60.0, count=1000),
        timeline=tl,
        config=SequencerConfig(**SEQ),
    )
    task = asyncio.create_task(seq.run())
    yield seq
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await remote.aclose()


async def _observing(truth) -> None:
    await await_until(lambda: truth.open() and truth.exposing(), 90, what="open and exposing")


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGSTOP], ids=["killed", "hung"])
async def test_supervisor_dies_watchdog_closes(
    supervisor, sequencer, watchdog, truth, record_timing, sig
):
    await _observing(truth)
    t0 = time.monotonic()
    os.kill(supervisor.pid, sig)
    closed = await await_until(truth.closed, BOUND, what="watchdog close")
    await await_until(truth.safe_state, BOUND, what="closed, parked, camera idle")
    safe = time.monotonic() - t0
    noticed = sequencer.timeline.first_event("cancel_requested").t - t0
    await wait_until(lambda: sequencer.cleanups, BOUND)
    record_timing(sequencer_noticed=noticed, closed=closed, safe_state=safe)
    assert watchdog.trips == 1
    assert "unreachable" in sequencer.timeline.first_event("cancel_requested").detail["reason"]
    report = sequencer.cleanups[0]
    assert report.aborted and report.parked and not report.errors, report
    assert noticed < REMOTE["poll"] + REMOTE["timeout"] + 1.0
    assert closed >= HB_TIMEOUT - SAFETY["heartbeat_interval"]  # the watchdog, not the supervisor


def _sequencer_process(omnisim, supervisor, services, tag: str = "sequencer") -> Service:
    """The production entry point (robofast_core.sequencer_service)."""
    config = {
        "name": "enclosure",
        "sequencer_name": "seq",
        "supervisor_url": supervisor.url,
        "telescopes": {"t1": {"telescope": omnisim, "camera": omnisim}},
        "sequencer": SEQ,
        "lease_client": REMOTE,
        "plan": {"type": "static", "targets": [{"name": "field", "exptime": 60, "count": 1000}]},
    }
    seq = SequencerProcess(config, supervisor.log_path.parent, tag)
    services.append(seq)
    return seq


def _lease(supervisor) -> dict:
    return httpx.get(f"{supervisor.url}/status", trust_env=False, timeout=5).json()["lease"]


async def test_sequencer_process_killed(omnisim, supervisor, services, truth, record_timing):
    seq = _sequencer_process(omnisim, supervisor, services)
    await _observing(truth)
    seq.signal(signal.SIGKILL)
    closed = await await_until(truth.closed, BOUND, what="close after lease expiry")
    record_timing(closed=closed, lease_ttl=SAFETY["lease_ttl"])
    assert closed >= SAFETY["lease_ttl"] - SEQ["lease_renew_interval"]
    assert truth.exposing() and not truth.parked()  # nobody left to clean up


async def test_sequencer_restart_recovers(omnisim, supervisor, services, truth, record_timing):
    """SIGKILL mid-exposure and an immediate restart: the new process aborts,
    parks, recovers the lease and observes again under a new one."""
    first = _sequencer_process(omnisim, supervisor, services, "first")
    await _observing(truth)
    old = _lease(supervisor)
    t0 = time.monotonic()
    first.signal(signal.SIGKILL)
    second = _sequencer_process(omnisim, supervisor, services, "second")
    await await_until(truth.closed, BOUND, what="closed")
    closed = time.monotonic() - t0
    # Parked-and-idle lasts only until the new lease, so use the report (and
    # the supervisor, which grants nothing until it has read AtPark).
    await await_until(lambda: second.log_line("startup_cleanup_done"), BOUND, what="cleanup")
    cleaned_up = time.monotonic() - t0
    assert "'aborted': ['t1'], 'parked': ['t1'], 'errors': []" in second.log_line(
        "startup_cleanup_done"
    )
    await await_until(lambda: _lease(supervisor)["id"] != old["id"], BOUND, what="new lease")
    recovered = time.monotonic() - t0
    await _observing(truth)
    observing = time.monotonic() - t0
    record_timing(
        closed=closed,
        cleaned_up=cleaned_up,
        new_lease=recovered,
        observing_again=observing,
        lease_ttl=SAFETY["lease_ttl"],
    )
    new = _lease(supervisor)
    assert new["id"] == old["id"] + 1 and new["holder"] == "seq" and new["live"]
    assert "lease_recovered" in supervisor.log_path.read_text()
