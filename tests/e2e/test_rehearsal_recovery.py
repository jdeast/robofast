"""The hardware rehearsal recovering a telescope (robofast.testing.rehearsal,
docs/recovery.md), with simulators standing for the real hardware:

* T1's PWI2 dies in the middle of the night (its simulator process is
  killed, as Windows Task Scheduler closed MINERVA T1's PWI2 on
  2026-11-06). The sequencer makes T1 safe and climbs its ladder: reconnect
  (fails), the restart hook (a command that starts a new PWI2 simulator
  process, in a restarted PWI2's state), the PWI reconnect, home; the PWI
  server loads the port's pointing model again; T1 observes on.
* A telescope that is dead at the start and whose ladder cannot bring it
  back is dropped at the preflight; the night goes on with the other one,
  and the evaluation names the drop.

Real time (the PWI simulator's sidereal time is the real one), daylight
allowed (``ignore_twilight``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from robofast.devices.pwi_server import PWIConfig, PWIServer
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera
from robofast.testing.mount_guard import Endpoint
from robofast.testing.rehearsal import (
    MINERVA,
    RealTelescope,
    Rehearsal,
    RehearsalSettings,
    check,
    rehearse,
    summary,
)
from robofast.testing.rv_night import SEQUENCER

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]

HOOK = Path(__file__).with_name("pwi2_restart.py")
PORT1 = "Mount_Model_Port1.PXP"


class QuickRetry(Rehearsal):
    """The rehearsal with a short error backoff (test only: the real one
    waits 30 s before a recovered telescope's next try)."""

    def _sequencer_config(self, plan: dict[str, Any]) -> dict[str, Any]:
        cfg = super()._sequencer_config(plan)
        cfg["sequencer"]["error_backoff"] = 3.0
        return cfg


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pwi_server(port: int) -> PWIServer:
    return PWIServer(
        PWIConfig.from_dict(
            {
                "name": "t1",
                "backend": "pwi2",
                "latitude": MINERVA.latitude,
                "longitude": MINERVA.longitude,
                "elevation": MINERVA.elevation,
                "min_alt": 20.0,
                "max_alt": 85.0,
                "park_alt": 45.0,
                "park_az": 180.0,
                "focus_min": 0,
                "focus_max": 50000,
                "listen": "127.0.0.1:0",
                "pwi": {"host": "127.0.0.1", "port": port, "timeout": 2.0},
                "allow_find_home": True,
                "pointing_models": {"1": PORT1, "2": "Mount_Model_Port2.PXP"},
                "home_record": str(Path(tempfile.mkdtemp(prefix="pwi-home-")) / "home.json"),
            }
        )
    ).start()


def sim_state(port: int) -> dict[str, Any]:
    return httpx.get(f"http://127.0.0.1:{port}/robofast-sim/state", timeout=5).json()


async def test_pwi2_dies_mid_night_and_the_ladder_brings_t1_back(tmp_path):
    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path) / "rehearsal-pwi-restart"
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    pidfile = tmp_path / "pwi2.pid"
    hook = [
        sys.executable,
        str(HOOK),
        "--port",
        str(port),
        "--pidfile",
        str(pidfile),
        "--log",
        str(out / "pwi2-sim.log"),
    ]
    await asyncio.to_thread(
        subprocess.run, [*hook, "--initial", "--model-file", PORT1], check=True, timeout=60
    )
    server = pwi_server(port)
    cam_srv = FakeAlpacaServer({"camera": FakeCamera(256, 256)}).start()
    ep = Endpoint.parse(f"{server.address}/0")
    policy = {
        "telescope": {
            "steps": ["reconnect", "restart", "home"],
            "restart": {"command": hook, "settle": 1},
            "verify_timeout": 5,
            "step_timeout": 60,
            "home_timeout": 60,
        }
    }
    settings = RehearsalSettings(
        real={"t1": RealTelescope(ep, Endpoint.parse(f"{cam_srv.address}/0"), ep, ep)},
        recovery={"t1": policy},
        duration=300.0,
        exptime=2.0,
        images=2,
        image_exptime=0.5,
        image_count=1,
        spectra=3,
        console_port=None,
        idle_poll=1.0,
        ignore_twilight=True,
    )
    night = QuickRetry(out, settings)
    killed: dict[str, float] = {}

    async def kill_pwi_once_t1_has_observed() -> None:
        while True:
            await asyncio.sleep(0.5)
            tl = night.outcome.timelines.get("night")
            if tl is None:
                continue
            fed = [
                e for e in tl.events("spectrum_done", SEQUENCER) if "t1" in e.detail["telescopes"]
            ]
            if fed:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
                killed["t"] = time.monotonic()
                print(f"PWI2 simulator killed at {datetime.now(UTC):%H:%M:%S}")
                return

    killer: asyncio.Task | None = None
    try:
        await night.start()
        killer = asyncio.create_task(kill_pwi_once_t1_has_observed())
        outcome = await night.run()
    finally:
        if killer is not None:
            killer.cancel()
            with contextlib.suppress(BaseException):
                await killer
        await night.close()
        state = None
        with contextlib.suppress(Exception):
            state = sim_state(port)
        server.stop()
        cam_srv.stop()
        with contextlib.suppress(Exception):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
    print(summary(outcome))
    assert killed, "the night ended before T1 fed a spectrum"
    tl = outcome.timelines["night"]
    done = [e for e in tl.events("recovery_done") if e.detail["telescope"] == "t1"]
    assert done and done[0].detail["device"] == "telescope", tl.events("telescope_error")
    first = done[0]
    assert first.detail["recovered"], first.detail
    assert [t.split(":")[0] for t in first.detail["tried"]] == ["reconnect", "restart", "home"]
    # The restarted PWI2: connected, homed, its port's model loaded again.
    assert state is not None and state["connected"] and state["homed"], state
    assert state["model_file"] == PORT1, state
    cmds = state["commands"]
    assert "mount:connect" in cmds and "mount:findhome" in cmds
    assert f"mount:setmodel&filename={PORT1}" in cmds
    # The ladder's commands went through the guard, and nothing was refused.
    assert [r["what"] for r in outcome.recovery_commands] == [
        "Action MountConnect",
        "Action MotorsEnable",
        "findhome",
    ]
    # T1 observed on after it came back.
    after = [
        s for s in outcome.slews if s["telescope"] == "t1" and s["t"] > first.t and "seconds" in s
    ]
    assert after, outcome.slews
    assert not any(u.gave_up for u in outcome.runs["night"].units.values())
    problems = check(outcome)
    assert not problems, "\n".join(problems)


async def test_a_dead_telescope_is_dropped_and_the_night_goes_on(tmp_path):
    """T1's PWI is down and its ladder (reconnect only) cannot bring it back:
    T1 is dropped at the preflight and T2 (fake "real" hardware) observes the
    night alone. The evaluation reports the drop."""
    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path) / "rehearsal-dropped"
    from tests.e2e.test_rehearsal import serve_real

    _, cam, srvs = serve_real("t2")
    dead = Endpoint("127.0.0.1", free_port(), 0)
    t1_cam = FakeAlpacaServer({"camera": FakeCamera(64, 64)}).start()
    srvs.append(t1_cam)
    ep = Endpoint.parse(f"{srvs[0].address}/0")
    settings = RehearsalSettings(
        real={
            "t1": RealTelescope(dead, Endpoint.parse(f"{t1_cam.address}/0"), dead, None),
            "t2": RealTelescope(ep, Endpoint.parse(f"{srvs[1].address}/0"), ep, ep),
        },
        recovery={"t1": {"telescope": {"steps": ["reconnect"], "verify_timeout": 1}}},
        duration=3600.0,
        exptime=2.0,
        images=1,
        image_exptime=0.5,
        spectra=2,
        console_port=None,
        idle_poll=1.0,
        start=datetime(2026, 11, 10, 4, 0, tzinfo=UTC),
        scale=10.0,
    )
    try:
        outcome = await rehearse(out, settings)
    finally:
        for s in srvs:
            s.stop()
    print(summary(outcome))
    s = outcome.rehearsal
    assert s is not None and list(s.dropped) == ["t1"] and list(s.real) == ["t2"]
    assert settings.real.keys() == {"t1", "t2"}
    assert "DROPPED at the preflight: t1" in summary(outcome)
    problems = check(outcome)
    assert len(problems) == 1 and problems[0].startswith("t1 dropped at the preflight"), problems
    seq = outcome.runs["night"]
    assert set(seq.units) == {"t2"}
    assert all(c == "COMPLETED" for c in outcome.statuses.values()), outcome.statuses
    assert cam.exposures_started >= 2 and outcome.why_end == "every request done"
    assert json.loads((out / "sequencer-night.json").read_text())["telescopes"].keys() == {"t2"}
