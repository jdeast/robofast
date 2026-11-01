"""Where notifications go: email, a chat webhook, a command (docs/notifier.md).

Every sink has ``name`` and ``async send(notification)``, which delivers or
raises; its :class:`~robofast_core.notifier.core.SinkWorker` adds the
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
from typing import Any
from urllib.parse import urlsplit

import httpx

from robofast_core.cancel import isolated
from robofast_core.notifier.core import Notification


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
            r.raise_for_status()
        finally:
            if own:
                await client.aclose()


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


SINK_TYPES: dict[str, type] = {
    "email": EmailSink,
    "webhook": WebhookSink,
    "command": CommandSink,
}

__all__ = [
    "SINK_TYPES",
    "CommandSink",
    "EmailSink",
    "WebhookSink",
    "check_secret_file",
    "is_loopback",
    "read_secret_json",
    "read_secret_text",
]
