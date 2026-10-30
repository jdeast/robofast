"""A TCP proxy with switchable faults, for making a real device misbehave.

Put it between an Alpaca client and an Alpaca server (e.g. OmniSim) and
switch modes at any time:

* ``"pass"``   -- forward bytes both ways;
* ``"hang"``   -- accept connections and read requests but forward nothing,
                  in either direction (the device stops answering);
* ``"refuse"`` -- drop every open connection and refuse new ones (the
                  device has disappeared from the network).

It runs its own event loop on a background thread, so it is independent of
the event loop under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading


class FaultProxy:
    def __init__(self, target_host: str, target_port: int, listen_host: str = "127.0.0.1") -> None:
        self.target = (target_host, target_port)
        self.listen_host = listen_host
        self.mode = "pass"
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def address(self) -> str:
        return f"{self.listen_host}:{self.port}"

    def start(self) -> FaultProxy:
        # Reserve a port now so that "refuse" can close and later reopen it.
        with socket.socket() as s:
            s.bind((self.listen_host, 0))
            self.port = s.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="fault-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("fault proxy did not start")
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._listen())
        self._ready.set()
        self._loop.run_forever()

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.port, reuse_address=True
        )

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                while self.mode == "hang":
                    await asyncio.sleep(0.05)
                if self.mode == "refuse":
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        if self.mode == "refuse":
            c_writer.close()
            return
        try:
            s_reader, s_writer = await asyncio.open_connection(*self.target)
        except OSError:
            c_writer.close()
            return
        self._writers.update((c_writer, s_writer))
        try:
            await asyncio.gather(self._pump(c_reader, s_writer), self._pump(s_reader, c_writer))
        finally:
            self._writers.difference_update((c_writer, s_writer))

    def _call(self, coro) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)

    async def _set_mode(self, mode: str) -> None:
        old, self.mode = self.mode, mode
        if mode == "refuse" and old != "refuse":
            for w in list(self._writers):
                with contextlib.suppress(Exception):
                    w.transport.abort()
            if self._server is not None:
                self._server.close()
                self._server = None
        elif mode != "refuse" and self._server is None:
            await self._listen()

    def set_mode(self, mode: str) -> None:
        if mode not in ("pass", "hang", "refuse"):
            raise ValueError(mode)
        self._call(self._set_mode(mode))

    def stop(self) -> None:
        if self._loop is None:
            return
        self._call(self._set_mode("refuse"))
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
