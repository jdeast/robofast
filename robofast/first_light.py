"""First light: from a telescope plugged into this computer to a solved image.

::

    pip install .          # the repository or a wheel built from it (not on PyPI yet)
    python -m robofast --no-enclosure      # or: robofast ...

One command, for a classroom or a first night: it finds the telescope and
camera (Alpaca discovery, :mod:`robofast.discover`), reads their plate
scale from them, writes the enclosure config itself (``--dir``, default
``first-light/``; it never overwrites one), and runs robofast's own safety
supervisor and sequencer on it, in this process: a focus, from however
far out, on the brightest planet or star up (robofast.first_focus), a
first pointing calibration from three plate solves (docs/pointing_model.md, "A first
calibration"), then one light frame near the zenith, plate-solved
(docs/sky.md). It prints where the frame points and exits 0 once it is
solved; 1 if robofast could not get there, saying why.

The site is the telescope's own (``SiteLatitude``, ``SiteLongitude``; a GPS
mount fills them; its elevation is not read: 0 m), or ``--site LAT,LON[,ELEV]``,
which is used instead. The telescope's site is shown and asked about (a
mount with no GPS may report its factory site, another continent: points
laid for it can be below the real horizon); a telescope that cannot
report its site, or reports 0, 0, with no ``--site``, stops before anything
moves. The pointing run lays its points in altitude from it; a site
hundreds of km off still calibrates (docs/first_light.md, "The site").

What it never reads or guesses, and so asks for in words:

* the enclosure: ``--no-enclosure`` (a telescope on a tripod: the operator
  on duty judges the weather; robofast.safety, "No enclosure"), or
  ``--dome HOST:PORT/N --safety-monitor HOST:PORT/N``.

Light frames need the supervisor's lease. With ``--no-enclosure`` that is
an operator on duty: the person running this command, who is asked first
whether the sky is clear and whether they will stay with the telescope
(and whether the telescope's site is where it stands; ``--yes`` answers
yes to all of it). Their duty lasts while the command runs; Ctrl-C,
or the end, takes them off duty, and the telescope parks.

The catalog is ``--catalog`` (default ``gaia``: Gaia DR3 tiles, docs/sky.md),
downloaded for the four frames before the telescope moves. ``--plugin`` loads plug-ins first (the
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
import math
import signal
import sys
import threading
import time
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

#: Fewest matched stars for a solve, the calibration's and the frame's.
MIN_MATCHES = 8

#: Margin (deg) on each field's catalog cone: the calibration's points are
#: slewed to a minute or two after they are laid out, and the sky turns.
FIELD_MARGIN = 0.25

#: The calibration's points are fixed in altitude and azimuth, so their
#: fields move with the sky: its tiles are fetched for where they will be
#: over the calibration, every SKY_STEP s (the sky turns 0.25 deg a
#: minute, within :data:`FIELD_MARGIN`), allowing PER_POINT s a point
#: (slew, settle, readout, solve) besides its exposure.
SKY_STEP = 60.0
PER_POINT = 60.0

#: At most this many download passes: on a network slower than the sky
#: turns (about six tiles a minute for three points), fetching after the
#: fields cannot catch them, and the rest download during the calibration.
FETCH_PASSES = 5

#: Minutes the telescope's sidereal time may differ from the site's (this
#: computer's clock): the calibration still solved at 2 min off and not at
#: 2.5 on the simulated telescope (docs/first_light.md, "The telescope's clock").
CLOCK_LIMIT = 1.0

#: Give up once this many calibration points went unsolved with none solved
#: (two pointing runs of three): the frames will not solve.
UNSOLVED_LIMIT = 6


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
    _one_telescope(devices)  # before anything is read from, or connected on, any of them
    config = discover.starter_config(devices, name, optics=True)
    scopes = config["telescopes"]
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
    # The solver accepts 4 matched stars by default; a sparse field can
    # solve falsely with so few: first light asks 8.
    config["images"]["solve"] = {
        "catalog": dict(catalog),
        "telescopes": {tel: {"min_matches": MIN_MATCHES}},
    }
    scopes[tel]["pointing"] = {
        "latitude": lat,
        "longitude": lon,
        "store": "pointing",
        "solver": {"plate_scale": optics["plate_scale"], "min_matches": MIN_MATCHES},
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
                # Three points carry the zero points only (docs/pointing_model.md).
                "pointing": {"min_points": 3, "terms": ["IH", "ID"]},
            },
            # The zenith is filled in when the run starts (``_zenith``).
            {"name": "first-light", "ra_hours": None, "dec_deg": lat, "exptime": exptime},
        ],
    }
    return config


def _one_telescope(devices: list[discover.Device]) -> discover.Device:
    found = [d for d in devices if d.type == "Telescope"]
    if len(found) != 1:
        raise FirstLightError(
            f"first light runs one telescope; {len(found)} answered"
            f" ({', '.join(d.spec for d in found) or 'none'})"
        )
    return found[0]


def telescope_site(telescope: discover.Device) -> tuple[float, float, float]:
    """(lat, lon, 0.0): the telescope's ``SiteLatitude`` and ``SiteLongitude``;
    its elevation is not read (0 m, as for ``--site LAT,LON``: a site's
    elevation barely changes the calibration, and a mount without it would
    need a second way). :class:`robofast.discover.OpticsUnavailable` naming
    the member it cannot report; :class:`FirstLightError` for a site out of
    range or never set."""
    lat, lon = discover.read_members(telescope, ["SiteLatitude", "SiteLongitude"])
    elev = 0.0
    what = f"Telescope {telescope.spec} ({telescope.name})"
    try:
        site = _checked(float(lat), float(lon), float(elev))
    except (TypeError, ValueError) as exc:
        raise FirstLightError(f"{what} reports its site as {lat!r}, {lon!r}: {exc}") from exc
    if site[0] == 0.0 and site[1] == 0.0:
        raise FirstLightError(f"{what} reports its site as 0, 0: its site is not set")
    return site


def _where(site: tuple[float, float, float]) -> str:
    lat, lon, elev = site
    lon = (lon + 180.0) % 360.0 - 180.0
    return (
        f"{abs(lat):.5f} {'N' if lat >= 0 else 'S'}, {abs(lon):.5f} {'E' if lon >= 0 else 'W'},"
        f" {elev:g} m"
    )


def _focus_reads(config: dict[str, Any], devices: list[discover.Device]) -> dict[str, Any]:
    """What the focus step reads from the devices (in the setup's session)."""
    (tel,) = config["telescopes"]
    scope = config["telescopes"][tel]
    by = {(d.type, d.spec): d for d in devices}
    focal, aperture, area = discover.read_members(
        by["Telescope", scope["telescope"]], ["FocalLength", "ApertureDiameter", "ApertureArea"]
    )
    (full,) = discover.read_members(by["Camera", scope["camera"]], ["MaxADU"])
    reads = {"focal": focal, "aperture": aperture, "area": area, "full": full}
    if "focuser" in scope:
        (reads["step"],) = discover.read_members(by["Focuser", scope["focuser"]], ["StepSize"])
    return reads


def _target(config: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in config["plan"]["targets"] if t["name"] == name)


def _focus_step(
    config: dict[str, Any],
    devices: list[discover.Device],
    site: tuple[float, float, float],
    when: datetime,
    *,
    attended: bool,
    reads: dict[str, Any],
) -> str:
    """Put the focus step first in the plan (robofast.first_focus): on the
    brightest planet or star up, from the devices' optics. Returns what it
    says about it (one line)."""
    from robofast import first_focus

    (tel,) = config["telescopes"]
    scope = config["telescopes"][tel]
    if "focuser" not in scope and not attended:
        return (
            "no focuser: no focus step (--yes answers no walk-through: run without --yes"
            " to focus by hand, the program saying which way to turn)"
        )
    target = first_focus.bright_target(site, when)
    if target is None:
        return "no planet or bright star is up to focus on: no focus step"
    focal, aperture, area, full = (reads[k] for k in ("focal", "aperture", "area", "full"))
    optics = config["images"]["telescopes"][tel]
    pixel = optics["plate_scale"] * float(focal) * 1000.0 / discover.ARCSEC_PER_UM_PER_MM
    obstruction = math.sqrt(max(0.0, 1.0 - float(area) / (math.pi * (float(aperture) / 2) ** 2)))
    settings: dict[str, Any] = {
        "f_ratio": float(focal) / float(aperture),
        "pixel_size": pixel,
        "plate_scale": optics["plate_scale"],
        "obstruction": round(obstruction, 3),
        "fwhm_px": 2.5 / optics["plate_scale"],
        "saturation": float(full),
        "exptime": 0.01,
        "sweep_exptime": 1.0,
    }
    if "focuser" in scope:
        settings["um_per_step"] = reads["step"]
        # The telescope's autofocus entry (docs/autofocus.md): what gives the
        # sequencer its focuser; its sweep finishes the focus step.
        scope["autofocus"] = {
            "optics": {
                k: settings[k] for k in ("f_ratio", "pixel_size", "obstruction", "um_per_step")
            }
            | {"pixel_scale": settings["plate_scale"]},
            "method": "sweep",
            "exptime": settings["sweep_exptime"],
            "measure": {"saturation": 0.9 * settings["saturation"]},
        }
    else:
        settings["walk_through"] = True  # a person is there (no --yes)
    config["plan"]["targets"].insert(
        0,
        {
            "name": "first-focus",
            "kind": "first_focus",
            "ra_hours": round(target.ra / 15.0, 6),
            "dec_deg": round(target.dec, 5),
            "exptime": settings["exptime"],
            "object": target.name,
            "autofocus": settings,
        },
    )
    config.setdefault("plugins", [])
    if "robofast.first_focus:register" not in config["plugins"]:
        config["plugins"].append("robofast.first_focus:register")
    first_focus.register()
    how = "with the focuser" if "focuser" in scope else "by hand: you turn the knob"
    return f"focus first, {how}, on {target.name} (altitude {target.alt:.0f} deg)"


def _zenith(config: dict[str, Any], lst_hours: float) -> None:
    for t in config["plan"]["targets"]:
        if t["name"] == "first-light":
            t["ra_hours"] = round(lst_hours % 24.0, 5)


def _lst_hours(site: tuple[float, float, float], when: datetime) -> float:
    """Local apparent sidereal time (hours) at ``site`` (lat, lon, elev), ``when``."""
    from robofast.sky import Site, offline_iers, sky_now

    offline_iers()
    return sky_now(Site(site[0], site[1], site[2]), when).lst_hours


def _fields(
    config: dict[str, Any], site: tuple[float, float, float], when: datetime, span: float = 0.0
):
    """(ra, dec) deg of the frames first light takes: the calibration's
    points as the pointing run lays them out (:func:`robofast.pointmodel.sky_grid`,
    from the site and this computer's clock) at ``when`` and every
    :data:`SKY_STEP` s for ``span`` s after it, and the zenith at ``when``."""
    from robofast import pointmodel
    from robofast.sequencer import POINTING_RUN_DEFAULTS

    (tel,) = config["telescopes"]
    pc = config["telescopes"][tel]["pointing"]
    cal = _target(config, "first-pointing")
    run = {**POINTING_RUN_DEFAULTS, **pc.get("run", {}), **cal.get("pointing", {})}
    lst = _lst_hours(site, when) * 15.0
    avoid = []
    if run["moon_distance"]:
        from robofast.sky import Site, sky_now

        moon = sky_now(Site(site[0], site[1], site[2]), when).moon
        avoid.append((float(moon.ra.deg), float(moon.dec.deg), float(run["moon_distance"])))
    grid = pointmodel.sky_grid(
        cal["count"],
        min_alt=run["min_alt"],
        max_alt=run["max_alt"],
        latitude=site[0],
        lst=lst,
        avoid=avoid,
        max_hour_angle=run["max_hour_angle"],
        min_dec=run["min_dec"],
        max_dec=run["max_dec"],
    )
    steps = int(span // SKY_STEP) + 1
    fields = [
        pointmodel.grid_radec(az, alt, site[0], lst + k * SKY_STEP * pointmodel.SIDEREAL_DEG_PER_S)
        for k in range(steps)
        for az, alt in grid
    ]
    return [*fields, (lst % 360.0, site[0])]


def _calibration_span(config: dict[str, Any]) -> float:
    """Seconds the calibration takes, at most (its points, :data:`PER_POINT`
    each besides the exposure)."""
    cal = _target(config, "first-pointing")
    return cal["count"] * (float(cal["exptime"]) + PER_POINT)


def _field_radius(plate_scale: float, size: tuple[int, int]) -> float:
    """The catalog cone (deg) the solver reads around a frame's centre
    (:func:`robofast.astrometry.solve_near`), with :data:`FIELD_MARGIN`."""
    from robofast.astrometry import SolverConfig

    c = SolverConfig(plate_scale=plate_scale)
    half_diag = 0.5 * (size[0] ** 2 + size[1] ** 2) ** 0.5 * plate_scale / 3600.0
    return half_diag * (1 + c.scale_tolerance) + c.search_radius + FIELD_MARGIN


def _missing(catalog: dict[str, Any], fields, radius: float) -> int:
    from robofast.catalog import catalog_from_config

    store = catalog_from_config(catalog).tiles
    if store is None or store.fetch is None:
        return 0
    return len({p for ra, dec in fields for p in store.missing(ra, dec, radius)})


def _fetch_catalog(
    catalog: dict[str, Any],
    fields,
    radius: float,
    timeout: float,
    said: float | None = None,
    frames: int | None = None,
) -> int:
    """Download the catalog tiles the solver will read for ``fields``
    before the telescope moves (a tile downloaded inside a solve holds the
    process until it arrives, past any timeout or Ctrl-C). Returns how many
    were fetched; :class:`FirstLightError` if they do not arrive within
    ``timeout`` s (``said``: the ``--timeout`` given, for the message), or
    cannot be downloaded."""
    from robofast.catalog import catalog_from_config

    store = catalog_from_config(catalog).tiles
    if store is None or store.fetch is None:
        return 0
    tiles = list(dict.fromkeys(p for ra, dec in fields for p in store.missing(ra, dec, radius)))
    if not tiles:
        return 0
    print(
        f"first light: fetching the catalog for its {frames or len(fields)} frames:"
        f" {len(tiles)} tiles"
        f" ({store.source.title}; cached in {store.directory})",
        flush=True,
    )
    done: list[int] = []
    failed: list[BaseException] = []

    def fetch() -> None:
        try:
            for p in tiles:
                store.tile(p)
                done.append(p)
        except Exception as exc:  # said, below
            failed.append(exc)

    # A daemon thread: a download still in flight at the deadline, or at
    # Ctrl-C, does not hold the process.
    worker = threading.Thread(target=fetch, name="catalog", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive() and time.monotonic() < deadline:
        worker.join(0.2)
    if failed:
        raise FirstLightError(f"the catalog: {failed[0]!r} ({len(done)} of {len(tiles)} tiles)")
    if worker.is_alive():
        raise FirstLightError(
            f"the catalog did not download within {said or timeout:g} s ({len(done)} of"
            f" {len(tiles)} tiles; the telescope did not move): run it again, or give"
            " a longer --timeout"
        )
    return len(tiles)


def _check_clock(telescope: discover.Device, site: tuple[float, float, float]) -> None:
    """The telescope's sidereal time against the site's from this computer's
    clock, before anything moves: a mount whose clock, date, time zone or
    longitude is wrong points elsewhere than it reports, and nothing solves.
    :class:`FirstLightError` past :data:`CLOCK_LIMIT` minutes."""
    (theirs,) = discover.read_members(telescope, ["SiderealTime"])
    ours = _lst_hours(site, datetime.now(UTC))
    off = ((float(theirs) - ours + 12.0) % 24.0 - 12.0) * 60.0  # minutes
    if abs(off) > CLOCK_LIMIT:
        raise FirstLightError(
            f"the telescope's clock disagrees with this computer's by {abs(off):.0f} min"
            f" (its sidereal time {_hms(float(theirs))}, the site's {_hms(ours)}): set the"
            " mount's date, time, time zone and longitude (or give --site with the"
            " right longitude), then run again"
        )


def _hms(hours: float) -> str:
    s = round((hours % 24.0) * 3600.0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _site(text: str) -> tuple[float, float, float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) not in (2, 3):
        raise ValueError("--site: LAT,LON or LAT,LON,ELEV (deg north, deg east, m)")
    try:
        return _checked(parts[0], parts[1], parts[2] if len(parts) == 3 else 0.0)
    except ValueError as exc:
        raise ValueError(f"--site: {text}: {exc}") from None


def _checked(lat: float, lon: float, elev: float) -> tuple[float, float, float]:
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        raise ValueError("latitude -90..90, longitude -180..360")
    return lat, lon, elev


def _first_light_frame(data_root: Path, tel: str) -> dict[str, Any] | None:
    """The first-light frame's index entry (docs/data.md), if written."""
    for index in sorted(data_root.glob(f"*/{tel}/n*/*.{tel}.frames.jsonl")):
        for line in index.read_text().splitlines():
            entry = json.loads(line)
            if entry.get("object") == "first-light":
                return entry
    return None


def _frames(tl: Timeline) -> str:
    """The calibration's frames so far: how many solved, the last errors."""
    solved = len(tl.events("pointing_point"))
    errors = [str(e.detail.get("error")) for e in tl.events("pointing_point_unsolved")]
    out = f"{solved} calibration frame(s) solved, {len(errors)} did not solve"
    return out + (f" (last: {'; '.join(errors[-2:])})" if errors else "")


async def _focus_failed(tl: Timeline) -> str:
    """The focus step's failure (robofast.first_focus), once it is reported."""
    while not tl.events("first_focus_failed"):
        await asyncio.sleep(0.5)
    return str(tl.events("first_focus_failed")[-1].detail.get("error"))


async def _unsolvable(tl: Timeline) -> None:
    """Return once the calibration's frames are found not to solve."""
    while not (
        len(tl.events("pointing_point_unsolved")) >= UNSOLVED_LIMIT
        and not tl.events("pointing_point")
    ):
        await asyncio.sleep(0.5)


def _waiting(sup: Any) -> str:
    """What the supervisor says, for a first light that did not finish."""
    st = sup.status()
    out = f"the supervisor: {st['reason']}; opening allowed: {st['open_allowed']}"
    if st.get("lease") is None:
        out += "; no lease granted"
        if sup.alarms:
            out += f" (its last alarm: {sup.alarms[-1]})"
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
    started: float | None = None,
) -> dict[str, Any]:
    """Run first light on ``config`` (already written in ``workdir``);
    return the first-light frame's index entry, with its solve. Raises
    :class:`FirstLightError` saying what stopped it; ``stop`` set (Ctrl-C),
    or this task cancelled (Ctrl-C where the loop has no signal handlers),
    stops the sequencer, whose cleanup parks, and raises
    :class:`FirstLightStopped` saying whether every telescope parked.
    ``timeout`` counts from ``started`` (:func:`time.monotonic`; default
    now)."""
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
        unsolvable = asyncio.create_task(_unsolvable(tl), name="unsolvable")
        unfocused = asyncio.create_task(_focus_failed(tl), name="unfocused")
        left = timeout - (time.monotonic() - started) if started is not None else timeout
        done, _ = await asyncio.wait({seq_task, stop_wait, unsolvable, unfocused},
                                     timeout=max(1.0, left),
                                     return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        stop_wait.cancel()
        unsolvable.cancel()
        unfocused.cancel()
        stopped = stop.is_set()
        if unfocused in done and not stopped:
            raise FirstLightError(f"focus: {unfocused.result()}")
        if unsolvable in done and not stopped:
            raise FirstLightError(
                f"the frames did not solve: {_frames(tl)}; check the plate scale"
                " (the telescope's FocalLength and the camera's pixel size), the focus,"
                " and that the sky is clear"
            )
        if not done:
            raise FirstLightError(f"not done after {timeout:g} s; {_frames(tl)}; {_waiting(sup)}")
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
    ap.add_argument(
        "--site",
        help="LAT,LON[,ELEV]: deg north, deg east, m (default: the telescope's own site)",
    )
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
    ap.add_argument(
        "--yes",
        action="store_true",
        help="answer yes to the question: the sky is clear and you stay with the telescope"
        " (no enclosure), and the telescope's site, as printed, is where it stands",
    )
    ap.add_argument("--timeout", type=float, default=1800.0, help="give up after (s)")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    started = time.monotonic()  # --timeout counts from here
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    site = None
    if args.site:
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
    # One connected session for the reads before anything moves: each device
    # is connected once (a connect can take seconds; some mounts home on it).
    with discover.session():
        try:
            if site is None:
                site = telescope_site(_one_telescope(devices))
                whose = (
                    "the telescope's (--site to change it); the elevation is not read from"
                    " it: 0 m unless --site gives one"
                )
            else:
                whose = "--site"
        except (FirstLightError, discover.OpticsUnavailable) as exc:
            print(f"first light: {exc}; give the site with --site LAT,LON", file=sys.stderr)
            sys.exit(1)
        print(f"site {site[0]:.5f}, {site[1]:.5f}, {site[2]:g} m: {whose}")
        try:
            _check_clock(_one_telescope(devices), site)
        except (FirstLightError, discover.OpticsUnavailable) as exc:
            print(f"first light: {exc}", file=sys.stderr)
            sys.exit(1)
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
        camera = next(
            d
            for d in devices
            if d.type == "Camera" and d.spec == config["telescopes"][tel]["camera"]
        )
        try:
            size = discover.read_members(camera, ["CameraXSize", "CameraYSize"])
            # A focus step can happen with a focuser, or a person (no --yes).
            focusable = "focuser" in config["telescopes"][tel] or not args.yes
            reads = _focus_reads(config, devices) if focusable else {}
        except discover.OpticsUnavailable as exc:
            print(f"first light: {exc}", file=sys.stderr)
            sys.exit(1)
    # One question: what robofast cannot see, and what the operator must
    # confirm. A mount with no GPS reports its factory site, which may be
    # another continent: points laid for it can be below the real horizon.
    asks = []
    if enclosure is None:
        asks.append(
            "No enclosure: robofast cannot see the weather. Is the sky clear, and will you"
            " stay with the telescope until this finishes?"
        )
    if args.site is None:
        asks.append(
            f"The site is the telescope's: {_where(site)}. Is this where the telescope stands?"
        )
    if asks and not args.yes:
        answer = input("\n".join(asks) + " [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("first light: not started", file=sys.stderr)
            sys.exit(1)
        started = time.monotonic()  # the time taken to answer is not counted
    try:
        radius = _field_radius(config["images"]["telescopes"][tel]["plate_scale"], size)
        catalog = config["images"]["solve"]["catalog"]
        frames = _target(config, "first-pointing")["count"] + 1
        # The sky turns while it downloads: fetch again for where the fields
        # are then, over the calibration and the last download's length,
        # until nothing is missing, or a pass no longer gains on the sky.
        took, fetched = 0.0, None
        for _ in range(FETCH_PASSES):
            begun = datetime.now(UTC)
            fields = _fields(config, site, begun, _calibration_span(config) + took)
            left = args.timeout - (time.monotonic() - started)
            n = _fetch_catalog(catalog, fields, radius, left, args.timeout, frames)
            if not n or (fetched is not None and n >= fetched):
                break
            fetched, took = n, (datetime.now(UTC) - begun).total_seconds()
        now = datetime.now(UTC)
        behind = _missing(catalog, _fields(config, site, now, _calibration_span(config)), radius)
        if behind:
            print(
                f"first light: the sky turns faster than the catalog downloads here: {behind}"
                " tiles download during the calibration (a stop then waits for the one"
                " downloading)",
                flush=True,
            )
    except (FirstLightError, discover.OpticsUnavailable) as exc:
        print(f"first light: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("first light: stopped while fetching the catalog; the telescope did not move",
              file=sys.stderr)  # fmt: skip
        sys.exit(1)
    # The zenith now, from the site and this computer's clock (the mount's
    # clock is not read: docs/first_light.md), after the download.
    planned = datetime.now(UTC)
    _zenith(config, _lst_hours(site, planned))
    try:
        print(
            "first light: "
            + _focus_step(config, devices, site, planned, attended=not args.yes, reads=reads)
        )
    except discover.OpticsUnavailable as exc:
        print(f"first light: {exc}", file=sys.stderr)
        sys.exit(1)
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
        return await run(
            config, workdir, args.operator, timeout=args.timeout, stop=stop, started=started
        )

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
    target_ra, target_dec = _target(config, "first-light")["ra_hours"] * 15.0, site[0]
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
