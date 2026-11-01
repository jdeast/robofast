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
* per telescope, ``calstage``: the TRES front end's calibration stage (an
  Alpaca FilterWheel whose ``Names`` are its positions,
  ``docs/tres_frontend.md``); an observation's ``calstage`` is selected by
  name before its exposures.
* per telescope, ``focuser`` and ``rotator`` (Alpaca devices, read for
  the FITS headers). With an ``autofocus`` entry
  (:class:`~robofast_core.autofocus.AutofocusSetup`; ``docs/autofocus.md``)
  the sequencer also moves the focuser, for ``kind="autofocus"``
  observations.
* ``autofocus_triggers`` -- insert autofocus observations into the plan
  (:class:`~robofast_core.autofocus_plan.AutofocusTriggers`): at the start
  of the night, on a temperature change, on FWHM degradation.
* per telescope, ``guider``: the guider service's URL, or ``{"url": ...,
  "acquire_timeout": s, "timeout": s, "poll_interval": s}``
  (:class:`~robofast_core.guider.service.GuiderRemote`; docs/guider.md).
  Observations with ``guide`` start it on target; cleanup stops it before
  parking.
* per telescope, ``pointing``: robofast's own pointing model
  (:class:`~robofast_core.pointmodel.PointingCorrector`;
  docs/pointing_model.md): applied to every slew, measured on every solved
  frame, built by ``kind="pointing_model"`` runs.
* per telescope, ``pa_calibration``: the port PA calibration store and the
  routine's settings (:class:`~robofast_core.rotcal.RotatorSetup`;
  docs/rotcal.md); the sequencer then also moves the rotator, for
  ``kind="rotator_calibration"``, and the frame pipeline seeds every
  frame's WCS from it.
* per telescope, ``recovery``: the recovery ladder per device
  (:class:`~robofast_core.recovery.RecoveryLadder`; docs/recovery.md):
  reconnect, restart the vendor software, power-cycle its PDU outlet, then
  give up for the night.
* ``images`` -- save every frame (:mod:`robofast_core.imaging`;
  ``docs/data.md``): ``{"data_root": "/data", "site": "minerva", ...}``
  with :class:`~robofast_core.imaging.ImagingConfig` fields; a relative
  ``data_root`` is relative to the config file. The enclosure's ``site``
  gives the Sun and Moon and the night; ``devices.observingconditions``
  (the weather bridge, only read) the weather keywords; the supervisor's
  status the shutter. Without ``images`` no frame is saved.
* ``spectrograph`` -- the enclosure's fiber-fed spectrograph
  (:class:`~robofast_core.devices.kiwispec_service.KiwiSpecRemote`;
  ``docs/kiwispec.md``, ``docs/rv_night.md``): ``{"url": ...,
  "telescopes": {"t1": "T1", ...}, "timeout": s, "abort_timeout": s,
  "queue_wait": s, "data_root": ..., "instrument": "kiwispec"}``.
  ``queue_wait`` (default ``sequencer.spectrograph_queue_wait``) is how
  long a request waits in the service's queue while another enclosure
  holds the shared spectrograph (docs/kiwispec.md). ``telescopes`` maps the
  enclosure's telescope names to the spectrograph service's (default: the
  same). Spectrum and spectrograph calibration sidecars go under
  ``data_root`` (default: ``images.data_root``; relative to the config
  file), with the barycentric correction for the enclosure's ``site``.
  Observations of kind ``spectrum`` need it (the dispatch scheduler's
  ``SPECTRUM`` configurations), as do a calibration plan's ``spectrograph``
  frames.
* ``operator_control`` -- a directory: the observer console's pause,
  resume and stop commands come in through it
  (:class:`~robofast_core.console.hold.OperatorHold`, which wraps the plan;
  ``docs/console.md``). Relative to the config file.
* ``clock`` -- simulation only: the clock every night name and time stamp
  comes from (:func:`robofast_core.clock.clock_from_config`; e.g.
  ``{"file": "clock.json"}``). Default: the host's clock (which must be
  NTP-disciplined, docs/deploy.md).
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
from robofast_core.autofocus import AutofocusSetup
from robofast_core.autofocus_plan import AutofocusTriggers, TriggerConfig
from robofast_core.cancel import stop_tasks
from robofast_core.clock import Clock, clock_from_config
from robofast_core.config import device_address, load_config, telescopes
from robofast_core.imaging import imaging_from_config
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.owner import DeviceOwner
from robofast_core.plan import PlanProvider, StaticPlan, plan_from_config
from robofast_core.sequencer import Sequencer, SequencerConfig, TelescopeUnit
from robofast_core.spectra import SpectrumRecorder
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: Exit status for "do not restart me; an operator must look".
NEEDS_OPERATOR = 3


TELESCOPE_KEYS = {
    "telescope",
    "camera",
    "filterwheel",
    "calstage",
    "port_action",
    "focuser",
    "rotator",
    "autofocus",
    "guider",
    "pointing",
    "pa_calibration",
    "recovery",
    "sky_flats",
}


def build_units(
    config: dict[str, Any],
    timeline: Timeline,
    command_timeout: float,
    *,
    imagebytes: bool = True,
    aux: dict[str, dict[str, DeviceOwner]] | None = None,
    base: Path | None = None,
) -> list[TelescopeUnit]:
    """One :class:`TelescopeUnit` per entry of ``telescopes``. Focusers and
    rotators (read for headers) go into ``aux["focuser"]`` and
    ``aux["rotator"]`` by telescope name, if ``aux`` is given; a telescope
    with an ``autofocus`` entry also lets the sequencer move its focuser."""
    units = []
    for name, entry in telescopes(config).items():
        if "camera" not in entry:
            raise ValueError(f"telescope {name!r} has no camera")
        unknown = set(entry) - TELESCOPE_KEYS
        if unknown:
            raise ValueError(f"telescope {name!r}: unknown keys {sorted(unknown)}")
        owners: dict[str, Any] = {}
        for kind in ("telescope", "camera", "filterwheel", "calstage", "focuser", "rotator"):
            if kind not in entry:
                continue
            address, number = device_address(entry[kind])
            owners[kind] = alpaca_owner(
                f"{name}.{kind}",
                "filterwheel" if kind == "calstage" else kind,
                address,
                timeline,
                number=number,
                default_timeout=command_timeout,
                imagebytes=imagebytes,
            )
            if kind in ("focuser", "rotator") and aux is not None:
                aux.setdefault(kind, {})[name] = owners[kind]
        port_action = entry.get("port_action")
        if port_action is not None and (not isinstance(port_action, str) or not port_action):
            raise ValueError(f"telescope {name!r}: port_action must be a non-empty string")
        autofocus = None
        if entry.get("autofocus") is not None:
            if "focuser" not in owners:
                raise ValueError(f"telescope {name!r}: autofocus needs a focuser")
            try:
                autofocus = AutofocusSetup.from_dict(entry["autofocus"], base)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"telescope {name!r}: {exc}") from exc
        pointing = None
        if entry.get("pointing") is not None:
            from robofast_core.pointmodel import PointingCorrector

            try:
                pointing = PointingCorrector.from_config(name, entry["pointing"], base=base)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"telescope {name!r}: {exc}") from exc
        pa_cal = None
        if entry.get("pa_calibration") is not None:
            from robofast_core.rotcal import RotatorSetup

            try:
                pa_cal = RotatorSetup.from_config(entry["pa_calibration"], base=base)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"telescope {name!r}: {exc}") from exc
        recovery = None
        if entry.get("recovery") is not None:
            from robofast_core.recovery import RecoveryLadder

            try:
                recovery = RecoveryLadder.from_config(name, entry["recovery"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"telescope {name!r}: {exc}") from exc
        guider = None
        if "guider" in entry:
            from robofast_core.guider.service import GuiderRemote

            g = entry["guider"]
            g = {"url": g} if isinstance(g, str) else dict(g)
            guider = GuiderRemote(g.pop("url"), scope=name, **g)
        units.append(
            TelescopeUnit(
                name,
                owners["telescope"],
                owners["camera"],
                filterwheel=owners.get("filterwheel"),
                calstage=owners.get("calstage"),
                port_action=port_action,
                focuser=owners.get("focuser") if autofocus is not None else None,
                autofocus=autofocus,
                guider=guider,
                pointing=pointing,
                rotator=owners.get("rotator") if pa_cal is not None else None,
                pa_calibration=pa_cal,
                recovery=recovery,
                flats=_flats(name, entry.get("sky_flats")),
            )
        )
    if not units:
        raise ValueError("the config lists no telescopes")
    return units


def _flats(name: str, d: Any) -> Any:
    if d is None:
        return None
    from robofast_core.skyflats import FlatConfig

    try:
        return FlatConfig.from_dict(d)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"telescope {name!r}: {exc}") from exc


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
    clock: Clock | None = None,
) -> tuple[Sequencer, RemoteSupervisor]:
    """A sequencer wired to the devices and the supervisor in ``config``
    (and, with an ``images`` entry, its frame pipeline: ``seq.imaging``).
    ``clock`` (else the config's ``clock``, else a given plan's, else the
    host's) is the one clock of :mod:`robofast_core.clock`."""
    if clock is None and config.get("clock") is not None:
        clock = clock_from_config(config["clock"], base)
    command_timeout = float(config.get("command_timeout", 5.0))
    images = config.get("images") or {}
    aux: dict[str, dict[str, DeviceOwner]] = {}
    units = build_units(
        config,
        timeline,
        command_timeout,
        imagebytes=bool(images.get("imagebytes", True)),
        aux=aux,
        base=base,
    )
    names = [u.name for u in units]
    if plan is None:
        if "plan" not in config:
            raise ValueError("no plan: give the config a 'plan' entry or use --plan")
        plan = plan_from_config(config["plan"], names, base=base, clock=clock)
    triggers = None
    if config.get("autofocus_triggers") is not None:
        focusable = [u.name for u in units if u.autofocus is not None]
        if not focusable:
            raise ValueError("autofocus_triggers: no telescope has an autofocus entry")
        tcfg = TriggerConfig.from_dict(config["autofocus_triggers"])
        plan = triggers = AutofocusTriggers(plan, tcfg, telescopes=focusable)
    if config.get("operator_control") is not None:
        # The observer console's pause/resume/stop (docs/console.md).
        from robofast_core.console.hold import OperatorHold

        control = Path(config["operator_control"])
        if base is not None and not control.is_absolute():
            control = base / control
        plan = OperatorHold(plan, names, control)
    url = config.get("supervisor_url") or f"http://{config.get('listen', '127.0.0.1:8100')}"
    remote = RemoteSupervisor(url, **config.get("lease_client", {}))
    weather = None
    oc = (config.get("devices") or {}).get("observingconditions")
    if config.get("images") is not None and oc:
        address, number = device_address(oc)
        weather = alpaca_owner(
            "telemetry.observingconditions",
            "observingconditions",
            address,
            timeline,
            number=number,
            default_timeout=command_timeout,
        )
    imaging = imaging_from_config(
        config,
        timeline,
        supervisor=remote,
        weather=weather,
        focusers=aux.get("focuser"),
        rotators=aux.get("rotator"),
        base=base,
    )
    if imaging is not None:
        for u in units:
            if u.guider is not None and getattr(u.guider, "registry", 0) is None:
                u.guider.registry = imaging.registry  # guide values in the headers
        imaging.owners = [o for d in aux.values() for o in d.values()]
        if weather is not None:
            imaging.owners.append(weather)
    seq_config = SequencerConfig(**config.get("sequencer", {}))
    spectrograph, spec_names, spectra = spectrograph_from_config(
        config, base, queue_wait=seq_config.spectrograph_queue_wait
    )
    seq = Sequencer(
        config.get("sequencer_name") or config["name"],
        supervisor=remote,
        telescopes=units,
        plan=plan,
        timeline=timeline,
        config=seq_config,
        imaging=imaging,
        spectrograph=spectrograph,
        spectrograph_names=spec_names,
        spectra=spectra,
        clock=clock,
    )
    if triggers is not None:
        triggers.bind(seq.registry)
    return seq, remote


SPECTROGRAPH_KEYS = {
    "url",
    "telescopes",
    "timeout",
    "abort_timeout",
    "queue_wait",
    "data_root",
    "instrument",
}


def spectrograph_from_config(
    config: dict[str, Any], base: Path | None = None, *, queue_wait: float = 600.0
) -> tuple[Any, dict[str, str], SpectrumRecorder | None]:
    """The ``spectrograph`` entry (module doc): its client, the telescope
    name map, and the sidecar writer (None without a data root)."""
    entry = config.get("spectrograph")
    if entry is None:
        return None, {}, None
    if isinstance(entry, str):
        entry = {"url": entry}
    entry = {k: v for k, v in entry.items() if not k.startswith("_")}
    unknown = set(entry) - SPECTROGRAPH_KEYS
    if unknown:
        raise ValueError(f"spectrograph: unknown keys {sorted(unknown)}")
    if "url" not in entry:
        raise ValueError("spectrograph: 'url' is required")
    from robofast_core.devices.kiwispec_service import KiwiSpecRemote
    from robofast_core.sky import Site  # imports astropy

    remote = KiwiSpecRemote(
        str(entry["url"]),
        timeout=float(entry.get("timeout", 10.0)),
        abort_timeout=float(entry.get("abort_timeout", 45.0)),
        client=str(config.get("sequencer_name") or config.get("name") or ""),
        queue_wait=float(entry.get("queue_wait", queue_wait)),
    )
    names = {str(k): str(v) for k, v in (entry.get("telescopes") or {}).items()}
    unknown_tels = set(names) - set(telescopes(config))
    if unknown_tels:
        raise ValueError(f"spectrograph.telescopes: unknown telescopes {sorted(unknown_tels)}")
    root = entry.get("data_root") or (config.get("images") or {}).get("data_root")
    recorder = None
    if root is not None:
        site = config.get("site")
        if not isinstance(site, dict):
            raise ValueError("spectrograph: the barycentric correction needs the config's 'site'")
        path = Path(root)
        if base is not None and not path.is_absolute():
            path = base / path
        images = config.get("images") or {}
        recorder = SpectrumRecorder(
            path,
            str(images.get("site", config.get("name", "site"))),
            Site(float(site["latitude"]), float(site["longitude"]), float(site["elevation"])),
            instrument=str(entry.get("instrument", "kiwispec")),
            night_rollover_hour=float(images.get("night_rollover_hour", 12.0)),
            timezone=images.get("timezone"),
        )
    return remote, names, recorder


#: Timeline entries in each status snapshot.
TIMELINE_IN_SNAPSHOT = 100


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
            "spectrum": u.spectrum,
            "exposing": u.exposing,
            "consecutive_errors": u.consecutive_errors,
            "current": current.get(name),
            "park_requested": u.park_requested,
            "alarms": list(u.alarms),
        }
    out = {
        "time": datetime.now(UTC).isoformat(),
        "max_age": 3 * interval,
        "sequencer": seq.name,
        "generation": seq.generation,
        "quarantined": seq.quarantined,
        "needs_operator": seq.needs_operator,
        "telescopes": tels,
        # The latest of the sequencer's own timeline (the console's log).
        "timeline": seq.timeline.recent(TIMELINE_IN_SNAPSHOT, skip_sent=True),
    }
    if seq.spectrograph is not None:
        out["spectrograph"] = {
            "down": seq.spectrograph_down,
            "failures": seq.spectrograph_failures,
        }
    if seq.imaging is not None:
        # The value registry (design.md 3.8), and frames lost per telescope.
        out["values"] = seq.imaging.registry.as_json()
        for name in tels:
            failed = seq.imaging.failed(name)
            if failed:
                tels[name]["frames_failed"] = failed
    return out


def write_snapshot(seq: Sequencer, path: Path, interval: float = 5.0) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(snapshot(seq, interval), default=str))
        os.replace(tmp, path)
    except OSError:
        log.exception("could not write the status snapshot %s", path)


#: How long stopping waits for a background task (the plan's, telemetry's,
#: the status snapshot's) to end once cancelled.
STOP_TIMEOUT = 10.0


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
    if seq.imaging is not None:
        background.append(asyncio.create_task(seq.imaging.telemetry.run(), name="telemetry"))
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
        await stop_tasks(background, STOP_TIMEOUT)  # bounded (robofast_core.cancel)
        if seq.imaging is not None:
            await seq.imaging.aclose()  # the frames still queued are written
            for owner in seq.imaging.owners:
                owner.close()
        if status_file is not None:
            write_snapshot(seq, Path(status_file), status_interval)  # the final state
        await remote.aclose()
        for unit in seq.units.values():
            for owner in (unit.telescope, unit.camera, unit.filterwheel, unit.calstage):
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
