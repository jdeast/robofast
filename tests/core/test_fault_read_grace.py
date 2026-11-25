"""An unreadable enclosure fault channel (night1124, 2026-11-25): one PAC
STATUS read that came back late revoked the lease and cycled the roof, and so
cost a spectrum. Now an unreadable fault channel counts as faulted only after
``fault_unreadable_grace``. Meanwhile no new lease is granted, and a fault the
enclosure reports still closes at once."""

from __future__ import annotations

import asyncio
import json

import pytest

from robofast.testing.checks import wait_until

GRACE = 1.0


def rig_with_faults(make_rig):
    rig = make_rig(
        safety=dict(
            attended_only=None,
            heartbeat_interval=0.1,
            heartbeat_timeout=2.0,
            fault_action="Faults",
            fault_unreadable_grace=GRACE,
        ),
        heartbeat_timeout=2.0,
    )
    state = {"mode": "ok"}
    action = rig.dome.Action

    def faults(name: str, parameters: str = "") -> str:
        if name != "Faults":
            return action(name, parameters)
        if state["mode"] == "down":
            raise RuntimeError("no complete reply within 2 s")
        return json.dumps({"fault": state["mode"] == "fault", "error": False})

    rig.dome.Action = faults
    return rig, state


async def _open(rig):
    lease = await asyncio.wait_for(rig.supervisor.request_open("seq"), 5)

    async def renew() -> None:
        while True:
            lease.renew()
            await asyncio.sleep(0.1)

    renewer = asyncio.create_task(renew())
    await asyncio.wait_for(lease.wait_open(), 5)
    return lease, renewer


async def test_regression_20261125_one_unreadable_fault_read_does_not_cycle_the_roof(make_rig):
    rig, state = rig_with_faults(make_rig)
    await rig.start(sequencer=False)
    lease, renewer = await _open(rig)
    try:
        state["mode"] = "down"
        await asyncio.sleep(GRACE / 2)  # several polls, all failing
        state["mode"] = "ok"
        await asyncio.sleep(GRACE)
        assert lease.live and rig.dome_open()
        assert rig.supervisor.fault is None and not rig.timeline.events("lease_revoked")
    finally:
        renewer.cancel()
        lease.release()


async def test_an_unreadable_fault_channel_past_the_grace_closes(make_rig):
    rig, state = rig_with_faults(make_rig)
    await rig.start(sequencer=False)
    lease, renewer = await _open(rig)
    try:
        state["mode"] = "down"
        await asyncio.wait_for(lease.revoked.wait(), GRACE + 3)
        assert "unreadable for" in lease.revoke_reason
        await wait_until(rig.dome_closed, 5, what="closed")
    finally:
        renewer.cancel()
        lease.release()


async def test_a_reported_fault_still_closes_at_once(make_rig):
    rig, state = rig_with_faults(make_rig)
    await rig.start(sequencer=False)
    lease, renewer = await _open(rig)
    try:
        state["mode"] = "fault"
        await asyncio.wait_for(lease.revoked.wait(), GRACE / 2 + 0.5)
        assert "enclosure reports Fault" in lease.revoke_reason
    finally:
        renewer.cancel()
        lease.release()


async def test_no_new_lease_while_the_fault_channel_is_unreadable(make_rig):
    rig, state = rig_with_faults(make_rig)
    state["mode"] = "down"
    await rig.start(sequencer=False)
    await asyncio.sleep(GRACE / 2)  # within the grace: still no grant
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(rig.supervisor.request_open("seq"), GRACE / 2)
    state["mode"] = "ok"
    lease = await asyncio.wait_for(rig.supervisor.request_open("seq"), 5)
    lease.release()
