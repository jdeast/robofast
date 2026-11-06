"""OpenShutter is never sent while the SafetyMonitor already says unsafe.

The tests are from Castle639 (PR #109, and its follow-up).

Real time, the test rig and its fakes, no virtual clock. The weather turns unsafe
just before the reopen hold runs out, between two weather polls; or just after the
first reading taken after the hold, before the enclosure loop's next pass.
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


@pytest.mark.parametrize("lease_path", ["local"])
async def test_open_is_not_sent_after_the_weather_changed_since_the_reading(make_rig):
    """A weather read takes 0.3 s (an HTTP read does take time): the reading
    after the hold is taken at ~2.3 s and lands at ~2.6 s; the weather turns
    unsafe at 3.0 s, before the enclosure loop's next pass."""
    rig = make_rig(
        safety=dict(
            poll_interval=2.0, reopen_hold=1.0, read_timeout=1.0, stale_after=5.0, lease_ttl=10.0
        ),
        seq=dict(lease_renew_interval=5.0),
    )
    sent_while_unsafe = []
    orig_open = rig.dome.OpenShutter

    def open_shutter():
        if not rig.weather.safe:
            sent_while_unsafe.append(time.monotonic())
        orig_open()

    rig.dome.OpenShutter = open_shutter
    orig_get = rig.sm.get

    async def slow_get(member, **kw):
        await asyncio.sleep(0.3)
        return await orig_get(member, **kw)

    rig.sm.get = slow_get
    t0 = time.monotonic()
    await rig.start()
    await asyncio.sleep(3.0 - (time.monotonic() - t0))
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.timeline.events("unsafe"), 6, what="unsafe seen")
    await wait_until(rig.dome_closed, 6, what="closed")
    opened = [round(e.t - t0, 2) for e in rig.timeline.events("open_commanded")]
    assert not sent_while_unsafe, (
        f"OpenShutter sent {len(sent_while_unsafe)}x while IsSafe=False (opens at {opened} s)"
    )
