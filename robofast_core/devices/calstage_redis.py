"""Optional transition shim: the calibration stage's state on the legacy
Redis keys, so the 60-inch's legacy TCS window keeps working while robofast
owns the stage (``docs/tres_frontend.md``, "Redis transition shim").

**Off by default**, and meant to be deleted once the legacy window no longer
moves the stage. robofast itself never needs Redis: the stage is an Alpaca
device.

What the legacy window does (from its source): ``calstage <name>`` either
``SET``\\ s the key ``calstage`` to the name (one build) or ``PUBLISH``\\ es
``{"set_cal_stage": "<name>"}`` on ``guider_commands`` (another), then polls
``GET calstage_status`` until it equals the name, for up to 60 s.
``calstage position <mm>`` ``SET``\\ s ``calstage`` to the number.

What this shim does, every ``poll_interval``:

* **Mirror** (always, once enabled): ``SET calstage_status`` to the
  stage's state when it changes -- the position's name, ``moving``, the
  position in mm (``"12.500"``) when it is not at a named one, or
  ``unreferenced``. After a move to a named position it also sets
  ``calstage`` to that name, so the key always holds the last position
  reached (and a legacy request for a different one is a change).
* **Commands** (only with ``accept_commands: true``): ``GET calstage``; a
  *change* of its value to a configured position name or a number (mm)
  moves the stage, through the same checks as an Alpaca request. The value
  found when the shim starts is only a baseline: starting never moves the
  stage. Anything else (the legacy window writes an error sentence there
  for an out-of-range position) is logged and ignored. The ``PUBLISH``
  path is not followed (it needs a subscription); see the doc.

The client below can send only ``SELECT``, ``GET`` and ``SET``, and only
for the two configured keys: nothing else on the server can be read or
written. Each operation opens its own connection, bounded by ``timeout``.
A Redis that is down or slow never affects the stage: errors are logged
(once per outage) and the next poll tries again. Like every robofast
device link, a non-loopback Redis needs ``allow_hardware`` in the server's
config **and** ``--hardware``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from robofast_core.cancel import stop_task
from robofast_core.resp import RedisError, read_reply

log = logging.getLogger(__name__)

#: The only commands :class:`LegacyKeys` sends.
COMMANDS = frozenset({b"SELECT", b"GET", b"SET"})


@dataclass
class LegacyRedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    command_key: str = "calstage"
    status_key: str = "calstage_status"
    #: Move the stage when ``command_key`` changes (off: mirror only).
    accept_commands: bool = False
    poll_interval: float = 1.0
    timeout: float = 1.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LegacyRedisConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        names = {f.name for f in dataclasses.fields(cls)}
        if set(d) - names:
            raise ValueError(f"unknown legacy_redis keys: {sorted(set(d) - names)}")
        cfg = cls(**d)
        if cfg.poll_interval <= 0 or cfg.timeout <= 0:
            raise ValueError("legacy_redis: poll_interval and timeout must be positive")
        if not 0 <= cfg.db <= 15:
            raise ValueError("legacy_redis: db must be 0-15")
        if not cfg.command_key or cfg.command_key == cfg.status_key:
            raise ValueError("legacy_redis: command_key and status_key must differ")
        return cfg


class LegacyKeys:
    """Read and write exactly two Redis keys (RESP2, one connection per call)."""

    def __init__(self, host: str, port: int, db: int, keys: set[str], *, timeout: float) -> None:
        self.host, self.port, self.db = host, port, db
        self.keys = frozenset(keys)
        self.timeout = timeout

    def _encode(self, *parts: bytes | str | int) -> bytes:
        raw = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
        cmd = raw[0].upper()
        if cmd not in COMMANDS:
            raise RedisError(f"refusing to send {raw[0]!r}")
        if cmd != b"SELECT" and raw[1].decode() not in self.keys:
            raise RedisError(f"refusing to touch key {raw[1]!r}")
        out = [b"*%d\r\n" % len(raw)]
        out += [b"$%d\r\n%s\r\n" % (len(p), p) for p in raw]
        return b"".join(out)

    async def _call(self, *parts: bytes | str | int) -> Any:
        msg = self._encode(*parts)
        async with asyncio.timeout(self.timeout):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            try:
                if self.db:
                    writer.write(self._encode(b"SELECT", self.db))
                writer.write(msg)
                await writer.drain()
                if self.db and await read_reply(reader) != "OK":
                    raise RedisError(f"SELECT {self.db} refused")
                return await read_reply(reader)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def get(self, key: str) -> str | None:
        v = await self._call(b"GET", key)
        if v is not None and not isinstance(v, bytes):
            raise RedisError(f"GET {key}: unexpected reply {v!r:.60}")
        return None if v is None else v.decode("utf-8", "replace")

    async def set(self, key: str, value: str) -> None:
        if await self._call(b"SET", key, value) != "OK":
            raise RedisError(f"SET {key} not acknowledged")


class LegacyCalstageMirror:
    """Runs on the stage server's event loop.

    ``state()`` returns the stage's legacy status string (or None if it
    cannot be read now); ``at_name()`` the named position it rests at (or
    None); ``move_name(name)`` / ``move_mm(mm)`` start a move with the
    server's own checks (raising if refused).
    """

    def __init__(
        self,
        keys: LegacyKeys,
        config: LegacyRedisConfig,
        *,
        names: list[str],
        state: Callable[[], Awaitable[str | None]],
        at_name: Callable[[], Awaitable[str | None]],
        move_name: Callable[[str], Awaitable[Any]],
        move_mm: Callable[[float], Awaitable[Any]],
    ) -> None:
        self.keys = keys
        self.config = config
        self.names = list(names)
        self._state, self._at_name = state, at_name
        self._move_name, self._move_mm = move_name, move_mm
        self._written: str | None = None
        self._named: str | None = None
        #: The command key's last value seen (None until the first read).
        self._seen: str | None = None
        self._baseline = False
        self._down = False
        self._task: asyncio.Task | None = None
        #: Commands taken from the command key, for tests and the log.
        self.accepted: list[str] = []

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def shutdown(self) -> None:
        await stop_task(self._task, 2.0)

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
                if self._down:
                    log.info("legacy redis: reachable again")
                self._down = False
            except (OSError, TimeoutError, RedisError) as exc:
                if not self._down:
                    log.warning("legacy redis: %r (retrying every %g s)", exc, self.config.poll_interval)
                self._down = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("legacy redis mirror")
            await asyncio.sleep(self.config.poll_interval)

    async def step(self) -> None:
        cfg = self.config
        if cfg.accept_commands:
            await self._commands()
        state = await self._state()
        if state is not None and state != self._written:
            await self.keys.set(cfg.status_key, state)
            self._written = state
        named = await self._at_name()
        if named is not None and named != self._named:
            if named != self._seen:
                await self.keys.set(cfg.command_key, named)
                self._seen = named
            self._named = named
        elif named is None and state == "moving":
            self._named = None

    async def _commands(self) -> None:
        value = await self.keys.get(self.config.command_key)
        if not self._baseline:
            self._seen, self._baseline = value, True
            log.info("legacy redis: %s=%r at start (baseline, not acted on)",
                     self.config.command_key, value)
            return
        if value is None or value == self._seen:
            return
        self._seen = value
        text = value.strip()
        try:
            if text in self.names:
                log.warning("legacy redis: %s=%r: moving there", self.config.command_key, text)
                await self._move_name(text)
            else:
                mm = float(text)
                if not math.isfinite(mm):
                    raise ValueError(text)
                log.warning("legacy redis: %s=%r: moving to %g mm", self.config.command_key, text, mm)
                await self._move_mm(mm)
        except ValueError:
            log.warning("legacy redis: ignoring %s=%r (not a position)", self.config.command_key, value)
            return
        except Exception as exc:
            log.warning("legacy redis: %s=%r refused: %s", self.config.command_key, value, exc)
            return
        self.accepted.append(text)
