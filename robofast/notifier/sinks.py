"""Where notifications go: email, a chat webhook, a command, and phones --
ntfy, Pushover, Twilio SMS (docs/notifier.md).

Every sink has ``name`` and ``async send(notification)``, which delivers or
raises; its :class:`~robofast.notifier.core.SinkWorker` adds the
timeout, retries and rate limit. Secrets (the SMTP login, a webhook URL that
embeds a token) are read from files outside the repository, which must be
regular files no one but their owner can read or write
(:func:`check_secret_file`); systemd's ``LoadCredential=`` provides exactly
that (``$CREDENTIALS_DIRECTORY`` is expanded in their paths).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import signal
import smtplib
import ssl
import stat
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from robofast.cancel import isolated
from robofast.notifier.core import Notification
from robofast.plugins import Registry


def check_secret_file(path: str | Path) -> Path:
    """``path``, if it is a regular file with no group or other permissions;
    raises ``PermissionError`` (or ``OSError``) otherwise."""
    p = Path(os.path.expandvars(str(path)))
    st = p.stat()
    if not stat.S_ISREG(st.st_mode):
        raise PermissionError(f"{p}: not a regular file")
    if st.st_mode & 0o077:
        raise PermissionError(
            f"{p}: mode {stat.S_IMODE(st.st_mode):04o} lets group or others read it; "
            "chmod 600 (owner: the notifier's user)"
        )
    return p


def read_secret_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(check_secret_file(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def read_secret_text(path: str | Path) -> str:
    return check_secret_file(path).read_text().strip()


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# -- email ---------------------------------------------------------------------------------


class EmailSink:
    """SMTP: STARTTLS on 587 (the default), implicit TLS (``tls: "ssl"``) on
    465, or ``tls: "none"`` to a relay on loopback only. ``credentials_file``
    is JSON ``{"username": ..., "password": ...}``, re-read for each message.
    smtplib is blocking, so each message is sent in a thread with
    ``timeout`` on every socket operation."""

    def __init__(
        self,
        name: str,
        *,
        host: str,
        sender: str,
        to: list[str] | str,
        port: int | None = None,
        tls: str = "starttls",
        credentials_file: str | None = None,
        timeout: float = 20.0,
        smtp_factory: Callable[..., Any] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if tls not in ("starttls", "ssl", "none"):
            raise ValueError(f"email {name}: tls must be starttls, ssl or none")
        if tls == "none" and not is_loopback(host):
            raise ValueError(f"email {name}: tls none is allowed only to a relay on loopback")
        if tls == "none" and credentials_file:
            raise ValueError(f"email {name}: refusing to send a password without TLS")
        self.name, self.host, self.tls = name, host, tls
        self.port = port or {"starttls": 587, "ssl": 465, "none": 25}[tls]
        self.sender = sender
        self.to = [to] if isinstance(to, str) else list(to)
        if not self.to:
            raise ValueError(f"email {name}: no recipients")
        self.credentials_file = credentials_file
        if credentials_file:
            creds = read_secret_json(credentials_file)  # fail at start, not at 3 am
            if not {"username", "password"} <= set(creds):
                raise ValueError(f"{credentials_file}: needs username and password")
        self.timeout = float(timeout)
        self.smtp_factory = smtp_factory or (smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP)
        self.ssl_context = ssl_context

    def message(self, n: Notification) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = n.subject.replace("\n", " ")
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg["Date"] = formatdate(n.time, localtime=True)
        msg["Message-ID"] = make_msgid("robofast")
        msg["X-Robofast-Kind"] = n.kind
        msg["X-Robofast-Level"] = n.level
        msg.set_content(n.body)
        return msg

    def send_sync(self, n: Notification) -> None:
        msg = self.message(n)
        creds = read_secret_json(self.credentials_file) if self.credentials_file else None
        ctx = self.ssl_context or ssl.create_default_context()
        if self.tls == "ssl":
            smtp = self.smtp_factory(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            smtp = self.smtp_factory(self.host, self.port, timeout=self.timeout)
        with smtp:
            smtp.ehlo()
            if self.tls == "starttls":
                # Raises if the server does not offer it: never falls back to plain text.
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if creds is not None:
                smtp.login(str(creds["username"]), str(creds["password"]))
            smtp.send_message(msg)

    async def send(self, n: Notification) -> None:
        await asyncio.to_thread(self.send_sync, n)


# -- chat webhook ----------------------------------------------------------------------------


class WebhookSink:
    """POST JSON to a Slack-style incoming webhook (Slack, Mattermost, a
    Matrix hookshot generic webhook...): ``{"text": subject + body,
    "robofast": the notification}`` plus any static ``extra`` fields, or the
    notification alone with ``raw: true``. The URL comes from ``url`` or,
    since such URLs usually carry a token, ``url_file``. It must be https
    unless the host is loopback."""

    def __init__(
        self,
        name: str,
        *,
        url: str | None = None,
        url_file: str | None = None,
        timeout: float = 10.0,
        extra: dict[str, Any] | None = None,
        raw: bool = False,
        max_text: int = 3500,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if (url is None) == (url_file is None):
            raise ValueError(f"webhook {name}: give exactly one of url and url_file")
        self.name = name
        self._url, self.url_file = url, url_file
        self.check_url(self.url())
        self.timeout = float(timeout)
        self.extra = dict(extra or {})
        self.raw, self.max_text = raw, int(max_text)
        self._client = client

    def url(self) -> str:
        return self._url if self._url is not None else read_secret_text(str(self.url_file))

    def check_url(self, url: str) -> None:
        u = urlsplit(url)
        if u.scheme == "https" or (u.scheme == "http" and is_loopback(u.hostname or "")):
            return
        raise ValueError(f"webhook {self.name}: the URL must be https (or http to loopback)")

    def payload(self, n: Notification) -> dict[str, Any]:
        if self.raw:
            return n.as_dict()
        text = f"{n.subject}\n{n.body}"
        if len(text) > self.max_text:
            text = text[: self.max_text - 15] + "\n... (truncated)"
        return {**self.extra, "text": text, "robofast": n.as_dict()}

    async def send(self, n: Notification) -> None:
        url = self.url()
        self.check_url(url)
        client = self._client
        own = client is None
        if own:
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            r = await isolated(client.post(url, json=self.payload(n), timeout=self.timeout))
            check_response(self.name, r)
        finally:
            if own:
                await client.aclose()


def check_response(name: str, r: httpx.Response) -> None:
    """Raise on a non-2xx answer, naming the sink and the status but never the
    URL (it may carry a token or a private topic)."""
    if not r.is_success:
        raise httpx.HTTPStatusError(f"{name}: HTTP {r.status_code}", request=r.request, response=r)


def check_https(name: str, url: str) -> None:
    u = urlsplit(url)
    if u.scheme == "https" or (u.scheme == "http" and is_loopback(u.hostname or "")):
        return
    raise ValueError(f"{name}: the URL must be https (or http to loopback)")


class _HTTPSink:
    """Shared by the phone sinks: one POST per message with ``httpx`` (no
    redirects, no proxy environment), https only (plain http to loopback,
    for a self-hosted server on the same host or a test)."""

    name: str
    timeout: float
    _client: httpx.AsyncClient | None

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        check_https(self.name, url)
        client = self._client
        own = client is None
        if own:
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            r = await isolated(client.post(url, timeout=self.timeout, **kw))
            check_response(self.name, r)
            return r
        finally:
            if own:
                await client.aclose()


def _cut(text: str, n: int) -> str:
    tail = "\n... (truncated)"
    return text if len(text) <= n else text[: n - len(tail)] + tail


#: ``digest``, ``suppressed`` and ``test`` are not urgent, whatever their level.
QUIET_KINDS = ("digest", "suppressed", "test", "clear", "ack")


# -- ntfy ----------------------------------------------------------------------------------------


class NtfySink(_HTTPSink):
    """Publish to an `ntfy <https://ntfy.sh>`_ topic (the public server or a
    self-hosted one): JSON to the server's root, ``{"topic", "title",
    "message", "priority", "tags"}``. The phone runs the ntfy app subscribed
    to the topic. On a public server the topic name is the only secret
    unless the topic is reserved with an access token: give it as
    ``topic_file`` and the token (``tk_...``, sent as a Bearer token) as
    ``token_file``. Priority by level: ``priorities`` (default info 2,
    warning 3, alarm 4, critical 5 -- urgent, which the Android app may let
    through Do Not Disturb); a digest, clear, ack or test is 3."""

    PRIORITIES: ClassVar[dict[str, int]] = {"info": 2, "warning": 3, "alarm": 4, "critical": 5}

    def __init__(
        self,
        name: str,
        *,
        server: str = "https://ntfy.sh",
        topic: str | None = None,
        topic_file: str | None = None,
        token_file: str | None = None,
        priorities: dict[str, int] | None = None,
        tags: list[str] | None = None,
        click: str | None = None,
        max_text: int = 3500,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if (topic is None) == (topic_file is None):
            raise ValueError(f"ntfy {name}: give exactly one of topic and topic_file")
        self.name, self.server = name, server.rstrip("/")
        check_https(f"ntfy {name}", self.server)
        self._topic, self.topic_file, self.token_file = topic, topic_file, token_file
        self.topic()  # a missing or unprotected file fails at start, not at 3 am
        if token_file:
            read_secret_text(token_file)
        self.priorities = {**self.PRIORITIES, **(priorities or {})}
        self.tags = list(tags or [])
        self.click, self.max_text = click, int(max_text)
        self.timeout, self._client = float(timeout), client

    def topic(self) -> str:
        t = self._topic if self._topic is not None else read_secret_text(str(self.topic_file))
        if not t or "/" in t:
            raise ValueError(f"ntfy {self.name}: a topic name (no slashes)")
        return t

    def payload(self, n: Notification) -> dict[str, Any]:
        prio = 3 if n.kind in QUIET_KINDS else int(self.priorities.get(n.level, 3))
        out: dict[str, Any] = {
            "topic": self.topic(),
            "title": n.subject[:250],
            "message": _cut(n.body, self.max_text),
            "priority": max(1, min(5, prio)),
            "tags": [*self.tags, n.level],
        }
        if self.click:
            out["click"] = self.click
        return out

    async def send(self, n: Notification) -> None:
        headers = {}
        if self.token_file:
            headers["Authorization"] = f"Bearer {read_secret_text(self.token_file)}"
        await self.post(self.server + "/", json=self.payload(n), headers=headers)


# -- Pushover ------------------------------------------------------------------------------------


class PushoverSink(_HTTPSink):
    """`Pushover <https://pushover.net>`_'s message API (a form POST).
    ``credentials_file``: ``{"token": <the application's API token>, "user":
    <the user or group key>}``. Priority by level: ``priorities`` (default
    info -1, warning 0, alarm 1, critical 2). Priority 2 is Pushover's
    *emergency*: repeated every ``retry`` s (>= 30) until acknowledged in
    the app or ``expire`` s (<= 10800) pass -- a pager."""

    PRIORITIES: ClassVar[dict[str, int]] = {"info": -1, "warning": 0, "alarm": 1, "critical": 2}
    API = "https://api.pushover.net/1/messages.json"

    def __init__(
        self,
        name: str,
        *,
        credentials_file: str,
        api_url: str = API,
        priorities: dict[str, int] | None = None,
        retry: int = 120,
        expire: int = 3600,
        sound: str | None = None,
        device: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name, self.api_url = name, api_url
        check_https(f"pushover {name}", api_url)
        self.credentials_file = credentials_file
        creds = read_secret_json(credentials_file)
        if not {"token", "user"} <= set(creds):
            raise ValueError(f"{credentials_file}: needs token and user")
        if not 30 <= int(retry) or not 0 < int(expire) <= 10800:
            raise ValueError(f"pushover {name}: retry >= 30 s, 0 < expire <= 10800 s")
        self.priorities = {**self.PRIORITIES, **(priorities or {})}
        self.retry, self.expire = int(retry), int(expire)
        self.sound, self.device = sound, device
        self.timeout, self._client = float(timeout), client

    def form(self, n: Notification) -> dict[str, str]:
        creds = read_secret_json(self.credentials_file)
        prio = 0 if n.kind in QUIET_KINDS else int(self.priorities.get(n.level, 0))
        prio = max(-2, min(2, prio))
        out = {
            "token": str(creds["token"]),
            "user": str(creds["user"]),
            "title": n.subject[:250],
            "message": _cut(n.body, 1024),
            "priority": str(prio),
            "timestamp": str(int(n.time)),
        }
        if prio == 2:
            out["retry"], out["expire"] = str(self.retry), str(self.expire)
        if self.sound:
            out["sound"] = self.sound
        if self.device:
            out["device"] = self.device
        return out

    async def send(self, n: Notification) -> None:
        await self.post(self.api_url, data=self.form(n))


# -- Twilio SMS ----------------------------------------------------------------------------------


class TwilioSink(_HTTPSink):
    """An SMS through `Twilio <https://www.twilio.com>`_'s Messages API, one
    to each of ``to``, from ``sender`` (a Twilio number) or a
    ``messaging_service_sid``. ``credentials_file``: ``{"account_sid": ...,
    "auth_token": ...}`` (HTTP basic auth). The text is the subject and the
    start of the body, cut to ``max_text`` characters (an SMS segment is
    160; each costs)."""

    API = "https://api.twilio.com"

    def __init__(
        self,
        name: str,
        *,
        credentials_file: str,
        to: list[str] | str,
        sender: str | None = None,
        messaging_service_sid: str | None = None,
        api_url: str = API,
        max_text: int = 600,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if (sender is None) == (messaging_service_sid is None):
            raise ValueError(f"twilio {name}: give exactly one of sender and messaging_service_sid")
        self.name, self.api_url = name, api_url.rstrip("/")
        check_https(f"twilio {name}", self.api_url)
        self.credentials_file = credentials_file
        creds = read_secret_json(credentials_file)
        if not {"account_sid", "auth_token"} <= set(creds):
            raise ValueError(f"{credentials_file}: needs account_sid and auth_token")
        self.to = [to] if isinstance(to, str) else list(to)
        if not self.to:
            raise ValueError(f"twilio {name}: no recipients")
        self.sender, self.service = sender, messaging_service_sid
        self.max_text = int(max_text)
        self.timeout, self._client = float(timeout), client

    def text(self, n: Notification) -> str:
        return _cut(f"{n.subject}\n{n.body}", self.max_text)

    async def send(self, n: Notification) -> None:
        creds = read_secret_json(self.credentials_file)
        sid = str(creds["account_sid"])
        url = f"{self.api_url}/2010-04-01/Accounts/{sid}/Messages.json"
        auth = (sid, str(creds["auth_token"]))
        for number in self.to:
            form = {"To": number, "Body": self.text(n)}
            if self.sender:
                form["From"] = self.sender
            else:
                form["MessagingServiceSid"] = str(self.service)
            await self.post(url, data=form, auth=auth)


# -- command hook ------------------------------------------------------------------------------


class CommandSink:
    """Run a program (an absolute path; never a shell) with the notification
    as JSON on stdin and in ``ROBOFAST_NOTIFY_*`` environment variables, e.g.
    to page or phone an observer. A nonzero exit is a failure (retried); one
    that runs past ``timeout`` is killed, with its process group."""

    def __init__(
        self,
        name: str,
        *,
        argv: list[str],
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError(f"command {name}: argv must be a non-empty list of strings")
        if not os.path.isabs(argv[0]):
            raise ValueError(f"command {name}: argv[0] must be an absolute path")
        self.name, self.argv, self.timeout = name, list(argv), float(timeout)
        self.env = {str(k): str(v) for k, v in (env or {}).items()}

    def environment(self, n: Notification) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            **self.env,
            "ROBOFAST_NOTIFY_KIND": n.kind,
            "ROBOFAST_NOTIFY_LEVEL": n.level,
            "ROBOFAST_NOTIFY_SUBJECT": n.subject,
            "ROBOFAST_NOTIFY_ALARM_ID": str((n.alarm or {}).get("id", "")),
            "ROBOFAST_NOTIFY_ENCLOSURE": str((n.alarm or {}).get("enclosure", "")),
        }

    async def send(self, n: Notification) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment(n),
            start_new_session=True,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(json.dumps(n.as_dict()).encode()), self.timeout
            )
        except BaseException as exc:  # timeout or cancellation: kill it
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            if isinstance(exc, TimeoutError):
                raise TimeoutError(f"{self.argv[0]} ran past {self.timeout:g} s") from None
            raise
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[-300:]
            raise RuntimeError(f"{self.argv[0]} exited {proc.returncode}: {tail}")


#: Sink type name -> class ``(name, **options)``; a plug-in adds its own
#: (docs/plugins.md).
SINK_TYPES: Registry[type] = Registry("notifier sink type")
SINK_TYPES.register("email", EmailSink)
SINK_TYPES.register("webhook", WebhookSink)
SINK_TYPES.register("command", CommandSink)
SINK_TYPES.register("ntfy", NtfySink)
SINK_TYPES.register("pushover", PushoverSink)
SINK_TYPES.register("twilio", TwilioSink)

__all__ = [
    "SINK_TYPES",
    "CommandSink",
    "EmailSink",
    "NtfySink",
    "PushoverSink",
    "TwilioSink",
    "WebhookSink",
    "check_https",
    "check_response",
    "check_secret_file",
    "is_loopback",
    "read_secret_json",
    "read_secret_text",
]
