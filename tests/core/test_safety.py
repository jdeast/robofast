"""SafetySupervisor on its own (fakes, no sequencer)."""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast.safety import RecoveryRefused
from robofast.testing.checks import wait_until

pytestmark = pytest.mark.virtual_time


async def _open(rig, holder="test"):
    lease = await asyncio.wait_for(rig.supervisor.request_open(holder), 5)
    await asyncio.wait_for(lease.wait_open(), 5)
    return lease


async def test_starts_closed_and_waits_for_the_hold(make_rig):
    rig = make_rig(safety=dict(reopen_hold=0.8))
    t0 = time.monotonic()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    safe_at = rig.timeline.first_event("weather_safe").t
    open_cmd = rig.timeline.first_event("open_commanded").t
    assert open_cmd - safe_at >= 0.8
    assert open_cmd - t0 >= 0.8
    assert rig.dome_open()
    lease.release()
    await wait_until(rig.dome_closed, 2, what="close after release")


async def test_unsafe_revokes_lease_and_closes(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    t0 = time.monotonic()
    rig.weather.set_safe(False)
    await asyncio.wait_for(lease.revoked.wait(), 1)
    await wait_until(rig.dome_closed, 1, what="dome closed")
    assert time.monotonic() - t0 < 1.0
    assert "IsSafe" in lease.revoke_reason


async def test_frozen_feed_is_unsafe(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.weather.freeze()  # IsSafe stays True but the data ages
    await asyncio.wait_for(lease.revoked.wait(), 2)
    assert "stale" in lease.revoke_reason
    await wait_until(rig.dome_closed, 1)


async def test_weather_device_error_is_unsafe(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.sm.fail("weather server gone")
    await asyncio.wait_for(lease.revoked.wait(), 1)
    assert "read failed" in lease.revoke_reason
    await wait_until(rig.dome_closed, 1)


async def test_weather_device_hang_is_unsafe_within_read_timeout(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    t0 = time.monotonic()
    rig.sm.hang()
    await asyncio.wait_for(lease.revoked.wait(), 2)
    await wait_until(rig.dome_closed, 1)
    # read_timeout 0.3 + poll 0.05 + shutter travel 0.2, with slack
    assert time.monotonic() - t0 < 1.2


async def test_flicker_restarts_the_hold(make_rig):
    rig = make_rig(safety=dict(reopen_hold=0.6))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.weather.set_safe(False)
    await asyncio.wait_for(lease.revoked.wait(), 1)
    lease.release()
    await wait_until(rig.dome_closed, 1)
    rig.weather.set_safe(True)
    req = asyncio.create_task(rig.supervisor.request_open("again"))
    await asyncio.sleep(0.4)
    rig.weather.set_safe(False)  # blip before the hold is met
    await asyncio.sleep(0.15)
    rig.weather.set_safe(True)
    t_last_safe = time.monotonic()
    lease2 = await asyncio.wait_for(req, 3)
    assert time.monotonic() - t_last_safe >= 0.6 - 0.06
    await asyncio.wait_for(lease2.wait_open(), 2)


async def test_unrenewed_lease_expires_and_closes(make_rig):
    rig = make_rig(safety=dict(lease_ttl=0.3))
    await rig.start(sequencer=False)
    lease = await _open(rig)  # and never renew it
    await asyncio.wait_for(lease.revoked.wait(), 1)
    assert "not renewed" in lease.revoke_reason
    await wait_until(rig.dome_closed, 1)


async def test_no_new_lease_until_previous_released(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.weather.set_safe(False)
    await asyncio.wait_for(lease.revoked.wait(), 1)
    rig.weather.set_safe(True)
    req = asyncio.create_task(rig.supervisor.request_open("second"))
    await asyncio.sleep(1.5)  # well past the hold
    assert not req.done(), "granted a new lease while the old one was unreleased"
    assert rig.dome_closed()
    lease.release()
    lease2 = await asyncio.wait_for(req, 2)
    await asyncio.wait_for(lease2.wait_open(), 2)


async def test_sequencer_cannot_command_the_dome(make_rig):
    from robofast import NotAuthorized

    rig = make_rig()
    g = rig.owners["dome"].grant("seq")
    for member in ("OpenShutter", "CloseShutter"):
        try:
            await g.call(member)
        except NotAuthorized:
            continue
        raise AssertionError(f"{member} allowed")


async def test_roll_off_parks_before_closing(make_rig):
    """park_before_close: with no sequencer to park, safety parks, then closes."""
    rig = make_rig(safety=dict(park_before_close=True, park_wait=1.0))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.Unpark()  # a lease is granted only on a parked telescope
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, 3)
    assert rig.parked()
    park = rig.timeline.commands(device="t1.telescope", source_prefix="safety", outcome="sent")
    close = rig.timeline.commands(device="dome", outcome="sent")
    close_t = [e.t for e in close if e.what == "CloseShutter"]
    assert any(e.what == "Park" for e in park)
    assert min(e.t for e in park if e.what == "Park") < min(close_t)
    lease.release()


async def test_a_new_lease_during_park_before_close_is_not_parked_or_closed_on(make_rig):
    """An idle release followed at once by more work: the supervisor, parking
    before it closes, gets a new live lease meanwhile. It must not park the
    telescope the new lease's sequencer has unparked, nor close on it."""
    rig = make_rig(safety=dict(park_before_close=True, park_wait=1.0))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.Unpark()  # the new lease's sequencer, slewing
    parks = len(rig.timeline.commands(device="t1.telescope", source_prefix="safety"))
    closes = len(rig.timeline.commands(device="dome", outcome="sent"))
    await rig.supervisor._ensure_closed()
    assert not rig.tel.AtPark
    assert len(rig.timeline.commands(device="t1.telescope", source_prefix="safety")) == parks
    sent = rig.timeline.commands(device="dome", outcome="sent")
    assert not [c for c in sent[closes:] if c.what == "CloseShutter"]
    lease.release()


async def test_no_lease_is_granted_while_the_roof_closes(make_rig):
    """Once the supervisor commits to closing (after park-before-close), a
    lease asked for meanwhile waits until the roof is closed: granted at
    once, its sequencer would unpark a telescope under the closing roof
    (the e2e night's "CLOSE with t1 not parked (last: unpark)")."""
    rig = make_rig(safety=dict(park_before_close=True, park_wait=1.0))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    sup = rig.supervisor
    real = sup._dome.call
    asked: dict[str, asyncio.Task] = {}

    async def call(member, *args, **kwargs):
        if member == "CloseShutter" and "req" not in asked:
            # The next generation asks for a lease just as the close goes out
            # (telescopes parked, weather safe, nothing outstanding).
            asked["req"] = asyncio.create_task(sup.request_open("next"))
            await asyncio.sleep(0.2)
        return await real(member, *args, **kwargs)

    sup._dome.call = call
    lease.release()
    await wait_until(lambda: "req" in asked, 3, what="the close")
    lease2 = await asyncio.wait_for(asked["req"], 5)
    granted = rig.timeline.events("lease_granted")[-1]
    closed = rig.timeline.events("enclosure_closed")
    assert closed and closed[-1].t <= granted.t, "a lease was granted while the roof closed"
    await asyncio.wait_for(lease2.wait_open(), 5)
    lease2.release()


async def test_recover_only_a_dead_lease_of_the_holder_once_closed(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    sup = rig.supervisor
    lease = await _open(rig, "seq")  # and never renewed: its holder "crashed"

    async def refused(lease_id: int, holder: str, why: str) -> None:
        with pytest.raises(RecoveryRefused, match=why):
            await sup.recover(lease_id, holder)

    await refused(lease.id, "seq", "live")
    rig.dome.hang()  # the close cannot be confirmed
    await wait_until(lease.revoked.is_set, 2, what="lease expired")
    await refused(lease.id, "seq", "not confirmed closed")
    rig.dome.unhang()
    await wait_until(rig.dome_closed, 3, what="closed")
    await refused(lease.id, "other", "belongs to 'seq'")
    await refused(lease.id + 1, "seq", "no lease")
    lease.quarantine()
    await refused(lease.id, "seq", "quarantined")
    assert await sup.clear(lease.id, "operator", "checked the telescope") is True
    assert await sup.recover(lease.id, "seq") is False  # idempotent
    assert sup.outstanding() is None
    assert rig.timeline.first_event("lease_cleared").detail["operator"] == "operator"


async def test_concurrent_recovers_release_once(make_rig):
    rig = make_rig()
    await rig.start(sequencer=False)
    lease = await _open(rig, "seq")
    await wait_until(lease.revoked.is_set, 2, what="lease expired")
    await wait_until(rig.dome_closed, 3, what="closed")
    results = await asyncio.gather(*(rig.supervisor.recover(lease.id, "seq") for _ in range(3)))
    assert sorted(results) == [False, False, True]
    assert len(rig.timeline.events("lease_recovered")) == 1


async def test_orphaned_lease_alarms_and_stays_closed(make_rig):
    rig = make_rig(safety=dict(orphan_timeout=0.5))
    await rig.start(sequencer=False)
    lease = await _open(rig, "seq")  # never renewed, never released
    await wait_until(lambda: any("orphaned" in a for a in rig.supervisor.alarms), 3)
    alarm = next(e for e in rig.timeline.events("alarm") if "orphaned" in e.detail["msg"])
    assert lease.revoked_at is not None and alarm.t - lease.revoked_at >= 0.5
    assert rig.dome_closed() and not lease.released.is_set()
    assert rig.supervisor.status()["orphaned_for"] >= 0.5


async def test_shutter_error_while_open_closes_and_holds(make_rig):
    rig = make_rig(safety=dict(shutter_error_backoff=60.0))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.dome.status_override = 4  # shutterError while it should be open
    await asyncio.wait_for(lease.revoked.wait(), 1)
    assert "shutter in error" in lease.revoke_reason
    await wait_until(lambda: rig.timeline.first_event("close_commanded"), 1, what="close sent")
    rig.dome.status_override = None
    await wait_until(rig.dome_closed, 1, what="dome closed")
    lease.release()
    assert not rig.supervisor.open_allowed()  # held for shutter_error_backoff
    assert rig.supervisor.status()["shutter_hold_for"] > 50
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(rig.supervisor.request_open("again"), 0.5)


async def test_unreadable_shutter_closes_only_after_grace(make_rig):
    rig = make_rig(safety=dict(shutter_unreadable_grace=0.5))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    t0 = time.monotonic()
    rig.dome.status_override = "dome server unreachable"
    await asyncio.wait_for(lease.revoked.wait(), 2)
    assert time.monotonic() - t0 >= 0.5
    assert "unreadable" in lease.revoke_reason


async def test_one_unreadable_shutter_read_is_tolerated(make_rig):
    rig = make_rig(safety=dict(shutter_unreadable_grace=0.5))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.dome.status_override = "blip"
    await asyncio.sleep(0.15)
    rig.dome.status_override = None
    for _ in range(7):  # no sequencer here: renew by hand (lease_ttl 1 s)
        lease.renew()
        await asyncio.sleep(0.1)
    assert lease.live and rig.dome_open()


async def test_the_clear_faults_action_is_configurable(make_rig):
    from robofast.safety import ENCLOSURE_MEMBERS, SafetyConfig, enclosure_members

    assert enclosure_members(SafetyConfig()) == ENCLOSURE_MEMBERS
    cfg = SafetyConfig(clear_faults_action="ResetFaults")
    assert "Action:ResetFaults" in enclosure_members(cfg)
    assert "Action:ClearFaults" not in enclosure_members(cfg)
    with pytest.raises(ValueError, match="heartbeat_action"):
        SafetyConfig(heartbeat_action="")
    rig = make_rig(safety={"clear_faults_action": "ResetFaults"})
    sent = []
    rig.dome.Action = lambda action, parameters="": sent.append((action, parameters)) or "ok"
    await rig.supervisor.clear_faults("jde", "testing")
    assert sent == [("ResetFaults", "jde: testing")]


# -- the park-before-close interlock (require_park_before_close) ------------------------

INTERLOCK = dict(
    park_before_close=True,
    require_park_before_close=True,
    park_wait=1.0,
    park_interlock_escalate=0.5,
)


def _stuck(tel) -> None:
    """A telescope that will not park (Park does nothing)."""
    tel.Park = lambda: None


def test_the_interlock_needs_park_before_close():
    from robofast.safety import SafetyConfig

    with pytest.raises(ValueError, match="needs park_before_close"):
        SafetyConfig(require_park_before_close=True, allow_unattended_without_watchdog=True)


async def test_routine_close_waits_for_every_telescope_to_park(make_rig):
    """A released lease (a routine close) with a telescope that will not
    park: the roof stays open, alarms, escalates to critical; once the
    telescope parks the roof closes."""
    rig = make_rig(telescopes=2, safety=INTERLOCK)
    await rig.start(sequencer=False)
    lease = await _open(rig)
    sup = rig.supervisor
    rig.tels[1].Unpark()
    _stuck(rig.tels[1])
    lease.release()
    await wait_until(lambda: sup.status()["close_interlock"]["blocked"], 3, what="blocked")
    assert "t2.telescope not parked" in sup.status()["close_interlock"]["blocked"]
    assert rig.timeline.events("close_blocked")
    assert rig.parked(0)  # the supervisor parked the one that would
    await wait_until(lambda: sup.critical(), 3, what="escalated")
    assert "NOT CLOSED" in sup.critical()[0]
    assert rig.dome_open()
    sent = rig.timeline.commands(device="dome", outcome="sent")
    assert not [c for c in sent if c.what == "CloseShutter"]
    del rig.tels[1].Park  # it parks again (the class's Park)
    await wait_until(rig.dome_closed, 3, what="closed once parked")
    assert rig.parked()
    assert sup.status()["close_interlock"]["blocked"] is None and not sup.critical()
    assert rig.timeline.events("close_unblocked")


async def test_an_unreadable_telescope_holds_a_routine_close(make_rig):
    rig = make_rig(safety=INTERLOCK)
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.fail("telescope server gone")
    rig.supervisor.operator_close("alice", "done for the night")  # routine
    await wait_until(
        lambda: rig.supervisor.status()["close_interlock"]["blocked"], 3, what="blocked"
    )
    assert "unreadable" in rig.supervisor.status()["close_interlock"]["blocked"]
    await asyncio.sleep(0.3)
    assert rig.dome_open()
    lease.release()
    rig.tel.unfail()
    await wait_until(rig.dome_closed, 3)


async def test_unsafe_weather_overrides_the_interlock(make_rig):
    """An emergency (weather) still closes after park_wait, parked or not."""
    rig = make_rig(safety=INTERLOCK)
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.Unpark()
    _stuck(rig.tel)
    lease.release()
    await wait_until(
        lambda: rig.supervisor.status()["close_interlock"]["blocked"], 3, what="blocked"
    )
    assert rig.dome_open()
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, 3, what="emergency close")
    assert not rig.parked()
    assert rig.timeline.events("close_unblocked")[-1].detail["how"].startswith("overridden")
    assert rig.supervisor.status()["close_interlock"]["blocked"] is None


async def test_unsafe_weather_closes_on_an_unparked_telescope_without_waiting(make_rig):
    rig = make_rig(safety=INTERLOCK)
    await rig.start(sequencer=False)
    await _open(rig)
    rig.tel.Unpark()
    _stuck(rig.tel)
    t0 = time.monotonic()
    rig.weather.set_safe(False)
    await wait_until(rig.dome_closed, 3)
    assert not rig.parked()
    # park_wait (cleanup half + parking half) + shutter travel, with slack
    assert time.monotonic() - t0 < 1.0 + 1.0
    assert not rig.timeline.events("close_blocked")


async def test_an_operator_emergency_close_overrides_the_interlock(make_rig):
    rig = make_rig(safety=INTERLOCK)
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.Unpark()
    _stuck(rig.tel)
    sup = rig.supervisor
    assert sup.operator_close("alice", "stop") is True  # routine: held back
    lease.release()
    await wait_until(lambda: sup.status()["close_interlock"]["blocked"], 3, what="blocked")
    assert rig.dome_open()
    sup.operator_close("alice", "EMERGENCY: smoke", emergency=True)
    await wait_until(rig.dome_closed, 3, what="emergency close")
    assert not rig.parked()
    # cleared on the supervisor's next pass once closed (not the same instant)
    await wait_until(
        lambda: sup.status()["close_interlock"]["emergency_close"] is None, 3, what="cleared"
    )


async def test_without_the_interlock_a_routine_close_goes_ahead(make_rig):
    """park_before_close alone: closes after park_wait, parked or not (as before)."""
    rig = make_rig(safety=dict(park_before_close=True, park_wait=0.4))
    await rig.start(sequencer=False)
    lease = await _open(rig)
    rig.tel.Unpark()
    _stuck(rig.tel)
    lease.release()
    await wait_until(rig.dome_closed, 3)
    assert not rig.parked()
    assert rig.supervisor.status()["close_interlock"] is None
