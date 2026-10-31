"""The site weather bridge: real weather feeds -> Alpaca SafetyMonitor + ObservingConditions.

It polls each configured source (:mod:`robofast_core.weather_sources`, with
site adapters in :mod:`robofast_core.sites`), judges the readings
(:mod:`robofast_core.weather_eval`: per-source staleness, open/close limits
with hysteresis, rain, required vs advisory sources) and publishes the
verdict and the conditions to a :class:`~robofast_core.weather_server.WeatherState`,
which the Alpaca server in :mod:`robofast_core.weather_server` serves to the
safety supervisor.

It fails closed everywhere: a fetch that errors or times out makes its source
unknown at once (the last good values are *not* kept), any error in an
evaluation publishes "unsafe", and the Alpaca server itself reports unsafe if
the bridge stops publishing.

Running it
----------
::

    # shadow mode: fetch, evaluate and log one line per poll; serve nothing
    python -m robofast_core.weather_bridge --config sites/minerva/weather.yaml --shadow

    # serve Alpaca on the config's ``listen`` address (or --listen host:port)
    python -m robofast_core.weather_bridge --config sites/minerva/weather.yaml

``--once`` polls once, prints the line and exits (status 0 if safe, else 1);
``--json`` prints each poll's full status as one JSON object per line. The
config format is described in ``docs/weather_bridge.md``; YAML (``.yaml``) and
JSON (``.json``) are both accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import robofast_core.sites  # noqa: F401  (registers the site adapters)
from robofast_core.sun import now_utc
from robofast_core.weather_eval import (
    UNKNOWN,
    Evaluation,
    Evaluator,
    FieldLimits,
    RainRule,
    SourceSpec,
)
from robofast_core.weather_server import WeatherServer, WeatherState
from robofast_core.weather_sources import (
    ASCOM_UNITS,
    SOURCE_TYPES,
    UNITS,
    Reading,
    SourceConfig,
    WeatherSource,
    to_ascom,
)

log = logging.getLogger(__name__)

_SOURCE_KEYS = {
    "type",
    "required",
    "stale_after",
    "timeout",
    "enabled",
    "url",
    "credentials_file",
    "limits",
    "groups",
    "rain_counters",
}
_TOP_KEYS = {
    "site",
    "listen",
    "poll_interval",
    "evaluate_interval",
    "rain_hold",
    "max_clock_skew",
    "server_stale_after",
    "sources",
    "conditions",
}


@dataclass(frozen=True)
class Condition:
    """One ObservingConditions member, served from a source's field."""

    member: str
    source: str
    field: str
    unit: str
    description: str


@dataclass
class BridgeConfig:
    site: dict[str, Any]
    sources: list[tuple[SourceConfig, SourceSpec]]
    conditions: list[Condition] = field(default_factory=list)
    listen: str = "127.0.0.1:11111"
    #: Seconds between fetches of every source.
    poll_interval: float = 60.0
    #: Seconds between evaluations (staleness and the Sun move on between polls).
    evaluate_interval: float = 5.0
    #: Unsafe for this long after rain was last detected (legacy: 1800 s).
    rain_hold: float = 1800.0
    #: A reading timestamped further than this in the future is unknown.
    max_clock_skew: float = 120.0
    #: The Alpaca server reports unsafe if the bridge has not published for this long.
    server_stale_after: float = 60.0


def load_config(path: str | Path) -> dict[str, Any]:
    """Read a YAML or JSON config file."""
    path = Path(path)
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml  # only YAML configs need PyYAML

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the top level must be a mapping")
    return data


def parse_config(raw: dict[str, Any]) -> BridgeConfig:
    """Validate a config mapping (strictly: unknown keys are errors)."""
    extra = set(raw) - _TOP_KEYS
    if extra:
        raise ValueError(f"unknown config keys: {sorted(extra)}")
    site = dict(raw["site"])
    for key in ("latitude", "longitude"):
        site[key] = float(site[key])
    sources: list[tuple[SourceConfig, SourceSpec]] = []
    for name, spec in raw["sources"].items():
        spec = dict(spec)
        if not spec.pop("enabled", True):
            continue
        known = {k: spec.pop(k) for k in list(spec) if k in _SOURCE_KEYS}
        if known.get("type") not in SOURCE_TYPES:
            raise ValueError(
                f"source {name}: type {known.get('type')!r} is not one of {sorted(SOURCE_TYPES)}"
            )
        scfg = SourceConfig(
            name=name,
            type=known["type"],
            required=bool(known.get("required", True)),
            stale_after=float(known.get("stale_after", 300.0)),
            timeout=float(known.get("timeout", 10.0)),
            url=known.get("url"),
            credentials_file=known.get("credentials_file"),
            options=spec,  # the rest is the site adapter's
        )
        limits = {
            f: FieldLimits.parse(v, f"{name}.limits.{f}")
            for f, v in (known.get("limits") or {}).items()
        }
        rain = {
            f: RainRule.parse(v, f"{name}.rain_counters.{f}")
            for f, v in (known.get("rain_counters") or {}).items()
        }
        groups = {g: list(fs) for g, fs in (known.get("groups") or {}).items()}
        live = known["type"] == "sun"
        sources.append(
            (
                scfg,
                SourceSpec(
                    name,
                    required=scfg.required,
                    stale_after=scfg.stale_after,
                    live=live,
                    limits=limits,
                    groups=groups,
                    rain=rain,
                ),
            )
        )
    by_name = {s.name: (s, spec) for s, spec in sources}
    conditions = []
    for member, spec in (raw.get("conditions") or {}).items():
        member = member.lower()
        if member not in ASCOM_UNITS:
            raise ValueError(f"conditions: {member} is not a supported ObservingConditions member")
        src, fld, unit = spec["source"], spec["field"], spec["unit"]
        if src not in by_name:
            raise ValueError(f"conditions.{member}: no enabled source {src!r}")
        if unit not in UNITS or UNITS[unit][0] != ASCOM_UNITS[member]:
            raise ValueError(
                f"conditions.{member}: unit {unit!r} does not convert to {ASCOM_UNITS[member]}"
            )
        desc = spec.get("description") or f"{src}.{fld} ({unit})"
        conditions.append(Condition(member, src, fld, unit, desc))
    cfg = BridgeConfig(site=site, sources=sources, conditions=conditions)
    for key in (
        "poll_interval",
        "evaluate_interval",
        "rain_hold",
        "max_clock_skew",
        "server_stale_after",
    ):
        if key in raw:
            setattr(cfg, key, float(raw[key]))
    if "listen" in raw:
        cfg.listen = str(raw["listen"])
    if not any(spec.required and not spec.live for _, spec in sources):
        raise ValueError("at least one required weather source (not just the Sun) is needed")
    if cfg.server_stale_after <= cfg.evaluate_interval:
        raise ValueError("server_stale_after must exceed evaluate_interval")
    return cfg


def build_sources(cfg: BridgeConfig, **kw: Any) -> list[WeatherSource]:
    """Instantiate each configured source (``kw`` goes to every factory, e.g. a test transport)."""
    out = []
    for scfg, _ in cfg.sources:
        factory = SOURCE_TYPES[scfg.type]
        out.append(factory(scfg, cfg.site, **({} if scfg.type == "sun" else kw)))
    return out


class WeatherBridge:
    """Polls the sources, evaluates, and publishes to a :class:`WeatherState`."""

    def __init__(
        self,
        cfg: BridgeConfig,
        sources: list[WeatherSource] | None = None,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.cfg = cfg
        self.sources = {s.name: s for s in (sources if sources is not None else build_sources(cfg))}
        self.evaluator = Evaluator(
            [spec for _, spec in cfg.sources],
            rain_hold=cfg.rain_hold,
            max_clock_skew=cfg.max_clock_skew,
        )
        self.clock = clock
        self.readings: dict[str, Reading | str | None] = dict.fromkeys(self.sources)
        self.last: Evaluation | None = None

    async def _fetch(self, src: WeatherSource) -> Reading | str:
        bound = src.config.timeout + 1.0
        try:
            async with asyncio.timeout(bound):
                reading = await src.fetch(self.clock())
        except TimeoutError:
            return f"no answer within {bound:g} s"
        except Exception as exc:  # fail closed on anything
            return f"{type(exc).__name__}: {exc}"
        if reading.source != src.name:
            return f"reading labelled {reading.source!r}"
        return reading

    async def poll(self) -> None:
        """Fetch every (non-live) source concurrently; each is bounded by its timeout."""
        polled = [s for s in self.sources.values() if not s.live]
        results = await asyncio.gather(*(self._fetch(s) for s in polled))
        for src, result in zip(polled, results, strict=True):
            if isinstance(result, str):
                log.warning("%s: %s", src.name, result)
            self.readings[src.name] = result

    def evaluate(self) -> Evaluation:
        now = self.clock()
        try:
            readings = dict(self.readings)
            for src in self.sources.values():
                if src.live:
                    readings[src.name] = src.read(now)
            ev = self.evaluator.evaluate(now, readings)
        except Exception as exc:  # fail closed
            log.exception("evaluation failed")
            self.evaluator.was_safe = False
            ev = Evaluation(now, UNKNOWN, "open", [f"bridge error: {exc!r}"], {})
        self.last = ev
        return ev

    def observing_conditions(
        self, ev: Evaluation
    ) -> tuple[dict[str, float | None], dict[str, float], dict[str, str]]:
        """(conditions, sensor_times, descriptions) for :meth:`WeatherState.publish`."""
        conditions: dict[str, float | None] = {}
        times: dict[str, float] = {}
        descriptions: dict[str, str] = {}
        for c in self.cfg.conditions:
            descriptions[c.member] = c.description
            status = ev.sources.get(c.source)
            value = None
            if status is not None and status.fresh and status.time is not None:
                raw = status.values.get(c.field)
                if raw is not None:
                    value = to_ascom(raw, c.unit)
                    times[c.member] = status.time.timestamp()
            conditions[c.member] = value
        return conditions, times, descriptions

    def publish(self, state: WeatherState, ev: Evaluation) -> None:
        try:
            conditions, times, descriptions = self.observing_conditions(ev)
            status = ev.as_dict()
        except Exception as exc:  # fail closed
            log.exception("publish failed")
            state.publish(
                safe=False,
                conditions={c.member: None for c in self.cfg.conditions},
                status={"safe": False, "reasons": [f"bridge error: {exc!r}"]},
            )
            return
        state.publish(
            safe=ev.safe,
            conditions=conditions,
            sensor_times=times,
            descriptions=descriptions,
            status=status,
        )

    async def run(
        self,
        state: WeatherState | None = None,
        on_poll: Callable[[Evaluation], None] | None = None,
    ) -> None:
        """Poll every ``poll_interval``, evaluate (and publish) every
        ``evaluate_interval`` until cancelled; ``on_poll`` gets the evaluation
        right after each poll."""
        next_poll = 0.0
        while True:
            polled = False
            if time.monotonic() >= next_poll:
                next_poll = time.monotonic() + self.cfg.poll_interval
                try:
                    await self.poll()
                except Exception as exc:  # fail closed, keep running
                    log.exception("poll failed")
                    for src in self.sources.values():
                        if not src.live:
                            self.readings[src.name] = f"poll failed: {exc!r}"
                polled = True
            ev = self.evaluate()
            if state is not None:
                self.publish(state, ev)
            if polled and on_poll is not None:
                try:
                    on_poll(ev)
                except Exception:
                    log.exception("on_poll")
            delay = self.cfg.evaluate_interval if state is not None else self.cfg.poll_interval
            await asyncio.sleep(max(0.0, min(delay, next_poll - time.monotonic())))

    async def aclose(self) -> None:
        for src in self.sources.values():
            await src.aclose()


# -- command line ------------------------------------------------------------------


def _printer(as_json: bool) -> Callable[[Evaluation], None]:
    def show(ev: Evaluation) -> None:
        if as_json:
            print(json.dumps(ev.as_dict()), flush=True)
        else:
            print(f"{ev.time:%Y-%m-%dT%H:%M:%SZ} {ev.line()}", flush=True)

    return show


async def _main(args: argparse.Namespace, cfg: BridgeConfig) -> int:
    bridge = WeatherBridge(cfg)
    show = _printer(args.json)
    server = None
    try:
        if args.once:
            await bridge.poll()
            ev = bridge.evaluate()
            show(ev)
            return 0 if ev.safe else 1
        state = None
        if not args.shadow:
            state = WeatherState(safe=False, stale_after=cfg.server_stale_after)
            host, _, port = (args.listen or cfg.listen).rpartition(":")
            server = WeatherServer(state, host, int(port)).start()
            log.info("serving Alpaca on http://%s", server.address)
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await bridge.run(state, show)
        except asyncio.CancelledError:
            pass
        return 0
    finally:
        if server is not None:
            server.stop()
        await bridge.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="YAML or JSON bridge config")
    ap.add_argument("--shadow", action="store_true", help="log only; do not serve Alpaca")
    ap.add_argument("--once", action="store_true", help="poll once, print, exit (0 = safe)")
    ap.add_argument("--json", action="store_true", help="print each poll as one JSON line")
    ap.add_argument("--listen", help="host:port for the Alpaca server (overrides the config)")
    ap.add_argument("--poll-interval", type=float, help="seconds between polls")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # not one line per request
    cfg = parse_config(load_config(args.config))
    if args.poll_interval is not None:
        cfg.poll_interval = args.poll_interval
    return asyncio.run(_main(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
