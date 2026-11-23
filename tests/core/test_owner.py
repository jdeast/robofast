"""DeviceOwner: serialization, fencing, timeouts, privilege."""

from __future__ import annotations

import asyncio
import time

import pytest

from robofast import DeviceError, DeviceOwner, DeviceTimeout, GrantRevoked, NotAuthorized
from robofast.safety import ENCLOSURE_MEMBERS
from robofast.testing.fakes import FakeDome, FakeTelescope
from robofast.timeline import Timeline


class Slow:
    """A driver that records the maximum number of concurrent calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[int] = []

    async def call(self, member: str, *args, timeout: float | None = None):
        return await getattr(self, member)(*args)

    async def work(self, i: int) -> int:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        self.calls.append(i)
        return i


async def test_commands_are_serialized_in_order():
    tl = Timeline()
    dev = Slow()
    owner = DeviceOwner("slow", lambda: dev, tl)
    a, b = owner.grant("a"), owner.grant("b")
    results = await asyncio.gather(*(g.call("work", i) for i, g in enumerate([a, b] * 5)))
    assert results == list(range(10))
    assert dev.max_active == 1
    assert dev.calls == list(range(10))
    owner.close()


async def test_revoked_grant_is_rejected_before_send():
    tl = Timeline()
    tel = FakeTelescope()
    owner = DeviceOwner("telescope", lambda: tel, tl)
    g = owner.grant("observer")
    await g.call("Unpark")
    g.revoke("test")
    with pytest.raises(GrantRevoked):
        await g.call("Park")
    assert not tel.AtPark  # Park never reached the device
    assert [e.outcome for e in tl.commands(source_prefix="observer")] == [
        "sent",
        "ok",
        "rejected",
    ]
    owner.close()


async def test_queued_command_of_revoked_grant_never_sent():
    """A command queued behind a slow one is fenced if revoked meanwhile."""
    tl = Timeline()
    dev = Slow()
    owner = DeviceOwner("slow", lambda: dev, tl)
    g = owner.grant("observer")
    first = asyncio.create_task(g.call("work", 1))
    second = asyncio.create_task(g.call("work", 2))
    await asyncio.sleep(0.005)
    g.revoke("cancelled")
    assert await first == 1
    with pytest.raises(GrantRevoked):
        await second
    assert dev.calls == [1]
    owner.close()


async def test_timeout_is_bounded_surfaced_and_recovers():
    tl = Timeline()
    tel = FakeTelescope()
    builds = []

    def factory():
        builds.append(1)
        return tel

    owner = DeviceOwner("telescope", factory, tl, default_timeout=0.2)
    g = owner.grant("x")
    tel.hang()
    t0 = time.monotonic()
    with pytest.raises(DeviceTimeout):
        await g.get("AtPark")
    assert time.monotonic() - t0 < 0.5
    assert tl.commands(outcome="timeout")
    assert tl.events("driver_discarded")
    tel.unhang()
    assert await g.get("AtPark") is True
    assert len(builds) == 2  # a fresh driver/session after the timeout
    owner.close()


async def test_device_error_is_surfaced():
    tl = Timeline()
    tel = FakeTelescope()
    owner = DeviceOwner("telescope", lambda: tel, tl)
    tel.fail("gone")
    with pytest.raises(DeviceError, match="gone"):
        await owner.grant("x").get("AtPark")
    owner.close()


async def test_only_the_privileged_grant_may_close():
    tl = Timeline()
    dome = FakeDome()
    owner = DeviceOwner("dome", lambda: dome, tl, privileged_members=ENCLOSURE_MEMBERS)
    safety = owner.grant("safety", privileged=True)
    other = owner.grant("sequencer")
    with pytest.raises(NotAuthorized):
        await other.call("OpenShutter")
    with pytest.raises(NotAuthorized):
        await other.call("CloseShutter")
    with pytest.raises(NotAuthorized):
        owner.grant("impostor", privileged=True)
    await safety.call("OpenShutter")
    assert await other.get("ShutterStatus") in (0, 2)  # reads are fine
    owner.close()


async def test_a_privileged_action_is_refused_to_others():
    """``Action:ClearFaults`` in the privileged set: only that one action
    (any case) is the supervisor's; other actions stay open to everyone."""
    tl = Timeline()
    dome = FakeDome(heartbeat_timeout=60.0)
    owner = DeviceOwner("dome", lambda: dome, tl, privileged_members=ENCLOSURE_MEMBERS)
    other = owner.grant("sequencer")
    for name in ("ClearFaults", "clearfaults", "CLEARFAULTS"):
        with pytest.raises(NotAuthorized):
            await other.call("Action", name, "please")
    assert len(tl.commands(outcome="not_authorized")) == 3
    await other.call("Action", "Heartbeat", "")  # not privileged
    owner.close()


async def test_cancelled_caller_does_not_reorder_commands():
    tl = Timeline()
    dev = Slow()
    owner = DeviceOwner("slow", lambda: dev, tl)
    g = owner.grant("x")
    t = asyncio.create_task(g.call("work", 1))
    await asyncio.sleep(0.005)
    t.cancel()
    assert await g.call("work", 2) == 2
    assert dev.calls == [1, 2]
    assert tl.commands(outcome="cancelled")
    owner.close()


async def test_a_command_that_fails_after_its_caller_gave_up_logs_no_traceback():
    """Night 2026-11-22/23 (night1122): a caller gave up on t2's Tracking
    read, the read then failed with a PWI ReadTimeout, and asyncio.shield
    (Python 3.12+) logged "DriverException exception in shielded future"
    with a traceback although the owner had retrieved and recorded it. The
    failure is in the timeline; the loop's exception handler hears nothing."""
    tl = Timeline()
    release = asyncio.Event()

    class Failing:
        async def call(self, member: str, *args, timeout: float | None = None):
            await release.wait()
            raise RuntimeError("ReadTimeout")

    owner = DeviceOwner("failing", lambda: Failing(), tl)
    g = owner.grant("x")
    reported: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reported.append(ctx))
    t = asyncio.create_task(g.call("work"))
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert tl.commands(outcome="error")  # recorded where it belongs
    assert reported == []
    owner.close()


async def test_not_connected_reconnects_and_retries_once():
    """Night 2026-11-09/10: T2's PWI server, restarted mid-night, starts with
    Connected false and answers NotConnected (0x407); the owner connects it
    again (and runs on_connect, as for any fresh connection) and the command
    goes through."""
    tl = Timeline()
    tel = FakeTelescope()
    hooked: list[bool] = []

    async def on_connect(driver, timeout):
        hooked.append(driver.Connected)

    owner = DeviceOwner("t2.telescope", lambda: tel, tl, on_connect=on_connect)
    g = owner.grant("safety")
    assert await g.get("AtPark") is True
    assert hooked == [True]  # the first connection
    tel.restart()
    assert await g.get("AtPark") is True
    assert tel.Connected
    assert hooked == [True, True]
    assert [e.detail["member"] for e in tl.events("reconnected")] == ["AtPark"]
    assert await g.get("AtPark") is True  # connected: no reconnect
    assert len(tl.events("reconnected")) == 1

    # Once only: a device that is still not connected after Connected = True
    # is an error, not a loop; nor is a NotConnected from Connected itself.
    class Stubborn(FakeTelescope):
        async def set(self, member, value, *, timeout=None):
            if member != "Connected":
                await super().set(member, value, timeout=timeout)

    stub = Stubborn()
    stub.restart()
    owner2 = DeviceOwner("stub", lambda: stub, tl)
    with pytest.raises(DeviceError, match="not connected"):
        await owner2.grant("safety").get("AtPark")
    assert [e.detail["member"] for e in tl.events("reconnected", source="stub")] == ["AtPark"]
