"""alpyca + the robofast weather server, in-process (no OmniSim needed)."""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from robofast_core import DeviceError, DeviceTimeout, Timeline
from robofast_core.alpaca import alpaca_owner, isolated_class
from robofast_core.weather_server import WeatherServer, WeatherState


def test_alpyca_internals_the_shim_relies_on():
    from alpaca.device import Device
    from alpaca.safetymonitor import SafetyMonitor

    assert hasattr(Device, "_Device__check_error")
    for name in ("_get", "_put"):
        params = list(inspect.signature(getattr(Device, name)).parameters)
        assert params[:3] == ["self", "attribute", "tmo"], (name, params)
    iso = isolated_class(SafetyMonitor)
    assert iso._get is not Device._get and iso._put is not Device._put
    assert issubclass(iso, SafetyMonitor)


@pytest.fixture
def weather():
    state = WeatherState(safe=True, stale_after=None)
    server = WeatherServer(state, hang_limit=10).start()
    yield state, server
    server.stop()


@pytest.mark.parametrize("isolate", [True, False])
async def test_read_weather_server_through_owner(weather, isolate):
    state, server = weather
    tl = Timeline()
    sm = alpaca_owner("sm", "safetymonitor", server.address, tl, isolate_requests=isolate)
    oc = alpaca_owner("oc", "observingconditions", server.address, tl, isolate_requests=isolate)
    g, h = sm.grant("t"), oc.grant("t")
    assert await g.get("IsSafe") is True
    assert await h.call("TimeSinceLastUpdate", "") < 5
    state.update(safe=False)
    assert await g.get("IsSafe") is False
    assert await h.get("Humidity") == 30.0
    sm.close()
    oc.close()


async def test_weather_server_faults_surface_as_errors(weather):
    state, server = weather
    tl = Timeline()
    sm = alpaca_owner("sm", "safetymonitor", server.address, tl, default_timeout=0.5)
    g = sm.grant("t")
    state.set_mode("error")
    with pytest.raises(DeviceError, match="injected fault"):
        await g.get("IsSafe")
    state.set_mode("hang")
    t0 = time.monotonic()
    with pytest.raises(DeviceTimeout):
        await g.get("IsSafe")
    assert time.monotonic() - t0 < 1.0
    state.set_mode("ok")
    assert await g.get("IsSafe") is True
    sm.close()


async def test_weather_server_is_itself_fail_closed():
    state = WeatherState(safe=True, stale_after=0.2)
    server = WeatherServer(state).start()
    try:
        tl = Timeline()
        g = alpaca_owner("sm", "safetymonitor", server.address, tl).grant("t")
        state.update(safe=True)
        assert await g.get("IsSafe") is True
        await asyncio.sleep(0.3)  # no update arrives
        assert await g.get("IsSafe") is False
        state.start_feed(0.05)
        await asyncio.sleep(0.1)
        assert await g.get("IsSafe") is True
        g.owner.close()
    finally:
        server.stop()
