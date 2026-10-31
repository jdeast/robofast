"""KiwiSpec: the protocol clients, the service and its abort hook, against the
spectrograph-PC simulator.

Fast tier: everything runs in this process on loopback, with the
simulator's timings shrunk (readout, stage travel, exposure-meter period).
Nothing here talks to a real spectrograph.
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import httpx
import pytest

from robofast_core.devices.kiwispec_client import (
    COMMANDS,
    FORBIDDEN,
    HEADER_CHUNK,
    HardwareNotAllowed,
    KiwiSpecFailed,
    KiwiSpecProtocolError,
    KiwiSpecRefused,
    KiwiSpecTimeout,
    KiwiSpecUnreachable,
    SICameraClient,
    SpecServerClient,
    format_command,
    header_chunks,
)
from robofast_core.devices.kiwispec_service import (
    KiwiSpecConfig,
    KiwiSpecRemote,
    KiwiSpecServer,
    flux_weighted_midpoint,
    main,
)
from robofast_core.devices.kiwispec_sim import KiwiSpecSim, SimConfig

FAST = dict(
    readout=0.2,
    stage_speed=400.0,
    stage_home_time=0.05,
    stage_power_cycle=0.1,
    expmeter_period=0.05,
    heater_tau=0.2,
    backlight_failsafe=0.5,
)


@pytest.fixture
async def sim():
    s = await KiwiSpecSim(SimConfig(**FAST)).start()
    yield s
    await s.stop()


def service_config(sim: KiwiSpecSim, **over) -> KiwiSpecConfig:
    d = {
        "listen": "127.0.0.1:0",
        "spectrograph": {
            "host": "127.0.0.1",
            "port": sim.spec_port,
            "connect_timeout": 0.5,
            "send_timeout": 0.5,
            "max_reply_timeout": 0.5,
        },
        "camera": {
            "host": "127.0.0.1",
            "port": sim.camera_port,
            "connect_timeout": 0.5,
            "command_timeout": 0.5,
        },
        "stage_poll": 0.02,
        "stage_timeout": 1.0,
        "heater_wait": 0.3,
        "readout_time": 0.2,
        "exposure_margin": 1.0,
        "expmeter_poll": 0.05,
        "expmeter_period": 0.05,
        "abort_timeout": 1.5,
        "save_timeout": 2.0,
        "save_retry": 0.1,
        "status_interval": 0.2,
        "status_timeout": 3.0,
        **over,
    }
    return KiwiSpecConfig.from_dict(d)


@pytest.fixture
async def service(sim):
    server = await KiwiSpecServer(service_config(sim)).start(status_loop=False)
    yield server
    await server.stop()


@pytest.fixture
async def http(service):
    async with httpx.AsyncClient(base_url=service.url, timeout=10) as c:
        yield c


def spec_client(sim: KiwiSpecSim, **kw) -> SpecServerClient:
    kw = {"connect_timeout": 0.5, "send_timeout": 0.5, "max_reply_timeout": 0.5, **kw}
    return SpecServerClient("127.0.0.1", sim.spec_port, **kw)


async def run_job(http: httpx.AsyncClient, path: str, body: dict, wait: float = 10.0) -> dict:
    r = await http.post(path, json=body)
    assert r.status_code == 202, r.text
    job = r.json()
    r = await http.get(f"/jobs/{job['id']}", params={"wait": wait})
    return r.json()


# -- the spectrograph server protocol ------------------------------------------------

ARGS = {
    "cell_heater_set_temp": 55.0,
    "i2stage_move": "out",
    "i2stage_movef": 42.5,
    "set_file_name": "n20261031.test.0001.fits",
    "write_header": '{"A": ',
    "write_header_done": '["b", "c"]}',
}


@pytest.mark.parametrize("name", sorted(COMMANDS))
async def test_every_allowlisted_command_against_the_simulator(sim, name):
    client = spec_client(sim)
    if name == "save_image":
        sim.buffer_image = {"exptime_actual": 1.0}
        await client.call("set_file_name", "n20261031.x.0001.fits")
    if name == "write_header_done":
        sim.files["n20261031.test.0001.fits"] = {"header": {}}
        sim.file_name = "n20261031.test.0001.fits"
        sim.header_buffer = '{"A": '
    fields = await client.call(name, ARGS.get(name))
    assert sim.commands[-1] == name
    assert isinstance(fields, list)
    assert not sim.crashed


async def test_typed_replies(sim):
    c = spec_client(sim)
    assert await c.cell_heater_set_temp(54.5) == 54.5
    assert await c.cell_heater_get_set_temp() == 54.5
    assert isinstance(await c.cell_heater_temp(), float)
    assert await c.spec_pressure() == pytest.approx(0.002)
    pos = await c.stage_position()
    assert (pos.mm, pos.name) == (11.0, "in")
    await c.stage_move("out")
    await asyncio.sleep(0.4)
    pos = await c.stage_position()
    assert (pos.mm, pos.name) == (102.0, "out")
    await c.stage_move_mm(50.0)
    await asyncio.sleep(0.3)
    assert (await c.stage_position()).name == "unknown"
    assert await c.get_index() == 1
    await c.reset_expmeter()
    sim.set_sky(1000.0)
    await asyncio.sleep(0.3)
    assert await c.expmeter_total() > 0


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
async def test_forbidden_commands_are_never_sent(sim, name):
    with pytest.raises(KiwiSpecRefused):
        await spec_client(sim).call(name)
    assert sim.commands == []


@pytest.mark.parametrize(
    ("name", "arg"),
    [
        ("i2stage_movef", 151.0),
        ("i2stage_movef", -1.0),
        ("i2stage_movef", float("nan")),
        ("i2stage_move", "in; rm"),
        ("set_file_name", "a b.fits"),
        ("write_header", "it's"),
        ("write_header", "back\\slash"),
        ("write_header", "x" * 1100),
        ("cell_heater_on", "now"),
        ("not_a_command", None),
    ],
)
def test_arguments_are_validated(name, arg):
    with pytest.raises(KiwiSpecRefused):
        format_command(name, arg)


async def raw(sim: KiwiSpecSim, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", sim.spec_port)
    writer.write(data)
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(), 2)
    writer.close()
    return reply


async def test_simulator_reproduces_the_legacy_quirks(sim):
    # exactly two tokens
    assert await raw(sim, b"get_index") == b"fail"
    # a quote switches repr() to double quotes: the command token is mangled
    assert await raw(sim, b"write_header it's") == b"fail"
    # a command routed to a method the server lacks kills the listener
    assert await raw(sim, b"thar_turn_on None") == b""
    await asyncio.sleep(0.1)
    assert sim.crashed
    with pytest.raises(KiwiSpecUnreachable):
        await spec_client(sim).cell_heater_temp()
    await sim.restart("spec", downtime=0.05)
    assert await spec_client(sim).cell_heater_temp() > 0


def test_header_chunks_never_start_with_whitespace():
    text = json.dumps({f"KEY{i}": ["a value with spaces", "and a comment"] for i in range(80)})
    chunks = header_chunks(text)
    assert "".join(chunks) == text
    assert all(len(c) <= HEADER_CHUNK for c in chunks)
    assert all(not c[0].isspace() for c in chunks)


async def test_long_header_round_trips(sim):
    c = spec_client(sim)
    sim.files["f.fits"] = {"header": {}}
    await c.call("set_file_name", "f.fits")
    header = {f"KEY{i:03d}": [f"value {i} with spaces", "comment"] for i in range(60)}
    text = json.dumps(header)
    assert len(text) > 3 * HEADER_CHUNK
    await c.write_header(text)
    assert sim.files["f.fits"]["header"] == {k: list(v) for k, v in header.items()}


async def test_hung_server_is_bounded(sim):
    sim.set_fault("hang")
    c = spec_client(sim, max_attempts=2, retry_delay=0.05)
    t0 = time.monotonic()
    with pytest.raises(KiwiSpecTimeout):
        await c.cell_heater_temp()
    assert time.monotonic() - t0 < 4.0
    # a move is never retried
    n = len(sim.commands)
    with pytest.raises(KiwiSpecError_):
        await c.stage_move("out")
    sim.set_fault("hang", False)
    assert len(sim.commands) <= n + 1
    assert await c.cell_heater_temp() > 0  # the wire lock was released


KiwiSpecError_ = (KiwiSpecTimeout, KiwiSpecUnreachable)


async def test_garbled_reply_is_retried_then_reported(sim):
    sim.set_fault("garble")
    c = spec_client(sim, max_attempts=3, retry_delay=0.01)
    with pytest.raises(KiwiSpecProtocolError):
        await c.cell_heater_temp()
    assert sim.commands.count("cell_heater_temp") == 3
    with pytest.raises(KiwiSpecProtocolError):
        await c.stage_move("out")
    assert sim.commands.count("i2stage_move") == 1


async def test_fail_reply_is_final(sim):
    c = spec_client(sim)
    with pytest.raises(KiwiSpecFailed):
        await c.call("save_image")  # nothing in the camera buffer
    assert sim.commands.count("save_image") == 1


# -- the SI camera protocol ------------------------------------------------------------


async def test_camera_client(sim):
    cam = SICameraClient("127.0.0.1", sim.camera_port, command_timeout=1.0)
    try:
        await cam.set_readout_mode(3)
        await cam.set_ccd_format(50, 2090, 1, 0, 2052, 1)
        await cam.set_cooler(True)
        assert await cam.ccd_temperature() == -90.0
        acq = await cam.start_acquisition(0.1, dark=True)
        await acq.wait(2.0)
        assert sim.buffer_image["dark"] is True
        assert sim.buffer_image["exptime_actual"] == pytest.approx(0.1, abs=0.05)
        # terminate ends the exposure early; the frame is still read out
        acq = await cam.start_acquisition(10.0, dark=False)
        await asyncio.sleep(0.2)
        await cam.terminate(timeout=1.0)
        await acq.wait(2.0)
        assert sim.buffer_image["terminated"] is True
        assert sim.buffer_image["exptime_actual"] < 1.0
    finally:
        await cam.close()


async def test_camera_restart_fails_the_acquisition_at_once(sim):
    cam = SICameraClient("127.0.0.1", sim.camera_port, command_timeout=1.0)
    try:
        acq = await cam.start_acquisition(10.0, dark=False)
        await sim.restart("camera", downtime=0.05)
        t0 = time.monotonic()
        with pytest.raises(Exception, match="connection"):
            await acq.wait(5.0)
        assert time.monotonic() - t0 < 1.0
        assert await cam.ccd_temperature() == -90.0  # reconnects
    finally:
        await cam.close()


# -- exposure-meter math --------------------------------------------------------------


def sampled(rate, T: float, dt: float) -> list[tuple[float, float]]:
    out, total, t = [(0.0, 0.0)], 0.0, 0.0
    n = round(T / dt)
    for i in range(n):
        a, b = i * dt, (i + 1) * dt
        total += rate((a + b) / 2) * dt
        t = b
        out.append((t, total))
    return out


@pytest.mark.parametrize(
    ("rate", "analytic"),
    [
        (lambda t: 100.0, 5.0),  # constant: the middle
        (lambda t: 10.0 * t, 2 * 10 / 3),  # linear ramp: 2T/3
        (lambda t: math.exp(-t / 2), None),  # clouds coming in
    ],
)
def test_flux_weighted_midpoint_matches_analytic(rate, analytic):
    T, tau = 10.0, 2.0
    if analytic is None:
        # integral of t e^{-t/tau} over [0, T] / integral of e^{-t/tau}
        num = tau**2 * (1 - math.exp(-T / tau) * (1 + T / tau))
        den = tau * (1 - math.exp(-T / tau))
        analytic = num / den
    mid, counts = flux_weighted_midpoint(sampled(rate, T, 0.01), 0.0, T)
    assert mid == pytest.approx(analytic, abs=1e-3)
    assert counts > 0


def test_flux_weighted_midpoint_clips_to_the_window():
    samples = [(0.0, 0.0), (1.0, 10.0), (2.0, 20.0), (3.0, 30.0)]
    mid, counts = flux_weighted_midpoint(samples, 0.5, 2.0)
    assert counts == pytest.approx(15.0)
    assert mid == pytest.approx(1.25)
    assert flux_weighted_midpoint([(0.0, 0.0), (1.0, 0.0)], 0, 1) == (None, 0.0)


# -- the service ---------------------------------------------------------------------


async def test_status(http, sim):
    r = await http.get("/status", params={"refresh": 1})
    s = r.json()
    assert r.status_code == 200
    assert s["readings"]["stage"]["value"] == {"mm": 11.0, "name": "in"}
    assert s["readings"]["ccd_temp"]["value"] == -90.0
    assert s["busy"] is None
    assert s["alarms"] == []


async def test_configure(http, sim):
    r = await http.post("/configure", json={"iodine": "out", "led": True, "heater_setpoint": 54})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == {"mm": 102.0, "name": "out"}
    assert sim.led_on and sim.heater_setpoint == 54.0
    r = await http.post("/configure", json={"iodine": 40.0, "led": False})
    assert r.json()["stage"]["mm"] == 40.0 and not sim.led_on
    assert (await http.post("/configure", json={"iodine": "sideways"})).status_code == 400


async def test_science_exposure(http, sim):
    sim.set_sky(2000.0)
    job = await run_job(
        http,
        "/expose",
        {"kind": "science", "exptime": 1.0, "object": "HD1234", "telescopes": ["T1", "T3"]},
    )
    assert job["state"] == "done", job
    r = job["results"][0]
    assert r["ended_by"] == "time"
    assert r["exptime"] == pytest.approx(1.0, abs=0.1)
    assert r["iodine"]["name"] == "in"
    # constant rate: the flux-weighted midpoint is the middle of the exposure
    assert r["flux_midpoint_s"] == pytest.approx(0.5, abs=0.08)
    assert r["expmeter_counts"] == pytest.approx(2000.0, rel=0.15)
    assert r["flux_midpoint_jd"] > 2460000
    saved = sim.files[r["file"]]
    assert r["file"].endswith(".HD1234.0001.fits")
    assert saved["dark"] is False
    assert saved["header"]["FIBFEED"][0] == "T1,T3"
    assert saved["header"]["FLUXMID"][0] == pytest.approx(r["flux_midpoint_jd"])
    assert saved["header"]["EXPTERM"][0] == "time"
    assert sim.heater_on


async def test_flux_midpoint_follows_a_ramp(http, sim):
    sim.set_sky(lambda t: 4000.0 * t)
    job = await run_job(
        http,
        "/expose",
        {"kind": "science", "exptime": 1.5, "telescopes": ["T2"], "iodine": False, "object": "x"},
    )
    r = job["results"][0]
    assert job["state"] == "done", job
    assert r["iodine"]["name"] == "out"
    # the sky clock starts a little before the shutter opens: allow for it
    assert 0.9 < r["flux_midpoint_s"] < 1.15  # 2T/3 = 1.0


async def test_exposure_meter_terminates(http, sim):
    sim.set_sky(1000.0)
    job = await run_job(
        http,
        "/expose",
        {
            "kind": "science",
            "exptime": 5.0,
            "telescopes": ["T1"],
            "target_counts": 500,
            "object": "x",
        },
    )
    r = job["results"][0]
    assert r["ended_by"] == "counts", job
    assert 0.3 < r["exptime"] < 1.0
    assert r["expmeter_counts"] == pytest.approx(r["exptime"] * 1000, rel=0.2)
    assert sim.files[r["file"]]["terminated"] is True
    assert sim.files[r["file"]]["header"]["MEXPFLUX"][0] == 500


async def test_stop_keeps_the_frame(http, sim):
    r = await http.post("/expose", json={"kind": "thar", "exptime": 5.0})
    job = r.json()
    await asyncio.sleep(0.6)
    assert (await http.post("/stop")).json()["stopped"] is True
    job = (await http.get(f"/jobs/{job['id']}", params={"wait": 5})).json()
    assert job["state"] == "done"
    assert job["results"][0]["ended_by"] == "stop"
    assert job["results"][0]["file"] in sim.files


async def test_abort_mid_exposure(http, sim):
    r = await http.post(
        "/expose", json={"kind": "science", "exptime": 20, "telescopes": ["T1"], "object": "x"}
    )
    job = r.json()
    await asyncio.sleep(0.6)
    assert sim.acquisition is not None
    t0 = time.monotonic()
    r = await http.post("/abort", json={"reason": "test"})
    assert time.monotonic() - t0 < 1.5
    a = r.json()
    assert a["aborted"] and a["ended"]
    assert a["job"]["state"] == "aborted"
    assert sim.files == {}  # discarded
    assert sim.acquisition is None
    # the spectrograph is free again
    job = await run_job(http, "/expose", {"kind": "bias"})
    assert job["state"] == "done", job


async def test_one_job_at_a_time(http, sim):
    r = await http.post("/expose", json={"kind": "dark", "exptime": 2})
    assert r.status_code == 202
    assert (await http.post("/expose", json={"kind": "bias"})).status_code == 409
    assert (await http.post("/configure", json={"led": True})).status_code == 409
    assert (await http.post("/backlight", json={"on": True})).status_code == 409
    assert (await http.get("/status")).json()["busy"].startswith("job")
    await http.post("/abort", json={"reason": "test"})


async def test_concurrent_submits_get_one_job(http, sim):
    rs = await asyncio.gather(
        *(http.post("/expose", json={"kind": "bias"}) for _ in range(5)),
    )
    assert sorted(r.status_code for r in rs) == [202, 409, 409, 409, 409]


async def test_calibration_sequence(http, sim):
    job = await run_job(
        http,
        "/calibrate",
        {
            "frames": [
                {"kind": "bias", "n": 2},
                {"kind": "dark", "exptime": 0.1},
                {"kind": "slitflat", "exptime": 0.1},
            ]
        },
    )
    assert job["state"] == "done", job
    files = [r["file"] for r in job["results"]]
    assert [f.split(".")[1] for f in files] == ["Bias", "Bias", "Dark", "slitFlat"]
    assert [sim.files[f]["dark"] for f in files] == [True, True, True, False]
    flat = job["results"][3]
    assert flat["led_on"] and flat["iodine"]["name"] == "flat"
    bad = await http.post("/calibrate", json={"frames": [{"kind": "science", "exptime": 1}]})
    assert bad.status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "science", "exptime": 1, "object": "x"},  # no telescopes
        {"kind": "science", "exptime": 1, "telescopes": ["T1"]},  # no object
        {"kind": "science", "exptime": 1, "telescopes": ["T9"], "object": "x"},
        {"kind": "science", "exptime": 1, "telescopes": ["T1", "T1"], "object": "x"},
        {"kind": "thar", "exptime": 1, "telescopes": ["T1"]},
        {"kind": "dark", "exptime": 1, "target_counts": 5},
        {"kind": "dark", "exptime": -1},
        {"kind": "dark", "exptime": 1e6},
        {"kind": "dark", "exptime": 1, "iodine": "out"},
        {"kind": "science", "exptime": 1, "telescopes": ["T1"], "object": "it's"},
        {"kind": "selfie", "exptime": 1},
    ],
)
async def test_bad_requests(http, body):
    assert (await http.post("/expose", json=body)).status_code == 400


async def test_backlight_pauses_the_exposure_meter(http, sim):
    r = await http.post("/backlight", json={"on": True})
    assert r.json() == {"backlight_on": True}
    assert sim.backlight_on and not sim.expmeter_logging
    await asyncio.sleep(0.2)
    assert not sim.expmeter_tripped
    await http.post("/backlight", json={"on": False})
    assert not sim.backlight_on and sim.expmeter_logging


async def test_expmeter_disabled_falls_back_to_time(sim):
    sim.config.expmeter_enabled = False
    sim.expmeter_total = None
    sim.expmeter_logging = False
    server = await KiwiSpecServer(service_config(sim)).start(status_loop=False)
    try:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as http:
            # the legacy server answers reset with success (the attribute is
            # created) but nothing ever adds to it
            job = await run_job(
                http,
                "/expose",
                {
                    "kind": "science",
                    "exptime": 0.5,
                    "telescopes": ["T1"],
                    "target_counts": 9,
                    "object": "x",
                },
            )
    finally:
        await server.stop()
    r = job["results"][0]
    assert job["state"] == "done"
    assert r["ended_by"] == "time"
    assert r["flux_midpoint_s"] is None


# -- faults ----------------------------------------------------------------------------


async def test_hung_server_fails_bounded_and_releases(http, sim, service):
    sim.set_fault("hang")
    t0 = time.monotonic()
    job = await run_job(http, "/expose", {"kind": "bias"}, wait=20)
    assert job["state"] == "failed", job
    assert time.monotonic() - t0 < 10
    r = await http.post("/configure", json={"led": True})
    assert r.status_code == 503  # not 409: nothing is held
    sim.set_fault("hang", False)
    job = await run_job(http, "/expose", {"kind": "bias"})
    assert job["state"] == "done", job


async def test_hung_camera_abort_is_bounded(http, sim, service):
    r = await http.post("/expose", json={"kind": "dark", "exptime": 10})
    await asyncio.sleep(0.4)
    sim.set_fault("camera_hang")
    t0 = time.monotonic()
    a = (await http.post("/abort", json={"reason": "weather"})).json()
    elapsed = time.monotonic() - t0
    assert elapsed < service.config.abort_timeout + 2
    assert a["job"]["state"] in ("aborted", "failed")
    status = (await http.get("/status")).json()
    assert status["busy"] is None
    sim.set_fault("camera_hang", False)
    await sim.restart("camera", downtime=0.05)
    job = await run_job(http, "/expose", {"kind": "bias"})
    assert job["state"] == "done", job
    del r


async def test_spec_server_restart_mid_exposure(http, sim):
    sim.set_sky(1000.0)
    r = await http.post(
        "/expose", json={"kind": "science", "exptime": 1.5, "telescopes": ["T1"], "object": "x"}
    )
    job = r.json()
    await asyncio.sleep(0.5)
    await sim.restart("spec", downtime=0.4)
    job = (await http.get(f"/jobs/{job['id']}", params={"wait": 10})).json()
    # the camera is a separate program: the frame completes and is saved once
    # the server is back; the exposure meter's total did not survive
    assert job["state"] == "done", job
    res = job["results"][0]
    assert res["file"] in sim.files
    assert any("exposure meter" in w for w in res["warnings"])


async def test_camera_restart_mid_exposure(http, sim):
    r = await http.post("/expose", json={"kind": "dark", "exptime": 5})
    job = r.json()
    await asyncio.sleep(0.4)
    t0 = time.monotonic()
    await sim.restart("camera", downtime=0.05)
    job = (await http.get(f"/jobs/{job['id']}", params={"wait": 10})).json()
    assert job["state"] == "failed" and "connection" in job["error"], job
    assert time.monotonic() - t0 < 2
    job = await run_job(http, "/expose", {"kind": "bias"})
    assert job["state"] == "done", job


async def test_warm_ccd_refuses_exposures(http, sim):
    sim.set_fault("ccd_warm")
    job = await run_job(http, "/expose", {"kind": "bias"})
    assert job["state"] == "failed" and "CCD temperature" in job["error"]
    assert sim.camera_commands.count(1037) == 0  # never acquired
    status = (await http.get("/status", params={"refresh": 1})).json()
    assert any("CCD temperature" in a for a in status["alarms"])


async def test_stuck_stage_fails_bounded(http, sim, service):
    sim.set_fault("stage_stuck")
    t0 = time.monotonic()
    job = await run_job(http, "/expose", {"kind": "thar", "exptime": 0.1})
    assert job["state"] == "failed" and "stuck" in job["error"], job
    assert time.monotonic() - t0 < service.config.stage_timeout + 2
    assert sim.files == {}


async def test_garbled_replies_show_in_status(http, sim):
    sim.set_fault("garble")
    status = (await http.get("/status", params={"refresh": 1})).json()
    assert status["readings"]["heater_temp"]["error"].startswith("KiwiSpecProtocolError")
    assert status["alarms"]


# -- the abort hook (sequencer cleanup) ------------------------------------------------


async def test_abort_hook_for_the_sequencer_cleanup(service, sim):
    """Weather goes unsafe mid-spectrum: the sequencer's cleanup calls the
    hook, which ends the exposure (frame discarded) and frees the spectrograph."""
    remote = KiwiSpecRemote(service.url)
    job = await remote.expose(kind="science", exptime=30, telescopes=["T1", "T2"], object="HD1")
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    assert await remote.abort("weather unsafe") is True
    assert time.monotonic() - t0 < 2
    job = await remote.job(job["id"])
    assert job["state"] == "aborted" and job["abort_reason"] == "weather unsafe"
    assert (await remote.status())["busy"] is None
    # nothing running: still True
    assert await remote.abort("again") is True


async def test_abort_hook_never_raises():
    remote = KiwiSpecRemote("http://127.0.0.1:9")  # nothing listens there
    t0 = time.monotonic()
    assert await remote.abort("weather unsafe", timeout=2) is False
    assert time.monotonic() - t0 < 3


# -- the hardware-safety gate ------------------------------------------------------------


TEST_NET = "192.0.2.10"  # RFC 5737 documentation address; never contacted


def gate_config(sim_like_port: int = 1, **over) -> KiwiSpecConfig:
    return KiwiSpecConfig.from_dict(
        {
            "spectrograph": {"host": TEST_NET, "port": sim_like_port},
            "camera": {"host": TEST_NET, "port": sim_like_port},
            **over,
        }
    )


def test_non_loopback_is_refused_without_both_permissions():
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecServer(gate_config())
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecServer(gate_config(), hardware=True)
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecServer(gate_config(allow_hardware=True))
    with pytest.raises(HardwareNotAllowed):
        SpecServerClient(TEST_NET)
    with pytest.raises(HardwareNotAllowed):
        SICameraClient(TEST_NET)
    # both: constructed (nothing is contacted until it is started)
    server = KiwiSpecServer(gate_config(allow_hardware=True), hardware=True)
    assert server.kiwispec.spec.host == TEST_NET


def test_main_exits_on_a_non_loopback_config(tmp_path):
    cfg = tmp_path / "k.json"
    cfg.write_text(
        json.dumps(
            {
                "listen": "127.0.0.1:0",
                "spectrograph": {"host": TEST_NET},
                "camera": {"host": TEST_NET},
            }
        )
    )
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg)])
    assert exc.value.code == 2


def test_unknown_config_keys_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        KiwiSpecConfig.from_dict(
            {"spectrograph": {"host": "127.0.0.1"}, "camera": {"host": "127.0.0.1"}, "x": 1}
        )


def test_example_config_loads_and_is_loopback():
    from pathlib import Path

    path = Path(__file__).parents[2] / "deploy" / "kiwispec.example.json"
    config = KiwiSpecConfig.from_dict(json.loads(path.read_text()))
    server = KiwiSpecServer(config)
    assert server.kiwispec.spec.host == "127.0.0.1"
