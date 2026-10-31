"""The sequencer of one enclosure as its own process.

Running the sequencer
---------------------
::

    python -m robofast_core.sequencer_service --config aqawan1.json [--plan PLAN]

``--config`` is the enclosure's config file (:mod:`robofast_core.config`),
the same one its safety supervisor reads. The sequencer uses these parts of
it (``sites/minerva/aqawan1.json`` is a complete example)::

    {
      "name": "aqawan1",
      "listen": "127.0.0.1:8100",
      "telescopes": {
        "t1": {"telescope": "127.0.0.1:11120/0", "camera": "127.0.0.1:11130/0"},
        "t2": {"telescope": "127.0.0.1:11121/0", "camera": "127.0.0.1:11131/0",
               "filterwheel": "127.0.0.1:11131/0", "port_action": "..."}
      },
      "sequencer": {"error_backoff": 300, "max_consecutive_errors": 3,
                    "idle_release_after": 900},
      "plan": {"type": "calibration", "biases": 10, "darks": [[60, 5]]}
    }

and optionally:

* ``sequencer_name`` -- its lease holder name (default: ``name``). Keep it
  stable: a restarted sequencer recovers its predecessor's lease by it.
* ``supervisor_url`` -- the supervisor's API (default: ``http://`` +
  ``listen``).
* ``lease_client`` -- :class:`~robofast_core.lease_client.RemoteSupervisor`
  keyword arguments (``timeout``, ``poll``, ``retry``).
* ``command_timeout`` -- default per-command timeout for the telescopes and
  cameras (s; default 5).
* per telescope, ``filterwheel`` (an Alpaca FilterWheel; filters are
  selected by name from its ``Names``) and ``port_action`` (the telescope's
  Alpaca ``Action`` that selects an M3 port, given the port name as its
  parameter; without it, an observation that names a port fails rather than
  use the wrong one).
* ``status_file`` -- write a status snapshot (:func:`snapshot`) to this
  JSON file every ``status_interval`` seconds (default 5), atomically. The
  request API (:mod:`robofast_core.request_api`) reads it; a snapshot older
  than its ``max_age`` means the sequencer is not running.

``sequencer`` holds :class:`~robofast_core.sequencer.SequencerConfig` fields.
``plan`` is a plan entry (:func:`robofast_core.plan.plan_from_config`): the
dispatch scheduler (``{"type": "dispatch", ...}``, see
:func:`robofast_core.dispatch.dispatch_from_config`;
``sites/minerva/dispatch.example.json``), a static target list or a
calibration-only plan. ``--plan FILE`` replaces it with the entry in FILE,
or, if FILE holds a JSON list, with a static plan of those targets. A plan
with a background task (the dispatch scheduler's ToO drop box) runs it
alongside the sequencer.

Signals and exit status
-----------------------
SIGTERM or SIGINT stops it cleanly: the observations are cancelled, every
exposure aborted and every telescope parked, and the lease released. It also
exits when the plan has nothing more for any telescope: the dispatch
scheduler's night ends at dawn, a calibration plan's once its frames are
taken (with the enclosure closed). ``deploy/robofast-sequencer@.service``
and its timer start it each afternoon: evening calibrations, a wait for the
Sun (:mod:`robofast_core.when`), the night, morning calibrations. Exit
status:

* 0 -- stopped by a signal, or the plan is done;
* 2 -- bad command line or config;
* 3 -- needs an operator: quarantined, or a telescope gave up for the night.
  The process manager must not restart it (``RestartPreventExitStatus`` in
  ``deploy/robofast-sequencer@.service``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robofast_core.alpaca import alpaca_owner
from robofast_core.config import device_address, load_config, telescopes
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.plan import PlanProvider, StaticPlan, plan_from_config
from robofast_core.sequencer import Sequencer, SequencerConfig, TelescopeUnit
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: Exit status for "do not restart me; an operator must look".
NEEDS_OPERATOR = 3


def build_units(
    config: dict[str, Any], timeline: Timeline, command_timeout: float
) -> list[TelescopeUnit]:
    """One :class:`TelescopeUnit` per entry of ``telescopes``."""
    units = []
    for name, entry in telescopes(config).items():
        if "camera" not in entry:
            raise ValueError(f"telescope {name!r} has no camera")
        unknown = set(entry) - {"telescope", "camera", "filterwheel", "port_action"}
        if unknown:
            raise ValueError(f"telescope {name!r}: unknown keys {sorted(unknown)}")
        owners: dict[str, Any] = {}
        for kind in ("telescope", "camera", "filterwheel"):
            if kind not in entry:
                continue
            address, number = device_address(entry[kind])
            owners[kind] = alpaca_owner(
                f"{name}.{kind}",
                kind,
                address,
                timeline,
                number=number,
                default_timeout=command_timeout,
            )
        port_action = entry.get("port_action")
        if port_action is not None and (not isinstance(port_action, str) or not port_action):
            raise ValueError(f"telescope {name!r}: port_action must be a non-empty string")
        units.append(
            TelescopeUnit(
                name,
                owners["telescope"],
                owners["camera"],
                filterwheel=owners.get("filterwheel"),
                port_action=port_action,
            )
        )
    if not units:
        raise ValueError("the config lists no telescopes")
    return units


def load_plan(path: str | Path, names: list[str]) -> PlanProvider:
    """A plan entry from a JSON file, or a static plan from a JSON list."""
    path = Path(path)
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return StaticPlan(data, names)
    return plan_from_config(data, names, base=path.parent)


def build(
    config: dict[str, Any],
    timeline: Timeline,
    *,
    plan: PlanProvider | None = None,
    base: Path | None = None,
) -> tuple[Sequencer, RemoteSupervisor]:
    """A sequencer wired to the devices and the supervisor in ``config``."""
    units = build_units(config, timeline, float(config.get("command_timeout", 5.0)))
    names = [u.name for u in units]
    if plan is None:
        if "plan" not in config:
            raise ValueError("no plan: give the config a 'plan' entry or use --plan")
        plan = plan_from_config(config["plan"], names, base=base)
    url = config.get("supervisor_url") or f"http://{config.get('listen', '127.0.0.1:8100')}"
    remote = RemoteSupervisor(url, **config.get("lease_client", {}))
    seq = Sequencer(
        config.get("sequencer_name") or config["name"],
        supervisor=remote,
        telescopes=units,
        plan=plan,
        timeline=timeline,
        config=SequencerConfig(**config.get("sequencer", {})),
    )
    return seq, remote


def snapshot(seq: Sequencer, interval: float = 5.0) -> dict[str, Any]:
    """The sequencer's state for status pages: per telescope ``state``
    (``observing``, ``idle``, ``backoff``, ``gave_up``, ``finished`` or
    ``waiting`` -- for the enclosure to open), whether it is exposing, and
    what it is observing (from a plan with ``current()``, the dispatch
    scheduler). Stamped with the time and a ``max_age`` (design.md 3.8)."""
    current_fn = getattr(seq.plan, "current", None)
    try:
        current = current_fn() if current_fn is not None else {}
    except Exception:
        log.exception("plan.current()")
        current = {}
    now = time.monotonic()
    tels = {}
    for name, u in seq.units.items():
        if u.gave_up:
            state = "gave_up"
        elif u.finished:
            state = "finished"
        elif u.retry_at > now:
            state = "backoff"
        elif u.idle:
            state = "idle"
        elif name in current or u.exposing:
            state = "observing"
        else:
            state = "waiting"
        tels[name] = {
            "state": state,
            "exposing": u.exposing,
            "consecutive_errors": u.consecutive_errors,
            "current": current.get(name),
        }
    return {
        "time": datetime.now(UTC).isoformat(),
        "max_age": 3 * interval,
        "sequencer": seq.name,
        "generation": seq.generation,
        "quarantined": seq.quarantined,
        "needs_operator": seq.needs_operator,
        "telescopes": tels,
    }


def write_snapshot(seq: Sequencer, path: Path, interval: float = 5.0) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(snapshot(seq, interval)))
        os.replace(tmp, path)
    except OSError:
        log.exception("could not write the status snapshot %s", path)


async def _publish(seq: Sequencer, path: Path, interval: float) -> None:
    while True:
        write_snapshot(seq, path, interval)
        await asyncio.sleep(interval)


async def run(
    seq: Sequencer,
    remote: RemoteSupervisor,
    *,
    status_file: str | Path | None = None,
    status_interval: float = 5.0,
) -> None:
    """Run until the plan is done, an operator is needed, or a signal."""
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    # A plan's background task (the dispatch scheduler's ToO intake), if any.
    serve = getattr(seq.plan, "serve", None)
    background = [asyncio.create_task(serve(), name="plan-serve")] if serve else []
    if status_file is not None:
        background.append(
            asyncio.create_task(
                _publish(seq, Path(status_file), status_interval), name="status-snapshot"
            )
        )
    try:
        await seq.run()
    except asyncio.CancelledError:
        pass  # a signal: run() cleaned up and released the lease on its way out
    finally:
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if status_file is not None:
            write_snapshot(seq, Path(status_file), status_interval)  # the final state
        await remote.aclose()
        for unit in seq.units.values():
            for owner in (unit.telescope, unit.camera, unit.filterwheel):
                if owner is not None:
                    owner.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="enclosure config file (see module doc)")
    ap.add_argument("--plan", help="plan file, replacing the config's 'plan' entry")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config(args.config)
        tl = Timeline(maxlen=config.get("timeline_size", 10000))
        plan = load_plan(args.plan, list(telescopes(config))) if args.plan else None
        seq, remote = build(config, tl, plan=plan, base=Path(args.config).parent)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        ap.error(f"{args.config}: {exc!r}")
    status_file = config.get("status_file")
    if status_file is not None:
        status_file = Path(args.config).parent / status_file
    asyncio.run(
        run(
            seq,
            remote,
            status_file=status_file,
            status_interval=float(config.get("status_interval", 5.0)),
        )
    )
    if seq.needs_operator:
        log.critical(
            "%s: needs an operator (quarantined=%s, gave up: %s)",
            seq.name,
            seq.quarantined,
            [u.name for u in seq.units.values() if u.gave_up],
        )
        sys.exit(NEEDS_OPERATOR)


if __name__ == "__main__":
    main()
