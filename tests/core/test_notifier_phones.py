"""The notifier's phone channels (robofast/notifier/sinks.py, docs/notifier.md):
ntfy, Pushover, Twilio SMS and the generic webhook, each against a fake HTTP
server on loopback that records what it was sent. Nothing leaves the
machine; the secrets come from mode-600 files and never appear in an error."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from robofast.notifier.core import Notification
from robofast.notifier.service import build_sink_workers
from robofast.notifier.sinks import NtfySink, PushoverSink, TwilioSink, WebhookSink


class FakeServer:
    """Records each request (path, headers, body); answers ``status``."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        me = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                me.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(me.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": 1}')

            def log_message(self, *a) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def secret(tmp_path: Path, name: str, content: str, mode: int = 0o600) -> str:
    p = tmp_path / name
    p.write_text(content)
    p.chmod(mode)
    return str(p)


def note(level: str = "critical", kind: str = "raise") -> Notification:
    return Notification(
        kind,
        level,
        "[robofast CRITICAL] aqawan1: t1 camera incident 1: dropped for the night",
        "t1 camera: boom\n\nAgent: exited 1\nOutcome: DROPPED",
        1_800_000_000.0,
        {"id": "abc"},
    )


async def test_ntfy(server, tmp_path):
    topic = secret(tmp_path, "topic", "robofast-secret-topic\n")
    token = secret(tmp_path, "token", "tk_secrettoken\n")
    sink = NtfySink(
        "phone", server=server.url, topic_file=topic, token_file=token, tags=["telescope"]
    )
    await sink.send(note())
    await sink.send(note("warning"))
    await sink.send(note("critical", kind="digest"))
    first, warn, digest = server.requests
    assert first["path"] == "/"
    assert first["headers"]["Authorization"] == "Bearer tk_secrettoken"
    body = json.loads(first["body"])
    assert body["topic"] == "robofast-secret-topic" and body["priority"] == 5
    assert body["title"].startswith("[robofast CRITICAL]") and "DROPPED" in body["message"]
    assert body["tags"] == ["telescope", "critical"]
    assert json.loads(warn["body"])["priority"] == 3
    assert json.loads(digest["body"])["priority"] == 3  # a digest is never urgent
    # An error names the sink and the status, never the topic or the token.
    server.status = 403
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sink.send(note())
    assert "secret" not in str(exc.value) and "403" in str(exc.value)


def test_ntfy_config_refusals(tmp_path):
    with pytest.raises(ValueError, match="https"):
        NtfySink("p", server="http://ntfy.example.org", topic="t")
    with pytest.raises(ValueError, match="exactly one"):
        NtfySink("p", topic="t", topic_file="/x")
    loose = secret(tmp_path, "token", "tk_x", mode=0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        NtfySink("p", topic="t", token_file=loose)


async def test_pushover(server, tmp_path):
    creds = secret(tmp_path, "po.json", json.dumps({"token": "apptoken", "user": "userkey"}))
    sink = PushoverSink(
        "pager", credentials_file=creds, api_url=server.url + "/1/messages.json", retry=60
    )
    await sink.send(note())
    await sink.send(note("alarm"))
    crit, alarm = (parse_qs(r["body"].decode()) for r in server.requests)
    assert server.requests[0]["path"] == "/1/messages.json"
    assert crit["token"] == ["apptoken"] and crit["user"] == ["userkey"]
    # Critical is Pushover's emergency priority: repeated until acknowledged.
    assert crit["priority"] == ["2"] and crit["retry"] == ["60"] and crit["expire"] == ["3600"]
    assert alarm["priority"] == ["1"] and "retry" not in alarm
    assert len(crit["message"][0]) <= 1024
    server.status = 400
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sink.send(note())
    assert "apptoken" not in str(exc.value) and "userkey" not in str(exc.value)
    with pytest.raises(ValueError, match="retry"):
        PushoverSink("p", credentials_file=creds, retry=5)
    bad = secret(tmp_path, "bad.json", json.dumps({"token": "x"}))
    with pytest.raises(ValueError, match="token and user"):
        PushoverSink("p", credentials_file=bad)


async def test_twilio(server, tmp_path):
    creds = secret(
        tmp_path, "tw.json", json.dumps({"account_sid": "AC123", "auth_token": "authsecret"})
    )
    sink = TwilioSink(
        "sms",
        credentials_file=creds,
        to=["+15550000001", "+15550000002"],
        sender="+15550000009",
        api_url=server.url,
        max_text=120,
    )
    await sink.send(note())
    assert [r["path"] for r in server.requests] == ["/2010-04-01/Accounts/AC123/Messages.json"] * 2
    auth = server.requests[0]["headers"]["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == "AC123:authsecret"
    forms = [parse_qs(r["body"].decode()) for r in server.requests]
    assert [f["To"][0] for f in forms] == ["+15550000001", "+15550000002"]
    assert forms[0]["From"] == ["+15550000009"] and len(forms[0]["Body"][0]) <= 120
    server.status = 401
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sink.send(note())
    assert "authsecret" not in str(exc.value)
    with pytest.raises(ValueError, match="exactly one"):
        TwilioSink("s", credentials_file=creds, to="+1555", api_url=server.url)


async def test_generic_webhook_raw(server, tmp_path):
    url = secret(tmp_path, "hook", server.url + "/hooks/secret-token")
    sink = WebhookSink("hook", url_file=url, raw=True)
    await sink.send(note())
    [r] = server.requests
    assert r["path"] == "/hooks/secret-token"
    body = json.loads(r["body"])
    assert body["level"] == "critical" and "DROPPED" in body["body"]
    server.status = 500
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sink.send(note())
    assert "secret-token" not in str(exc.value)


def test_phone_sinks_from_the_notifier_config(tmp_path):
    secret(tmp_path, "topic", "t")
    secret(tmp_path, "po.json", json.dumps({"token": "a", "user": "b"}))
    workers = build_sink_workers(
        {
            "phone": {"type": "ntfy", "topic_file": "topic", "min_level": "critical"},
            "pager": {"type": "pushover", "credentials_file": "po.json", "on": ["raise"]},
        },
        tmp_path,
    )
    assert [type(w.sink).__name__ for w in workers] == ["NtfySink", "PushoverSink"]
