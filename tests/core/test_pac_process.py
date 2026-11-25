"""The simulated PAC as a process of its own (robofast.testing.rv_night.PACProcess),
used by the rehearsal so that a pause of the rehearsal process cannot look
like a dropped PAC reply (night1121-1124, 2026-11-25)."""

from __future__ import annotations

import asyncio
import os
import time

from robofast.devices.aqawan_pac import PACClient
from robofast.testing.rv_night import PACProcess


async def test_regression_20261125_the_pac_simulator_runs_in_its_own_process(tmp_path):
    sim = PACProcess(tmp_path, open_time=0.2, close_time=0.2, settle=0.05, heartbeat_timeout=0)
    sim.start()
    try:
        assert sim.proc is not None and sim.proc.pid != os.getpid()
        client = PACClient("127.0.0.1", sim.port)
        st = await client.status()
        assert (st.shutter1, st.shutter2) == ("CLOSED", "CLOSED")
        first = time.monotonic() + 5
        while not sim.closed() and time.monotonic() < first:
            await asyncio.sleep(0.1)  # the first state file
        assert sim.closed()
        await client.command("OPEN_SHUTTER_1")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(c == "OPEN_SHUTTER_1" for _, c in sim.log):
            await asyncio.sleep(0.1)
        assert [c for _, c in sim.log] == ["OPEN_SHUTTER_1"]  # polls left out
    finally:
        sim.stop()
    assert sim.proc.poll() is not None
