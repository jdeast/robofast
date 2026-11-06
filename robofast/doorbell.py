"""The drop box's doorbell: a file dropped is taken in at once, no polling.

The request API writes each submission or cancellation into the drop box
(the durable record), then *rings*: it connects to the scheduler's doorbell
and sends the token it read from ``<dropbox>/.doorbell``. The scheduler looks
in the drop box when it rings with that token, and once when it starts (for
files dropped while it was down). A ring carries nothing else: the API still
reaches the scheduler only through files, and a ring only makes it look.

The scheduler listens on ``doorbell_listen`` (dispatch config; default
``127.0.0.1:0``, a loopback port of the system's choosing) and writes its
address, port and a fresh random token to ``<dropbox>/.doorbell``. So the
default needs no configuration, several enclosures on one host need no port
numbers, and a request API on another host (sharing the drop box over a
network filesystem) works once ``doorbell_listen`` names an interface it can
reach (and a fixed port, for a firewall rule). Whoever can read the drop box
can ring, which is whoever can submit anyway. TCP, so the ringer knows the
ring arrived (a failure is logged); portable, no dependencies.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import socket
from pathlib import Path

log = logging.getLogger(__name__)

BELL_FILE = ".doorbell"
#: Seconds a ringer waits to connect and be heard.
RING_TIMEOUT = 2.0


def _split(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"doorbell_listen {listen!r}: expected host:port")
    return host.strip("[]"), int(port)


def _publish(dropbox: Path, bell: dict[str, object]) -> None:
    dropbox.mkdir(parents=True, exist_ok=True)
    tmp = dropbox / f"{BELL_FILE}.tmp"
    tmp.write_text(json.dumps(bell))
    os.replace(tmp, dropbox / BELL_FILE)


def _unpublish(dropbox: Path, token: str) -> None:
    path = dropbox / BELL_FILE
    try:
        if json.loads(path.read_text()).get("token") == token:
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


class Doorbell:
    """The scheduler's side: :meth:`open`, then :meth:`wait` for each ring."""

    def __init__(self, dropbox: Path, server: asyncio.Server, token: str) -> None:
        self.dropbox = dropbox
        self.port: int = server.sockets[0].getsockname()[1]
        self._server = server
        self._token = token.encode()
        self._event = asyncio.Event()

    @classmethod
    async def open(cls, dropbox: Path, listen: str = "127.0.0.1:0") -> Doorbell:
        host, port = _split(listen)
        token = secrets.token_hex(16)
        holder: list[Doorbell] = []

        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                got = await asyncio.wait_for(reader.readline(), RING_TIMEOUT)
                if holder and hmac.compare_digest(got.strip(), holder[0]._token):
                    holder[0]._event.set()
                    writer.write(b"ok\n")
                    await writer.drain()
            except (TimeoutError, OSError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(answer, host, port)
        bell = cls(dropbox, server, token)
        holder.append(bell)
        # A wildcard address is no address to ring: publish the host's name.
        where = socket.gethostname() if host in ("0.0.0.0", "::", "") else host
        _publish(dropbox, {"host": where, "port": bell.port, "token": token, "pid": os.getpid()})
        return bell

    async def wait(self) -> None:
        """Until it rang since the last wait returned."""
        await self._event.wait()
        self._event.clear()

    def close(self) -> None:
        self._server.close()
        _unpublish(self.dropbox, self._token.decode())


async def ring(dropbox: Path) -> bool:
    """The writer's side, after a file is in the drop box: wake the
    scheduler. Never raises; False if nobody heard (no scheduler has run:
    it looks when it starts; otherwise logged, and the file waits for the
    next ring or the scheduler's next start)."""
    try:
        bell = json.loads((dropbox / BELL_FILE).read_text())
        host, port, token = str(bell["host"]), int(bell["port"]), str(bell["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    why = ""
    for _ in range(2):
        try:
            async with asyncio.timeout(RING_TIMEOUT):
                reader, writer = await asyncio.open_connection(host, port)
                try:
                    writer.write(token.encode() + b"\n")
                    await writer.drain()
                    if (await reader.readline()).strip() == b"ok":
                        return True
                    why = "not heard (a stale .doorbell?)"
                finally:
                    writer.close()
        except (OSError, TimeoutError) as exc:
            why = repr(exc)
    log.error("drop box %s: ringing %s:%d: %s", dropbox, host, port, why)
    return False


def main(argv: list[str] | None = None) -> int:
    """``python -m robofast.doorbell ring <dropbox>``: after a file was put
    into the drop box by hand or by a script, ring so it is taken in now."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m robofast.doorbell", description=main.__doc__)
    ap.add_argument("command", choices=["ring"])
    ap.add_argument("dropbox", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    heard = asyncio.run(ring(args.dropbox))
    print("rang: taken in now" if heard else "nobody heard: taken in when the scheduler starts")
    return 0 if heard else 1


if __name__ == "__main__":
    raise SystemExit(main())
