"""Log hygiene from night1124 (2026-11-25), where the gateway ran out of file
descriptors and two alternating alarms were re-raised every loop (643 times),
and an undelivered lease release, which blocks all observing, logged only a
WARNING."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx

from robofast.lease_client import RemoteLease


async def test_regression_20261125_alternating_alarms_are_raised_once(make_rig):
    rig = make_rig()
    sup = rig.supervisor
    for _ in range(10):
        sup._alarm("lease 1 orphaned: closing")
        sup._alarm("not confirmed parked before close")
    assert list(sup.alarms) == ["lease 1 orphaned: closing", "not confirmed parked before close"]
    assert len(rig.timeline.events("alarm")) == 2
    sup._alarm("something new")
    assert sup.alarms[-1] == "something new"


async def test_regression_20261125_a_release_is_retried_until_delivered(caplog):
    """night1124: the release failed three times (EMFILE) and was dropped,
    which blocks every new lease until a restart. Now it is retried, with one
    ERROR, until it is delivered."""
    calls = []

    async def flaky(method, path, **kw):
        calls.append(path)
        if len(calls) <= 5:
            raise httpx.ConnectError("[Errno 24] Too many open files")
        return {}

    lease = SimpleNamespace(
        token="t1", _client=SimpleNamespace(_call=flaky, retry=0.0), RELEASE_RETRY_MAX=0.0
    )
    with caplog.at_level(logging.WARNING, logger="robofast.lease_client"):
        await RemoteLease._send(lease, "release")
    assert len(calls) == 6 and calls[-1] == "/leases/t1/release"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "release not delivered after 3 attempts" in errors[0].getMessage()
    assert "delivered at attempt 6" in caplog.records[-1].getMessage()


async def test_other_actions_still_give_up(caplog):
    calls = []

    async def refuse(method, path, **kw):
        calls.append(path)
        raise httpx.ConnectError("refused")

    lease = SimpleNamespace(
        token="t1", _client=SimpleNamespace(_call=refuse, retry=0.0), RELEASE_RETRY_MAX=0.0
    )
    await RemoteLease._send(lease, "quarantine")
    assert len(calls) == 3
