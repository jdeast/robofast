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
    assert sup.alarms == ["lease 1 orphaned: closing", "not confirmed parked before close"]
    assert len(rig.timeline.events("alarm")) == 2
    sup._alarm("something new")
    assert sup.alarms[-1] == "something new"


async def test_an_undelivered_release_is_an_error(caplog):
    async def refuse(method, path, **kw):
        raise httpx.ConnectError("[Errno 24] Too many open files")

    lease = SimpleNamespace(token="t1", _client=SimpleNamespace(_call=refuse, retry=0.0))
    with caplog.at_level(logging.WARNING, logger="robofast.lease_client"):
        await RemoteLease._send(lease, "release")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "release not delivered after 3 attempts" in errors[0].getMessage()
