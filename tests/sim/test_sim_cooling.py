"""Camera cooling (docs/cooling.md) against OmniSim's camera: the camera
owner's on-connect hook sets the setpoint and the cooler over Alpaca, again
after every reconnection, and a cooler found off is turned back on -- or,
on a camera that cannot set its temperature (OmniSim's default), the path
says Unsupported once and the camera works on."""

from __future__ import annotations

import pytest

from robofast.alpaca import alpaca_owner
from robofast.alpaca_client import AlpacaDevice
from robofast.cooling import CameraCooling, CoolingConfig
from robofast.timeline import Timeline

pytestmark = pytest.mark.alpaca_sim


async def test_cooling_on_omnisim(omnisim):
    raw = AlpacaDevice(omnisim, "camera")
    await raw.connect(timeout=10)
    if not await raw.get("CanSetCCDTemperature", timeout=10):
        # OmniSim's default camera: robofast's path says Unsupported, logs
        # it once, and the camera works on.
        tl = Timeline()
        events: list[str] = []
        cooling = CameraCooling("t1", CoolingConfig(setpoint=-7.5))
        cooling.event = lambda what, **kw: events.append(what)
        owner = alpaca_owner("t1.camera", "camera", omnisim, tl, default_timeout=10)
        owner.on_connect = cooling.on_connect
        grant = owner.grant("test")
        try:
            assert await grant.get("CameraState") == 0
            owner.reconnect("test")
            assert await grant.get("CameraState") == 0
            assert events == ["cooling_unsupported"] and cooling.unsupported
            assert await cooling.check(grant, wait=True) is None
            assert not tl.events("on_connect_failed")
        finally:
            owner.close()
            await raw.aclose()
        return
    before = (await raw.get("SetCCDTemperature", timeout=10), await raw.get("CoolerOn", timeout=10))
    tl = Timeline()
    cooling = CameraCooling("t1", CoolingConfig(setpoint=-7.5, poll=0.1))
    owner = alpaca_owner("t1.camera", "camera", omnisim, tl, default_timeout=10)
    owner.on_connect = cooling.on_connect
    grant = owner.grant("test")
    try:
        await raw.set("CoolerOn", False, timeout=10)
        assert await grant.get("CoolerOn") is True  # the first connection set it
        assert await grant.get("SetCCDTemperature") == pytest.approx(-7.5)
        # Reconnected (the recovery ladder's reconnect): set again.
        await raw.set("CoolerOn", False, timeout=10)
        owner.reconnect("test")
        assert await grant.get("CoolerOn") is True
        # Found off before an exposure: turned back on (an alarm).
        alarms: list[str] = []
        cooling.alarm = alarms.append
        await raw.set("CoolerOn", False, timeout=10)
        status = await cooling.check(grant, wait=False)
        assert status is not None and status.restarted and status.cooler_on
        assert await raw.get("CoolerOn", timeout=10) is True and alarms
    finally:
        owner.close()
        await raw.set("SetCCDTemperature", before[0], timeout=10)
        await raw.set("CoolerOn", before[1], timeout=10)
        await raw.aclose()
