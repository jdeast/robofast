"""The asyncio Alpaca client: protocol details against httpx.MockTransport,
then timeouts, faults and cross-device isolation against real sockets (the
in-process robofast weather server; no OmniSim needed)."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from urllib.parse import parse_qsl

import httpx
import pytest

from robofast_core import DeviceError, DeviceTimeout, Timeline
from robofast_core import alpaca_client as ac
from robofast_core.alpaca import alpaca_owner
from robofast_core.alpaca_client import AlpacaDevice
from robofast_core.weather_server import WeatherServer, WeatherState

# -- protocol, against a mock transport -------------------------------------------


class Recorder:
    """A mock Alpaca server: records requests, answers with ``reply``."""

    def __init__(self, value=None, *, error: int = 0, message: str = "") -> None:
        self.requests: list[httpx.Request] = []
        self.reply = lambda req: httpx.Response(
            200,
            json={
                "Value": value,
                "ClientTransactionID": 0,
                "ServerTransactionID": 1,
                "ErrorNumber": error,
                "ErrorMessage": message,
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    def device(self, device_type: str = "telescope", number: int = 0) -> AlpacaDevice:
        return AlpacaDevice("sim:1234", device_type, number, transport=httpx.MockTransport(self))

    def params(self, i: int = -1) -> dict[str, str]:
        req = self.requests[i]
        if req.method == "GET":
            return dict(req.url.params)
        return dict(parse_qsl(req.content.decode(), keep_blank_values=True))


async def test_get_url_params_and_value():
    srv = Recorder(True)
    dev = srv.device("safetymonitor", 2)
    assert await dev.get("IsSafe", timeout=1) is True
    req = srv.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/safetymonitor/2/issafe"
    p = srv.params()
    assert p["ClientID"] == str(ac.CLIENT_ID) and 1 <= ac.CLIENT_ID <= 65535
    assert p["ClientTransactionID"] == "1"
    await dev.aclose()


async def test_transaction_ids_increase_per_device():
    srv = Recorder(0)
    a, b = srv.device("dome"), srv.device("camera")
    for _ in range(3):
        await a.get("ShutterStatus", timeout=1)
    await b.get("CameraState", timeout=1)
    ids = [r.url.params["ClientTransactionID"] for r in srv.requests]
    assert ids == ["1", "2", "3", "1"]
    await a.aclose()
    await b.aclose()


async def test_put_uses_form_body_and_spec_parameter_names():
    srv = Recorder()
    cam, tel = srv.device("camera"), srv.device("telescope")
    await cam.call("StartExposure", 12.5, True, timeout=1)
    req = srv.requests[-1]
    assert req.method == "PUT" and req.url.path == "/api/v1/camera/0/startexposure"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert not req.url.params  # nothing in the query string
    body = srv.params()
    assert body["Duration"] == "12.5" and body["Light"] == "True"
    assert body["ClientTransactionID"] == "1" and "ClientID" in body

    await tel.call("SlewToCoordinatesAsync", 5.5, -20.0, timeout=1)
    assert {k: v for k, v in srv.params().items() if not k.startswith("Client")} == {
        "RightAscension": "5.5",
        "Declination": "-20.0",
    }
    await tel.set("Tracking", False, timeout=1)
    assert srv.requests[-1].url.path.endswith("/tracking")
    assert srv.params()["Tracking"] == "False"
    await tel.call("Park", timeout=1)
    assert srv.requests[-1].method == "PUT" and srv.requests[-1].url.path.endswith("/park")
    await cam.aclose()
    await tel.aclose()


async def test_get_method_with_parameter():
    srv = Recorder(1.5)
    oc = srv.device("observingconditions")
    assert await oc.call("TimeSinceLastUpdate", "", timeout=1) == 1.5
    req = srv.requests[0]
    assert req.method == "GET" and req.url.path.endswith("/timesincelastupdate")
    assert req.url.params["SensorName"] == ""
    await oc.aclose()


async def test_unknown_method_and_wrong_arity_are_refused_locally():
    srv = Recorder()
    tel = srv.device()
    with pytest.raises(ValueError, match="METHODS"):
        await tel.call("SlewToAltAzAsync", 1, 2, timeout=1)
    with pytest.raises(TypeError):
        await tel.call("SlewToCoordinatesAsync", 1, timeout=1)
    assert not srv.requests
    await tel.aclose()


async def test_connect_sets_connected_only_if_needed():
    srv = Recorder(False)
    dev = srv.device("dome")
    await dev.connect(timeout=1)
    assert [r.method for r in srv.requests] == ["GET", "PUT"]
    assert srv.params()["Connected"] == "True"
    srv2 = Recorder(True)
    dev2 = srv2.device("dome")
    await dev2.connect(timeout=1)
    assert [r.method for r in srv2.requests] == ["GET"]
    await dev.aclose()
    await dev2.aclose()


@pytest.mark.parametrize(
    "number, cls",
    [
        (0x400, ac.NotImplementedException),
        (0x401, ac.InvalidValueException),
        (0x402, ac.ValueNotSetException),
        (0x407, ac.NotConnectedException),
        (0x408, ac.ParkedException),
        (0x409, ac.SlavedException),
        (0x40B, ac.InvalidOperationException),
        (0x40C, ac.ActionNotImplementedException),
        (0x40E, ac.OperationCancelledException),
        (0x500, ac.DriverException),
        (0xFFF, ac.DriverException),
        (0x4FF, ac.AlpacaError),  # unassigned: still an AlpacaError
    ],
)
async def test_error_numbers_map_to_exceptions(number, cls):
    srv = Recorder(error=number, message="nope")
    tel = srv.device()
    with pytest.raises(cls, match="nope") as info:
        await tel.call("Park", timeout=1)
    assert type(info.value) is cls
    assert isinstance(info.value, DeviceError)
    assert info.value.number == number
    await tel.aclose()


@pytest.mark.parametrize(
    "status, cls",
    [
        (400, ac.AlpacaBadRequest),
        (404, ac.AlpacaNotFound),
        (500, ac.AlpacaServerError),
        (503, ac.AlpacaHTTPError),
    ],
)
async def test_http_errors_carry_the_text_body(status, cls):
    srv = Recorder()
    srv.reply = lambda req: httpx.Response(status, text="no such device")
    tel = srv.device()
    with pytest.raises(cls, match="no such device") as info:
        await tel.get("AtPark", timeout=1)
    assert type(info.value) is cls and info.value.status == status
    assert isinstance(info.value, DeviceError)
    await tel.aclose()


async def test_garbage_response_is_a_protocol_error():
    srv = Recorder()
    srv.reply = lambda req: httpx.Response(200, text="<html>hello</html>")
    tel = srv.device()
    with pytest.raises(ac.AlpacaProtocolError):
        await tel.get("AtPark", timeout=1)
    srv.reply = lambda req: httpx.Response(200, content=json.dumps([1, 2]))
    with pytest.raises(ac.AlpacaProtocolError):
        await tel.get("AtPark", timeout=1)
    await tel.aclose()


# -- real sockets: the robofast weather server ------------------------------------


@pytest.fixture
def make_weather():
    started = []

    def make() -> tuple[WeatherState, WeatherServer]:
        state = WeatherState(safe=True, stale_after=None)
        started.append((state, WeatherServer(state, hang_limit=10).start()))
        return started[-1]

    yield make
    for state, server in started:
        state.set_mode("ok")
        server.stop()


@pytest.fixture
def weather(make_weather):
    return make_weather()


async def test_timeout_raises_device_timeout_on_time(weather):
    state, server = weather
    dev = AlpacaDevice(server.address, "safetymonitor")
    assert await dev.get("IsSafe", timeout=1) is True
    state.set_mode("hang")
    t0 = time.monotonic()
    with pytest.raises(DeviceTimeout):
        await dev.get("IsSafe", timeout=0.3)
    assert 0.25 < time.monotonic() - t0 < 0.8
    state.set_mode("ok")
    assert await dev.get("IsSafe", timeout=1) is True  # the client recovers
    await dev.aclose()


async def test_refused_connection_is_a_transport_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    dev = AlpacaDevice(f"127.0.0.1:{port}", "dome")
    with pytest.raises(ac.AlpacaTransportError):
        await dev.get("ShutterStatus", timeout=1)
    await dev.aclose()


async def test_read_weather_server_through_owner(weather):
    state, server = weather
    tl = Timeline()
    sm = alpaca_owner("sm", "safetymonitor", server.address, tl)
    oc = alpaca_owner("oc", "observingconditions", server.address, tl)
    g, h = sm.grant("t"), oc.grant("t")
    assert await g.get("IsSafe") is True
    assert state.connected  # the owner connected the device first
    assert await h.call("TimeSinceLastUpdate", "") < 5
    state.update(safe=False)
    assert await g.get("IsSafe") is False
    assert await h.get("Humidity") == 30.0
    with pytest.raises(ac.NotImplementedException):
        await h.get("SkyQuality")
    sm.close()
    oc.close()


async def test_weather_server_faults_surface_as_errors(weather):
    state, server = weather
    tl = Timeline()
    sm = alpaca_owner("sm", "safetymonitor", server.address, tl, default_timeout=0.5)
    g = sm.grant("t")
    state.set_mode("error")
    with pytest.raises(ac.AlpacaServerError, match="injected fault"):
        await g.get("IsSafe")
    state.set_mode("hang")
    t0 = time.monotonic()
    with pytest.raises(DeviceTimeout):
        await g.get("IsSafe")
    assert time.monotonic() - t0 < 1.0
    assert tl.commands(device="sm", outcome="timeout")
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
        state.stop_feed()
        server.stop()


async def test_hung_device_does_not_delay_another(make_weather):
    """While a request to hung device A is in flight (5 s timeout), requests
    to device B, through their own owners, take no longer than usual."""
    (state_a, server_a), (_, server_b) = make_weather(), make_weather()
    tl = Timeline()
    a = alpaca_owner("a", "safetymonitor", server_a.address, tl, default_timeout=5.0)
    b = alpaca_owner("b", "safetymonitor", server_b.address, tl, default_timeout=5.0)
    ga, gb = a.grant("t"), b.grant("t")
    assert await ga.get("IsSafe") is True and await gb.get("IsSafe") is True  # warm up
    state_a.set_mode("hang")
    hung = asyncio.create_task(ga.get("IsSafe"))
    await asyncio.sleep(0.2)
    latencies = []
    for _ in range(20):
        t0 = time.monotonic()
        assert await gb.get("IsSafe") is True
        latencies.append(time.monotonic() - t0)
    assert not hung.done()  # A was still hung throughout
    print(f"device B read latency while A hung: max {max(latencies) * 1e3:.1f} ms")
    assert max(latencies) < 0.25
    state_a.set_mode("ok")
    assert await hung is True
    a.close()
    b.close()
