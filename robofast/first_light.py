"""First light: from a telescope plugged into this computer to a solved image.

::

    pip install .          # the repository or a wheel built from it (not on PyPI yet)
    robofast first-light --no-enclosure          # a telescope on a tripod
    robofast first-light --supervisor URL        # under an enclosure's supervisor

One command, for a classroom or a first night: it finds the telescope and
camera (Alpaca discovery, :mod:`robofast.discover`), reads their plate
scale from them, writes the config itself (``--dir``, default
``first-light/``; it never overwrites one), and runs robofast's sequencer
on it, in this process: a focus, from however far out, on the brightest
planet or star up (the autofocus's coarse mode, docs/autofocus.md), a
first pointing calibration from three plate solves (docs/pointing_model.md,
"A first calibration"), then one light frame near the zenith, plate-solved
(docs/sky.md). It prints where the frame points and exits 0 once it is
solved; 1 if robofast could not get there, saying why.

**The Sun** (:mod:`robofast.sun_gate`): first light is refused while the
Sun is above ``--max-sun-alt`` (default -12 deg, nautical twilight) and
stops when it rises to it; nothing it points at (the focus target, the
calibration's fields, the zenith) is within ``--min-sun-separation``
(default 30 deg) of the Sun. With an enclosure or without.

**The enclosure** is never guessed; it is said:

* ``--no-enclosure`` (a telescope on a tripod): the operator on duty
  judges the weather. First light runs the safety supervisor here, in this
  process, on the no-enclosure devices (docs/no_enclosure.md: the sky as a
  dome, the operator's duty as the safety monitor), with the person running
  it on duty. They are asked first whether the sky is clear and whether
  they will stay with the telescope; Ctrl-C, or the end, takes them off
  duty, and the telescope parks.
* ``--supervisor URL``: a permanent installation, whose enclosure the
  site's own supervisor service (``robofast-supervisor``) runs. First light
  starts no supervisor: it is a client of that one, as a sequencer is (it
  asks it for a lease, and the enclosure opens and closes only by its
  rules). It is refused if that supervisor does not answer, has a lease out
  to another sequencer, or has a critical alarm.

The site is the telescope's own (``SiteLatitude``, ``SiteLongitude``; a GPS
mount fills them; its elevation is not read: 0 m), or ``--site LAT,LON[,ELEV]``,
which is used instead. The telescope's site is shown and asked about (a
mount with no GPS may report its factory site, another continent); a
telescope that cannot report its site, or reports 0, 0, with no ``--site``,
stops before anything moves (docs/first_light.md, "The site").

The catalog is ``--catalog`` (default ``gaia``: Gaia DR3 tiles, docs/sky.md),
downloaded for the four frames before the telescope moves. ``--plugin`` loads plug-ins first (the
simulated telescope's ``synthetic`` sky: ``--plugin
robofast.testing.sky_device:register --catalog synthetic``).

First proposed and written by Guven Gurkan (PR #132).
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
from typing import Any, NoReturn

import httpx

from robofast import discover, sun_gate
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


#: Where the written config puts the no-enclosure devices (the sky and the
#: operator's duty) for ``robofast-no-enclosure-server``; first light itself
#: serves them on a free loopback port.
NO_ENCLOSURE_LISTEN = "127.0.0.1:11130"

#: The coarse focus's first exposure (s), on a planet or a bright star.
FOCUS_EXPTIME = 0.01
#: The focus sweep's exposure (s), on the field around it.
SWEEP_EXPTIME = 1.0
#: Seeing (FWHM, arcsec) the focus plans for.
SEEING = 2.5


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
    supervisor_url: str | None,
    catalog: dict[str, Any],
    plugins: list[str],
    exptime: float = EXPTIME,
    name: str = "first-light",
) -> dict[str, Any]:
    """The config first light runs on: the starter config of
    :func:`robofast.discover.starter_config` (with the optics read from the
    devices), and what the operator said (``site``; ``supervisor_url`` None:
    no enclosure, the no-enclosure devices at :data:`NO_ENCLOSURE_LISTEN`
    (docs/no_enclosure.md); else the site's running supervisor, which owns
    the enclosure: no devices of it are written here)."""
    _one_telescope(devices)  # before anything is read from, or connected on, any of them
    config = discover.starter_config(devices, name, optics=True)
    scopes = config["telescopes"]
    (tel,) = scopes
    if "camera" not in scopes[tel]:
        raise FirstLightError(f"{tel}: no camera paired with the telescope (see the device list)")
    lat, lon, elev = site
    optics = config["images"]["telescopes"][tel]
    config["_note"].append(
        "Written by robofast first light (robofast first-light); site and enclosure from"
        " its command line."
    )
    if supervisor_url is None:
        # No enclosure, as devices (docs/no_enclosure.md): the sky and the
        # operator's duty, served by robofast-no-enclosure-server.
        config["enclosure"] = "none"
        config["devices"] = {"dome": NO_ENCLOSURE_LISTEN, "safetymonitor": NO_ENCLOSURE_LISTEN}
        config["safety"] = {
            "attended_only": True,
            "park_before_close": True,
            "require_park_before_close": True,
            "reopen_hold": 0,
        }
    else:
        config["supervisor_url"] = supervisor_url
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


def _focus_reads(
    config: dict[str, Any], devices: list[discover.Device], f_ratio: float | None = None
) -> dict[str, Any]:
    """What the focus step reads from the devices (in the setup's session).
    ASCOM lets a mount leave out ApertureDiameter and ApertureArea, and a
    focuser StepSize: those are optional (no obstruction is said, in
    ``"said"``; the step size the focus step measures itself);
    the f-ratio the sweep needs is ``f_ratio`` (--f-ratio) if the aperture
    is not reported. A focuser that moves by steps (Absolute false) is not
    driven: it is taken out of the telescope's entry, and said."""
    (tel,) = config["telescopes"]
    scope = config["telescopes"][tel]
    by = {(d.type, d.spec): d for d in devices}
    mount = by["Telescope", scope["telescope"]]
    (focal,) = discover.read_members(mount, ["FocalLength"])
    aperture, area = _optional(mount, "ApertureDiameter"), _optional(mount, "ApertureArea")
    camera = by["Camera", scope["camera"]]
    (full,) = discover.read_members(camera, ["MaxADU"])
    said: list[str] = []
    reads = {"focal": focal, "aperture": aperture, "area": area, "full": full}
    # The camera's exposure limits (optional): never asked outside them.
    reads["emin"] = _optional(camera, "ExposureMin")
    reads["emax"] = _optional(camera, "ExposureMax")
    reads["f_ratio"], reads["said"] = f_ratio, said
    if "focuser" in scope:
        focuser = by["Focuser", scope["focuser"]]
        what = f"{focuser.type} {focuser.spec} ({focuser.name})"
        (absolute,) = discover.read_members(focuser, ["Absolute"])
        if absolute is not True:
            del scope["focuser"]
            said.append(
                f"{what} moves by steps, not to positions (Absolute {absolute}): not supported"
                " yet, so it is not driven; focus is by hand"
            )
        else:
            # Optional: the approach measures the star's size per step itself.
            reads["step"] = _optional(focuser, "StepSize")
            if aperture is None and f_ratio is None:
                raise discover.OpticsUnavailable(
                    f"Telescope {mount.spec} ({mount.name}) ApertureDiameter: not reported; the"
                    " focuser's sweep needs the f-ratio: give it with --f-ratio (the focal"
                    " length over the aperture)"
                )
    missing = "ApertureArea" if area is None else "ApertureDiameter" if aperture is None else ""
    if missing:
        said.append(
            f"Telescope {mount.spec} ({mount.name}) {missing}: not reported; no central"
            " obstruction assumed"
        )
    return reads


def _within(t: float, reads: dict[str, Any]) -> float:
    """``t`` s within the camera's ExposureMin and ExposureMax, where reported."""
    lo, hi = reads.get("emin"), reads.get("emax")
    t = max(t, float(lo)) if isinstance(lo, (int, float)) and lo > 0 else t
    return min(t, float(hi)) if isinstance(hi, (int, float)) and hi > 0 else t


def _optional(d: discover.Device, member: str) -> Any:
    """``member`` of ``d``, or None if it cannot report it."""
    try:
        (value,) = discover.read_members(d, [member])
    except discover.OpticsUnavailable:
        return None
    return value


def _target(config: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in config["plan"]["targets"] if t["name"] == name)


def _focus_step(
    config: dict[str, Any],
    site: tuple[float, float, float],
    when: datetime,
    *,
    reads: dict[str, Any],
    focus_on: str | None = None,
    min_sun_separation: float = sun_gate.MIN_SUN_SEPARATION,
) -> tuple[str, Any]:
    """Put the focus step first in the plan: an autofocus observation in its
    coarse mode (docs/autofocus.md, "Coarse mode") on the brightest planet
    or star up and away from the Sun (or ``focus_on``), and the telescope's
    autofocus entry from the devices' optics. Returns what it says about it
    (one line) and the target (None: no focus step)."""
    from robofast import autofocus_coarse as coarse

    (tel,) = config["telescopes"]
    scope = config["telescopes"][tel]
    if "focuser" not in scope:
        return (
            "no focuser to drive: no focus step (focus by hand first: docs/first_light.md,"
            ' "Focus")',
            None,
        )
    target = coarse.bright_target(site, when, focus_on, min_sun_separation=min_sun_separation)
    if target is None:
        return "no planet or bright star is up, away from the Sun: no focus step", None
    focal, aperture, area, full = (reads[k] for k in ("focal", "aperture", "area", "full"))
    optics = config["images"]["telescopes"][tel]
    pixel = optics["plate_scale"] * float(focal) * 1000.0 / discover.ARCSEC_PER_UM_PER_MM
    obstruction = 0.0
    if area is not None and aperture is not None:
        disk = math.pi * (float(aperture) / 2) ** 2
        obstruction = math.sqrt(max(0.0, 1.0 - float(area) / disk))
    f_ratio = reads.get("f_ratio") or float(focal) / float(aperture)
    # The telescope's autofocus entry (docs/autofocus.md): the fine sweep,
    # and the coarse mode the focus step asks for, from what the devices said.
    scope["autofocus"] = {
        "optics": {
            "f_ratio": round(f_ratio, 4),
            "pixel_size": round(pixel, 4),
            "pixel_scale": optics["plate_scale"],
            "obstruction": round(obstruction, 3),
            "um_per_step": float(reads.get("step") or 1.0),
        },
        "method": "sweep",
        "exptime": _within(SWEEP_EXPTIME, reads),
        "coarse_exptime": _within(FOCUS_EXPTIME, reads),
        "saturation": float(full),
        "max_seeing": SEEING,
        "measure": {"saturation": 0.9 * float(full)},
    }
    config["plan"]["targets"].insert(
        0,
        {
            "name": "first-focus",
            "kind": "autofocus",
            "ra_hours": round(target.ra / 15.0, 6),
            "dec_deg": round(target.dec, 5),
            "object": target.name,
            "autofocus": {"mode": "coarse"},
        },
    )
    return (
        f"focus first, with the focuser, on {target.name} (altitude {target.alt:.0f} deg)",
        target,
    )


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


async def _observation_failed(tl: Timeline) -> str:
    """The first observation that failed (the focus, the calibration), in
    its own words, once it is reported: first light stops there."""
    while not tl.events("observation_failed"):
        await asyncio.sleep(0.5)
    return str(tl.events("observation_failed")[-1].detail.get("error"))


def _parked_words(parked: bool, waited: float) -> str:
    """What the mount did, said once the sequencer's cleanup has returned."""
    if parked:
        return "the telescope parked"
    return (
        f"the telescope did not report parked after the sequencer's cleanup ({waited:.0f} s):"
        " park it by hand now"
    )


#: A device's role in the owner's name (``t1.focuser``), in plain words.
_ROLES = {"focuser": "the focuser", "telescope": "the mount", "camera": "the camera"}


def device_words(exc: Exception | str) -> str:
    """A device's error, its device first in plain words ("the focuser: ...")."""
    import re

    text = str(exc)
    m = re.search(r"\b(focuser|telescope|camera)\b", text.split(": ", 1)[0])
    return f"{_ROLES[m.group(1)]}: {text}" if m else text


async def _telescope_failed(tl: Timeline) -> str:
    """The sequencer's telescope error (a slew, a device), once it is
    reported, in the device's words (it then parks and backs off for
    minutes; first light does not wait for the back-off)."""
    while not tl.events("telescope_error"):
        await asyncio.sleep(0.5)
    return device_words(str(tl.events("telescope_error")[-1].detail.get("error")))


def _unsolved(tl: Timeline) -> bool:
    """Calibration frames taken, and none solved."""
    return bool(tl.events("pointing_point_unsolved")) and not tl.events("pointing_point")


def _not_solved(tl: Timeline) -> str:
    return (
        f"the frames did not solve: {_frames(tl)}; check the plate scale"
        " (the telescope's FocalLength and the camera's pixel size), the focus,"
        " and that the sky is clear"
    )


async def _unsolvable(tl: Timeline) -> None:
    """Return once the calibration's frames are found not to solve."""
    while not (
        len(tl.events("pointing_point_unsolved")) >= UNSOLVED_LIMIT
        and not tl.events("pointing_point")
    ):
        await asyncio.sleep(0.5)


async def _echo(tl: Timeline) -> None:
    """Print the focus as it goes: the coarse mode's lines (docs/autofocus.md)
    and where it ended."""
    seen = 0
    while True:
        events = [e for e in tl.events() if e.what in ("autofocus_coarse_step", "autofocus_done")]
        for e in events[seen:]:
            if e.what == "autofocus_coarse_step":
                print(e.detail.get("line"), flush=True)
            elif e.detail.get("success"):
                err = e.detail.get("focus_err") or 0
                print(f"focus: done at position {e.detail['focus']:.0f} (+-{err:.0f} steps)",
                      flush=True)  # fmt: skip
        seen = len(events)
        await asyncio.sleep(0.2)


async def _sun_rises(until: datetime | None) -> None:
    """Return once this computer's clock reaches ``until`` (the Sun at the
    gate's altitude: :func:`robofast.sun_gate.check_dark`); never if None."""
    while True:
        if until is not None:
            left = (until - datetime.now(UTC)).total_seconds()
            if left <= 0:
                return
        await asyncio.sleep(1.0 if until is None else min(5.0, max(0.05, left)))


def _waiting(st: dict[str, Any]) -> str:
    """What the supervisor says (its ``status()``), for a first light that
    did not finish."""
    out = f"the supervisor: {st.get('reason')}; opening allowed: {st.get('open_allowed')}"
    lease = st.get("lease")  # in process: its repr; over HTTP: its state
    if not (isinstance(lease, str) or _outstanding(st) is not None):
        out += "; no lease granted"
        if st.get("alarms"):
            out += f" (its last alarm: {st['alarms'][-1]})"
    duty = st.get("duty") or {}
    if duty.get("attended_only") and not duty.get("on_duty"):
        out += "; nobody is on duty there (the observer console's duty)"
    return out


class _Local:
    """No enclosure: the safety supervisor here, in this process, on the
    no-enclosure devices (docs/no_enclosure.md), with ``operator`` on duty
    while first light runs."""

    def __init__(self, config: dict[str, Any], workdir: Path, operator: str, tl: Timeline):
        self.config, self.workdir, self.operator, self.tl = config, workdir, operator, tl

    async def start(self) -> str:
        from robofast import supervisor_service
        from robofast.devices.no_enclosure_server import NoEnclosureConfig, NoEnclosureServer
        from robofast.modes import ModeStore

        holder: list[Any] = []

        def duty() -> tuple[bool, str]:
            who = holder[0].duty_status()["on_duty"] if holder else None
            return who is not None, f"operator {who} on duty" if who else "no operator on duty"

        self.sky = NoEnclosureServer(
            NoEnclosureConfig(
                name=str(self.config.get("name", "first-light")),
                listen="127.0.0.1:0",
                supervisor="(in process)",
                poll_interval=0.5,
                stale_after=5.0,
            ),
            duty=duty,
        ).start()
        here = {
            **self.config,
            "devices": {"dome": self.sky.address, "safetymonitor": self.sky.address},
        }
        self.sup = sup = supervisor_service.build(
            here, self.tl, mode_store=ModeStore(self.workdir / "mode.json")
        )
        holder.append(sup)
        self.server = await supervisor_service.SupervisorServer(sup, "127.0.0.1", 0).start()
        self.task = asyncio.create_task(sup.run(), name="supervisor")
        sup.duty_on(self.operator, "first light (robofast first-light)")
        self.sky.duty.poll()  # the safety monitor reads the duty now, not at its next poll

        async def keep_duty() -> None:
            while True:
                await asyncio.sleep(max(1.0, sup.config.duty_ttl / 3))
                sup.duty_renew(self.operator)

        self.duty = asyncio.create_task(keep_duty(), name="duty")
        return self.server.url

    def status(self) -> dict[str, Any]:
        return self.sup.status()

    async def refresh(self) -> None:
        pass

    async def parked(self) -> bool:
        return await self.sup._telescopes_parked()

    async def stop(self) -> None:
        self.duty.cancel()
        self.sup.duty_off(self.operator, "first light over")
        # The sky "closes" through the supervisor's own loop (the lease is
        # gone; park_before_close has parked the telescope).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.sup.config.shutter_timeout + 3 * self.sup.config.poll_interval
        while loop.time() < deadline and self.sup.outstanding() is not None:
            await asyncio.sleep(min(0.5, self.sup.config.poll_interval))
        self.task.cancel()
        with contextlib.suppress(BaseException):
            await self.task
        await self.server.stop()
        self.sky.stop()


def _outstanding(status: dict[str, Any]) -> dict[str, Any] | None:
    """The supervisor's lease (its ``GET /status``) that blocks a new one:
    granted and not released (an orphan, revoked but never released, too)."""
    lease = status.get("lease")
    if isinstance(lease, dict) and not lease.get("released"):
        return lease
    return None


class _Remote:
    """An enclosure: the site's own supervisor service at ``url``, which
    first light is a client of (it starts none). ``telescope``: the mount,
    read for AtPark at the end."""

    #: How long first light waits, at the end, for the enclosure to read closed.
    CLOSE_WAIT = 300.0

    def __init__(self, url: str, telescope: discover.Device):
        self.url, self.telescope = url.rstrip("/"), telescope
        self._last: dict[str, Any] = {}

    async def _get(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            r = await client.get(f"{self.url}/status")
            r.raise_for_status()
            self._last = r.json()
            return self._last

    async def start(self) -> str:
        try:
            st = await self._get()
        except (httpx.HTTPError, ValueError) as exc:
            raise FirstLightError(
                f"no supervisor answers at {self.url} ({exc!r}): at an enclosure, first light"
                " runs under the site's own supervisor service; start robofast-supervisor on"
                " the site's config, or give its address with --supervisor"
            ) from exc
        lease = _outstanding(st)
        if lease is not None:
            holder = lease.get("holder")
            raise FirstLightError(
                f"the supervisor at {self.url} has a lease out ({holder}): another sequencer"
                " is observing; stop it first"
            )
        duty = st.get("duty") or {}
        if duty.get("attended_only") and not duty.get("on_duty"):
            raise FirstLightError(
                f"the supervisor at {self.url} opens only with an operator on duty, and nobody"
                " is: go on duty at the observer console (docs/console.md), then run again"
            )
        if st.get("critical"):
            raise FirstLightError(
                f"the supervisor at {self.url} has a critical alarm"
                f" ({'; '.join(map(str, st.get('alarms', [])[-2:]))}): clear it at the observer"
                " console first"
            )
        return self.url

    def status(self) -> dict[str, Any]:
        return self._last

    async def refresh(self) -> None:
        with contextlib.suppress(httpx.HTTPError, ValueError):
            await self._get()

    async def parked(self) -> bool:
        try:
            (at,) = await asyncio.to_thread(discover.read_members, self.telescope, ["AtPark"])
        except discover.OpticsUnavailable:
            return False
        return at is True

    async def stop(self) -> None:
        # The site's supervisor closes the enclosure by its own rules once
        # the lease is released; say whether it reads closed.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CLOSE_WAIT
        while loop.time() < deadline:
            try:
                st = await self._get()
            except (httpx.HTTPError, ValueError):
                break
            if st.get("shutter") == "CLOSED" and _outstanding(st) is None:
                return
            await asyncio.sleep(2.0)


async def run(
    config: dict[str, Any],
    workdir: Path,
    operator: str,
    *,
    timeout: float = 1800.0,
    stop: asyncio.Event | None = None,
    started: float | None = None,
    dark_until: datetime | None = None,
    telescope: discover.Device | None = None,
    focus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run first light on ``config`` (already written in ``workdir``);
    return the first-light frame's index entry, with its solve. Raises
    :class:`FirstLightError` saying what stopped it; ``stop`` set (Ctrl-C),
    or this task cancelled (Ctrl-C where the loop has no signal handlers),
    stops the sequencer, whose cleanup parks, and raises
    :class:`FirstLightStopped` saying whether the telescope parked.
    ``timeout`` counts from ``started`` (:func:`time.monotonic`; default
    now); ``dark_until``: when the Sun reaches the gate's altitude, which
    stops it as a failure. With ``supervisor_url`` in the config (an
    enclosure) the site's supervisor is used (``telescope``: the mount, for
    AtPark); else one runs here on the no-enclosure devices."""
    from robofast import sequencer_service

    tl = Timeline(maxlen=20000)
    remote_url = config.get("supervisor_url")
    if remote_url:
        assert telescope is not None
        side: _Local | _Remote = _Remote(remote_url, telescope)
    else:
        side = _Local(config, workdir, operator, tl)
    url = await side.start()
    try:
        seq, remote = sequencer_service.build({**config, "supervisor_url": url}, tl, base=workdir)
    except BaseException:
        await side.stop()
        raise
    (tel,) = config["telescopes"]
    seq_task = asyncio.create_task(
        sequencer_service.run(seq, remote, handle_signals=False), name="sequencer"
    )
    stop = stop or asyncio.Event()
    stopped, parked, waited = False, False, 0.0
    failure: str | None = None  # why first light stops, said after the cleanup
    try:
        watchers = {
            "stop": asyncio.create_task(stop.wait(), name="stop"),
            "unsolvable": asyncio.create_task(_unsolvable(tl), name="unsolvable"),
            "failed": asyncio.create_task(_observation_failed(tl), name="failed"),
            "error": asyncio.create_task(_telescope_failed(tl), name="error"),
            "sun": asyncio.create_task(_sun_rises(dark_until), name="sun"),
        }
        echo = asyncio.create_task(_echo(tl), name="echo")
        left = timeout - (time.monotonic() - started) if started is not None else timeout
        done, _ = await asyncio.wait({seq_task, *watchers.values()},
                                     timeout=max(1.0, left),
                                     return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        for w in (*watchers.values(), echo):
            w.cancel()
        stopped = stop.is_set()
        if stopped:
            pass
        elif watchers["sun"] in done:
            failure = (
                "the Sun has reached the gate's altitude (--max-sun-alt): stopped for the dawn;"
                f" {_frames(tl)}"
            )
        elif watchers["failed"] in done and _unsolved(tl):
            failure = _not_solved(tl)
        elif watchers["failed"] in done:
            failure = _advice(watchers["failed"].result(), focus)
        elif watchers["error"] in done:
            failure = watchers["error"].result()
        elif watchers["unsolvable"] in done:
            failure = _not_solved(tl)
        elif not done:
            await side.refresh()
            failure = f"not done after {timeout:g} s; {_frames(tl)}; {_waiting(side.status())}"
    except asyncio.CancelledError:
        stopped = True  # Ctrl-C where the loop has no signal handlers (Windows)
    finally:
        cleanup = time.monotonic()
        if not seq_task.done():
            seq_task.cancel()  # its cleanup aborts and parks (it waits for AtPark)
            with contextlib.suppress(BaseException):
                await seq_task
        waited = time.monotonic() - cleanup
        # Every stop says what the mount did, read once the cleanup is over.
        if stopped or failure is not None:
            try:
                parked = await side.parked()
            except Exception:  # an unreadable mount: not known to be parked
                parked = False
        alarms = list(side.status().get("alarms") or [])
        await side.stop()
        closed = side.status().get("shutter") if remote_url else None
    if stopped:
        raise FirstLightStopped(parked, "; ".join(map(str, alarms[-2:])))
    if failure is not None:
        raise FirstLightError(f"{failure}; {_parked_words(parked, waited)}")
    if remote_url and closed != "CLOSED":
        raise FirstLightError(
            f"the site's supervisor did not read the enclosure closed within"
            f" {_Remote.CLOSE_WAIT:g} s of the end (shutter {closed}): check it now"
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
    entry["enclosure"] = None if closed is None else str(closed).lower()
    return entry


def _advice(error: str, focus: dict[str, Any] | None) -> str:
    """A failed observation's error, with what to do for a target too bright
    for the camera's shortest exposure (the coarse focus's): the brightest
    listed star up now faint enough (:func:`robofast.autofocus_coarse.fainter_star`).
    ``focus``: the focus target's ``mag`` and the ``stars_up`` (name, V mag)."""
    import re

    from robofast.autofocus_coarse import fainter_star

    m = re.search(
        r"shortest exposure \(([0-9.eE+-]+) s; focus needs about ([0-9.eE+-]+) s\)", error
    )
    if m is None or focus is None:
        return error
    star = fainter_star(
        [(str(n), float(v)) for n, v in focus.get("stars_up", [])],
        float(focus.get("mag", 0.0)),
        need=float(m.group(2)),
        shortest=float(m.group(1)),
    )
    if star is None:
        return (
            f"{error}: run again with --focus-on NAME, a fainter star up now (docs/first_light.md)"
        )
    return f"{error}: run again with --focus-on {star[0]} (V {star[1]:g}, up now)"


def _sun_targets(
    config: dict[str, Any], site: tuple[float, float, float], when: datetime
) -> list[tuple[str, float, float]]:
    """Where first light will point (name, RA, Dec deg): the calibration's
    fields over its span, and the zenith (the focus target is chosen away
    from the Sun: :func:`_focus_step`)."""
    fields = _fields(config, site, when, _calibration_span(config))
    *cal, zenith = fields
    return [("a calibration field", ra, dec) for ra, dec in cal] + [("the zenith", *zenith)]


def _fail(text: str) -> NoReturn:
    print(f"first light: {text}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="robofast first-light",
        description=__doc__.split("\n\n")[0],
        epilog="docs/first_light.md",
    )
    ap.add_argument(
        "--site",
        help="LAT,LON[,ELEV]: deg north, deg east, m (default: the telescope's own site)",
    )
    enc = ap.add_mutually_exclusive_group(required=True)
    enc.add_argument(
        "--no-enclosure",
        action="store_true",
        help="no dome or roof: the operator on duty judges the weather (docs/no_enclosure.md)",
    )
    enc.add_argument(
        "--supervisor",
        metavar="URL",
        help="an enclosure: the site's running supervisor service (robofast-supervisor),"
        " which first light asks for a lease",
    )
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
    ap.add_argument(
        "--focus-on",
        metavar="NAME",
        help="focus on this planet or bright star (docs/first_light.md) instead of the"
        " brightest up",
    )
    ap.add_argument(
        "--f-ratio",
        type=float,
        help="the telescope's focal ratio, for a mount that does not report its aperture",
    )
    ap.add_argument(
        "--max-sun-alt",
        type=float,
        default=sun_gate.MAX_SUN_ALT,
        help=f"refuse, and stop, with the Sun above this altitude (deg; default"
        f" {sun_gate.MAX_SUN_ALT:g}, nautical twilight; at most {sun_gate.MAX_SUN_ALT_CEILING:g})",
    )
    ap.add_argument(
        "--min-sun-separation",
        type=float,
        default=sun_gate.MIN_SUN_SEPARATION,
        help=f"point nowhere nearer the Sun than this (deg; default"
        f" {sun_gate.MIN_SUN_SEPARATION:g}; at least {sun_gate.MIN_SUN_SEPARATION_FLOOR:g})",
    )
    ap.add_argument("--timeout", type=float, default=1800.0, help="give up after (s)")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    started = time.monotonic()  # --timeout counts from here
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        sun_gate.check_limits(args.max_sun_alt, args.min_sun_separation)
    except ValueError as exc:
        ap.error(str(exc))
    site = None
    if args.site:
        try:
            site = _site(args.site)
        except ValueError as exc:
            ap.error(str(exc))
    supervisor_url = args.supervisor
    workdir = Path(args.dir)
    path = workdir / "observatory.json"
    if path.exists():
        ap.error(f"{path} exists: not overwritten (give another --dir)")
    from robofast.plugins import load_plugins

    load_plugins(args.plugin)
    servers = list(args.server) or discover.discover(args.address, args.discovery_port, 2.0)
    if not servers:
        _fail(f"no Alpaca server answered discovery at {args.address}")
    devices: list[discover.Device] = []
    for server in servers:  # as robofast-discover: a server with no device list is named
        try:
            devices += discover.configured_devices(server)
        except Exception as exc:  # as robofast-discover: named, and passed over
            print(f"first light: {server}: no device list ({exc})", file=sys.stderr)
    if not devices:
        _fail("no Alpaca device answered")
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
            _fail(f"{exc}; give the site with --site LAT,LON")
        print(f"site {site[0]:.5f}, {site[1]:.5f}, {site[2]:g} m: {whose}")
        # The Sun first: in daylight nothing else is asked or read.
        try:
            sun_gate.check_dark(site, datetime.now(UTC), args.max_sun_alt)
        except sun_gate.SunGateError as exc:
            _fail(f"{exc}; the telescope did not move")
        try:
            _check_clock(_one_telescope(devices), site)
        except (FirstLightError, discover.OpticsUnavailable) as exc:
            _fail(str(exc))
        catalog: dict[str, Any] = {"source": args.catalog}
        if args.catalog_dir:
            catalog["cache_dir"] = str(Path(args.catalog_dir).resolve())
        try:
            config = first_light_config(
                devices,
                site=site,
                supervisor_url=supervisor_url,
                catalog=catalog,
                plugins=args.plugin,
                exptime=args.exptime,
            )
        except (FirstLightError, discover.OpticsUnavailable) as exc:
            _fail(str(exc))
        (tel,) = config["telescopes"]
        print(f"plate scale {config['images']['telescopes'][tel]['plate_scale']} arcsec/pixel")
        camera = next(
            d
            for d in devices
            if d.type == "Camera" and d.spec == config["telescopes"][tel]["camera"]
        )
        try:
            size = discover.read_members(camera, ["CameraXSize", "CameraYSize"])
            reads = (
                _focus_reads(config, devices, args.f_ratio)
                if "focuser" in config["telescopes"][tel]
                else {}
            )
        except discover.OpticsUnavailable as exc:
            _fail(str(exc))
    for line in reads.get("said", []):
        print(f"first light: {line}", flush=True)
    from robofast import autofocus_coarse

    if args.focus_on:  # a name first light cannot use is refused before the question
        try:
            autofocus_coarse.bright_target(
                site,
                datetime.now(UTC),
                args.focus_on,
                min_sun_separation=args.min_sun_separation,
            )
        except autofocus_coarse.FocusError as exc:
            _fail(str(exc))
    # One question: what robofast cannot see, and what the operator must
    # confirm. A mount with no GPS reports its factory site, which may be
    # another continent: points laid for it can be below the real horizon.
    asks = []
    if supervisor_url is None:
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
            _fail("not started")
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
        _fail(str(exc))
    except KeyboardInterrupt:
        _fail("stopped while fetching the catalog; the telescope did not move")
    # The zenith now, from the site and this computer's clock (the mount's
    # clock is not read: docs/first_light.md), after the download.
    planned = datetime.now(UTC)
    _zenith(config, _lst_hours(site, planned))
    # The Sun again, now, for everything first light will point at: still
    # dark (the download took time), and nothing near it.
    try:
        dark_until = sun_gate.check_dark(site, planned, args.max_sun_alt)
        margin = _calibration_span(config) / 240.0  # the sky's turn over the calibration, deg
        sun_gate.check_targets(
            site, planned, _sun_targets(config, site, planned), args.min_sun_separation, margin
        )
        said, target = _focus_step(
            config,
            site,
            planned,
            reads=reads,
            focus_on=args.focus_on,
            min_sun_separation=args.min_sun_separation,
        )
    except (sun_gate.SunGateError, discover.OpticsUnavailable, autofocus_coarse.FocusError) as exc:
        _fail(f"{exc}; the telescope did not move")
    print(f"first light: {said}")
    focus = None
    if target is not None:
        focus = {
            "mag": target.mag,
            # The listed stars up now (name, V mag): a too-bright target's
            # stop names one of them for --focus-on.
            "stars_up": [
                (t.name, t.mag)
                for t in autofocus_coarse.targets_up(site, planned, args.min_sun_separation)
                if t.name != target.name and t.name not in dict(autofocus_coarse.PLANETS)
            ],
        }
    if dark_until is not None:
        print(
            f"first light: dark (the Sun below {args.max_sun_alt:g} deg) until"
            f" {dark_until:%H:%M} UTC; it stops then"
        )
    workdir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote {path}")
    if supervisor_url is not None:
        print(f"first light: under the site's supervisor at {supervisor_url}")
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
            config,
            workdir,
            args.operator,
            timeout=args.timeout,
            stop=stop,
            started=started,
            dark_until=dark_until,
            telescope=_one_telescope(devices),
            focus=focus,
        )

    try:
        entry = asyncio.run(go())
    except FirstLightError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:  # a second Ctrl-C, before the cleanup finished
        _fail("stopped before the cleanup finished; the telescope may NOT be parked: check it now")
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
