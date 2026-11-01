"""Attended-only enclosures (JDE 2026-10-31): an enclosure without a hardware
heartbeat (the FLWO 60-inch dome has no watchdog) opens only while an
operator is on duty, renewed by the observer console."""

from __future__ import annotations

import asyncio
import json

import pytest

from robofast_core import SafetyConfig
from robofast_core.safety import LeaseRevoked, RecoveryRefused
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT

ATTENDED = dict(attended_only=True, duty_ttl=0.6, duty_grace=0.6)


def test_attended_only_follows_the_heartbeat():
    assert SafetyConfig().attended_only is True  # no heartbeat device
    assert SafetyConfig(heartbeat_interval=10, heartbeat_timeout=60).attended_only is False
    flwo = SafetyConfig(**json.loads((ROOT / "sites/flwo60/flwo60.json").read_text())["safety"])
    assert flwo.attended_only and flwo.heartbeat_interval is None
    # MINERVA: the Aqawan's heartbeat; it stays unattended-capable.
    aqawan = json.loads((ROOT / "sites/minerva/aqawan1.json").read_text())["safety"]
    assert SafetyConfig(**aqawan).attended_only is False
    with pytest.raises(ValueError, match="duty_ttl"):
        SafetyConfig(duty_ttl=0)


async def test_no_lease_without_an_operator_on_duty(make_rig):
    rig = make_rig(safety=ATTENDED)
    await rig.start(sequencer=False)
    sup = rig.supervisor
    await wait_until(lambda: sup._safe, 2, what="weather safe")
    await asyncio.sleep(0.8)  # past the reopen hold
    assert not sup.open_allowed() and not sup.status()["duty"]["attended"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sup.request_open("seq"), 0.5)
    assert not rig.timeline.events("lease_granted") and rig.dome_closed()
    with pytest.raises(RecoveryRefused, match="not on duty"):
        sup.duty_renew("alice")
    sup.duty_on("alice", "observing tonight")
    lease = await asyncio.wait_for(sup.request_open("seq"), 2)
    renewer = asyncio.create_task(_renew(lease))
    try:
        await asyncio.wait_for(lease.wait_open(), 3)
        assert sup.status()["duty"]["on_duty"] == "alice"
        # Going off duty closes an attended-only enclosure at once.
        sup.duty_off("alice", "going home")
        await asyncio.wait_for(lease.revoked.wait(), 1)
    finally:
        renewer.cancel()
    assert "off duty" in lease.revoke_reason
    await wait_until(rig.dome_closed, 3, what="closed after off duty")
    lease.release()


async def test_duty_expiry_alarms_then_closes(make_rig):
    rig = make_rig(safety=ATTENDED)
    await rig.start(sequencer=False)
    sup = rig.supervisor
    sup.duty_on("alice", "observing")
    lease = await asyncio.wait_for(sup.request_open("seq"), 3)

    async def renew_lease() -> None:
        while True:
            lease.renew()
            await asyncio.sleep(0.1)

    renewer = asyncio.create_task(renew_lease())
    try:
        await asyncio.wait_for(lease.wait_open(), 3)
        # Renewed by the console: stays open past the ttl.
        for _ in range(4):
            await asyncio.sleep(0.3)
            sup.duty_renew("alice")
        assert rig.dome_open() and not sup.critical()
        # The console stops renewing: a critical alarm after duty_ttl ...
        await wait_until(lambda: sup.critical(), 2, what="duty expiry alarm")
        assert "not renewed" in sup.critical()[0]
        assert rig.timeline.events("duty_expired")
        assert rig.dome_open() and lease.live  # within the grace
        # ... then, after duty_grace, the lease is revoked and it closes.
        await asyncio.wait_for(lease.revoked.wait(), 2)
        assert "duty" in lease.revoke_reason
        await wait_until(rig.dome_closed, 3, what="closed after the grace")
        with pytest.raises(LeaseRevoked):
            lease.renew()
    finally:
        renewer.cancel()
    lease.release()
    # No new lease until someone is on duty (again).
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sup.request_open("seq"), 0.5)
    sup.duty_renew("alice")  # a late renewal brings the duty back
    assert not sup.critical()
    await asyncio.wait_for(sup.request_open("seq"), 3)


async def test_a_late_renewal_within_the_grace_keeps_it_open(make_rig):
    rig = make_rig(safety=ATTENDED)
    await rig.start(sequencer=False)
    sup = rig.supervisor
    sup.duty_on("alice", "observing")
    lease = await asyncio.wait_for(sup.request_open("seq"), 3)
    renewer = asyncio.create_task(_renew(lease))
    try:
        await asyncio.wait_for(lease.wait_open(), 3)
        await wait_until(lambda: sup.critical(), 2, what="duty expiry alarm")
        sup.duty_renew("alice")
        assert not sup.critical() and rig.timeline.events("duty_renewed_late")
        await asyncio.sleep(0.5)
        assert lease.live and rig.dome_open()
    finally:
        renewer.cancel()
        lease.release()


async def _renew(lease) -> None:
    while True:
        lease.renew()
        await asyncio.sleep(0.1)


async def test_unattended_enclosures_are_unaffected(make_rig):
    """MINERVA (a hardware heartbeat): no duty needed, ever."""
    rig = make_rig(
        safety=dict(attended_only=None, heartbeat_interval=0.1, heartbeat_timeout=2.0),
        heartbeat_timeout=2.0,
    )
    assert rig.supervisor.config.attended_only is False
    await rig.start(sequencer=False)
    lease = await asyncio.wait_for(rig.supervisor.request_open("seq"), 3)
    await asyncio.wait_for(lease.wait_open(), 3)
    assert rig.supervisor.status()["duty"]["attended"] is True
    lease.release()


async def test_the_duty_endpoint(make_rig):
    rig = make_rig(safety=ATTENDED)
    server = SupervisorServer(rig.supervisor)

    async def post(body):
        return await server._respond("POST", "/duty", json.dumps(body).encode())

    assert (await post({"operator": " ", "op": "on"}))[0] == 400
    assert (await post({"operator": "alice", "op": "maybe"}))[0] == 400
    code, body = await post({"operator": "alice", "op": "on", "reason": "tonight"})
    assert code == 200 and body["on_duty"] == "alice" and body["attended"]
    assert (await post({"operator": "bob", "op": "renew"}))[0] == 409
    code, body = await post({"operator": "alice", "op": "renew"})
    assert code == 200 and body["renewed_ago"] < 0.1
    code, body = await post({"operator": "alice", "op": "off"})
    assert code == 200 and body["on_duty"] is None
    assert [e.what for e in rig.timeline.events() if e.what.startswith("duty")] == [
        "duty_on",
        "duty_off",
    ]
