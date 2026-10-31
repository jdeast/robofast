"""Building and running the notifier from its config (docs/notifier.md).

::

    python -m robofast_core.notifier --config notifier.json           # run
    python -m robofast_core.notifier --config notifier.json --check   # validate, exit
    python -m robofast_core.notifier --config notifier.json --test    # one test message per sink

The config (``deploy/notifier.example.json``)::

    {
      "console": "/etc/robofast/console.json",   # enclosures and acks, as the console sees them
      "console_url": "http://127.0.0.1:8400",    # optional: alarm if the console is down
      "state": "/var/lib/robofast-notifier/state.json",
      "interval": 10, "clear_after": 120,
      "escalate_after": 600, "escalate_level": "critical",
      "digest_at": "08:00", "timezone": "America/Phoenix",
      "ignore": ["^sequencer not running"],
      "sinks": {"<name>": {"type": "email" | "webhook" | "command",
                           "min_level": ..., "on": [...], "rate": {"max": 20, "window": 3600},
                           "timeout": ..., "retries": ..., ...the sink's own keys}}
    }

Instead of ``console``, ``enclosures`` (the console's form) and ``acks``
may be given directly. Paths are relative to the config file;
``$CREDENTIALS_DIRECTORY`` (systemd ``LoadCredential=``) is expanded.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from robofast_core.config import load_config
from robofast_core.console.aggregate import Aggregator
from robofast_core.notifier.core import Notification, Notifier, Settings, SinkWorker
from robofast_core.notifier.sinks import SINK_TYPES
from robofast_core.notifier.sources import AcksFile, AggregatorSource, ConsoleHealth

log = logging.getLogger(__name__)

TOP_KEYS = {"_note", "console", "enclosures", "acks", "console_url", "status_timeout", "state"}
WORKER_KEYS = {"min_level", "on", "rate", "timeout", "retries", "retry_base", "retry_max"}
WORKER_KEYS |= {"queue_size", "type", "deliver_timeout"}
PATH_KEYS = ("credentials_file", "url_file")


def _path(p: str, base: Path) -> str:
    path = Path(os.path.expandvars(p))
    return str(path if path.is_absolute() else base / path)


def build_sink_workers(
    spec: dict[str, Any], base: Path, *, clock: Callable[[], float] = time.time
) -> list[SinkWorker]:
    if not isinstance(spec, dict) or not spec:
        raise ValueError("'sinks' must name at least one sink")
    workers = []
    for name, raw in spec.items():
        if not isinstance(raw, dict):
            raise ValueError(f"sink {name}: expected an object")
        raw = dict(raw)
        kind = raw.get("type")
        if kind not in SINK_TYPES:
            raise ValueError(f"sink {name}: type must be one of {sorted(SINK_TYPES)}")
        own = {k: v for k, v in raw.items() if k not in WORKER_KEYS}
        for k in PATH_KEYS:
            if isinstance(own.get(k), str):
                own[k] = _path(own[k], base)
        if "timeout" in raw:
            own["timeout"] = raw["timeout"]
        try:
            sink = SINK_TYPES[kind](name, **own)
        except TypeError as exc:
            raise ValueError(f"sink {name}: {exc}") from None
        rate = raw.get("rate") or {}
        sink_timeout = float(getattr(sink, "timeout", 30.0))
        workers.append(
            SinkWorker(
                sink,
                min_level=raw.get("min_level", "warning"),
                on=raw.get("on", ("raise", "escalate", "digest")),
                rate_max=int(rate.get("max", 20)),
                rate_window=float(rate.get("window", 3600)),
                # The sink's own timeout bounds each attempt; this is the backstop.
                timeout=float(raw.get("deliver_timeout", sink_timeout + 10)),
                retries=int(raw.get("retries", 3)),
                retry_base=float(raw.get("retry_base", 5.0)),
                retry_max=float(raw.get("retry_max", 300.0)),
                queue_size=int(raw.get("queue_size", 100)),
                clock=clock,
            )
        )
    return workers


def build(
    path: str | Path,
    *,
    http: httpx.AsyncClient,
    clock: Callable[[], float] = time.time,
) -> Notifier:
    """The notifier a config file describes (module doc)."""
    path = Path(path)
    raw = load_config(path)
    base = path.parent
    settings_keys = {f.name for f in dataclasses.fields(Settings)}
    bad = set(raw) - TOP_KEYS - settings_keys - {"sinks"}
    if bad:
        raise ValueError(f"{path}: unknown keys {sorted(bad)}")
    from robofast_core.console.server import build_enclosures, load_console_config

    acks = raw.get("acks")
    timeout = float(raw.get("status_timeout", 3.0))
    if "console" in raw:
        if "enclosures" in raw:
            raise ValueError(f"{path}: give console or enclosures, not both")
        cconf, spec, cbase = load_console_config(_path(raw["console"], base))
        acks = acks or cconf.acks
    elif isinstance(raw.get("enclosures"), dict) and raw["enclosures"]:
        spec, cbase = raw["enclosures"], base
    else:
        raise ValueError(f"{path}: give console (the console's config) or enclosures")
    # Only the alarms matter here: skip the plan record and the request queue.
    enclosures = {
        n: dataclasses.replace(e, requests=None, record_dir=None)
        for n, e in build_enclosures(spec, cbase).items()
    }
    sources: list[Any] = [
        AggregatorSource(
            Aggregator(enclosures, http, timeout=timeout, record_lines=0, timeline_entries=1)
        )
    ]
    if raw.get("console_url"):
        sources.append(ConsoleHealth(str(raw["console_url"]), http, timeout))
    settings = Settings(**{k: v for k, v in raw.items() if k in settings_keys})
    state = raw.get("state")
    return Notifier(
        sources,
        build_sink_workers(raw.get("sinks"), base, clock=clock),
        acks=None if acks is None else AcksFile(_path(acks, base)),
        settings=settings,
        clock=clock,
        state=None if state is None else _path(state, base),
    )


async def send_test(notifier: Notifier, timeout: float = 120.0) -> dict[str, dict[str, int]]:
    """One ``test`` notification to every sink; each sink's counts."""
    now = notifier.clock()
    n = Notification(
        "test",
        "info",
        "[robofast test] the notifier can reach this sink",
        "A test from python -m robofast_core.notifier --test. Nothing is wrong.",
        now,
    )
    notifier.start()
    try:
        for w in notifier.workers:
            w.offer(n)
        await notifier.drain(timeout)
    finally:
        await notifier.aclose()
    return {w.name: dict(w.stats) for w in notifier.workers}


async def serve(config: str | Path, *, test: bool = False) -> int:
    async with httpx.AsyncClient(trust_env=False) as http:
        notifier = build(config, http=http)
        if test:
            stats = await send_test(notifier)
            for name, s in stats.items():
                print(f"{name}: {'ok' if s['sent'] else 'FAILED'}", flush=True)
            return 0 if all(s["sent"] for s in stats.values()) else 1
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        log.info(
            "notifier: %d source(s), sinks %s",
            len(notifier.sources),
            [w.name for w in notifier.workers],
        )
        await notifier.run()
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.notifier",
        description="Send the observer console's alarms by email, chat or a command.",
    )
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", action="store_true", help="validate the config and secrets, exit")
    ap.add_argument("--test", action="store_true", help="send a test message to every sink")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    async def check() -> None:
        async with httpx.AsyncClient(trust_env=False) as http:
            build(args.config, http=http)

    try:
        if args.check:
            asyncio.run(check())
            print("config ok", flush=True)
            return
        sys.exit(asyncio.run(serve(args.config, test=args.test)))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        ap.error(f"{args.config}: {exc}")
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass


__all__ = ["build", "build_sink_workers", "main", "send_test", "serve"]
