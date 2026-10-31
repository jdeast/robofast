"""The alarm notifier (docs/notifier.md): severity, dedup, rate limit,
escalation and acknowledgement, the digest, sinks that fail or hang, and
each sink type -- on fake sources, fake sinks, a fake SMTP class, an
``httpx.MockTransport`` and local subprocesses. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from robofast_core.console.aggregate import Aggregator, ConsoleEnclosure, alarm_id
from robofast_core.notifier.core import Notification, Notifier, Settings, SinkWorker, next_daily
from robofast_core.notifier.service import build, main, send_test
from robofast_core.notifier.sinks import (
    CommandSink,
    EmailSink,
    WebhookSink,
    check_secret_file,
)
from robofast_core.notifier.sources import AcksFile, AggregatorSource, ConsoleHealth

REPO = Path(__file__).resolve().parents[2]
#: 2026-10-30 20:00 UTC.
T0 = datetime(2026, 10, 30, 20, 0, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeSource:
    name = "fake"

    def __init__(self) -> None:
        self.current: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def set(self, *alarms: tuple[str, str]) -> None:
        """``(text, level)`` pairs, all in enclosure aqawan1."""
        self.current = [
            {
                "id": alarm_id("aqawan1", "test", text),
                "enclosure": "aqawan1",
                "source": "test",
                "level": level,
                "text": text,
                "detail": "",
            }
            for text, level in alarms
        ]

    async def alarms(self) -> list[dict[str, Any]]:
        if self.fail is not None:
            raise self.fail
        return list(self.current)


class FakeSink:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.got: list[Notification] = []

    async def send(self, n: Notification) -> None:
        self.got.append(n)

    def kinds(self) -> list[str]:
        return [n.kind for n in self.got]


class FailingSink(FakeSink):
    async def send(self, n: Notification) -> None:
        self.got.append(n)
        raise ConnectionError("no route to the fake host")


class HangingSink(FakeSink):
    async def send(self, n: Notification) -> None:
        self.got.append(n)
        await asyncio.sleep(3600)


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def worker(sink: Any, clock: Clock, **kw: Any) -> SinkWorker:
    kw.setdefault("retry_base", 0.0)
    return SinkWorker(sink, clock=clock, sleep=no_sleep, **kw)


def make(
    clock: Clock,
    *workers: SinkWorker,
    source: FakeSource | None = None,
    acks: Any = None,
    **settings: Any,
) -> tuple[Notifier, FakeSource]:
    src = source or FakeSource()
    settings.setdefault("digest_at", None)
    settings.setdefault("timezone", "UTC")
    n = Notifier([src], list(workers), acks=acks, settings=Settings(**settings), clock=clock)
    n.start()
    return n, src


async def poll(n: Notifier) -> None:
    await n.poll_once()
    await n.drain(5)


class DictAcks:
    def __init__(self) -> None:
        self.acks: dict[str, dict[str, Any]] = {}

    def read(self) -> dict[str, dict[str, Any]]:
        return self.acks

    def ack(self, aid: str, at: float, by: str = "alice") -> None:
        self.acks[aid] = {
            "by": by,
            "time": datetime.fromtimestamp(at, UTC).isoformat(),
            "reason": "on it",
        }


# -- the engine ------------------------------------------------------------------------


async def test_severity_filter_per_sink() -> None:
    clock = Clock()
    low, high = FakeSink("low"), FakeSink("high")
    n, src = make(
        clock, worker(low, clock, min_level="warning"), worker(high, clock, min_level="critical")
    )
    src.set(
        ("just so you know", "info"), ("guider unreachable", "warning"), ("gave up", "critical")
    )
    await poll(n)
    assert sorted(x.alarm["text"] for x in low.got) == ["gave up", "guider unreachable"]
    assert [x.alarm["text"] for x in high.got] == ["gave up"]
    assert high.got[0].subject.startswith("[robofast CRITICAL] aqawan1: gave up")
    await n.aclose()


async def test_an_active_alarm_is_sent_once_and_cleared_after_clear_after() -> None:
    clock = Clock()
    sink = FakeSink()
    n, src = make(clock, worker(sink, clock, on=["raise", "clear"]), clear_after=120)
    src.set(("shutter did not close", "alarm"))
    for _ in range(5):
        await poll(n)
        clock.advance(10)
    assert sink.kinds() == ["raise"]
    src.set()
    await poll(n)  # gone for less than clear_after: a flap, not a clear
    src.set(("shutter did not close", "alarm"))
    await poll(n)
    assert sink.kinds() == ["raise"]
    src.set()
    clock.advance(121)
    await poll(n)
    assert sink.kinds() == ["raise", "clear"]
    assert sink.got[1].level == "alarm" and "cleared" in sink.got[1].subject
    src.set(("shutter did not close", "alarm"))  # a new episode: sent again
    await poll(n)
    assert sink.kinds() == ["raise", "clear", "raise"]
    await n.aclose()


async def test_rate_limit_suppresses_and_summarizes() -> None:
    clock = Clock()
    sink = FakeSink()
    n, src = make(clock, worker(sink, clock, rate_max=2, rate_window=600), escalate_after=None)
    src.set(*[(f"alarm {i}", "critical") for i in range(5)])
    await poll(n)
    assert len(sink.got) == 2
    clock.advance(300)
    await poll(n)  # the window still full: no summary yet
    assert len(sink.got) == 2
    clock.advance(301)
    await poll(n)
    assert [x.kind for x in sink.got] == ["raise", "raise", "suppressed"]
    summary = sink.got[-1]
    assert "3 notification(s) suppressed" in summary.subject
    assert summary.level == "critical" and "alarm 4" in summary.body
    await n.aclose()


async def test_escalation_goes_to_escalation_sinks_until_acknowledged() -> None:
    clock = Clock()
    mail, page = FakeSink("mail"), FakeSink("page")
    acks = DictAcks()
    n, src = make(
        clock,
        worker(mail, clock, on=["raise", "escalate", "ack"]),
        worker(page, clock, on=["escalate"], min_level="critical"),
        acks=acks,
        escalate_after=600,
        escalate_repeat=300,
    )
    src.set(("lease #7 quarantined", "critical"), ("would close: rain", "warning"))
    await poll(n)
    clock.advance(599)
    await poll(n)
    assert page.got == [] and mail.kinds() == ["raise", "raise"]
    clock.advance(2)
    await poll(n)
    assert [x.kind for x in page.got] == ["escalate"]
    assert "NOT ACKNOWLEDGED" in page.got[0].subject
    assert "unacknowledged for 10 min" in page.got[0].body
    clock.advance(300)
    await poll(n)
    assert len(page.got) == 2  # repeated every escalate_repeat
    acks.ack(src.current[0]["id"], clock.t)
    await poll(n)
    assert mail.kinds()[-1] == "ack" and "alice" in mail.got[-1].body
    clock.advance(3600)
    await poll(n)
    assert len(page.got) == 2  # acknowledged: no more escalation
    await n.aclose()


async def test_an_old_ack_does_not_silence_a_new_episode() -> None:
    clock = Clock()
    page = FakeSink("page")
    acks = DictAcks()
    n, src = make(clock, worker(page, clock, on=["escalate"]), acks=acks, escalate_after=60)
    src.set(("sequencer needs an operator", "critical"))
    acks.ack(src.current[0]["id"], clock.t - 3600)  # an hour before this episode
    await poll(n)
    clock.advance(61)
    await poll(n)
    assert [x.kind for x in page.got] == ["escalate"]
    await n.aclose()


async def test_acks_from_the_console_file(tmp_path: Path) -> None:
    clock = Clock()
    page = FakeSink("page")
    path = tmp_path / "console-acks.json"
    n, src = make(
        clock, worker(page, clock, on=["escalate"]), acks=AcksFile(path), escalate_after=60
    )
    src.set(("t1 gave up for the night", "critical"))
    await poll(n)  # no file yet: nothing acknowledged
    aid = src.current[0]["id"]
    path.write_text(
        json.dumps({aid: {"by": "bob", "time": datetime.now(UTC).isoformat(), "reason": "x"}})
    )
    clock.t = time.time()  # the console stamps acks with the wall clock
    n.active[aid].first_seen = clock.t - 5
    await poll(n)
    clock.advance(120)
    await poll(n)
    assert page.got == []
    await n.aclose()


async def test_daily_digest(tmp_path: Path) -> None:
    clock = Clock()
    mail = FakeSink("mail")
    n, src = make(
        clock,
        worker(mail, clock, min_level="critical", on=["raise", "digest"]),
        digest_at="08:00",
        timezone="UTC",
        clear_after=60,
        escalate_after=None,
    )
    src.set(("weather bridge status unreachable", "warning"), ("t2 gave up", "critical"))
    await poll(n)
    clock.advance(3600)
    src.set(("t2 gave up", "critical"))
    await poll(n)
    clock.advance(3600)
    await poll(n)
    assert mail.kinds() == ["raise"]
    clock.t = datetime(2026, 10, 31, 8, 0, 1, tzinfo=UTC).timestamp()
    await poll(n)
    assert mail.kinds() == ["raise", "digest"]  # the digest ignores min_level
    d = mail.got[-1]
    assert "2026-10-31: 2 alarm(s), 1 active" in d.subject
    assert "weather bridge status unreachable -- cleared" in d.body
    assert "t2 gave up -- still active" in d.body
    assert "mail: sent 1" in d.body
    clock.advance(3600)
    await poll(n)
    assert mail.kinds().count("digest") == 1  # once a day
    clock.t = datetime(2026, 11, 1, 8, 0, 1, tzinfo=UTC).timestamp()
    await poll(n)
    assert mail.kinds().count("digest") == 2
    assert "1 alarm(s)" in mail.got[-1].subject  # the cleared one was reported already
    await n.aclose()


def test_next_daily_across_dst() -> None:
    # 2026-11-01 is the end of US daylight time: 08:00 New York is 13:00 UTC.
    after = datetime(2026, 10, 31, 12, 30, tzinfo=UTC).timestamp()
    nxt = next_daily(after, "08:00", "America/New_York")
    assert datetime.fromtimestamp(nxt, UTC) == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        Settings(digest_at="25:00")


async def test_a_failing_or_hanging_sink_does_not_block_the_others() -> None:
    clock = Clock()
    bad, slow, good = FailingSink("bad"), HangingSink("slow"), FakeSink("good")
    wb, ws, wg = (
        worker(bad, clock, retries=2),
        worker(slow, clock, timeout=0.2, retries=0),
        worker(good, clock),
    )
    n, src = make(clock, wb, ws, wg)
    src.set(("enclosure fault", "critical"))
    t0 = time.monotonic()
    await asyncio.wait_for(n.poll_once(), 1)
    assert time.monotonic() - t0 < 0.5  # the poll never waits on a sink
    await asyncio.wait_for(wg.queue.join(), 1)
    assert [x.kind for x in good.got] == ["raise"]
    await asyncio.wait_for(asyncio.gather(wb.queue.join(), ws.queue.join()), 2)
    assert len(bad.got) == 3 and wb.stats["failed"] == 1  # 1 try + 2 retries
    assert ws.stats["failed"] == 1
    src.set(("enclosure fault", "critical"), ("another", "critical"))
    await poll(n)  # the workers outlive their failures
    assert len(good.got) == 2 and len(bad.got) == 6
    await n.aclose()


async def test_a_full_queue_drops_instead_of_blocking() -> None:
    clock = Clock()
    w = worker(HangingSink(), clock, queue_size=1, timeout=60)
    n, src = make(clock, w)
    src.set(*[(f"a{i}", "critical") for i in range(4)])
    await asyncio.wait_for(n.poll_once(), 1)
    assert w.stats["dropped"] >= 2
    await n.aclose()


async def test_a_failing_source_is_itself_an_alarm() -> None:
    clock = Clock()
    sink = FakeSink()
    n, src = make(clock, worker(sink, clock))
    src.fail = RuntimeError("boom")
    await poll(n)
    assert sink.got[0].level == "critical"
    assert "alarm source fake failed" in sink.got[0].subject
    assert "boom" in sink.got[0].body


async def test_ignore_patterns() -> None:
    clock = Clock()
    sink = FakeSink()
    n, src = make(clock, worker(sink, clock), ignore=["^sequencer not running"])
    src.set(
        ("sequencer not running (status snapshot stale)", "warning"), ("t1 gave up", "critical")
    )
    await poll(n)
    assert [x.alarm["text"] for x in sink.got] == ["t1 gave up"]
    await n.aclose()


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    sink = FakeSink()
    state = tmp_path / "state.json"
    src = FakeSource()
    settings = Settings(digest_at=None, escalate_after=600)
    n = Notifier([src], [worker(sink, clock)], settings=settings, clock=clock, state=state)
    n.start()
    src.set(("t1 gave up", "critical"))
    await poll(n)
    await n.aclose()
    sink2 = FakeSink()
    n2 = Notifier([src], [worker(sink2, clock)], settings=settings, clock=clock, state=state)
    n2.start()
    clock.advance(30)
    await poll(n2)
    assert sink2.got == []  # already sent before the restart
    clock.advance(600)
    await poll(n2)
    assert [x.kind for x in sink2.got] == ["escalate"]  # the clock kept running
    await n2.aclose()


# -- the sources ------------------------------------------------------------------------


async def test_aggregator_source_reuses_the_console_alarms(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "sup" and req.url.path == "/status":
            return httpx.Response(200, json={"alarms": ["shutter did not reach CLOSED"]})
        if req.url.path == "/timeline":
            return httpx.Response(200, json=[])
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        encs = {
            "a1": ConsoleEnclosure("a1", "http://sup", []),
            "a2": ConsoleEnclosure("a2", "http://down", [], spectrograph_url="http://spec"),
        }
        got = await AggregatorSource(Aggregator(encs, http, timeout=1)).alarms()
        texts = {(a["enclosure"], a["level"], a["text"]) for a in got}
        assert ("a1", "alarm", "shutter did not reach CLOSED") in texts
        assert any(e == "a2" and lv == "critical" and "supervisor" in t for e, lv, t in texts)
        assert any(e == "a2" and "spectrograph service unreachable" in t for e, _, t in texts)
        health = await ConsoleHealth("http://console", http).alarms()
        assert health[0]["text"] == "observer console unreachable"


def test_acks_file_keeps_the_last_good_copy(tmp_path: Path) -> None:
    p = tmp_path / "acks.json"
    a = AcksFile(p)
    assert a.read() == {}
    p.write_text(json.dumps({"x": {"by": "a", "time": "2026-10-30T20:00:00+00:00"}}))
    assert set(a.read()) == {"x"}
    p.write_text("{not json")
    assert set(a.read()) == {"x"}


# -- the sinks --------------------------------------------------------------------------


def note(kind: str = "raise", level: str = "critical") -> Notification:
    return Notification(
        kind,
        level,
        "[robofast CRITICAL] aqawan1: t1 gave up",
        "t1 gave up\nbody",
        T0,
        {"id": "abc", "enclosure": "aqawan1"},
    )


def secret(tmp_path: Path, name: str, content: str, mode: int = 0o600) -> Path:
    p = tmp_path / name
    p.write_text(content)
    p.chmod(mode)
    return p


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float, **kw: Any) -> None:
        self.calls: list[tuple] = [("connect", host, port, timeout, sorted(kw))]
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls.append(("quit",))

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self, context: Any) -> None:
        assert context is not None
        self.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))

    def send_message(self, msg: Any) -> None:
        self.calls.append(("send", msg["Subject"], msg["To"], msg.get_content()))


async def test_email_starttls_then_login(tmp_path: Path) -> None:
    FakeSMTP.instances.clear()
    creds = secret(tmp_path, "smtp.json", json.dumps({"username": "u", "password": "p"}))
    sink = EmailSink(
        "mail",
        host="smtp.example.org",
        sender="robofast@example.org",
        to=["observers@example.org", "pi@example.org"],
        credentials_file=str(creds),
        timeout=7,
        smtp_factory=FakeSMTP,
    )
    await sink.send(note())
    calls = FakeSMTP.instances[0].calls
    assert calls[0] == ("connect", "smtp.example.org", 587, 7.0, [])
    assert [c[0] for c in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert calls[4] == ("login", "u", "p")
    assert calls[5][1] == "[robofast CRITICAL] aqawan1: t1 gave up"
    assert calls[5][2] == "observers@example.org, pi@example.org"
    assert "t1 gave up" in calls[5][3]


async def test_email_implicit_tls_and_loopback_relay() -> None:
    FakeSMTP.instances.clear()
    ssl_sink = EmailSink(
        "m", host="smtp.example.org", sender="a@example.org", to="b@example.org", tls="ssl"
    )
    ssl_sink.smtp_factory = FakeSMTP
    await ssl_sink.send(note())
    assert FakeSMTP.instances[0].calls[0] == ("connect", "smtp.example.org", 465, 20.0, ["context"])
    assert "starttls" not in [c[0] for c in FakeSMTP.instances[0].calls]
    relay = EmailSink("r", host="127.0.0.1", sender="a@example.org", to="b@example.org", tls="none")
    assert relay.port == 25
    with pytest.raises(ValueError, match="loopback"):
        EmailSink("x", host="smtp.example.org", sender="a@example.org", to="b", tls="none")


def test_credentials_must_not_be_group_or_world_readable(tmp_path: Path) -> None:
    for mode in (0o644, 0o640, 0o604, 0o660):
        p = secret(tmp_path, f"c{mode:o}.json", '{"username": "u", "password": "p"}', mode)
        with pytest.raises(PermissionError, match="chmod 600"):
            check_secret_file(p)
        with pytest.raises(PermissionError):
            EmailSink(
                "m",
                host="smtp.example.org",
                sender="a@example.org",
                to="b@example.org",
                credentials_file=str(p),
            )
    ok = secret(tmp_path, "ok.json", '{"username": "u", "password": "p"}', 0o600)
    assert check_secret_file(ok) == ok
    with pytest.raises(PermissionError, match="regular file"):
        check_secret_file(tmp_path)


def test_credentials_directory_is_expanded(tmp_path: Path, monkeypatch) -> None:
    secret(tmp_path, "smtp", '{"username": "u", "password": "p"}', 0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert check_secret_file("$CREDENTIALS_DIRECTORY/smtp") == tmp_path / "smtp"


async def test_webhook_payload(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text="ok")

    url = secret(tmp_path, "hook", "https://chat.example.org/hooks/T0/B0/xyz\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sink = WebhookSink("chat", url_file=str(url), extra={"username": "robofast"}, client=http)
        await sink.send(note())
    req = seen[0]
    assert str(req.url) == "https://chat.example.org/hooks/T0/B0/xyz"
    body = json.loads(req.content)
    assert body["username"] == "robofast"
    assert body["text"].startswith("[robofast CRITICAL] aqawan1: t1 gave up\nt1 gave up")
    assert body["robofast"]["kind"] == "raise" and body["robofast"]["alarm"]["id"] == "abc"
    with pytest.raises(ValueError, match="https"):
        WebhookSink("x", url="http://chat.example.org/hook")
    WebhookSink("loop", url="http://127.0.0.1:9/hook")  # plain http to loopback is fine


async def test_webhook_error_status_raises() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as http:
        sink = WebhookSink("chat", url="https://chat.example.org/h", client=http)
        with pytest.raises(httpx.HTTPStatusError):
            await sink.send(note())


async def test_command_hook_gets_json_on_stdin_and_env(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    script = (
        "import json, os, sys; d = json.load(sys.stdin); "
        "d['env'] = {k: v for k, v in os.environ.items() if k.startswith('ROBOFAST_')}; "
        f"open({str(out)!r}, 'w').write(json.dumps(d))"
    )
    sink = CommandSink("page", argv=[sys.executable, "-c", script], env={"ROBOFAST_SITE": "x"})
    await sink.send(note("escalate"))
    d = json.loads(out.read_text())
    assert d["kind"] == "escalate" and d["alarm"]["enclosure"] == "aqawan1"
    assert d["env"]["ROBOFAST_NOTIFY_LEVEL"] == "critical"
    assert d["env"]["ROBOFAST_NOTIFY_ALARM_ID"] == "abc"
    assert d["env"]["ROBOFAST_SITE"] == "x"
    fail = CommandSink("f", argv=[sys.executable, "-c", "import sys; sys.exit('nope')"])
    with pytest.raises(RuntimeError, match="exited 1: nope"):
        await fail.send(note())
    with pytest.raises(ValueError, match="absolute"):
        CommandSink("rel", argv=["python3"])


async def test_command_hook_timeout_kills_it(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    sink = CommandSink("slow", argv=[sys.executable, "-c", script], timeout=1.0)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="ran past 1 s"):
        await sink.send(note())
    assert time.monotonic() - t0 < 10
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the hook is still running")


# -- the config -------------------------------------------------------------------------


def write_config(tmp_path: Path, **extra: Any) -> Path:
    hook = secret(tmp_path, "hook-url", "https://chat.example.org/hooks/abc")
    cfg = {
        "enclosures": {"aqawan1": str(REPO / "sites/minerva/aqawan1.json")},
        "acks": "acks.json",
        "state": "state.json",
        "digest_at": "08:00",
        "timezone": "UTC",
        "sinks": {
            "chat": {"type": "webhook", "url_file": hook.name, "min_level": "alarm"},
            "page": {
                "type": "command",
                "argv": [sys.executable, "-c", "pass"],
                "on": ["escalate", "test"],
                "min_level": "critical",
                "timeout": 5,
            },
        },
        **extra,
    }
    p = tmp_path / "notifier.json"
    p.write_text(json.dumps(cfg))
    return p


async def test_build_from_config(tmp_path: Path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as http:
        n = build(write_config(tmp_path), http=http)
        assert [w.name for w in n.workers] == ["chat", "page"]
        assert n.workers[1].on == {"escalate", "test"}
        assert n.workers[1].timeout == 15  # the sink's timeout plus a margin
        assert n.acks.path == tmp_path / "acks.json"
        assert n.state_path == tmp_path / "state.json"
        assert n.settings.digest_at == "08:00"
        alarms = await n.sources[0].alarms()  # everything down: alarms, not exceptions
        assert any("supervisor unreachable" in a["text"] for a in alarms)
        with pytest.raises(ValueError, match="unknown keys"):
            build(write_config(tmp_path, bogus=1), http=http)


async def test_send_test_reaches_every_sink(tmp_path: Path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as http:
        n = build(write_config(tmp_path), http=http)
        n.workers[0].sink._client = http
        stats = await send_test(n, timeout=20)
    assert stats["chat"]["sent"] == 1 and stats["page"]["sent"] == 1


def test_check_flag(tmp_path: Path, capsys) -> None:
    main(["--config", str(write_config(tmp_path)), "--check"])
    assert "config ok" in capsys.readouterr().out
    config = write_config(tmp_path)
    (tmp_path / "hook-url").chmod(0o644)
    with pytest.raises(SystemExit):
        main(["--config", str(config), "--check"])


def test_example_config_and_unit_exist() -> None:
    cfg = json.loads((REPO / "deploy/notifier.example.json").read_text())
    assert {s["type"] for s in cfg["sinks"].values()} == {"email", "webhook", "command"}
    text = json.dumps(cfg)
    assert "example.org" in text and "127.0.0.1" in text
    unit = (REPO / "deploy/robofast-notifier.service").read_text()
    assert "robofast_core.notifier --config" in unit
