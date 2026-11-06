"""The alert service: sources to the operator's rules to ToO requests.

``python -m robofast.alerts run --config alerts.json`` runs the configured
sources until stopped; ``submit --source SOURCE FILE...`` puts alert files
(Rubin, GCN and TNS JSON; a polled source's saved page) through the same
rules (``--dry-run``: print the requests, submit nothing); ``check`` loads the
config and says what it would run.

The config (``sites/minerva/alerts.example.json``)::

    {"api": {"url": ..., "token_file": ..., "enclosure": ...},
     "proposal": "alerts-too",
     "state": "/var/lib/robofast/alerts-seen.json",
     "sources": {
       "tocp": {"poll_seconds": 300},
       "ogle": {"poll_seconds": 1800}, "kmtnet": {...}, "moa": {...},
       "tns": {"poll_seconds": 300, "bot_id": 0, "bot_name": "...", "api_key_file": "..."},
       "gcn": {"client_id_file": "...", "client_secret_file": "...", "topics": [...]}},
     "rules": [...]}

Every alert is handled once (``state``: ``source:id`` seen, kept 30 days).
At its first start a polled source (TOCP, TNS) only marks what is already
listed as seen, so the page's history is never submitted. TOCP and TNS
offer no push, so they are polled; GCN is a Kafka stream (pushed).

The microlensing surveys (OGLE, KMTNet, MOA) are polled pages that refit
their events in place, so they are handled differently: their page is
never primed (the rules' ``peak_hours`` keep the season's history out), an
event no rule takes is looked at again at the next poll (a refit can raise
its magnification), and once one survey's name for a star is followed,
another's (its alias, or within 2 arcsec) is not followed again.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from robofast.alerts import gcn, kmtnet, moa, ogle, rubin, tns, tocp
from robofast.alerts.core import (
    Alert,
    ApiConfig,
    Rule,
    Seen,
    _keys,
    decide,
    load_rules,
    submit,
    utc,
)

log = logging.getLogger("robofast.alerts")

SOURCE_KEYS = {
    "tocp": {"poll_seconds", "url"},
    "tns": {"poll_seconds", "bot_id", "bot_name", "api_key_file"},
    "gcn": {"client_id_file", "client_secret_file", "topics"},
    "ogle": {"poll_seconds", "url"},
    "kmtnet": {"poll_seconds", "url"},
    "moa": {"poll_seconds", "url"},
}
#: The polled pages: their parser and fetcher.
PAGES = {"tocp": tocp, "ogle": ogle, "kmtnet": kmtnet, "moa": moa}


@dataclass
class AlertsConfig:
    rules: list[Rule]
    api: ApiConfig = field(default_factory=ApiConfig)
    proposal: str = "alerts-too"
    state: str | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AlertsConfig:
        _keys(d, {"api", "proposal", "state", "sources", "rules"}, "alerts config")
        api = ApiConfig(**_keys(d.get("api", {}), {"url", "token_file", "enclosure"}, "api"))
        sources = _keys(d.get("sources", {}), set(SOURCE_KEYS), "sources")
        for name, s in sources.items():
            _keys(s, SOURCE_KEYS[name], f"sources.{name}")
        if "gcn" in sources and not sources["gcn"].get("topics"):
            raise ValueError("sources.gcn.topics: the Kafka topics to follow")
        return cls(
            load_rules(d.get("rules")),
            api,
            str(d.get("proposal", "alerts-too")),
            d.get("state"),
            sources,
        )


def _page(mod, url: str | None):
    """Fetch and parse one polled page (its module's own URL unless ``url``)."""
    return lambda: mod.parse(mod.fetch(url) if url else mod.fetch())


def _secret(path: str) -> str:
    return Path(path).read_text().strip()


class AlertService:
    def __init__(self, config: AlertsConfig, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.seen = Seen(Path(config.state) if config.state else None).load()
        self.submitted: list[dict[str, Any]] = []

    async def handle(
        self, alerts: list[Alert], *, prime: bool = False, now: datetime | None = None
    ) -> None:
        """Each new alert through the rules (``prime``: only mark them seen,
        except those that update)."""
        now = now or datetime.now(UTC)
        for a in alerts:
            key = f"{a.source}:{a.id}"
            if key in self.seen:
                continue
            if a.updates:
                twin = self.seen.twin(a)
                if twin is not None:
                    self.seen.add(key, now)
                    log.info("%s %s: the same star as %s (followed)", a.source, a.id, twin)
                    continue
            else:
                self.seen.add(key, now)
                if prime:
                    continue
            group, why = decide(a, self.config.rules, now, self.config.proposal)
            if group is None:
                say = log.debug if a.updates else log.info  # a refit page lists it again
                say("%s %s: not followed (%s)", a.source, a.id, why)
                continue
            if a.updates:  # kept until its last window ends (then keep_days)
                ends = [utc(w["end"]) for r in group["requests"] for w in r.get("windows", [])]
                self.seen.add(key, max([now, *filter(None, ends)]), where=(a.ra, a.dec))
            if self.dry_run:
                print(json.dumps(group, indent=1), flush=True)
                self.submitted.append(group)
                continue
            try:
                reply = await asyncio.to_thread(submit, group, self.config.api)
                self.submitted.append(group)
                log.warning(
                    "%s %s: ToO %s by rule %r: %s",
                    a.source,
                    a.id,
                    group["id"],
                    why,
                    reply.get("intake"),
                )
            except Exception as exc:  # one alert's failure is not the others'
                log.error("%s %s: ToO %s not submitted: %s", a.source, a.id, group["id"], exc)
        self.seen.save(now)

    def _primed(self, source: str) -> bool:
        return any(k.startswith(f"{source}:") for k in self.seen.ids)

    async def _poll(self, source: str, fetch, every: float) -> None:
        prime = not self._primed(source)
        while True:
            try:
                alerts = await asyncio.to_thread(fetch)
                await self.handle(alerts, prime=prime)
                if prime:
                    log.info("%s: %d listed alerts marked seen (first start)", source, len(alerts))
                prime = False
            except Exception as exc:  # an outage costs this source's alerts, nothing else
                log.error("%s: %r", source, exc)
            await asyncio.sleep(every)

    async def _gcn(self, s: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Alert] = asyncio.Queue()

        def deliver(alert: Alert) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, alert)

        thread = asyncio.create_task(
            asyncio.to_thread(
                gcn.consume,
                _secret(s["client_id_file"]),
                _secret(s["client_secret_file"]),
                list(s["topics"]),
                deliver,
            )
        )
        try:
            while True:
                await self.handle([await queue.get()])
        finally:
            thread.cancel()

    async def run(self) -> None:
        tasks = []
        for name, s in self.config.sources.items():
            if name in PAGES:
                tasks.append(
                    self._poll(
                        name, _page(PAGES[name], s.get("url")), float(s.get("poll_seconds", 300))
                    )
                )
            elif name == "tns":
                client = tns.TNS(int(s["bot_id"]), str(s["bot_name"]), _secret(s["api_key_file"]))
                every = float(s.get("poll_seconds", 300))

                def recent(c=client, every=every):
                    return c.recent(datetime.now(UTC) - timedelta(seconds=3 * every))

                tasks.append(self._poll("tns", recent, every))
            elif name == "gcn":
                tasks.append(self._gcn(s))
        if not tasks:
            raise ValueError("alerts config: no sources to run")
        await asyncio.gather(*tasks)


def _gcn_file(d: dict[str, Any]) -> Alert:
    return gcn.from_gcn(d.get("$schema", "gcn.notices.file.json"), d)


PARSERS = {
    "rubin": lambda t: [rubin.from_rubin(json.loads(t))],
    "gcn": lambda t: [_gcn_file(json.loads(t))],
    "tns": lambda t: [tns.from_tns(json.loads(t))],
    **{name: mod.parse for name, mod in PAGES.items()},
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast.alerts")
    ap.add_argument("command", choices=["run", "submit", "check"])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--source", choices=sorted(PARSERS), help="submit: the files' format")
    ap.add_argument("--dry-run", action="store_true", help="print the requests; submit nothing")
    ap.add_argument("files", type=Path, nargs="*")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = AlertsConfig.from_dict(json.loads(args.config.read_text()))
    if args.command == "check":
        print("rules:", ", ".join(r.name for r in config.rules))
        print("sources:", ", ".join(config.sources) or "none")
        return 0
    service = AlertService(config, dry_run=args.dry_run)
    if args.command == "submit":
        if not args.source or not args.files:
            ap.error("submit needs --source and alert files")
        alerts = [
            a
            for f in args.files
            for a in PARSERS[args.source](f.read_bytes().decode("utf-8", "replace"))
        ]
        asyncio.run(service.handle(alerts))
        return 0
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(service.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
