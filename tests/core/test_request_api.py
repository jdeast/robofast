"""The request API (design.md 3.7): auth, endpoints, intake through the
drop box into the dispatch scheduler, and the HTTP layer's limits.

Astronomy is frozen at one instant of a MINERVA night (``NIGHT``) for both
the API and the scheduler; the HTTP server is real, on a loopback port.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from robofast_core import api_auth
from robofast_core.api_auth import RateLimiter, TokenStore
from robofast_core.dispatch import DispatchScheduler, dispatch_from_config
from robofast_core.plan import Idle
from robofast_core.request_api import (
    ApiConfig,
    HttpServer,
    RequestApi,
    build_enclosures,
    check_bind,
    load_api_config,
    main,
    tls_context,
)
from robofast_core.request_model import Status
from robofast_core.sequencer_service import snapshot, write_snapshot
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.checks import wait_until
from tests.core.test_dispatch import NIGHT, Clock, Preempts, group, too

SITE = {"latitude": 31.680407, "longitude": -110.878977, "elevation": 2316.0}
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = "/data/{night}/{telescope}/{configuration}.{instrument_config}.{frame:04d}.fits"


def plan_entry(**kw) -> dict:
    return {
        "type": "dispatch",
        "site": SITE,
        "requests": [group("base", telescopes=["t1"], priority=1)],
        "state": "state.json",
        "too_dropbox": "box",
        "telescopes": {
            "t1": {"instruments": ["imager"]},
            "t2": {"instruments": ["imager", "spectrograph"]},
        },
        "too_poll": 0.02,
        **kw,
    }


def write_site(tmp: Path, *, supervisor_url: str = "http://127.0.0.1:9", **api) -> Path:
    """An enclosure config, its dispatch plan and the API config in ``tmp``;
    returns the API config's path."""
    dev = "127.0.0.1:9/0"
    enclosure = {
        "name": "aqawan1",
        "supervisor_url": supervisor_url,
        "telescopes": {t: {"telescope": dev, "camera": dev} for t in ("t1", "t2")},
        "plan": plan_entry(),
        "status_file": "seq-status.json",
    }
    (tmp / "aqawan1.json").write_text(json.dumps(enclosure))
    cfg = {
        "listen": "127.0.0.1:0",
        "tokens": "secret/tokens.json",
        "audit_log": "log/audit.jsonl",
        "enclosures": {"aqawan1": "aqawan1.json"},
        "validate_step": 1800,
        "data_path_template": TEMPLATE,
        **api,
    }
    (tmp / "api.json").write_text(json.dumps(cfg))
    return tmp / "api.json"


@dataclass
class Rig:
    tmp: Path
    clock: Clock
    app: RequestApi
    server: HttpServer
    store: TokenStore
    tokens: dict[str, str]
    client: httpx.AsyncClient

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Token {self.tokens[who]}"}

    async def get(self, who: str, path: str, **kw) -> httpx.Response:
        return await self.client.get(f"/api/v1{path}", headers=self.auth(who), **kw)

    async def post(self, who: str, path: str, body=None, **kw) -> httpx.Response:
        return await self.client.post(f"/api/v1{path}", headers=self.auth(who), json=body, **kw)

    @property
    def box(self) -> Path:
        return self.tmp / "box"

    def scheduler(self, **kw) -> DispatchScheduler:
        """The sequencer's side: a scheduler from the same plan entry."""
        cfg = json.loads((self.tmp / "aqawan1.json").read_text())["plan"]
        cfg.pop("type")
        return dispatch_from_config(cfg, ["t1", "t2"], base=self.tmp, clock=self.clock, **kw)

    def audit(self) -> list[dict]:
        path = self.tmp / "log" / "audit.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def supervisor_stub(status: dict | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def make_api(tmp_path):
    made: list[Rig] = []

    async def build(*, http: httpx.AsyncClient | None = None, **api) -> Rig:
        path = write_site(tmp_path, **api)
        config, spec, base = load_api_config(path)
        clock = Clock()
        enclosures = build_enclosures(spec, base, clock=clock)
        store = TokenStore(config.tokens)
        tokens = {}
        for name, scopes in {
            "reader": ["read"],
            "tom": ["read", "submit"],
            "other": ["read", "submit"],
            "alerts": ["read", "too"],
            "boss": ["admin"],
        }.items():
            tokens[name] = store.add(name, scopes)[1]
        app = RequestApi(
            enclosures, store, config=config, http=http or supervisor_stub(None), clock=clock
        )
        server = await HttpServer(app).start()
        client = httpx.AsyncClient(base_url=server.url, timeout=30)
        rig = Rig(tmp_path, clock, app, server, store, tokens, client)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        await rig.client.aclose()
        await rig.server.stop()
        await rig.app.aclose()


@pytest.fixture
async def api(make_api) -> Rig:
    return await make_api()


def imaging(gid: str | None = None, *, ha: float = -1.0, dec: float = 20.0, **kw) -> dict:
    """A one-configuration group for the API (``group`` without ids if
    ``gid`` is None)."""
    g = group(gid or "x", ha, dec, **kw)
    if gid is None:
        del g["id"], g["requests"][0]["id"], g["requests"][0]["configurations"][0]["id"]
    return g


# -- authentication and scopes --------------------------------------------------------------


async def test_missing_malformed_and_invalid_tokens(api):
    assert (await api.client.get("/healthz")).json() == {"ok": True}  # no token needed
    r = await api.client.get("/api/v1/status")
    assert r.status_code == 401 and r.json() == {"error": "missing token"}
    assert r.headers["WWW-Authenticate"].startswith("Token")
    tok = api.tokens["tom"]
    wrong = tok[:-1] + ("B" if tok[-1] == "A" else "A")
    for value in ("Basic abc", "Token rft_nope", f"Token {wrong}", f"Basic {tok}"):
        r = await api.client.get("/api/v1/status", headers={"Authorization": value})
        assert r.status_code == 401, value
    # Bearer works as well as Token.
    r = await api.client.get("/api/v1/", headers={"Authorization": f"Bearer {api.tokens['tom']}"})
    assert r.status_code == 200 and r.json()["name"] == "tom"
    assert r.json()["enclosures"] == {"aqawan1": ["t1", "t2"]}


async def test_a_revoked_token_is_refused_at_its_next_request(api):
    assert (await api.get("tom", "/")).status_code == 200
    # Revoked by another process (the CLI), through the file.
    assert TokenStore(api.store.path).revoke(name="tom")
    r = await api.get("tom", "/")
    assert r.status_code == 401 and r.json()["error"] == "invalid token"
    assert (await api.get("other", "/")).status_code == 200


async def test_an_expired_token_is_refused(api):
    _, token = api.store.add("brief", ["read"], expires_days=1)
    assert api.store.authenticate(token) is not None
    later = datetime.now(UTC) + timedelta(days=2)
    assert api.store.authenticate(token, now=later) is None


async def test_scopes(api):
    body = imaging("g1")
    assert (await api.post("reader", "/requestgroups", body)).status_code == 403
    assert (await api.post("alerts", "/requestgroups", body)).status_code == 403
    r = await api.post("tom", "/toos", imaging("t1", too=too("next")))
    assert r.status_code == 403 and "too scope" in r.json()["error"]
    assert (await api.post("reader", "/requestgroups/validate", body)).status_code == 403
    assert (await api.post("tom", "/requestgroups", body)).status_code == 202
    assert (await api.post("boss", "/toos", imaging("t2", too=too("next")))).status_code == 202
    assert (await api.get("reader", "/status")).status_code == 200


def test_the_secret_is_compared_in_constant_time(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "t.json")
    _, token = store.add("a", ["read"])
    calls = []
    real = api_auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(api_auth.hmac, "compare_digest", spy)
    assert store.authenticate(token) is not None
    tid = token.split("_")[1]
    other = token.replace(tid, "f" * 12 if tid != "f" * 12 else "e" * 12)
    assert store.authenticate(other) is None  # unknown id: a compare all the same
    assert store.authenticate("garbage") is None
    assert len(calls) == 3


def test_tokens_are_hashed_at_rest(tmp_path):
    path = tmp_path / "t.json"
    store = TokenStore(path)
    _, token = store.add("a", ["read", "submit"])
    secret = token.split("_", 2)[2]
    text = path.read_text()
    assert secret not in text and token not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rec = json.loads(text)["tokens"][0]
    assert rec["name"] == "a" and rec["scopes"] == ["read", "submit"]
    assert len(bytes.fromhex(rec["salt"])) == 16
    # Same secret, different salt: different hash.
    store.add("b", ["read"])
    assert len({r.hash for r in store.records()}) == 2
    with pytest.raises(ValueError, match="scopes"):
        store.add("c", ["write"])
    with pytest.raises(ValueError, match="name"):
        store.add("bad name", ["read"])


def test_no_default_tokens(tmp_path):
    store = TokenStore(tmp_path / "missing.json")
    assert store.records() == []
    assert store.authenticate("rft_000000000000_" + "A" * 43) is None


async def test_rate_limit_per_token(make_api):
    api = await make_api(rate_burst=3, rate_per_minute=6)
    codes = [(await api.get("tom", "/")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await api.get("tom", "/")
    assert int(r.headers["Retry-After"]) >= 1
    assert (await api.get("other", "/")).status_code == 200  # its own bucket


async def test_failed_authentications_are_rate_limited(make_api):
    api = await make_api(auth_fail_burst=2, auth_fail_per_minute=1)
    bad = {"Authorization": "Token rft_000000000000_" + "A" * 43}
    codes = [(await api.client.get("/api/v1/", headers=bad)).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    assert (await api.get("tom", "/")).status_code == 200  # a good token still works


def test_rate_limiter_refills():
    t = [0.0]
    rl = RateLimiter(per_minute=60, burst=2, clock=lambda: t[0])
    assert rl.allow("k")[0] and rl.allow("k")[0]
    ok, wait = rl.allow("k")
    assert not ok and wait == pytest.approx(1.0)
    t[0] = 1.0
    assert rl.allow("k")[0]


# -- submitting ------------------------------------------------------------------------------


async def test_submit_get_and_list(api):
    r = await api.post("tom", "/requestgroups", imaging(priority=3))
    assert r.status_code == 202, r.text
    out = r.json()
    gid = out["id"]
    assert gid.startswith("rg-") and out["intake"] == "QUEUED" and out["enclosure"] == "aqawan1"
    assert out["requests"] == [{"id": f"{gid}-r0", "configurations": [f"{gid}-r0-c0"]}]
    (path,) = api.box.glob("*.json")
    assert path.name.endswith(f"-submit-{gid}.json")
    dropped = json.loads(path.read_text())
    assert dropped["submitter"] == "tom" and dropped["submitted"] == NIGHT.isoformat()

    g = (await api.get("tom", f"/requestgroups/{gid}")).json()
    assert g["intake"] == g["state"] == "QUEUED" and g["submitter"] == "tom"
    assert g["requests"][0]["configurations"][0]["status"] == "PENDING"
    assert "_path" not in g and str(api.tmp) not in json.dumps(g)
    # Other clients do not see it; admin does.
    assert (await api.get("other", f"/requestgroups/{gid}")).status_code == 404
    assert (await api.get("boss", f"/requestgroups/{gid}")).status_code == 200
    mine = (await api.get("tom", "/requestgroups")).json()
    assert [x["id"] for x in mine["results"]] == [gid]
    assert (await api.get("other", "/requestgroups")).json()["count"] == 0
    everyone = {x["id"] for x in (await api.get("boss", "/requestgroups")).json()["results"]}
    assert everyone == {gid, "base"}
    assert (await api.get("tom", "/requestgroups?state=PENDING")).json()["count"] == 0
    assert (await api.get("tom", "/requestgroups?state=QUEUED")).json()["count"] == 1


async def test_submitter_and_status_cannot_be_forged(api):
    body = imaging("forged")
    body["submitter"] = "boss"
    assert (await api.post("tom", "/requestgroups", body)).status_code == 202
    assert (await api.get("tom", "/requestgroups/forged")).json()["submitter"] == "tom"
    body = imaging("s")
    body["requests"][0]["configurations"][0]["status"] = "COMPLETED"
    r = await api.post("tom", "/requestgroups", body)
    assert r.status_code == 400
    assert r.json()["errors"] == [
        "requests[0].configurations[0].status: set by the scheduler, not by clients"
    ]


def _bad(**changes) -> dict:
    g = imaging("bad")
    for path, value in changes.items():
        *keys, last = path.split(".")
        d = g
        for k in keys:
            d = d[int(k)] if isinstance(d, list) else d[k]
        if value is _DEL:
            del d[last]
        else:
            d[last] = value
    return g


_DEL = object()
C0 = "requests.0.configurations.0"


@pytest.mark.parametrize(
    "body, message",
    [
        (_bad(**{f"{C0}.target.dec_deg": 95}), "target.dec_deg: 95 is outside [-90, 90]"),
        (_bad(**{f"{C0}.exposure": 3}), "unknown keys ['exposure']"),
        (_bad(**{"requests.0.configurations": _DEL}), "missing ['configurations']"),
        (_bad(**{f"{C0}.instrument_configs": []}), "instrument_configs: expected a non-empty"),
        (_bad(**{"requests.0.telescopes": ["t9"]}), "unknown telescopes ['t9']"),
        (
            _bad(**{f"{C0}.instrument": "spectrograph", "requests.0.telescopes": ["t1"]}),
            "no 'spectrograph' on ['t1'] (they have ['imager'])",
        ),
        (
            _bad(
                **{
                    "requests.0.windows": [
                        {"start": "2026-11-14T01:00:00Z", "end": "2026-11-14T02:00:00Z"}
                    ]
                }
            ),
            "every window has already ended",
        ),
        (
            _bad(
                **{
                    "requests.0.windows": [
                        {"start": "2026-11-15T01:00:00", "end": "2026-11-16T02:00:00Z"}
                    ]
                }
            ),
            "has no time zone",
        ),
        (_bad(id="base"), "id 'base' is already in use"),
        (_bad(id="validate"), "'validate' is reserved"),
        (_bad(id="../etc"), "is not a valid id"),
        (_bad(name={"a": 1}), "name: expected a string"),
        (_bad(**{"requests": [imaging("r")["requests"][0]] * 21}), "requests: at most 20"),
        (_bad(too=too("next")), "submit targets of opportunity to /toos"),
    ],
)
async def test_validation_errors_name_the_entry(api, body, message):
    r = await api.post("tom", "/requestgroups", body)
    assert r.status_code == 400, r.text
    found = " | ".join([r.json()["error"], *r.json().get("errors", [])])
    assert message in found
    assert not list(api.box.glob("*.json"))


async def test_bodies_that_are_not_request_groups(api):
    h = {**api.auth("tom"), "Content-Type": "application/json"}
    for body, msg in ((b"{nope", "not JSON"), (b"[1, 2]", "JSON object"), (b"\xff", "not JSON")):
        r = await api.client.post("/api/v1/requestgroups", content=body, headers=h)
        assert r.status_code == 400 and msg in r.json()["error"]


async def test_toos_need_a_future_deadline(api):
    late = imaging("late", too=too("now", deadline=NIGHT - timedelta(minutes=1)))
    r = await api.post("alerts", "/toos", late)
    assert r.status_code == 400 and "too.deadline" in r.json()["errors"][0]
    r = await api.post("alerts", "/toos", imaging("plain"))
    assert r.status_code == 400 and "required for a ToO" in r.json()["errors"][0]


async def test_validate_is_a_dry_run(api):
    good = imaging("v1", telescopes=["t1", "t2"])
    south = imaging("v2", dec=-80.0)
    spec = imaging("v3", instrument="spectrograph")
    r = await api.post("tom", "/requestgroups/validate", good)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["valid"] and out["errors"] == [] and out["enclosure"] == "aqawan1"
    dry = out["dry_run"]
    assert dry["night"]["start"] == NIGHT.isoformat() and not dry["truncated"]
    req = dry["requests"]["v1-r"]
    assert req["observable"]
    for tel in ("t1", "t2"):
        windows = req["telescopes"][tel]["windows"]
        assert windows and windows[0]["start"] == NIGHT.isoformat()
    r = (await api.post("tom", "/requestgroups/validate", south)).json()
    req = r["dry_run"]["requests"]["v2-r"]
    assert r["valid"] and not req["observable"]
    assert any("altitude" in x for x in req["telescopes"]["t1"]["reasons"])
    r = (await api.post("tom", "/requestgroups/validate", spec)).json()
    tels = r["dry_run"]["requests"]["v3-r"]["telescopes"]
    assert not tels["t1"]["windows"] and tels["t2"]["windows"]
    assert any("has no spectrograph" in x for x in tels["t1"]["reasons"])
    # Problems come back as a verdict, not an HTTP error.
    r = (await api.post("tom", "/requestgroups/validate", _bad(id="base"))).json()
    assert not r["valid"] and "already in use" in r["errors"][0] and "dry_run" not in r
    assert not list(api.box.glob("*.json"))  # nothing submitted
    assert (await api.post("alerts", "/requestgroups/validate", imaging("v4", too=too()))).json()[
        "valid"
    ]


# -- intake: the drop box and the scheduler -----------------------------------------------------


async def test_submissions_wait_for_the_sequencer_and_survive_restarts(api):
    """The sequencer is down: the group waits in the drop box. It starts,
    takes the group in, and records it in its state file; it restarts and
    still has it."""
    gid = (await api.post("tom", "/requestgroups", imaging("late-night"))).json()["id"]
    assert (await api.get("tom", f"/requestgroups/{gid}")).json()["intake"] == "QUEUED"
    sched = api.scheduler()
    assert await sched.poll_dropbox() == [gid]
    g = (await api.get("tom", f"/requestgroups/{gid}")).json()
    assert g["intake"] == "ACCEPTED" and g["state"] == "PENDING" and g["submitter"] == "tom"
    again = api.scheduler()
    assert again.groups[gid].submitter == "tom"
    obs = await again.next_observation("t2")  # t1 prefers... either way, it is there
    assert gid in again.groups and obs is not None
    # Rejected by the scheduler (a clash the API could not see): REJECTED, with the reason.
    clash = imaging("clash")
    clash["requests"][0]["id"] = "base-r"
    api.app.enclosures["aqawan1"].drop("submit", "clash", {**clash, "submitter": "tom"})
    await sched.poll_dropbox()
    g = (await api.get("tom", "/requestgroups/clash")).json()
    assert g["intake"] == "REJECTED" and "already in use" in g["error"]


async def test_cancel_a_queued_group(api):
    gid = (await api.post("tom", "/requestgroups", imaging("q"))).json()["id"]
    r = await api.post("tom", f"/requestgroups/{gid}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "CANCELED"
    assert not list(api.box.glob("*.json"))
    sched = api.scheduler()
    assert await sched.poll_dropbox() == []
    g = (await api.get("tom", f"/requestgroups/{gid}")).json()
    assert g["intake"] == "CANCELED" and g["requests"][0]["configurations"][0]["status"] == (
        "CANCELED"
    )
    r = await api.post("tom", f"/requestgroups/{gid}/cancel")
    assert r.status_code == 200 and r.json()["canceled_now"] is False
    # Its id stays taken.
    assert (await api.post("tom", "/requestgroups", imaging("q"))).status_code == 400


async def test_cancel_an_accepted_group_mid_visit(api):
    """An observation in progress finishes; the visit then ends and the
    group's configurations are CANCELED, never overwritten by the report."""
    body = imaging("two", telescopes=["t2"])
    second = json.loads(json.dumps(body["requests"][0]["configurations"][0]))
    second["id"] = "two-c2"
    body["requests"][0]["configurations"].append(second)
    assert (await api.post("tom", "/requestgroups", body)).status_code == 202
    sched = api.scheduler()
    await sched.poll_dropbox()
    obs = await sched.next_observation("t2")
    assert obs.name.startswith("two ")
    r = await api.post("tom", "/requestgroups/two/cancel")
    assert r.status_code == 202 and r.json()["cancel_pending"]
    assert (await api.get("tom", "/requestgroups/two")).json()["cancel_pending"] is True
    await sched.poll_dropbox()
    sched.report("t2", obs, "complete", 1)
    assert sched.status("two-c") is Status.CANCELED
    assert isinstance(await sched.next_observation("t2"), Idle)  # not two-c2
    g = (await api.get("tom", "/requestgroups/two")).json()
    assert g["state"] == "CANCELED" and not g["cancel_pending"]
    r = await api.post("tom", "/requestgroups/two/cancel")
    assert r.status_code == 200 and r.json()["canceled_now"] is False
    # Its one completed observation is still a data pointer.
    data = (await api.get("tom", "/requestgroups/two/data")).json()
    assert [len(c["observations"]) for c in data["configurations"]] == [1, 0]


async def test_cancel_scopes_and_ownership(api):
    await api.post("tom", "/requestgroups", imaging("mine"))
    await api.post("alerts", "/toos", imaging("alert", too=too("next")))
    assert (await api.post("other", "/requestgroups/mine/cancel")).status_code == 404
    assert (await api.post("reader", "/requestgroups/mine/cancel")).status_code == 403
    assert (await api.post("tom", "/requestgroups/alert/cancel")).status_code == 404
    assert (await api.post("tom", "/requestgroups/nothing/cancel")).status_code == 404
    assert (await api.post("boss", "/requestgroups/mine/cancel")).status_code == 200
    assert (await api.post("alerts", "/requestgroups/alert/cancel")).status_code == 200


async def test_a_too_end_to_end_into_the_scheduler(api):
    """A ToO submitted through the API preempts the busy telescope through
    the scheduler's drop-box task, runs, and reports COMPLETED with data
    pointers."""
    sched = api.scheduler()
    await sched.poll_dropbox()
    await api.post("tom", "/requestgroups", imaging("other-work", telescopes=["t2"]))
    await sched.poll_dropbox()
    p = Preempts()
    sched.set_preempt_callback(p)
    base = await sched.next_observation("t1")
    assert base.name.startswith("base ")
    assert (await sched.next_observation("t2")).name.startswith("other-work ")
    serve = asyncio.create_task(sched.serve())
    try:
        burst = imaging("burst", telescopes=["t1"], too=too("now"), count=2)
        r = await api.post("alerts", "/toos", burst)
        assert r.status_code == 202 and r.json()["too"]
        await wait_until(lambda: p.calls, 5, what="the preemption")
    finally:
        serve.cancel()
    assert p.calls == [("t1", "now", "ToO burst-r")]
    g = (await api.get("alerts", "/requestgroups/burst")).json()
    assert g["intake"] == "ACCEPTED" and g["too"]["policy"] == "now"
    sched.report("t1", base, "interrupted", 0)
    obs = await sched.next_observation("t1")
    assert obs.name.startswith("burst ") and obs.count == 2
    sched.report("t1", obs, "complete", 2)
    g = (await api.get("alerts", "/requestgroups/burst")).json()
    assert g["state"] == "COMPLETED"
    assert g["requests"][0]["configurations"][0]["status"] == "COMPLETED"
    data = (await api.get("alerts", "/requestgroups/burst/data")).json()
    (cfg,) = data["configurations"]
    (o,) = cfg["observations"]
    assert o["telescope"] == "t1" and o["frames"] == 2
    assert o["files"] == [
        "/data/20261114/t1/burst-c.0.0000.fits",
        "/data/20261114/t1/burst-c.0.0001.fits",
    ]
    assert sched.status("base-c") is Status.ATTEMPTED


# -- status ------------------------------------------------------------------------------------


SUPERVISOR = {
    "safe": False,
    "reason": "humidity 91% > 85%",
    "open_allowed": False,
    "fault": None,
    "shutter": "Closed",
    "alarms": ["a", "b"],
    "epoch": "abc",
    "lease": {
        "token": "abc.7",
        "id": 7,
        "holder": "aqawan1-sequencer",
        "live": True,
        "opened": False,
        "revoked": False,
        "reason": "",
        "released": False,
        "quarantined": False,
    },
}


async def test_status(make_api):
    api = await make_api(http=supervisor_stub(SUPERVISOR))
    snap = {
        "time": datetime.now(UTC).isoformat(),
        "max_age": 15,
        "sequencer": "aqawan1-sequencer",
        "quarantined": False,
        "needs_operator": False,
        "telescopes": {"t1": {"state": "observing", "exposing": True, "current": {"request": "x"}}},
    }
    (api.tmp / "seq-status.json").write_text(json.dumps(snap))
    s = (await api.get("reader", "/status")).json()["enclosures"]["aqawan1"]
    sup = s["supervisor"]
    assert sup["reachable"] and sup["safe"] is False and sup["reason"].startswith("humidity")
    assert sup["shutter"] == "Closed" and sup["alarms"] == 2
    assert sup["lease"]["holder"] == "aqawan1-sequencer" and "token" not in sup["lease"]
    assert "abc.7" not in json.dumps(s)
    assert s["sequencer"]["running"] is True
    assert s["telescopes"]["t1"] == {
        "state": "observing",
        "exposing": True,
        "current": {"request": "x"},
    }
    assert s["telescopes"]["t2"]["state"] == "unknown"
    assert s["queue"]["pending"] == 1
    # A stale snapshot: the sequencer is not running; nothing is claimed.
    snap["time"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    (api.tmp / "seq-status.json").write_text(json.dumps(snap))
    s = (await api.get("reader", "/status")).json()["enclosures"]["aqawan1"]
    assert s["sequencer"]["running"] is False and s["telescopes"]["t1"]["state"] == "unknown"


async def test_status_with_the_supervisor_down(api):
    s = (await api.get("reader", "/status")).json()["enclosures"]["aqawan1"]
    assert s["supervisor"] == {"reachable": False}
    assert s["sequencer"]["running"] is False


async def test_status_from_a_real_supervisor_and_sequencer(make_rig, tmp_path):
    """The whole path on fakes: the sequencer runs the dispatch plan from
    the API's files and writes its status snapshot; the API reads the
    supervisor's HTTP status and the snapshot; a ToO submitted through the
    API interrupts t1 on the real sequencer."""
    rig_files = write_site(tmp_path)
    config, spec, base = load_api_config(rig_files)
    clock = Clock()
    plan_cfg = json.loads((tmp_path / "aqawan1.json").read_text())["plan"]
    plan_cfg.pop("type")
    plan_cfg["requests"] = [
        group("a", telescopes=["t1"], exptime=1.0, count=100, cadence={"min_interval": 0}),
        group("b", telescopes=["t2"], exptime=1.0, count=100, cadence={"min_interval": 0}),
    ]
    enc_cfg = json.loads((tmp_path / "aqawan1.json").read_text())
    enc_cfg["plan"] = {"type": "dispatch", **plan_cfg}
    sched = dispatch_from_config(plan_cfg, ["t1", "t2"], base=tmp_path, clock=clock)
    rig = make_rig(telescopes=2, plan=sched)
    await rig.start()
    sup_server = await SupervisorServer(rig.supervisor).start()
    enc_cfg["supervisor_url"] = sup_server.url
    (tmp_path / "aqawan1.json").write_text(json.dumps(enc_cfg))
    store = TokenStore(config.tokens)
    token = store.add("alerts", ["read", "too"])[1]
    app = RequestApi(build_enclosures(spec, base, clock=clock), store, config=config, clock=clock)
    server = await HttpServer(app).start()
    client = httpx.AsyncClient(
        base_url=server.url, headers={"Authorization": f"Token {token}"}, timeout=30
    )
    serve = asyncio.create_task(sched.serve())
    try:
        await wait_until(rig.exposing, 5, what="both cameras exposing")
        write_snapshot(rig.sequencer, tmp_path / "seq-status.json")
        s = (await client.get("/api/v1/status")).json()["enclosures"]["aqawan1"]
        assert s["supervisor"]["reachable"] and s["supervisor"]["safe"] is True
        assert s["supervisor"]["lease"]["holder"] == "seq" and s["supervisor"]["lease"]["live"]
        assert s["telescopes"]["t1"]["state"] == "observing"
        assert s["telescopes"]["t1"]["current"]["request"] == "a-r"
        assert s["telescopes"]["t2"]["current"]["group"] == "b"
        burst = imaging("burst", telescopes=["t1"], too=too("now"), exptime=0.2, count=2)
        assert (await client.post("/api/v1/toos", json=burst)).status_code == 202

        def started():
            return [
                e
                for e in rig.timeline.events("target_start")
                if e.detail["target"].startswith("burst")
            ]

        await wait_until(started, 5, what="t1 starts the ToO")
        assert started()[0].detail["telescope"] == "t1"
        await wait_until(lambda: sched.status("burst-c") is Status.COMPLETED, 5, what="done")
        g = (await client.get("/api/v1/requestgroups/burst")).json()
        assert g["state"] == "COMPLETED"
    finally:
        serve.cancel()
        await client.aclose()
        await server.stop()
        await app.aclose()
        await sup_server.stop()
        await rig.stop()


async def test_sequencer_snapshot_states(make_rig, tmp_path):
    rig = make_rig(telescopes=1)
    snap = snapshot(rig.sequencer)
    assert snap["telescopes"]["t1"]["state"] == "waiting" and snap["max_age"] == 15
    rig.sequencer.units["t1"].gave_up = True
    assert snapshot(rig.sequencer)["telescopes"]["t1"]["state"] == "gave_up"
    write_snapshot(rig.sequencer, tmp_path / "s.json")
    assert json.loads((tmp_path / "s.json").read_text())["needs_operator"] is True


# -- the HTTP layer --------------------------------------------------------------------------


async def raw(api: Rig, data: bytes) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection(api.server.host, api.server.port)
    try:
        writer.write(data)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()
    head, _, body = resp.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


async def test_oversized_and_malformed_requests(api):
    tok = api.tokens["tom"].encode()
    auth = b"Authorization: Token " + tok + b"\r\n"
    cases = [
        (
            b"POST /api/v1/requestgroups HTTP/1.1\r\n"
            + auth
            + b"Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n",
            413,
        ),
        (
            b"POST /api/v1/requestgroups HTTP/1.1\r\n"
            + auth
            + b"Transfer-Encoding: chunked\r\n\r\n",
            411,
        ),
        (
            b"POST /api/v1/requestgroups HTTP/1.1\r\n"
            + auth
            + b"Content-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}",
            415,
        ),
        (b"GET /api/v1/ HTTP/2.0\r\n\r\n", 505),
        (b"NONSENSE\r\n\r\n", 400),
        (b"GET /api/v1/ HTTP/1.1\r\nX-Big: " + b"a" * 20000 + b"\r\n\r\n", 431),
        (b"GET /" + b"a" * 20000 + b" HTTP/1.1\r\n\r\n", 414),
        (
            b"GET /api/v1/ HTTP/1.1\r\n"
            + b"".join(b"X-%d: 1\r\n" % i for i in range(100))
            + b"\r\n",
            431,
        ),
        (b"GET /api/v1/ HTTP/1.1\r\nContent-Length: x\r\n\r\n", 400),
        (b"GET /api/v1/ HTTP/1.1\r\n" + auth + auth + b"\r\n", 400),
    ]
    for data, code in cases:
        status, body = await raw(api, data)
        assert status == code, (data[:60], body)
        assert b"Traceback" not in body and json.loads(body)["error"]


async def test_a_slow_client_is_cut_off(make_api):
    api = await make_api(header_timeout=0.2)
    reader, writer = await asyncio.open_connection(api.server.host, api.server.port)
    writer.write(b"GET /api/v1/ HTTP/1.1\r\n")  # and nothing more
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert resp.startswith(b"HTTP/1.1 408 ")


async def test_routes(api):
    assert (await api.get("tom", "/nowhere")).status_code == 404
    r = await api.client.delete("/api/v1/requestgroups", headers=api.auth("tom"))
    assert r.status_code == 405 and "POST" in r.headers["Allow"]
    assert (await api.get("tom", "/requestgroups/")).status_code == 200  # trailing slash
    assert (await api.client.get("/elsewhere")).status_code == 404
    assert (await api.get("tom", "/requestgroups?limit=x")).status_code == 400


async def test_errors_never_leak_a_stack_trace(api, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("secret detail /etc/robofast")

    monkeypatch.setattr(api.app, "_list", boom)
    r = await api.get("tom", "/requestgroups")
    assert r.status_code == 500
    assert set(r.json()) == {"error", "incident"}
    assert "secret" not in r.text and "Traceback" not in r.text
    assert r.json()["incident"] in caplog.text


# -- the audit log --------------------------------------------------------------------------------


async def test_every_post_is_audited(api):
    gid = (await api.post("tom", "/requestgroups", imaging("aud"))).json()["id"]
    await api.post("tom", f"/requestgroups/{gid}/cancel")
    await api.post("reader", "/requestgroups", imaging("nope"))
    await api.post("tom", "/requestgroups", imaging("bad", dec=95))
    await api.client.post("/api/v1/toos", json=imaging("anon"))
    await api.get("tom", "/requestgroups")  # a read: not audited
    entries = api.audit()
    assert [(e.get("client"), e["path"], e["status"]) for e in entries] == [
        ("tom", "/api/v1/requestgroups", 202),
        ("tom", "/api/v1/requestgroups/aud/cancel", 200),
        ("reader", "/api/v1/requestgroups", 403),
        ("tom", "/api/v1/requestgroups", 400),
        (None, "/api/v1/toos", 401),
    ]
    assert entries[0]["group"] == "aud" and entries[1]["group"] == "aud"
    assert entries[0]["addr"] == "127.0.0.1" and entries[0]["token_id"]
    assert "dec_deg" in entries[3]["errors"][0]
    text = (api.tmp / "log" / "audit.jsonl").read_text()
    assert not any(t.split("_", 2)[2] in text for t in api.tokens.values())


# -- binding, TLS and the command line ----------------------------------------------------------


def test_refuses_a_public_address_without_tls():
    check_bind("127.0.0.1", None, False)
    check_bind("::1", None, False)
    check_bind("localhost", None, False)
    for host in ("0.0.0.0", "192.0.2.10", "", "example.org"):
        with pytest.raises(ValueError, match="without TLS"):
            check_bind(host, None, False)
        check_bind(host, None, True)  # --insecure-bind


def test_serve_refuses_a_public_address_from_the_command_line(tmp_path, capsys):
    path = write_site(tmp_path, listen="0.0.0.0:8300")
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--config", str(path)])
    assert exc.value.code == 2
    assert "--insecure-bind" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
async def test_tls(tmp_path, monkeypatch):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(  # noqa: ASYNC221 (a test fixture, before the server starts)
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    path = write_site(tmp_path, tls_cert=str(cert), tls_key=str(key))
    config, spec, base = load_api_config(path)
    ctx = tls_context(config)
    check_bind("0.0.0.0", ctx, False)  # TLS here: a public address is allowed
    app = RequestApi(build_enclosures(spec, base), TokenStore(config.tokens), config=config)
    server = await HttpServer(app, tls=ctx).start()
    try:
        assert server.url.startswith("https://")
        async with httpx.AsyncClient(verify=False) as c:
            assert (await c.get(f"{server.url}/healthz")).json() == {"ok": True}
    finally:
        await server.stop()
        await app.aclose()


async def test_the_service_process(tmp_path):
    """``python -m robofast_core.request_api serve``: prints its URL,
    answers, refuses without a token, exits 0 on SIGTERM."""
    import signal
    import sys

    path = write_site(tmp_path)
    token = TokenStore(load_api_config(path)[0].tokens).add("tom", ["read"])[1]
    root = ROOT
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "robofast_core.request_api",
        "serve",
        "--config",
        str(path),
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), 60)
        url = json.loads(line)["url"]
        async with httpx.AsyncClient(base_url=url) as c:
            assert (await c.get("/api/v1/status")).status_code == 401
            r = await c.get("/api/v1/", headers={"Authorization": f"Token {token}"})
            assert r.json()["name"] == "tom"
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 30) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def test_tokens_cli(tmp_path, capsys):
    path = tmp_path / "secret" / "tokens.json"
    main(["tokens", "add", "--tokens", str(path), "--name", "tom", "--scope", "read,submit"])
    out = capsys.readouterr()
    token = out.out.strip()
    assert token.startswith("rft_") and "cannot be shown again" in out.err
    rec = TokenStore(path).authenticate(token)
    assert rec.name == "tom" and rec.scopes == ["read", "submit"]
    main(["tokens", "list", "--tokens", str(path)])
    listed = json.loads(capsys.readouterr().out)
    assert listed["name"] == "tom" and "hash" not in listed and "salt" not in listed
    main(["tokens", "revoke", "--tokens", str(path), "--name", "tom"])
    assert json.loads(capsys.readouterr().out)["revoked"] == [rec.id]
    assert TokenStore(path).authenticate(token) is None
    with pytest.raises(SystemExit):
        main(["tokens", "revoke", "--tokens", str(path), "--name", "tom"])  # none left


def test_tokens_cli_with_the_api_config_audits(tmp_path, capsys):
    path = write_site(tmp_path)
    main(["tokens", "add", "--config", str(path), "--name", "x", "--scope", "admin"])
    capsys.readouterr()
    (entry,) = [json.loads(x) for x in (tmp_path / "log" / "audit.jsonl").read_text().splitlines()]
    assert entry["action"] == "token_add" and entry["name"] == "x"


def test_tokens_cli_refuses_a_repository(tmp_path, capsys):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    path = tmp_path / "repo" / "tokens.json"
    with pytest.raises(SystemExit):
        main(["tokens", "add", "--tokens", str(path), "--name", "x", "--scope", "read"])
    assert "git work tree" in capsys.readouterr().err and not path.exists()
    main(["tokens", "add", "--tokens", str(path), "--name", "x", "--scope", "read", "--force"])
    assert path.exists()


def test_config_errors(tmp_path):
    path = write_site(tmp_path, bogus=1)
    with pytest.raises(ValueError, match="unknown keys"):
        load_api_config(path)
    path = write_site(tmp_path)
    cfg = json.loads((tmp_path / "aqawan1.json").read_text())
    del cfg["plan"]["too_dropbox"]
    (tmp_path / "aqawan1.json").write_text(json.dumps(cfg))
    _, spec, base = load_api_config(path)
    with pytest.raises(ValueError, match="too_dropbox"):
        build_enclosures(spec, base)
    with pytest.raises(ValueError, match="data_path_template"):
        RequestApi({}, TokenStore(tmp_path / "t.json"), config=ApiConfig(data_path_template="{x}"))


def test_the_example_config_loads(tmp_path):
    """sites/minerva/request_api.example.json, with its paths made local."""
    sites = Path(__file__).resolve().parents[2] / "sites" / "minerva"
    for name in ("aqawan1.json", "dispatch.example.json", "requests.example.json"):
        shutil.copy(sites / name, tmp_path / name)
    cfg = json.loads((sites / "request_api.example.json").read_text())
    cfg["tokens"] = str(tmp_path / "tokens.json")
    cfg["audit_log"] = str(tmp_path / "audit.jsonl")
    (tmp_path / "api.json").write_text(json.dumps(cfg))
    config, spec, base = load_api_config(tmp_path / "api.json")
    encs = build_enclosures(spec, base)
    assert encs["aqawan1"].telescopes == ["t1", "t2"]
    assert encs["aqawan1"].dropbox == tmp_path / "too-dropbox"
    assert os.path.isabs(config.tokens)
