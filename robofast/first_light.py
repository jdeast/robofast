"""First light: from a telescope plugged into this computer to a solved image.

::

    pip install robofast
    python -m robofast --site 42.38,-71.13 --no-enclosure      # or: robofast ...

One command, for a classroom or a first night: it finds the telescope and
camera (Alpaca discovery, :mod:`robofast.discover`), reads their plate
scale from them, writes the enclosure config itself (``--dir``, default
``first-light/``; it never overwrites one), and runs robofast's own safety
supervisor and sequencer on it, in this process: a first pointing
calibration from three plate solves (docs/pointing_model.md, "A first
calibration"), then one light frame near the zenith, plate-solved
(docs/sky.md). It prints where the frame points and exits 0 once it is
solved; 1 if robofast could not get there, saying why.

What it never reads or guesses, and so asks for in words:

* ``--site LAT,LON[,ELEV]``: the pointing run lays its points in altitude
  from it (a mount's own site setting is often wrong);
* the enclosure: ``--no-enclosure`` (a telescope on a tripod: the operator
  on duty judges the weather; robofast.safety, "No enclosure"), or
  ``--dome HOST:PORT/N --safety-monitor HOST:PORT/N``.

Light frames need the supervisor's lease. With ``--no-enclosure`` that is
an operator on duty: the person running this command, who is asked first
whether the sky is clear and whether they will stay with the telescope
(``--yes`` answers yes). Their duty lasts while the command runs; Ctrl-C,
or the end, takes them off duty, and the telescope parks.

The catalog is ``--catalog`` (default ``gaia``: Gaia DR3 tiles, downloaded
on first use; docs/sky.md). ``--plugin`` loads plug-ins first (the
simulated telescope's ``synthetic`` sky: ``--plugin
robofast.testing.sky_device:register --catalog synthetic``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from robofast import discover
from robofast.catalog import separation
from robofast.timeline import Timeline

log = logging.getLogger(__name__)

#: Exposure (s) of the calibration's frames and of the first light frame.
EXPTIME = 5.0


class FirstLightError(RuntimeError):
    """What stopped first light (printed; exit status 1)."""


class FirstLightStopped(FirstLightError):
    """Stopped by the operator (Ctrl-C): ``parked`` says whether every
    telescope reported parked after the sequencer's cleanup."""

    def __init__(self, parked: bool, why: str = "") -> None:
        self.parked = parked
        if parked:
            text = "stopped; the telescope parked"
        else:
            text = (
                "stopped; the telescope DID NOT PARK"
                + (f" ({why})" if why else "")
                + ": park it by hand now"
            )
        super().__init__(text)


def first_light_config(
    devices: list[discover.Device],
    *,
    site: tuple[float, float, float],
    enclosure: dict[str, str] | None,
    catalog: dict[str, Any],
    plugins: list[str],
    exptime: float = EXPTIME,
    name: str = "first-light",
) -> dict[str, Any]:
    """The enclosure config first light runs on: the starter config of
    :func:`robofast.discover.starter_config` (with the optics read from the
    devices), and what the operator said (``site``; ``enclosure`` None: no
    enclosure, else ``{"dome": spec, "safetymonitor": spec}``)."""
    config = discover.starter_config(devices, name, optics=True)
    scopes = config["telescopes"]
    if len(scopes) != 1:
        raise FirstLightError(
            f"first light runs one telescope; {len(scopes)} answered"
            f" ({', '.join(d.spec for d in devices if d.type == 'Telescope') or 'none'})"
        )
    (tel,) = scopes
    if "camera" not in scopes[tel]:
        raise FirstLightError(f"{tel}: no camera paired with the telescope (see the device list)")
    lat, lon, elev = site
    optics = config["images"]["telescopes"][tel]
    config["_note"].append(
        "Written by robofast first light (python -m robofast); site and enclosure from"
        " its command line."
    )
    if enclosure is None:
        config["enclosure"] = "none"
    else:
        config["devices"] = dict(enclosure)
    config["site"] = {"latitude": lat, "longitude": lon, "elevation": elev}
    if plugins:
        config["plugins"] = list(plugins)
    config["images"]["solve"] = {"catalog": dict(catalog)}
    scopes[tel]["pointing"] = {
        "latitude": lat,
        "longitude": lon,
        "store": "pointing",
        "solver": {"plate_scale": optics["plate_scale"]},
        "catalog": dict(catalog),
        "run": {"settle": 1.0},
    }
    config["plan"] = {
        "type": "static",
        "targets": [
            {
                "name": "first-pointing",
                "kind": "pointing_model",
                "count": 3,
                "exptime": exptime,
                "pointing": {"min_points": 3},
            },
            # The zenith is filled in when the run starts (``_zenith``).
            {"name": "first-light", "ra_hours": None, "dec_deg": lat, "exptime": exptime},
        ],
    }
    return config


def _zenith(config: dict[str, Any], lst_hours: float) -> None:
    for t in config["plan"]["targets"]:
        if t["name"] == "first-light":
            t["ra_hours"] = round(lst_hours % 24.0, 5)


def _lst_hours(site: tuple[float, float, float], when: datetime) -> float:
    """Local apparent sidereal time (hours) at ``site`` (lat, lon, elev), ``when``."""
    from robofast.sky import Site, offline_iers, sky_now

    offline_iers()
    return sky_now(Site(site[0], site[1], site[2]), when).lst_hours


def _site(text: str) -> tuple[float, float, float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) not in (2, 3):
        raise ValueError("--site: LAT,LON or LAT,LON,ELEV (deg north, deg east, m)")
    lat, lon = parts[0], parts[1]
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        raise ValueError(f"--site: {text}: latitude -90..90, longitude -180..360")
    return lat, lon, parts[2] if len(parts) == 3 else 0.0


def _first_light_frame(data_root: Path, tel: str) -> dict[str, Any] | None:
    """The first-light frame's index entry (docs/data.md), if written."""
    for index in sorted(data_root.glob(f"*/{tel}/n*/*.{tel}.frames.jsonl")):
        for line in index.read_text().splitlines():
            entry = json.loads(line)
            if entry.get("object") == "first-light":
                return entry
    return None


def _waiting(sup: Any) -> str:
    """What the supervisor says, for a first light that did not finish."""
    st = sup.status()
    out = f"the supervisor: {st['reason']}; opening allowed: {st['open_allowed']}"
    if st.get("lease") is None:
        out += "; no lease granted"
    hold = sup.config.reopen_hold
    if sup.enclosure and st["safe"] and not st["open_allowed"]:
        out += f" (an enclosure opens after {hold:g} s of safe weather: safety.reopen_hold)"
    return out


def _close_bound(sup: Any) -> float:
    return sup.config.shutter_timeout + 3 * sup.config.poll_interval


async def _enclosure_closed(sup: Any) -> bool:
    """Wait (bounded) for the supervisor to read the shutter closed."""
    from robofast.safety import Shutter

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _close_bound(sup)
    while loop.time() < deadline:
        if sup.shutter == Shutter.CLOSED and sup.outstanding() is None:
            return True
        await asyncio.sleep(min(0.5, sup.config.poll_interval))
    return False


async def run(
    config: dict[str, Any],
    workdir: Path,
    operator: str,
    *,
    timeout: float = 1800.0,
    stop: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Run first light on ``config`` (already written in ``workdir``);
    return the first-light frame's index entry, with its solve. Raises
    :class:`FirstLightError` saying what stopped it; ``stop`` set (Ctrl-C),
    or this task cancelled (Ctrl-C where the loop has no signal handlers),
    stops the sequencer, whose cleanup parks, and raises
    :class:`FirstLightStopped` saying whether every telescope parked."""
    from robofast import sequencer_service, supervisor_service

    tl = Timeline(maxlen=20000)
    sup = supervisor_service.build(config, tl)
    server = await supervisor_service.SupervisorServer(sup, "127.0.0.1", 0).start()
    config = {**config, "supervisor_url": server.url}
    seq, remote = sequencer_service.build(config, tl, base=workdir)
    (tel,) = config["telescopes"]
    sup_task = asyncio.create_task(sup.run(), name="supervisor")

    async def keep_duty() -> None:
        while True:
            await asyncio.sleep(max(1.0, sup.config.duty_ttl / 3))
            sup.duty_renew(operator)

    duty = None
    if sup._attended_only():
        sup.duty_on(operator, "first light (python -m robofast)")
        duty = asyncio.create_task(keep_duty(), name="duty")
    seq_task = asyncio.create_task(
        sequencer_service.run(seq, remote, handle_signals=False), name="sequencer"
    )
    stop = stop or asyncio.Event()
    stopped, parked = False, False
    try:
        stop_wait = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait({seq_task, stop_wait}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        stop_wait.cancel()
        stopped = stop.is_set()
        if not done:
            raise FirstLightError(f"not done after {timeout:g} s; {_waiting(sup)}")
    except asyncio.CancelledError:
        stopped = True  # Ctrl-C where the loop has no signal handlers (Windows)
    finally:
        if not seq_task.done():
            seq_task.cancel()  # its cleanup aborts and parks
            with contextlib.suppress(BaseException):
                await seq_task
        if stopped:
            try:
                parked = await sup._telescopes_parked()
            except Exception:  # an unreadable mount: not known to be parked
                parked = False
        if duty is not None:
            duty.cancel()
            sup.duty_off(operator, "first light over")
        # The enclosure closes through the supervisor's own enclosure loop
        # (the lease is gone); first light returns once it reads closed.
        closed = await _enclosure_closed(sup) if sup.enclosure else None
        sup_task.cancel()
        with contextlib.suppress(BaseException):
            await sup_task
        await server.stop()
    if stopped:
        raise FirstLightStopped(parked, "; ".join(list(sup.alarms)[-2:]))
    if closed is False:
        raise FirstLightError(
            f"the enclosure did not read closed within {_close_bound(sup):g} s"
            f" (shutter {sup.status()['shutter']}): check it now"
        )
    errors = [e.detail.get("error") for e in tl.events("telescope_error")]
    if not seq.pointing_models:
        unsolved = [e.detail.get("error") for e in tl.events("pointing_point_unsolved")]
        raise FirstLightError(
            "the pointing calibration did not build a model"
            + (f" (unsolved: {'; '.join(map(str, unsolved[:3]))})" if unsolved else "")
            + (f" ({'; '.join(map(str, errors[:2]))})" if errors else "")
        )
    images = config["images"]
    root = Path(images["data_root"])
    entry = _first_light_frame(root if root.is_absolute() else workdir / root, tel)
    if entry is None:
        raise FirstLightError(
            "no first-light frame was written"
            + (f" ({'; '.join(map(str, errors[:2]))})" if errors else "")
        )
    solve = entry.get("solve") or {}
    if not solve.get("ra") and solve.get("ra") != 0:
        raise FirstLightError(f"the first-light frame {entry.get('file')} did not solve: {solve}")
    entry["model"] = seq.pointing_models[-1].model.terms
    entry["enclosure"] = None if closed is None else "closed"
    return entry


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="robofast", description=__doc__.split("\n\n")[0], epilog="docs/first_light.md"
    )
    ap.add_argument("--site", required=True, help="LAT,LON[,ELEV]: deg north, deg east, m")
    enc = ap.add_mutually_exclusive_group(required=True)
    enc.add_argument(
        "--no-enclosure",
        action="store_true",
        help="no dome or roof: the operator on duty judges the weather",
    )
    enc.add_argument("--dome", metavar="HOST:PORT/N", help="the enclosure's Alpaca Dome")
    ap.add_argument("--safety-monitor", metavar="HOST:PORT/N", help="with --dome")
    ap.add_argument("--address", default="255.255.255.255", help="where to send discovery")
    ap.add_argument("--discovery-port", type=int, default=discover.DISCOVERY_PORT)
    ap.add_argument("--server", action="append", default=[], metavar="HOST:PORT")
    ap.add_argument("--dir", default="first-light", help="where the config and data go")
    ap.add_argument("--catalog", default="gaia", help="catalog source (docs/sky.md)")
    ap.add_argument("--catalog-dir", help="the catalog's cache directory")
    ap.add_argument("--plugin", action="append", default=[], help="a plug-in module to load")
    ap.add_argument("--exptime", type=float, default=EXPTIME, help="seconds per frame")
    ap.add_argument("--operator", default=getpass.getuser(), help="who is on duty")
    ap.add_argument("--yes", action="store_true", help="answer yes to the duty question")
    ap.add_argument("--timeout", type=float, default=1800.0, help="give up after (s)")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        site = _site(args.site)
    except ValueError as exc:
        ap.error(str(exc))
    if args.dome and not args.safety_monitor:
        ap.error("--dome needs --safety-monitor (the enclosure's weather verdict)")
    enclosure = (
        None if args.no_enclosure else {"dome": args.dome, "safetymonitor": args.safety_monitor}
    )
    workdir = Path(args.dir)
    path = workdir / "observatory.json"
    if path.exists():
        ap.error(f"{path} exists: not overwritten (give another --dir)")
    from robofast.plugins import load_plugins

    load_plugins(args.plugin)
    servers = list(args.server) or discover.discover(args.address, args.discovery_port, 2.0)
    if not servers:
        print(f"no Alpaca server answered discovery at {args.address}", file=sys.stderr)
        sys.exit(1)
    devices: list[discover.Device] = []
    for server in servers:  # as robofast-discover: a server with no device list is named
        try:
            devices += discover.configured_devices(server)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            print(f"first light: {server}: no device list ({exc!r})", file=sys.stderr)
    if not devices:
        print("first light: no Alpaca device answered", file=sys.stderr)
        sys.exit(1)
    for d in devices:
        print(f"found {d.type:<20} {d.spec:<24} {d.name}")
    catalog: dict[str, Any] = {"source": args.catalog}
    if args.catalog_dir:
        catalog["cache_dir"] = str(Path(args.catalog_dir).resolve())
    try:
        config = first_light_config(
            devices,
            site=site,
            enclosure=enclosure,
            catalog=catalog,
            plugins=args.plugin,
            exptime=args.exptime,
        )
    except (FirstLightError, discover.OpticsUnavailable) as exc:
        print(f"first light: {exc}", file=sys.stderr)
        sys.exit(1)
    (tel,) = config["telescopes"]
    print(f"plate scale {config['images']['telescopes'][tel]['plate_scale']} arcsec/pixel")
    if enclosure is None and not args.yes:
        answer = input(
            "No enclosure: robofast cannot see the weather. Is the sky clear, and will you"
            " stay with the telescope until this finishes? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("first light: not started", file=sys.stderr)
            sys.exit(1)
    # The zenith now, from the site given and this computer's clock (the
    # mount's own site and clock are not read: docs/first_light.md).
    planned = datetime.now(UTC)
    _zenith(config, _lst_hours(site, planned))
    workdir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote {path}")
    print("first light: a pointing calibration (3 plate solves), then one frame at the zenith")

    async def go() -> dict[str, Any]:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Windows' loops have none: Ctrl-C reaches asyncio.run, which
            # cancels this task, and run() treats that as the same stop.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        return await run(config, workdir, args.operator, timeout=args.timeout, stop=stop)

    try:
        entry = asyncio.run(go())
    except FirstLightError as exc:
        print(f"first light: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:  # a second Ctrl-C, before the cleanup finished
        print(
            "first light: stopped before the cleanup finished; the telescope may NOT be"
            " parked: check it now",
            file=sys.stderr,
        )
        sys.exit(1)
    solve = entry["solve"]
    target_ra, target_dec = config["plan"]["targets"][1]["ra_hours"] * 15.0, site[0]
    ra, dec = float(solve["ra"]), float(solve["dec"])
    off = separation(ra, dec, target_ra, target_dec) * 3600.0
    taken = datetime.fromisoformat(str(solve["date_obs"])).replace(tzinfo=UTC)
    zenith = separation(ra, dec, _lst_hours(site, taken) * 15.0, site[0])
    terms = ", ".join(f'{k} {v:+.1f}"' for k, v in entry["model"].items())
    root = Path(config["images"]["data_root"])
    if entry.get("enclosure"):
        print(f"first light: enclosure {entry['enclosure']}")
    print(f"first light frame: {(root if root.is_absolute() else workdir / root) / entry['file']}")
    print(
        f"  solved: RA {ra:.5f} Dec {dec:+.5f} deg, {off:.1f} arcsec from its target (the"
        f" zenith at {planned:%H:%M:%S} UTC), {zenith * 60:.1f} arcmin from the zenith"
        f" at {taken:%H:%M:%S} UTC, when it was taken; pointing model {terms}"
    )
    print(json.dumps({"first_light": entry}))


if __name__ == "__main__":
    main()
