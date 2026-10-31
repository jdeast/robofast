"""The emergency close (docs/console.md, "Emergency close"): the console
asks the supervisor; if the supervisor is down or slow, it closes through
the dome's close-only channel (robofast_core.emergency), which is latched,
audited and alarmed, and can never open anything.

Unit tests run the console on fakes (an ``httpx.MockTransport``); the
integration tests run the real Aqawan Dome server on the PAC simulator with
the supervisor dead, all on loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time

import httpx
import pytest

from robofast_core.alpaca_client import InvalidOperationException
from robofast_core.devices.aqawan_server import CLOSED, OPEN
from robofast_core.emergency import (
    PATH,
    EmergencyConfig,
    EmergencyFailed,
    close_dome,
    hash_token,
    main,
    new_token,
    read_token,
)
from robofast_core.testing.checks import wait_until

from . import test_aqawan_server as _aqawan
from . import test_console as _console
from .test_aqawan_server import until_status
from .test_console import Fakes

# The fixtures, shared with those modules.
make_rig = _aqawan.make_rig
make_console = _console.make_console

TOKEN = "emergency-token-for-tests-0123456789"


def token_file(tmp, token: str = TOKEN):
    p = tmp / "emergency.token"
    p.write_text(token + "\n")
    os.chmod(p, 0o600)
    return p


def enclosure(tmp, **em):
    token_file(tmp)
    spec = {"config": "aqawan1.json", "spectrograph": "http://spec.test"}
    if em is not None:
        spec["emergency_close"] = {"token_file": "emergency.token", **em}
    return {"aqawan1": spec}


class DomeFakes(Fakes):
    """The console's fakes plus the dome's emergency endpoint (127.0.0.1:9)
    and a supervisor that may be down or slow."""

    supervisor_down: bool = False
    supervisor_delay: float = 0.0
    dome_answer: int = 200

    async def ahandler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "127.0.0.1" and path == PATH:
            body = json.loads(request.content)
            self.calls.append((request.method, f"dome{path}", body))
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return httpx.Response(401, json={"ok": False, "error": "not authorized"})
            if self.dome_answer != 200:
                return httpx.Response(self.dome_answer, json={"ok": False, "error": "PAC refused"})
            return httpx.Response(200, json={"ok": True, "closing": True, "shutter": 3})
        if host == "sup.test" and path == "/close":
            if self.supervisor_down:
                raise httpx.ConnectError("refused", request=request)
            await asyncio.sleep(self.supervisor_delay)
        return self.handler(request)


async def console_with_dome(make_console, fakes: DomeFakes, **em):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fakes.ahandler))
    rig = await make_console(http=http, enclosures=enclosure(make_console.tmp, **em))
    rig.fakes = fakes
    return rig


@pytest.fixture
def mc(make_console, tmp_path):
    make_console.tmp = tmp_path
    return make_console


# -- the console's side (fakes) ----------------------------------------------------------


async def test_the_supervisor_closes_when_it_answers(mc):
    fakes = DomeFakes()
    rig = await console_with_dome(mc, fakes)
    await rig.login("alice")
    r = await rig.act("emergency_close", reason="", confirm=True)
    assert r.status_code == 200, r.text
    res = r.json()["result"]["enclosures"]["aqawan1"]
    assert res["closing"] is True and res["via"] == "supervisor" and res["lease_revoked"] is True
    assert fakes.posts() == [
        ("sup.test/close", {"operator": "alice", "reason": "EMERGENCY: emergency close"})
    ]
    # Every telescope held (stopped), so nothing asks for a lease again.
    (cmd,) = [json.loads(p.read_text()) for p in (rig.control / "inbox").iterdir()]
    assert cmd["action"] == "stop" and cmd["telescopes"] == ["t1", "t2"]
    alarm = (await rig.status())["alarms"][0]
    assert alarm["source"] == "console" and "emergency close by alice" in alarm["text"]
    line = rig.audit()[-1]
    assert line["action"] == "emergency_close" and line["status"] == 200


async def test_supervisor_down_closes_through_the_dome(mc):
    fakes = DomeFakes()
    fakes.supervisor_down = True
    rig = await console_with_dome(mc, fakes)
    await rig.login("alice")
    r = await rig.act("emergency_close", reason="smoke from the ridge", confirm=True)
    assert r.status_code == 200, r.text
    res = r.json()["result"]["enclosures"]["aqawan1"]
    assert res["via"] == "dome" and res["closing"] is True
    assert "ConnectError" in res["supervisor"]
    (_, path, body) = fakes.calls[-1]
    assert path == f"dome{PATH}" and body["operator"] == "alice"
    assert body["reason"] == "smoke from the ridge" and "ConnectError" in body["via"]
    alarm = (await rig.status())["alarms"][0]
    assert alarm["level"] == "critical" and "directly to the dome" in alarm["text"]
    assert TOKEN not in json.dumps(await rig.status())
    assert TOKEN not in (rig.tmp / "log" / "console-audit.jsonl").read_text()


async def test_a_slow_supervisor_falls_back_after_the_timeout(mc):
    fakes = DomeFakes()
    fakes.supervisor_delay = 5.0
    rig = await console_with_dome(mc, fakes)
    rig.app.config.emergency_timeout = 0.3
    await rig.login("alice")
    t0 = time.monotonic()
    r = await rig.act("emergency_close", confirm=True)
    assert time.monotonic() - t0 < 3.0
    res = r.json()["result"]["enclosures"]["aqawan1"]
    assert res["via"] == "dome" and "no answer within 0.3 s" in res["supervisor"]


async def test_direct_skips_the_supervisor(mc):
    fakes = DomeFakes()
    rig = await console_with_dome(mc, fakes)
    await rig.login("alice")
    r = await rig.act("emergency_close", direct=True, reason="supervisor hung", confirm=True)
    res = r.json()["result"]["enclosures"]["aqawan1"]
    assert res["via"] == "dome" and "skipped" in res["supervisor"]
    assert not [c for c in fakes.calls if c[1] == "sup.test/close"]


async def test_no_fallback_configured_reports_the_failure(mc):
    fakes = DomeFakes()
    fakes.supervisor_down = True
    rig = await console_with_dome(mc, fakes, fallback="none")
    await rig.login("alice")
    r = await rig.act("emergency_close", confirm=True)
    assert r.status_code == 502 and "NOT accepted" in r.json()["error"]
    assert not [c for c in fakes.calls if c[1].startswith("dome")]
    alarm = (await rig.status())["alarms"][0]
    assert alarm["level"] == "critical" and "FAILED" in alarm["text"]


async def test_the_dome_refusing_is_a_failure(mc):
    fakes = DomeFakes()
    fakes.supervisor_down = True
    fakes.dome_answer = 502
    rig = await console_with_dome(mc, fakes)
    await rig.login("alice")
    r = await rig.act("emergency_close", confirm=True)
    assert r.status_code == 502 and "PAC refused" in r.json()["error"]


async def test_emergency_close_needs_operator_and_confirm_but_no_rate_limit(mc):
    fakes = DomeFakes()
    rig = await console_with_dome(mc, fakes)
    await rig.login("watcher")
    assert (await rig.act("emergency_close", confirm=True)).status_code == 403
    await rig.login("alice")
    assert (await rig.act("emergency_close")).status_code == 400  # no confirm
    # The action limit (burst 10) does not block an emergency close.
    for _ in range(12):
        await rig.act("pause", telescope="t1", reason="x")
    assert (await rig.act("pause", telescope="t1", reason="x")).status_code == 429
    assert (await rig.act("emergency_close", confirm=True)).status_code == 200


async def test_config_validation(tmp_path):
    from robofast_core.console.aggregate import ConsoleEnclosure
    from tests.core.test_console import write_site

    write_site(tmp_path)
    token_file(tmp_path)
    ok = ConsoleEnclosure.from_spec(
        "a",
        {"config": "aqawan1.json", "emergency_close": {"token_file": "emergency.token"}},
        tmp_path,
    )
    assert ok.emergency_fallback == "dome" and ok.dome_url == "http://127.0.0.1:9"
    for bad in ({"fallback": "dome"}, {"token_file": "x", "fallback": "maybe"}, {"x": 1}):
        with pytest.raises(ValueError):
            ConsoleEnclosure.from_spec(
                "a", {"config": "aqawan1.json", "emergency_close": bad}, tmp_path
            )


def test_token_helpers(tmp_path, capsys):
    token, digest = new_token()
    assert hash_token(token + "\n") == digest and len(digest) == 64
    p = tmp_path / "t"
    p.write_text(token)
    os.chmod(p, 0o644)
    with pytest.raises(ValueError, match="owner only"):
        read_token(p)
    os.chmod(p, 0o600)
    assert read_token(p) == token
    main(["token"])
    out = json.loads(capsys.readouterr().out)
    assert hash_token(out["token"]) == out["sha256"]
    assert EmergencyConfig.from_dict({"token_sha256": digest}).token_sha256 == [digest]
    for bad in ({"token_sha256": "abc"}, {"token_sha256": []}, {"token_sha256": digest, "x": 1}):
        with pytest.raises(ValueError):
            EmergencyConfig.from_dict(bad)


# -- the dome's side: the real Aqawan server on the PAC simulator --------------------------


def dead_url() -> str:
    """A loopback URL nothing listens on (a dead supervisor)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def emergency_rig(make_rig, tmp_path):
    return make_rig(
        {"heartbeat_timeout": None},
        emergency_close={
            "token_sha256": [hash_token(TOKEN)],
            "log": str(tmp_path / "emergency.jsonl"),
        },
    )


async def test_the_dome_server_closes_latches_and_logs(make_rig, tmp_path):
    rig = emergency_rig(make_rig, tmp_path)
    dome = rig.dome()
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)
    url = f"http://{rig.server.address}"
    async with httpx.AsyncClient() as http:
        with pytest.raises(EmergencyFailed, match="401"):
            await close_dome(http, url, "wrong", operator="mallory", reason="x")
        r = await http.put(url + PATH, json={})  # no token at all
        assert r.status_code == 401 and rig.sim.open()
        r = await http.get(url + PATH)
        assert r.status_code == 405
        # Nothing next to it opens.
        for path in ("/robofast/v1/emergency_open", "/robofast/v1/open"):
            r = await http.put(url + path, headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 400
        reply = await close_dome(
            http, url, TOKEN, operator="alice", reason="smoke", via="console (test)"
        )
    assert reply["ok"] and reply["latched"]["operator"] == "alice"
    await until_status(dome, CLOSED, 5)
    faults = json.loads(await dome.call("Action", "Faults", ""))
    assert faults["fault"] is True and "emergency close by alice" in faults["faults"]
    with pytest.raises(InvalidOperationException, match="emergency close"):
        await dome.call("OpenShutter")
    log = [json.loads(x) for x in (tmp_path / "emergency.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log] == ["emergency_close"]
    assert log[0]["ok"] and log[0]["operator"] == "alice" and TOKEN not in json.dumps(log)

    # The latch survives a restart of the dome server.
    rig2 = emergency_rig(make_rig, tmp_path)
    assert rig2.server.aqawan.gate.latched is not None

    # ClearFaults (the supervisor's, with the enclosure closed) releases it.
    reply = await dome.call("Action", "ClearFaults", "alice: smoke gone")
    assert "latch cleared" in reply
    faults = json.loads(await dome.call("Action", "Faults", ""))
    assert faults["fault"] is False and faults["emergency_close"] is None
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)
    log = [json.loads(x) for x in (tmp_path / "emergency.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log] == ["emergency_close", "latch_cleared"]


async def test_without_the_config_there_is_no_channel(make_rig):
    rig = make_rig({"heartbeat_timeout": None})
    async with httpx.AsyncClient() as http:
        r = await http.put(
            f"http://{rig.server.address}{PATH}", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert r.status_code == 404


async def test_supervisor_dead_the_console_closes_the_sim_enclosure(make_rig, mc, tmp_path):
    """The whole path: a dead supervisor, the console's emergency close, the
    real dome server and PAC simulator; then the console still cannot open."""
    rig = emergency_rig(make_rig, tmp_path / "dome")
    dome = rig.dome()
    await dome.call("OpenShutter")
    await until_status(dome, OPEN, 5)
    console = await mc(
        http=httpx.AsyncClient(),
        supervisor_url=dead_url(),
        enclosures=enclosure(tmp_path),
    )
    # Point the enclosure's dome at the real server.
    console.app.enclosures["aqawan1"].dome_url = f"http://{rig.server.address}"
    await console.login("alice")
    t0 = time.monotonic()
    r = await console.act("emergency_close", reason="supervisor crashed, rain", confirm=True)
    assert r.status_code == 200, r.text
    res = r.json()["result"]["enclosures"]["aqawan1"]
    assert res["via"] == "dome"
    await wait_until(rig.sim.closed, 5)
    assert time.monotonic() - t0 < 5
    assert "CLOSE_SEQUENTIAL" in rig.sim.commands()
    # The console has no way to open: nothing it can send reaches OpenShutter.
    for action in ("resume", "clear_faults", "clear_lease"):
        await console.act(action, reason="try to reopen", confirm=True)
    await asyncio.sleep(0.5)
    assert rig.sim.closed()
    assert rig.sim.commands().count("OPEN_SHUTTER_1") == 1  # only the test's own open
    statuses = [x for x in console.audit() if x.get("action") == "emergency_close"]
    assert statuses and statuses[-1]["status"] == 200


def test_the_page_has_the_button_and_its_dialog():
    from robofast_core.console.page import render_page

    page = render_page("n0nce")
    assert 'id="emergency"' in page and 'id="emergency-dialog"' in page
    assert "/console/api/emergency_close" in page and "does NOT wait for the telescopes" in page


async def test_clear_spectrograph_goes_through_the_command_box(mc):
    """(Here because it is the other new console button of this change.)"""
    rig = await mc()
    await rig.login("watcher")
    assert (await rig.act("clear_spectrograph", reason="x", confirm=True)).status_code == 403
    await rig.login("alice")
    assert (await rig.act("clear_spectrograph", reason="fixed")).status_code == 400  # confirm
    r = await rig.act("clear_spectrograph", reason="camera server restarted", confirm=True)
    assert r.status_code == 200, r.text
    (cmd,) = [json.loads(p.read_text()) for p in (rig.control / "inbox").iterdir()]
    assert cmd["action"] == "clear_spectrograph" and cmd["telescopes"] == []
    assert cmd["operator"] == "alice" and cmd["reason"] == "camera server restarted"
    line = rig.audit()[-1]
    assert line["action"] == "clear_spectrograph" and line["status"] == 200
    st = await rig.status()
    assert st["spectrograph"]["queue"] == []
