"""A minimal, read-only Redis client (RESP2 over asyncio streams).

The weather bridge reads a handful of keys from a site's Redis server once a
minute. That needs two commands, so instead of a client library this module
speaks just enough of the Redis protocol (RESP2) to send them, and refuses to
send anything else: :data:`ALLOWED` is the whole vocabulary, and none of it
writes. A new connection per read keeps no state between polls (no stale
connection, no half-read reply after a timeout).

Every call is bounded by its ``timeout`` (connect, send and read together).
Replies are size-capped, so a misbehaving server cannot make the reader
allocate without bound.
"""

from __future__ import annotations

import asyncio
import contextlib

#: The only commands this client will send. All are read-only: SELECT picks a
#: database for this connection only, PING checks liveness.
ALLOWED = frozenset({b"SELECT", b"GET", b"MGET", b"PING"})

_MAX_BULK = 1 << 20
_MAX_ARRAY = 10_000

Reply = str | int | bytes | None | list["Reply"]


class RedisError(Exception):
    """A protocol error, an error reply, or a refused command."""


def encode(*parts: bytes | str | int) -> bytes:
    """One command as a RESP array of bulk strings; refuses non-read-only commands."""
    if not parts:
        raise RedisError("empty command")
    raw = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
    if raw[0].upper() not in ALLOWED:
        raise RedisError(f"refusing to send {raw[0]!r}: this client is read-only")
    out = [b"*%d\r\n" % len(raw)]
    for p in raw:
        out.append(b"$%d\r\n%s\r\n" % (len(p), p))
    return b"".join(out)


async def read_reply(reader: asyncio.StreamReader) -> Reply:
    line = await reader.readline()
    if not line.endswith(b"\r\n"):
        raise RedisError("connection closed mid-reply")
    kind, body = line[:1], line[1:-2]
    if kind == b"+":
        return body.decode(errors="replace")
    if kind == b"-":
        raise RedisError(f"server error: {body.decode(errors='replace')}")
    if kind == b":":
        return int(body)
    if kind == b"$":
        n = int(body)
        if n < 0:
            return None
        if n > _MAX_BULK:
            raise RedisError(f"bulk reply of {n} bytes is too large")
        try:
            data = await reader.readexactly(n + 2)
        except asyncio.IncompleteReadError:
            raise RedisError("connection closed mid-reply") from None
        if data[-2:] != b"\r\n":
            raise RedisError("malformed bulk reply")
        return data[:-2]
    if kind == b"*":
        n = int(body)
        if n < 0:
            return None
        if n > _MAX_ARRAY:
            raise RedisError(f"array reply of {n} items is too large")
        return [await read_reply(reader) for _ in range(n)]
    raise RedisError(f"unexpected reply {line[:40]!r}")


async def mget(
    host: str, port: int, db: int, keys: list[str], *, timeout: float
) -> list[bytes | None]:
    """``MGET keys`` from database ``db``: one value (or None) per key."""
    if not keys:
        return []
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            # Pipelined: SELECT (unless db 0), then MGET; replies in order.
            if db:
                writer.write(encode(b"SELECT", db))
            writer.write(encode(b"MGET", *keys))
            await writer.drain()
            if db:
                ok = await read_reply(reader)
                if ok != "OK":
                    raise RedisError(f"SELECT {db}: {ok!r}")
            reply = await read_reply(reader)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    if not isinstance(reply, list) or len(reply) != len(keys):
        raise RedisError(f"MGET: unexpected reply {reply!r:.80}")
    if not all(v is None or isinstance(v, bytes) for v in reply):
        raise RedisError("MGET: non-string values in reply")
    return reply  # type: ignore[return-value]
