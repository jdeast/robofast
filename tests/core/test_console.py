"""The observer console (docs/console.md): what it gathers, how it signs
observers in, what each action does and records, and that it can never open
an enclosure.

Unit tests run the console's real HTTP server on a loopback port against
fake services (an ``httpx.MockTransport``); the integration tests run it
against a real safety supervisor (over its HTTP API) and a real sequencer on
fake devices.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from robofast_core.api_auth import TokenStore
from robofast_core.console.hold import OperatorHold, read_holds, send_command
from robofast_core.console.page import render_page
from robofast_core.console.server import (
    Console,
    ConsoleHttpServer,
    NotAllowed,
    SupervisorClient,
    build_enclosures,
    load_console_config,
    main,
)
from robofast_core.plan import CallablePlan, Idle, Observation, StaticPlan
from robofast_core.sequencer_service import write_snapshot
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.checks import wait_until
from tests.core.test_request_api import plan_entry

LEASE_TOKEN = "e0e0e0e0e0e0.7"

SUPERVISOR = {
    "safe": True,
    "reason": "weather safe",
    "open_allowed": True,
    "fault": "enclosure reports Fault (motor overcurrent)",
    "shutter_hold_for": None,
    "shutter": "OPEN",
    "lease": {
        "token": LEASE_TOKEN,
        "id": 7,
        "holder": "aqawan1-sequencer",
        "ttl": 60,
        "live": True,
        "opened": True,
        "revoked": False,
        "reason": None,
        "released": False,
        "quarantined": False,
    },
    "orphaned_for": None,
    "alarms": ["cannot read shutter: timeout"],
    "enforced_ago": 0.4,
    "pinged_ago": 3.5,
    "epoch": "e0e0e0e0e0e0",
}
WEATHER = {
    "time": "2026-11-15T07:00:00+00:00",
    "safe": True,
    "state": "safe",
    "limits": "close",
    "reasons": ["would close: rain (shadow)"],
    "rain_until": None,
    "would_close": ["rain (shadow)"],
    "sources": {
        "ridge": {
            "state": "safe",
            "required": True,
            "reasons": [],
            "age": 12.0,
            "fresh": True,
            "values": {"wind_speed": 4.5, "humidity": 31.0, "zz_other": 1.0},
            "notes": [],
            "field_times": {},
        }
    },
}
GUIDER = {
    "state": "guiding",
    "reason": "",
    "telemetry": {"residual_rms": 0.31, "flux": 12000.0, "fwhm": 2.1, "loop_rate": 4.0},
    "alarms": [{"kind": "residual_high", "message": "residual rms 1.20 px", "utc": "x", "t": 1}],
}
SPECTROGRAPH = {
    "name": "kiwispec",
    "busy": "expose",
    "job": {
        "id": 3,
        "kind": "expose",
        "state": "running",
        "phase": "exposing",
        "error": None,
        "frames": [{"kind": "science", "exptime": 1200.0, "object": "HD 1", "target_counts": 5e6}],
        "results": [],
        "abort_reason": None,
        "created": None,
        "ended": None,
    },
    "readings": {"expmeter_total": {"value": 123456.0}, "ccd_temp": {"value": -90.1}},
    "led_on": False,
    "backlight_on": False,
    "alarms": ["spectrograph pressure 0.01 mbar > 0.006"],
}


@dataclass
class Fakes:
    """The services the console reads, behind one MockTransport."""

    supervisor: dict | None = field(default_factory=lambda: json.loads(json.dumps(SUPERVISOR)))
    weather: dict | None = field(default_factory=lambda: dict(WEATHER))
    guider: dict | None = field(default_factory=lambda: dict(GUIDER))
    spectrograph: dict | None = field(default_factory=lambda: dict(SPECTROGRAPH))
    calls: list[tuple[str, str, dict]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, f"{request.url.host}{request.url.path}", body))
        host, path = request.url.host, request.url.path
        doc = {
            "sup.test": self.supervisor,
            "wx.test": self.weather,
            "guider.test": self.guider,
            "spec.test": self.spectrograph,
        }.get(host)
        if doc is None:
            raise httpx.ConnectError("down", request=request)
        if host == "sup.test":
            if path == "/timeline":
                return httpx.Response(200, json=[{"time": "t", "kind": "event", "what": "x"}])
            if path == "/close":
                return httpx.Response(200, json={"revoked": True})
            if path == "/faults/clear":
                return httpx.Response(200, json={"reply": "cleared"})
            if path.startswith("/leases/") and path.endswith("/clear"):
                lease = doc["lease"]
                return httpx.Response(200, json={**lease, "released_now": True})
        return httpx.Response(200, json=doc)

    def posts(self) -> list[tuple[str, dict]]:
        return [(p, b) for m, p, b in self.calls if m == "POST"]


def write_site(tmp: Path, *, supervisor_url: str = "http://sup.test", **console) -> Path:
    plan = plan_entry(record={"dir": "plans", "name": "aqawan1"})
    enclosure = {
        "name": "aqawan1",
        "supervisor_url": supervisor_url,
        "devices": {"dome": "127.0.0.1:9", "safetymonitor": "wx.test:80"},
        "telescopes": {
            "t1": {
                "telescope": "127.0.0.1:9/0",
                "camera": "127.0.0.1:9/0",
                "guider": "http://guider.test",
            },
            "t2": {"telescope": "127.0.0.1:9/0", "camera": "127.0.0.1:9/0"},
        },
        "plan": plan,
        "status_file": "seq-status.json",
        "operator_control": "control",
    }
    (tmp / "aqawan1.json").write_text(json.dumps(enclosure))
    cfg = {
        "listen": "127.0.0.1:0",
        "tokens": "secret/tokens.json",
        "audit_log": "log/console-audit.jsonl",
        "acks": "state/acks.json",
        "enclosures": {"aqawan1": {"config": "aqawan1.json", "spectrograph": "http://spec.test"}},
        "hold_ack_timeout": 0.3,
        **console,
    }
    (tmp / "console.json").write_text(json.dumps(cfg))
    return tmp / "console.json"


@dataclass
class Rig:
    tmp: Path
    app: Console
    server: ConsoleHttpServer
    tokens: dict[str, str]
    store: TokenStore
    fakes: Fakes | None
    client: httpx.AsyncClient
    csrf: str | None = None

    async def login(self, who: str) -> httpx.Response:
        r = await self.client.post("/console/login", json={"token": self.tokens[who]})
        if r.status_code == 200:
            self.csrf = r.json()["csrf"]
        return r

    async def act(self, action: str, csrf: bool = True, **body) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf} if csrf and self.csrf else {}
        return await self.client.post(f"/console/api/{action}", json=body, headers=headers)

    async def status(self) -> dict:
        r = await self.client.get("/console/api/status")
        assert r.status_code == 200, r.text
        return r.json()["enclosures"]["aqawan1"]

    def audit(self) -> list[dict]:
        path = self.tmp / "log" / "console-audit.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    @property
    def control(self) -> Path:
        return self.tmp / "control"


@pytest.fixture
async def make_console(tmp_path):
    made: list[Rig] = []

    async def build(*, fakes: Fakes | None = None, http=None, **console) -> Rig:
        path = write_site(tmp_path, **console)
        config, spec, base = load_console_config(path)
        store = TokenStore(config.tokens)
        tokens = {
            name: store.add(name, scopes)[1]
            for name, scopes in {
                "alice": ["read", "operator", "submit", "too"],
                "watcher": ["read"],
                "tom": ["submit"],
                "boss": ["admin"],
            }.items()
        }
        fakes = fakes if fakes is not None or http is not None else Fakes()
        if http is None:
            http = httpx.AsyncClient(transport=httpx.MockTransport(fakes.handler))
        app = Console(build_enclosures(spec, base), store, config=config, http=http)
        server = await ConsoleHttpServer(app).start()
        client = httpx.AsyncClient(base_url=server.url, timeout=30)
        rig = Rig(tmp_path, app, server, tokens, store, fakes, client)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        await rig.client.aclose()
        await rig.server.stop()
        await rig.app.aclose()
        await rig.app.http.aclose()


def snapshot_doc(**over) -> dict:
    now = datetime.now(UTC)
    return {
        "time": now.isoformat(),
        "max_age": 15,
        "sequencer": "aqawan1-sequencer",
        "generation": 4,
        "quarantined": False,
        "needs_operator": True,
        "telescopes": {
            "t1": {
                "state": "observing",
                "exposing": True,
                "consecutive_errors": 0,
                "current": {"request": "base-r", "observation": "base-c#0"},
            },
            "t2": {"state": "gave_up", "exposing": False, "consecutive_errors": 3},
        },
        "values": {
            "t1.tel.alt": {"value": 61.5, "unit": "deg", "age": 2.0, "stale": False},
            "t1.tel.az": {"value": 140.25, "unit": "deg", "age": 2.0, "stale": False},
            "t1.focuser.position": {"value": 23150, "unit": "step", "age": 2.0, "stale": False},
            "t1.camera.last_start": {"value": "2026-11-15T06:59:00", "unit": "", "stale": False},
            "t9.tel.alt": {"value": 1.0, "unit": "deg", "stale": False},
        },
        **over,
    }


def write_record(tmp: Path) -> None:
    d = tmp / "plans"
    d.mkdir(exist_ok=True)
    lines = [
        {"t": "2026-11-15T06:00:00+00:00", "kind": "snapshot", "requests": ["huge"]},
        {
            "t": "2026-11-15T06:01:00+00:00",
            "kind": "decision",
            "telescope": "t1",
            "request": "base-r",
            "config": "base-c",
            "score": 1.5,
            "terms": {"priority": 1},
            "runners_up": [{"request": "x"}],
            "rejected": [],
        },
        {
            "t": "2026-11-15T06:31:00+00:00",
            "kind": "outcome",
            "telescope": "t1",
            "outcome": "complete",
            "frames": 1,
        },
    ]
    (d / "aqawan1-2026-11-14.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (d / "aqawan1-2026-11-13.jsonl").write_text(json.dumps({"kind": "idle", "t": "old"}) + "\n")


# -- what it gathers ----------------------------------------------------------------------


async def test_aggregation_from_fake_services(make_console):
    rig = await make_console()
    (rig.tmp / "seq-status.json").write_text(json.dumps(snapshot_doc()))
    write_record(rig.tmp)
    send_command(rig.control, "pause", ["t2"], operator="alice", reason="flat field")
    hold = OperatorHold(StaticPlan([], ["t1", "t2"]), ["t1", "t2"], rig.control)
    hold.take_in()
    assert (await rig.login("watcher")).status_code == 200
    s = await rig.status()
    assert LEASE_TOKEN not in json.dumps(s)  # the lease token never leaves the server
    w = s["weather"]
    assert w["safe"] is True and w["bridge"]["would_close"] == ["rain (shadow)"]
    vals = w["bridge"]["sources"]["ridge"]["values"]
    assert list(vals) == ["humidity", "wind_speed", "zz_other"]  # key values first
    e = s["enclosure"]
    assert e["shutter"] == "OPEN" and e["heartbeat_age"] == 3.5
    assert e["lease"]["holder"] == "aqawan1-sequencer" and e["lease"]["live"]
    assert e["holds"]["t2"]["operator"] == "alice"
    t1, t2 = s["telescopes"]["t1"], s["telescopes"]["t2"]
    assert t1["state"] == "observing" and t1["current"]["request"] == "base-r"
    assert t1["values"]["alt"]["value"] == 61.5 and t1["values"]["focus"]["value"] == 23150
    assert t1["values"]["last_frame_start"]["value"].startswith("2026-11-15")
    assert t1["guider"]["state"] == "guiding" and t1["guider"]["rms"] == 0.31
    assert t2["hold"]["action"] == "pause" and "guider" not in t2
    sp = s["spectrograph"]
    assert sp["job"]["exptime"] == 1200.0 and sp["job"]["frames"] == 1
    assert sp["expmeter_total"] == 123456.0
    p = s["plan"]
    assert p["record"] == "aqawan1-2026-11-14.jsonl"
    assert [d["kind"] for d in p["recent"]] == ["decision", "outcome"]  # no snapshot
    assert p["recent"][0]["runners_up"] == 1 and "terms" not in p["recent"][0]
    assert [u["id"] for u in p["upcoming"]] == ["base"]
    assert s["timeline"] == [{"time": "t", "kind": "event", "what": "x"}]
    texts = {a["text"]: a for a in s["alarms"]}
    assert "cannot read shutter: timeout" in texts
    assert "enclosure fault: enclosure reports Fault (motor overcurrent)" in texts
    assert "would close: rain (shadow)" in texts
    assert (
        "t2 gave up for the night" in texts
        and texts["t2 gave up for the night"]["level"] == "critical"
    )
    assert "sequencer needs an operator" in texts
    assert "t1 guider: residual_high" in texts
    assert "spectrograph: spectrograph pressure 0.01 mbar > 0.006" in texts
    assert len({a["id"] for a in s["alarms"]}) == len(s["alarms"])


async def test_unreachable_services_are_alarms_not_errors(make_console):
    rig = await make_console(fakes=Fakes(None, None, None, None))
    await rig.login("watcher")
    s = await rig.status()
    assert s["enclosure"] == {
        "reachable": False,
        "holds": {},
        "holds_updated": None,
        "operator_control": True,
        "emergency": {"fallback": "none", "park_before_close": False},
    }
    assert s["weather"]["bridge"] == {"reachable": False} and s["weather"]["safe"] is None
    assert s["spectrograph"] == {"reachable": False}
    assert s["telescopes"]["t1"]["guider"] == {"reachable": False}
    assert s["telescopes"]["t1"]["state"] == "unknown"
    levels = {a["source"]: a["level"] for a in s["alarms"]}
    assert levels["supervisor"] == "critical" and levels["spectrograph"] == "warning"
    assert levels["sequencer"] == "warning"  # no snapshot


async def test_a_stale_snapshot_shows_nothing_as_current(make_console):
    rig = await make_console()
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    (rig.tmp / "seq-status.json").write_text(json.dumps(snapshot_doc(time=old)))
    await rig.login("watcher")
    s = await rig.status()
    assert s["sequencer"]["running"] is False
    assert s["telescopes"]["t1"]["state"] == "unknown" and s["telescopes"]["t1"]["values"] == {}


# -- the page --------------------------------------------------------------------------------


class _Dom(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, str] = {}
        self.tags: list[tuple[str, dict]] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            assert a["id"] not in self.ids, f"duplicate id {a['id']}"
            self.ids[a["id"]] = tag


async def test_the_page_renders_with_every_section_and_no_secrets(make_console):
    rig = await make_console()
    r = await rig.client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    dom = _Dom()
    dom.feed(r.text)
    for section in (
        "alarms",
        "weather",
        "enclosure",
        "telescopes",
        "spectrograph",
        "plan",
        "timeline",
        "requests",
        "signin",
    ):
        assert dom.ids.get(section) == "section", section
    assert dom.ids["confirm"] == "dialog" and dom.ids["theme"] == "select"
    scripts = [a for t, a in dom.tags if t == "script"]
    styles = [a for t, a in dom.tags if t == "style"]
    assert len(scripts) == 1 and len(styles) == 1
    nonce = scripts[0]["nonce"]
    assert f"'nonce-{nonce}'" in csp and styles[0]["nonce"] == nonce
    # Nothing fetched from anywhere, no inline handlers or styles, no secrets.
    assert not any("src" in a or "href" in a or "style" in a for _, a in dom.tags)
    assert not any(k.startswith("on") for _, a in dom.tags for k in a)
    assert "http://" not in r.text and "https://" not in r.text
    for token in rig.tokens.values():
        assert token not in r.text
    assert "rft_" not in r.text and LEASE_TOKEN not in r.text
    # The themes: dark by default, a red-light one, and a phone viewport.
    assert '<html lang="en" data-theme="dark">' in r.text
    assert ':root[data-theme="red"]' in r.text and 'name="viewport"' in r.text
    # A fresh nonce per response.
    again = (await rig.client.get("/")).headers["content-security-policy"]
    assert again != csp
    assert render_page('a"b').count('nonce="a&quot;b"') == 2


# -- signing in, sessions, CSRF ----------------------------------------------------------------


async def test_sign_in_sets_a_strict_cookie_and_needs_csrf_for_actions(make_console):
    rig = await make_console()
    assert (await rig.client.get("/console/api/status")).status_code == 401
    r = await rig.client.post("/console/login", json={"token": "rft_nope"})
    assert r.status_code == 401
    r = await rig.login("alice")
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert part in cookie
    assert "Secure" not in cookie  # plain loopback; on behind TLS
    assert rig.tokens["alice"] not in cookie and rig.tokens["alice"] not in r.text
    me = r.json()
    assert me["name"] == "alice" and me["can"]["operate"] and me["csrf"]
    assert (await rig.client.get("/console/session")).json()["csrf"] == me["csrf"]
    # A state-changing call needs the CSRF header, and a same-origin Origin.
    r = await rig.act("ack", csrf=False, id="abc", reason="seen")
    assert r.status_code == 403 and "CSRF" in r.json()["error"]
    rig.csrf = "wrong"
    assert (await rig.act("ack", id="abc", reason="seen")).status_code == 403
    rig.csrf = me["csrf"]
    r = await rig.client.post(
        "/console/api/ack",
        json={"id": "abc", "reason": "seen"},
        headers={"X-CSRF-Token": rig.csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and "cross-origin" in r.json()["error"]
    host = rig.server.url.split("://")[1]
    r = await rig.client.post(
        "/console/api/ack",
        json={"id": "abc", "reason": "seen"},
        headers={"X-CSRF-Token": rig.csrf, "Origin": f"http://{host}"},
    )
    assert r.status_code == 200
    # A login from another origin is refused too (login CSRF).
    r = await rig.client.post(
        "/console/login", json={"token": rig.tokens["alice"]}, headers={"Origin": "http://x"}
    )
    assert r.status_code == 403
    # A form post is not JSON: refused before it reaches the app.
    r = await rig.client.post(
        "/console/api/ack",
        content=b"id=abc",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415
    # Sign out ends the session.
    r = await rig.client.post("/console/logout", json={}, headers={"X-CSRF-Token": rig.csrf})
    assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
    rig.client.cookies.clear()
    assert (await rig.client.get("/console/api/status")).status_code == 401


async def test_scripts_may_use_the_token_header_instead(make_console):
    rig = await make_console()
    h = {"Authorization": f"Token {rig.tokens['alice']}"}
    r = await rig.client.post("/console/api/ack", json={"id": "abc", "reason": "seen"}, headers=h)
    assert r.status_code == 200  # no cookie, so no CSRF
    assert "set-cookie" not in r.headers


async def test_scopes(make_console):
    rig = await make_console()
    r = await rig.login("tom")  # submit only: cannot even read the console
    assert r.status_code == 401
    assert (await rig.login("watcher")).status_code == 200
    assert (await rig.client.get("/console/api/status")).status_code == 200
    for action in ("ack", "pause", "resume", "stop", "clear_lease", "clear_faults"):
        r = await rig.act(action, id="x", reason="r", confirm=True)
        assert r.status_code == 403 and "operator" in r.json()["error"], action
    assert not rig.control.exists()  # nothing was written
    assert rig.fakes.posts() == []
    rig.client.cookies.clear()
    assert (await rig.login("boss")).status_code == 200  # admin is everything
    assert (await rig.act("pause", telescope="t1", reason="r")).status_code == 200


async def test_a_revoked_token_ends_its_session(make_console):
    rig = await make_console()
    await rig.login("alice")
    assert (await rig.client.get("/console/api/status")).status_code == 200
    rig.store.revoke(name="alice")
    assert (await rig.client.get("/console/api/status")).status_code == 401
    assert (
        await rig.client.post("/console/login", json={"token": rig.tokens["alice"]})
    ).status_code == 401


async def test_rate_limits(make_console):
    rig = await make_console(
        action_per_minute=1, action_burst=2, auth_fail_per_minute=1, auth_fail_burst=3
    )
    await rig.login("alice")
    codes = [(await rig.act("ack", id=f"a{i}", reason="r")).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    other = httpx.AsyncClient(base_url=rig.server.url)
    try:
        codes = [
            (await other.post("/console/login", json={"token": "bad"})).status_code
            for _ in range(4)
        ]
    finally:
        await other.aclose()
    assert codes == [401, 401, 401, 429]


async def test_bad_requests(make_console):
    rig = await make_console()
    await rig.login("alice")
    assert (await rig.act("ack", id="a")).status_code == 400  # no reason
    assert (await rig.act("ack", id="a", reason="  ")).status_code == 400
    assert (await rig.act("ack", id="a", reason="x" * 501)).status_code == 400
    assert (await rig.act("pause", telescope="t9", reason="r")).status_code == 400
    assert (await rig.act("pause", enclosure="nope", reason="r")).status_code == 400
    r = await rig.act("stop", telescope="t1", reason="r")  # not confirmed
    assert r.status_code == 400 and "confirm" in r.json()["error"]
    assert (await rig.client.get("/console/api/pause")).status_code == 405
    assert (await rig.client.get("/console/api/nothing")).status_code == 404
    r = await rig.client.post(
        "/console/api/ack",
        content=b"[1]",
        headers={"X-CSRF-Token": rig.csrf, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


# -- the actions --------------------------------------------------------------------------------


async def test_acknowledge_an_alarm_is_recorded_and_kept(make_console):
    rig = await make_console()
    await rig.login("alice")
    alarm = next(a for a in (await rig.status())["alarms"] if a["source"] == "spectrograph")
    assert alarm["ack"] is None
    r = await rig.act("ack", id=alarm["id"], reason="pump being serviced")
    assert r.status_code == 200
    acked = next(a for a in (await rig.status())["alarms"] if a["id"] == alarm["id"])
    assert acked["ack"]["by"] == "alice" and acked["ack"]["reason"] == "pump being serviced"
    line = rig.audit()[-1]
    assert line["client"] == "alice" and line["action"] == "ack" and line["status"] == 200
    assert line["reason"] == "pump being serviced" and line["id"] == alarm["id"] and line["time"]
    # Kept across a restart of the console.
    acks = json.loads((rig.tmp / "state" / "acks.json").read_text())
    assert acks[alarm["id"]]["by"] == "alice"


async def test_pause_resume_and_stop_one_telescope_go_through_the_command_box(make_console):
    rig = await make_console()
    await rig.login("alice")
    r = await rig.act("pause", telescope="t1", reason="cloud bank to the west")
    assert r.status_code == 200
    cid = r.json()["result"]["command"]
    (cmd,) = [json.loads(p.read_text()) for p in (rig.control / "inbox").iterdir()]
    assert cmd == {
        "id": cid,
        "action": "pause",
        "telescopes": ["t1"],
        "operator": "alice",
        "reason": "cloud bank to the west",
        "time": cmd["time"],
    }
    r = await rig.act("stop", telescope="t2", reason="mirror cover", confirm=True)
    assert r.status_code == 200 and "lease_revoked" not in r.json()["result"]
    r = await rig.act("resume", reason="clear again")  # all telescopes
    assert r.status_code == 200 and r.json()["result"]["telescopes"] == ["t1", "t2"]
    assert rig.fakes.posts() == []  # one telescope never reaches the supervisor
    audit = [x for x in rig.audit() if x.get("action") in ("pause", "stop", "resume")]
    assert [(x["action"], x["telescope"], x["client"]) for x in audit] == [
        ("pause", "t1", "alice"),
        ("stop", "t2", "alice"),
        ("resume", None, "alice"),
    ]


async def test_stopping_the_enclosure_holds_then_asks_the_supervisor_to_close(make_console):
    rig = await make_console()
    await rig.login("alice")
    r = await rig.act("stop", reason="lightning 20 km", confirm=True)
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["lease_revoked"] is True and res["telescopes"] == ["t1", "t2"]
    assert res["hold_confirmed"] is False  # no sequencer here to confirm it
    assert rig.fakes.posts() == [
        ("sup.test/close", {"operator": "alice", "reason": "stop: lightning 20 km"})
    ]


async def test_clear_lease_and_faults(make_console):
    fakes = Fakes()
    rig = await make_console(fakes=fakes)
    await rig.login("alice")
    r = await rig.act("clear_lease", reason="t1 parked by hand", confirm=True)
    assert r.status_code == 409 and "live" in r.json()["error"]
    fakes.supervisor["lease"].update(live=False, revoked=True, quarantined=True)
    r = await rig.act("clear_lease", reason="t1 parked by hand", confirm=True)
    assert r.status_code == 200 and r.json()["result"]["released_now"] is True
    assert LEASE_TOKEN not in r.text
    r = await rig.act("clear_faults", reason="motor reset at the box", confirm=True)
    assert r.status_code == 200
    assert fakes.posts() == [
        (
            f"sup.test/leases/{LEASE_TOKEN}/clear",
            {"operator": "alice", "reason": "t1 parked by hand"},
        ),
        ("sup.test/faults/clear", {"operator": "alice", "reason": "motor reset at the box"}),
    ]
    fakes.supervisor["lease"] = None
    assert (await rig.act("clear_lease", reason="r", confirm=True)).status_code == 409


def test_the_supervisor_client_has_no_way_to_open():
    allowed = SupervisorClient.allowed
    assert allowed("GET", "status") and allowed("POST", "close")
    assert allowed("POST", "leases/abc.1/clear") and allowed("POST", "faults/clear")
    for method, path in [
        ("POST", "leases"),  # requesting a lease is how a sequencer gets the roof opened
        ("POST", "leases/abc.1/renew"),
        ("POST", "leases/abc.1/recover"),
        ("POST", "leases/abc.1/release"),
        ("POST", "leases/abc.1/quarantine"),
        ("POST", "open"),
        ("PUT", "close"),
        ("POST", "leases/../close/clear"),
        ("GET", "leases/abc.1"),
    ]:
        assert not allowed(method, path), (method, path)


async def test_the_supervisor_client_refuses_before_sending():
    sent = []
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    try:
        client = SupervisorClient("http://sup.test", http, 1.0)
        with pytest.raises(NotAllowed):
            await client.call("POST", "leases", {"holder": "console"})
        with pytest.raises(NotAllowed):
            await client.call("POST", "leases/x.1/renew")
    finally:
        await http.aclose()
    assert sent == []


async def test_requests_go_through_the_request_api(make_console):
    rig = await make_console()
    from tests.core.test_request_api import imaging

    await rig.login("alice")
    h = {"X-CSRF-Token": rig.csrf}
    r = await rig.client.post("/console/api/v1/requestgroups", json=imaging("night1"), headers=h)
    assert r.status_code == 202, r.text
    assert any(p.name.endswith("-submit-night1.json") for p in (rig.tmp / "box").iterdir())
    line = rig.audit()[-1]
    assert line["client"] == "alice" and line["via"] == "console" and line["group"] == "night1"
    r = await rig.client.get("/console/api/v1/requestgroups/night1")
    assert r.json()["intake"] == "QUEUED" and r.json()["submitter"] == "alice"
    up = [u["id"] for u in (await rig.status())["plan"]["upcoming"]]
    assert "night1" in up
    r = await rig.client.post("/console/api/v1/requestgroups/night1/cancel", json={}, headers=h)
    assert r.status_code == 200 and r.json()["intake"] == "CANCELED"
    # CSRF applies here too, and the request API's own scopes.
    r = await rig.client.post("/console/api/v1/requestgroups", json=imaging("night2"))
    assert r.status_code == 403
    rig.client.cookies.clear()
    await rig.login("watcher")
    r = await rig.client.post(
        "/console/api/v1/requestgroups", json=imaging("night3"), headers={"X-CSRF-Token": rig.csrf}
    )
    assert r.status_code == 403 and "submit" in r.json()["error"]


# -- events and polling ------------------------------------------------------------------------


async def read_event(lines, what: str = "status") -> dict:
    event = None
    async for line in lines:
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == what:
            return json.loads(line[6:])
    raise AssertionError("stream ended")


async def test_server_sent_events_push_now_after_an_action(make_console):
    rig = await make_console(refresh=60, max_streams_per_token=1)
    await rig.login("alice")
    async with rig.client.stream("GET", "/console/api/events") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        lines = r.aiter_lines()
        first = await asyncio.wait_for(read_event(lines), 5)
        assert "aqawan1" in first["enclosures"]
        # One stream per token here: a second is refused.
        assert (await rig.client.get("/console/api/events")).status_code == 429
        await rig.act("ack", id=first["enclosures"]["aqawan1"]["alarms"][0]["id"], reason="seen")
        second = await asyncio.wait_for(read_event(lines), 5)  # long before refresh=60
        assert second["enclosures"]["aqawan1"]["alarms"][0]["ack"]["by"] == "alice"
        # Revoking the token ends the stream at its next push.
        rig.store.revoke(name="alice")
        await rig.act("ack", id="x", reason="r")  # refused (401), but kicks nothing
        rig.app.changed()
        await asyncio.wait_for(read_event(lines, "signed-out"), 5)
    await wait_until(lambda: not rig.app.streams, 5, what="the stream slot freed")


async def test_polling_works_without_the_stream(make_console):
    rig = await make_console(cache=0)
    await rig.login("watcher")
    a = (await rig.client.get("/console/api/status")).json()
    rig.fakes.supervisor["shutter"] = "CLOSED"
    b = (await rig.client.get("/console/api/status")).json()
    assert a["enclosures"]["aqawan1"]["enclosure"]["shutter"] == "OPEN"
    assert b["enclosures"]["aqawan1"]["enclosure"]["shutter"] == "CLOSED"


# -- the hold wrapper ----------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, telescope: str, policy: str, reason: str) -> bool:
        self.calls.append((telescope, policy, reason))
        return True


async def test_the_hold_wrapper(tmp_path):
    inner = StaticPlan([{"name": "a"}, {"name": "b"}], ["t1", "t2"])
    hold = OperatorHold(inner, ["t1", "t2"], tmp_path / "ctl", hold_idle=7)
    pre = Recorder()
    hold.set_preempt_callback(pre)
    assert (await hold.peek("t1")).name == "a"
    send_command(tmp_path / "ctl", "pause", ["t1"], operator="alice", reason="clouds")
    send_command(tmp_path / "ctl", "stop", ["t2"], operator="bob", reason="cover")
    hold.take_in()
    assert pre.calls == [
        ("t1", "now-after-readout", "operator pause: clouds"),
        ("t2", "now", "operator stop: cover"),
    ]
    for t in ("t1", "t2"):
        idle = await hold.next_observation(t)
        assert isinstance(idle, Idle) and idle.seconds == 7 and "operator" in idle.reason
        assert isinstance(await hold.peek(t), Idle)
    doc = read_holds(tmp_path / "ctl")
    assert set(doc["holds"]) == {"t1", "t2"} and len(doc["applied"]) == 2
    assert not list((tmp_path / "ctl" / "inbox").iterdir())
    assert len(list((tmp_path / "ctl" / "done").iterdir())) == 2
    # Holds survive a restart of the sequencer.
    again = OperatorHold(StaticPlan([{"name": "a"}], ["t1", "t2"]), ["t1", "t2"], tmp_path / "ctl")
    assert isinstance(await again.next_observation("t1"), Idle)
    # Resume wakes the telescope (interrupting nothing) and the plan answers again.
    send_command(tmp_path / "ctl", "resume", ["t1"], operator="alice", reason="clear")
    hold.take_in()
    assert pre.calls[-1] == ("t1", "next", "operator resume: clear")
    assert (await hold.next_observation("t1")).name == "a"
    # Resuming a telescope that is not held does nothing at all.
    n = len(pre.calls)
    send_command(tmp_path / "ctl", "resume", ["t1"], operator="alice", reason="again")
    hold.take_in()
    assert len(pre.calls) == n
    # A bad command is filed with its error; the rest go on.
    (tmp_path / "ctl" / "inbox" / f"{'9' * 20}-{'a' * 12}.json").write_text('{"action": "open"}')
    (result,) = hold.take_in()
    assert result["ok"] is False
    with pytest.raises(ValueError):
        send_command(tmp_path / "ctl", "open", ["t1"], operator="x", reason="y")
    with pytest.raises(ValueError):
        hold.apply({"action": "pause", "telescopes": ["t9"], "operator": "x"})


async def test_the_hold_wrapper_forwards_reports_and_hooks(tmp_path):
    inner = CallablePlan(lambda: Observation("x"))
    hold = OperatorHold(inner, ["t1"], tmp_path)
    assert hold.peek is None  # the inner plan has none to wrap
    obs = await hold.next_observation("t1")
    hold.report("t1", obs, "complete", 1, files=["a.fits"])
    assert inner.history == [("t1", "x", "complete", 1)] and inner.files == [
        ("t1", "x", ["a.fits"])
    ]
    assert hold.history is inner.history  # other attributes come from the plan


async def test_the_sequencer_service_wraps_the_plan(tmp_path):
    from robofast_core.sequencer_service import build
    from robofast_core.timeline import Timeline

    config = {
        "name": "e",
        "telescopes": {"t1": {"telescope": "127.0.0.1:9/0", "camera": "127.0.0.1:9/0"}},
        "plan": {"type": "calibration", "biases": 1},
        "operator_control": "ctl",
    }
    seq, remote = build(config, Timeline(), base=tmp_path)
    await remote.aclose()
    assert isinstance(seq.plan, OperatorHold) and seq.plan.dir == tmp_path / "ctl"
    del config["operator_control"]
    seq, remote = build(config, Timeline(), base=tmp_path)
    await remote.aclose()
    assert not isinstance(seq.plan, OperatorHold)


def test_a_non_loopback_address_needs_tls(tmp_path, capsys):
    path = write_site(tmp_path, listen="0.0.0.0:8400")
    with pytest.raises(SystemExit):
        main(["serve", "--config", str(path)])
    assert "without TLS" in capsys.readouterr().err
    cfg = json.loads(path.read_text())
    cfg["surprise"] = 1
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="unknown keys"):
        load_console_config(path)


# -- the invariant, on a real supervisor and sequencer --------------------------------------------


async def test_the_console_never_opens_the_enclosure(make_rig, tmp_path):
    """Every console action, against a real supervisor (over HTTP) and a real
    sequencer on fakes: pause holds one telescope with the roof open; an
    enclosure stop parks everything and closes; while held nothing reopens
    although the weather is safe; no action opens; resuming lets the
    supervisor open by its own policy (a lease first, after the hold)."""
    k = iter(range(10**6))
    inner = CallablePlan(lambda: Observation(f"target{next(k)}", exptime=0.3, count=1000))
    hold = OperatorHold(inner, ["t1", "t2"], tmp_path / "control", poll=0.05)
    rig = make_rig(telescopes=2, plan=hold, seq={"idle_release_after": None})
    await rig.start()
    sup = await SupervisorServer(rig.supervisor).start()
    serve = asyncio.create_task(hold.serve())

    async def snapshots():
        while True:
            write_snapshot(rig.sequencer, tmp_path / "seq-status.json")
            await asyncio.sleep(0.1)

    snaps = asyncio.create_task(snapshots())
    path = write_site(tmp_path, supervisor_url=sup.url, hold_ack_timeout=5.0, cache=0)
    cfg = json.loads((tmp_path / "aqawan1.json").read_text())
    del cfg["telescopes"]["t1"]["guider"], cfg["devices"]["safetymonitor"]
    (tmp_path / "aqawan1.json").write_text(json.dumps(cfg))
    config, spec, base = load_console_config(path)
    del spec["aqawan1"]["spectrograph"]
    store = TokenStore(config.tokens)
    token = store.add("alice", ["read", "operator"])[1]
    app = Console(build_enclosures(spec, base), store, config=config)
    server = await ConsoleHttpServer(app).start()
    client = httpx.AsyncClient(base_url=server.url, timeout=30)

    async def act(action: str, **body) -> httpx.Response:
        return await client.post(
            f"/console/api/{action}", json={"reason": "test", "confirm": True, **body}, headers=h
        )

    def opens_after(t: float) -> list:
        return [
            c for c in rig.timeline.commands(device="dome") if c.what == "OpenShutter" and c.t > t
        ]

    try:
        csrf = (await client.post("/console/login", json={"token": token})).json()["csrf"]
        h = {"X-CSRF-Token": csrf}
        await wait_until(lambda: rig.dome_open() and rig.exposing(), 10, what="observing")
        s = (await client.get("/console/api/status")).json()["enclosures"]["aqawan1"]
        assert s["enclosure"]["shutter"] == "OPEN" and s["enclosure"]["lease"]["live"]
        assert s["sequencer"]["running"] is True and s["telescopes"]["t1"]["state"] != "unknown"
        assert len(s["timeline"]) == 60 and all(e["time"] and e["what"] for e in s["timeline"])
        assert "token" not in s["enclosure"]["lease"]

        # Pause t1: it finishes its exposure and holds; t2 and the roof carry on.
        assert (await act("pause", telescope="t1")).status_code == 200
        await wait_until(lambda: rig.camera_idle(0) and "t1" in hold.holds, 5, what="t1 held")
        await asyncio.sleep(0.5)
        assert rig.camera_idle(0) and rig.exposing(1) and rig.dome_open()
        aborts = [c for c in rig.timeline.commands(device="t1.camera") if c.what == "AbortExposure"]
        assert aborts == []  # pause lets the exposure finish

        # Stop everything: held first, then the lease revoked; parked and closed.
        r = await act("stop")
        assert r.status_code == 200, r.text
        assert r.json()["result"]["hold_confirmed"] is True
        assert r.json()["result"]["lease_revoked"] is True
        await wait_until(lambda: rig.dome_closed() and rig.parked(), 10, what="parked and closed")
        t_closed = rig.timeline.entries()[-1].t
        opens = rig.dome.opens

        # Weather safe, reopen hold long past: still closed while held, and no
        # console action opens it.
        for action, body in [
            ("pause", {}),
            ("pause", {"telescope": "t2"}),
            ("stop", {"telescope": "t1"}),
            ("clear_faults", {}),
            ("clear_lease", {}),
            ("ack", {"id": "x"}),
            ("stop", {}),
        ]:
            r = await act(action, **body)
            assert r.status_code in (200, 409), (action, r.text)
        await asyncio.sleep(1.5)  # three reopen holds
        assert rig.dome_closed() and rig.dome.opens == opens and opens_after(t_closed) == []
        grants = [e for e in rig.timeline.events("lease_granted") if e.t > t_closed]
        assert grants == []
        s = (await client.get("/console/api/status")).json()["enclosures"]["aqawan1"]
        assert s["enclosure"]["shutter"] == "CLOSED" and set(s["enclosure"]["holds"]) == {
            "t1",
            "t2",
        }

        # Unsafe weather, then resume: the console cannot make it open.
        rig.weather.safe = False
        await wait_until(lambda: not rig.supervisor.safe, 5, what="unsafe")
        t_resume = rig.timeline.entries()[-1].t
        assert (await act("resume")).status_code == 200
        await asyncio.sleep(1.5)
        assert rig.dome_closed() and opens_after(t_resume) == []

        # Safe again: the supervisor opens by its own policy -- the weather
        # held safe for reopen_hold, then a lease to the sequencer, then open.
        rig.weather.safe = True
        await wait_until(lambda: rig.dome_open() and rig.exposing(), 10, what="reopened")
        safe = rig.timeline.first_event("weather_safe", after=t_resume)
        grant = rig.timeline.first_event("lease_granted", after=t_resume)
        (first_open, *_) = opens_after(t_resume)
        assert safe.t < grant.t < first_open.t
        assert grant.t - safe.t >= rig.supervisor.config.reopen_hold - 0.05
        assert first_open.source == "safety"  # the supervisor's own grant
        assert all(c.source == "safety" for c in rig.timeline.commands(device="dome"))
        # Every action is in the audit log with who and why.
        audit = [
            json.loads(x)
            for x in (tmp_path / "log" / "console-audit.jsonl").read_text().splitlines()
        ]
        assert {a.get("action") for a in audit} >= {
            "pause",
            "stop",
            "resume",
            "clear_faults",
            "ack",
        }
        assert all(
            a["client"] == "alice" and a["reason"] == "test"
            for a in audit
            if a.get("action") != "login"
        )
    finally:
        snaps.cancel()
        serve.cancel()
        await client.aclose()
        await server.stop()
        await app.aclose()
        await sup.stop()
        await rig.stop()


async def test_park_one_telescope_goes_through_the_command_box_never_the_roof(make_console):
    rig = await make_console()
    await rig.login("alice")
    r = await rig.act("park", telescope="t2", reason="check the mount")
    assert r.status_code == 400  # it asks first
    r = await rig.act("park", reason="all of them", confirm=True)
    assert r.status_code == 400  # one telescope; the enclosure is "stop"
    r = await rig.act("park", telescope="t2", reason="check the mount", confirm=True)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["telescopes"] == ["t2"] and "lease_revoked" not in res
    (cmd,) = [json.loads(p.read_text()) for p in (rig.control / "inbox").iterdir()]
    assert (cmd["action"], cmd["telescopes"]) == ("park", ["t2"])
    assert rig.fakes.posts() == []  # the supervisor (and so the roof) is never asked


def test_the_hold_wrapper_parks_and_clears_through_the_sequencer_hooks(tmp_path):
    plan = OperatorHold(StaticPlan([], ["t1", "t2"]), ["t1", "t2"], tmp_path)
    stops, parks, clears = Recorder(), [], []
    plan.set_preempt_callback(stops)
    plan.set_operator_hooks(
        park=lambda t, why: parks.append((t, why)) or True,
        clear_spectrograph=lambda who, why: clears.append((who, why)) or True,
    )
    send_command(tmp_path, "park", ["t2"], operator="bob", reason="mount noise")
    send_command(tmp_path, "clear_spectrograph", [], operator="bob", reason="fixed")
    park, clear = plan.take_in()
    assert park["park_requested"] == {"t2": True} and park["preempted"] == {"t2": True}
    assert [c[:2] for c in stops.calls] == [("t2", "now")]
    assert parks == [("t2", "operator park by bob: mount noise")]
    assert plan.holds["t2"]["action"] == "park" and plan.held == {"t2"}
    assert clear["ok"] is True and clears == [("bob", "fixed")]
    with pytest.raises(ValueError):
        send_command(tmp_path, "park", [], operator="bob", reason="x")
