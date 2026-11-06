"""OpenShutter is never sent while the SafetyMonitor already says unsafe.

The test is from Castle639's PR #109.

Real time, the test rig and its fakes, no virtual clock. The weather turns unsafe just
before the reopen hold runs out, between two weather polls.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast.testing.checks import wait_until


@pytest.mark.parametrize("lease_path", ["local"])
async def test_open_is_not_sent_on_a_cached_safe_verdict(make_rig):
    rig = make_rig(
        safety=dict(
            poll_interval=2.0, reopen_hold=1.0, read_timeout=0.5, stale_after=3.0, lease_ttl=5.0
        )
    )
    sent_while_unsafe = []
    orig = rig.dome.OpenShutter

    def open_shutter():
        if not rig.weather.safe:
            sent_while_unsafe.append(time.monotonic())
        orig()

    rig.dome.OpenShutter = open_shutter
    await rig.start()
    await asyncio.sleep(0.8)  # first read (safe) done; hold ends at ~1.0 s
    rig.weather.set_safe(False)  # unsafe before the hold ends, before the next poll
    await wait_until(lambda: rig.timeline.events("unsafe"), 5, what="unsafe seen")
    await wait_until(rig.dome_closed, 5, what="closed again")
    assert not sent_while_unsafe, f"OpenShutter sent {len(sent_while_unsafe)}x while IsSafe=False"
