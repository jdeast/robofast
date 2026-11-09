"""The KiwiSpec spectrograph service: the one owner of the spectrograph.

This process is the only thing that talks to the spectrograph PC (its legacy
``spectrograph_server.py`` and its SI Image camera server, through
:mod:`robofast.devices.kiwispec_client`). Everything else -- the
sequencer, an operator tool -- uses its small HTTP/JSON API (design.md 3.1:
"one owning service, a small typed HTTP API, a simulator"). The wire
protocol, the hardware actions and the open questions are in
``docs/kiwispec.md``.

The API
-------
All bodies are JSON; errors are ``{"error": ...}`` with 400 (bad request),
404, 409 (busy, or refused by a check such as the CCD temperature) or 503
(the spectrograph PC did not answer).

* ``GET /status`` -- the last reading of every sensor, each with its age and
  any error, the current job, and ``alarms`` (``?refresh=1`` reads now,
  bounded).
* ``POST /configure`` ``{"iodine": "in"|"out"|"flat"|mm, "led": bool,
  "heater": bool, "heater_setpoint": C}`` (every key optional) -- waits
  (bounded) for the iodine stage to arrive and the slit-flat LED to warm up.
* ``POST /expose`` ``{"kind", "exptime", "object"?, "telescopes"?,
  "target_counts"?, "iodine"?}`` -- start one frame; 202 with the job. Kinds:
  ``science`` (``telescopes`` -- the ones feeding the fiber -- required;
  ``iodine`` default from config; terminated on the flux record
  (``flux_source``: the FAU guide frames, or the legacy exposure meter)
  when ``target_counts`` is given, ``exptime`` then being the maximum),
  ``thar``, ``slitflat``, ``fiberflat``, ``dark``, ``bias`` (``exptime`` 0).
  Only the kinds this spectrograph can take are accepted (``/status``
  ``frame_kinds``): ``thar`` and ``fiberflat`` need ``lamp_frames``, and
  ``slitflat`` needs ``slit_flat_led``. MINERVA's KiwiSpec has neither:
  it is iodine only (JDE 2026-10-31; docs/kiwispec.md, "Calibration").
* ``POST /calibrate`` ``{"frames": [{"kind", "exptime", "n"}, ...]}`` -- a
  calibration sequence as one job; 202.
* ``GET /jobs/{id}?wait=S`` -- a job's state; waits up to ``S`` s for it to
  finish. Each frame's result has the file name, the actual exposure time,
  what ended it (``time``, ``counts``, ``stop``), the flux record's counts,
  the **flux-weighted midpoint** (seconds after the start, UTC, JD; None
  without a flux record: then ``midpoint_source`` is ``geometric``), and,
  with ``raw_fits``, the raw file's rewrite (``fits``: checksums).
* ``POST /abort`` ``{"reason"}`` -- abort the running job: terminate the
  exposure and discard the frame; answers once the job has ended, or after
  ``abort_timeout`` (then ``"ended": false``; the job is cancelled and every
  lock released anyway). An abort that arrives once the shutter has closed
  lets that frame finish reading out and be saved, then ends the job.
  ``POST /stop`` ends the exposure early and keeps the frame.
* ``POST /backlight`` ``{"on": bool}`` -- the FAU fiber-acquisition backlight
  (an LED disk on an arm that swings in over the fiber; with the legacy
  exposure meter in use, the meter is paused while it is on; the server
  itself turns the backlight off after 60 s).
* ``POST /flux`` ``{"telescope", "frames": [[t_start, t_end, counts], ...]}``
  -- FAU guide-frame flux from a guider (Unix seconds; counts inside the
  fiber's footprint): the flux record of ``flux_source: "fau"``
  (:mod:`robofast.devices.kiwispec_flux`).
* ``POST /restart_camera`` ``{"reason"?}`` -- kill SI Image and start it
  again at once (its CCD cooler stops while it is down); also done by the
  service itself after ``camera_restart_after`` camera failures in a row.

One job at a time. ``configure``, ``expose``, ``calibrate`` and
``backlight`` take an optional ``"wait": S`` and ``"client": name``: while
the spectrograph is busy, the request waits in a first-come, first-served
queue for up to ``S`` s (at most ``max_queue_wait``; at most ``max_queue``
waiting), then gets it -- or answers 409 with ``"busy": true``. Without
``wait`` a busy spectrograph answers 409 at once. ``/status`` lists the
queue. This is how two enclosures' sequencers share the one spectrograph
(docs/kiwispec.md, "Sharing the spectrograph"). Every wait is bounded;
locks are released in ``finally``.

``expose`` and ``calibrate`` also take ``"night": "nYYYYMMDD"``: the night
the raw files are named for (the sequencer's, from its clock). Without
it, the night of this service's clock by the config's ``site`` night rule
(``{"timezone": "America/Phoenix", "night_rollover_hour": 12}``,
:class:`robofast.clock.NightRule`, the rule every night name comes from);
never the bare UT date, which would split a MINERVA night at 17:00 MST.

Safety
------
The spectrograph PC must be a verified simulator (loopback, and the ports
registered by a robofast simulator: :mod:`robofast.hwgate`; an SSH
tunnel to the real PC is loopback too) unless the config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``.

Running
-------
::

    python -m robofast.devices.kiwispec_service --config kiwispec.json [--hardware]

with a JSON config (every key optional except the two hosts and
``telescopes``)::

    {"name": "kiwispec", "listen": "127.0.0.1:8130",
     "spectrograph": {"host": "127.0.0.1", "port": 44445},
     "camera": {"host": "127.0.0.1", "port": 2055},
     "telescopes": ["T1", "T2", "T3", "T4"]}

It prints one JSON line, ``{"url": ...}``, once listening.

The sequencer's cleanup aborts a spectrum through :class:`KiwiSpecRemote`
(``await KiwiSpecRemote(url).abort("weather unsafe")``), which never raises.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import itertools
import json
import logging
import math
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast.cancel import isolated, stop_task
from robofast.clock import NightRule, clock_from_config
from robofast.config import load_device_config, require_keys
from robofast.devices.kiwispec_aux import (
    AuxChiller,
    AuxConfig,
    AuxDevices,
    AuxRouted,
    Chiller,
    build_chiller,
)
from robofast.devices.kiwispec_client import (
    CAMERA_PORT,
    SPEC_PORT,
    HardwareNotAllowed,
    KiwiSpecBusy,
    KiwiSpecError,
    KiwiSpecFailed,
    KiwiSpecTimeout,
    KiwiSpecUnreachable,
    SIAcquisition,
    SICameraClient,
    SpecServerClient,
    StagePosition,
    si_temp_raw,
)
from robofast.devices.kiwispec_fits import FitsRewriteError, standardize
from robofast.devices.kiwispec_flux import FluxLog, fau_flux, parse_frames

log = logging.getLogger(__name__)

#: Frame kinds: (SI dark frame?, iodine position or None for the request's,
#: slit-flat LED on?, uses the exposure meter?, needs telescopes?). ``thar``
#: and ``fiberflat`` switch no lamp (legacy's lamp calls were commented out,
#: and the server does not implement them): they only set the stage and
#: expose, for a spectrograph whose lamps are lit some other way
#: (``lamp_frames``). **Unavailable at MINERVA**: KiwiSpec has no ThAr or
#: fiber-flat lamp and no way to switch to one (JDE 2026-10-31).
KINDS: dict[str, tuple[bool, str | None, bool, bool, bool]] = {
    # legacy control.spec_equipment_check, per objname
    "science": (False, None, False, True, True),
    "thar": (False, "out", False, False, False),
    "slitflat": (False, "flat", True, False, False),
    "fiberflat": (False, None, False, False, False),
    "dark": (True, "in", False, False, False),
    "bias": (True, "in", False, False, False),
}
#: Where a science frame's flux record comes from (``flux_source``): the FAU
#: guide frames the guiders report (``POST /flux``), the legacy exposure
#: meter (a PMT; not used at MINERVA), or nothing (time-terminated, and the
#: barycentric correction uses the geometric midpoint).
FLUX_SOURCES = ("fau", "expmeter", "none")
#: Kinds that need a calibration lamp the service does not control.
LAMP_KINDS = ("thar", "fiberflat")
#: The iodine stage's named positions (the server's ``[I2POSITIONS]`` names).
STAGE_NAMES = ("in", "flat", "out")
#: Legacy object names in file names (``n20261031.<object>.0001.fits``).
DEFAULT_OBJECT = {
    "thar": "ThAr",
    "slitflat": "slitFlat",
    "fiberflat": "fiberFlat",
    "dark": "Dark",
    "bias": "Bias",
}
_OBJECT = re.compile(r"^[A-Za-z0-9_+-]{1,60}$")
_NIGHT = re.compile(r"^n\d{8}$")
_CLIENT = re.compile(r"^[A-Za-z0-9_.:/+-]{0,80}$")
JD_UNIX_EPOCH = 2440587.5


# -- expmeter math ------------------------------------------------------------------


def flux_weighted_midpoint(
    samples: list[tuple[float, float]], start: float, end: float
) -> tuple[float | None, float]:
    """The flux-weighted mean time of an exposure from exposure-meter samples.

    ``samples`` are ``(t, cumulative counts)`` in increasing ``t``; the counts
    between two samples are taken to arrive uniformly in between (exact for a
    rate that is constant between samples), and only the part inside
    ``[start, end]`` counts. Returns ``(midpoint, counts)``; the midpoint is
    ``None`` when no counts fell in the window.
    """
    weighted = total = 0.0
    for (t0, c0), (t1, c1) in itertools.pairwise(samples):
        dc = c1 - c0
        if t1 <= t0 or dc <= 0:
            continue
        a, b = max(t0, start), min(t1, end)
        if b <= a:
            continue
        part = dc * (b - a) / (t1 - t0)
        weighted += part * (a + b) / 2
        total += part
    return (weighted / total if total > 0 else None), total


def queue_args(args: dict[str, Any]) -> tuple[float, str]:
    """A request's ``wait`` (s) and ``client`` (module doc)."""
    wait = _num(args.get("wait", 0.0), "wait")
    if wait < 0:
        raise ValueError("wait must be >= 0")
    client = str(args.get("client") or "")
    if not _CLIENT.match(client):
        raise ValueError(f"client {client!r} must match {_CLIENT.pattern}")
    return wait, client


def jd(t: datetime.datetime) -> float:
    return t.timestamp() / 86400.0 + JD_UNIX_EPOCH


def night(t: datetime.datetime | None = None, rule: NightRule | None = None) -> str:
    """The night ``t`` (default: now) belongs to, by ``rule`` (default:
    UTC, noon rollover; :class:`robofast.clock.NightRule`)."""
    return (rule or NightRule()).name(t or datetime.datetime.now(datetime.UTC))


# -- config -------------------------------------------------------------------------


@dataclass
class SpecEndpoint:
    host: str
    port: int = SPEC_PORT
    connect_timeout: float = 3.0
    send_timeout: float = 5.0
    max_attempts: int = 2
    #: Per-command reply timeouts (s), overriding the client's defaults.
    reply_timeouts: dict[str, float] = field(default_factory=dict)
    #: A cap on every reply timeout (tests; a slow site may raise others).
    max_reply_timeout: float | None = None


@dataclass
class CameraEndpoint:
    host: str
    port: int = CAMERA_PORT
    connect_timeout: float = 5.0
    command_timeout: float = 10.0


@dataclass
class KiwiSpecConfig:
    spectrograph: SpecEndpoint
    camera: CameraEndpoint
    #: Telescopes that can feed the fiber (legacy tele_list): a site value,
    #: required.
    telescopes: list[str]
    name: str = "kiwispec"
    listen: str = "127.0.0.1:8130"
    #: Must be true (and ``--hardware`` given) for a PC that is not a verified simulator.
    allow_hardware: bool = False
    #: The iodine stage's named positions (mm), as a CROSS-CHECK only. The
    #: live ``spectrograph_server.ini`` on the spectrograph PC is
    #: authoritative (docs/kiwispec.md, "The iodine stage's positions"): the
    #: service moves by name, the server resolves the name, and the service
    #: learns the millimetres from the server's own replies (a stopped stage
    #: the server names as the position asked for has arrived). A configured
    #: value that disagrees with the live server raises an alarm. Default:
    #: none (learn them).
    stage_positions: dict[str, float] = field(default_factory=dict)
    #: Arrived: within this of the target (mm) and stopped (two consecutive
    #: polls agree to a tenth of it).
    stage_tolerance: float = 0.1
    stage_timeout: float = 60.0  # legacy ctrl_i2stage_move
    stage_poll: float = 1.0
    default_iodine: str = "in"
    heater_setpoint: float = 55.0  # legacy I2SETTEMP
    heater_tolerance: float = 0.501  # legacy I2TEMPTOL
    heater_wait: float = 10.0  # legacy: wait this long, then go on
    #: The slit-flat LED works: slit flats (``slitflat``) and ``configure``'s
    #: ``led: true`` are allowed. Off by default: legacy stopped slit flats in
    #: 2023 ("Slit flat lamp is dead, skipping slit flats", control.specCalib).
    slit_flat_led: bool = False
    #: The FAU backlight works. False: the service never sends a backlight
    #: command (frames skip "backlight off", ``POST /backlight`` is refused);
    #: the guiders use their configured fiber positions instead. MINERVA's
    #: servo stopped answering in 2026-11 and the legacy server then answers
    #: ``backlight_off`` with ``fail``, which would fail every frame.
    backlight: bool = True
    #: Lamp frames (``thar``, ``fiberflat``) this spectrograph can take; the
    #: service switches no lamp. Default none: MINERVA's KiwiSpec is iodine
    #: only ("There are no ThAr lamps or a way to robotically switch over.
    #: It's all iodine." JDE 2026-10-31).
    lamp_frames: list[str] = field(default_factory=list)
    #: Seconds the slit-flat LED must have been on before a slit flat.
    led_warmup: float = 0.0  # legacy WARMUPMINUTES = 0
    readout_time: float = 22.0  # legacy ro_time
    #: Added to every exposure's bound (exposure + readout + this).
    exposure_margin: float = 60.0
    max_exptime: float = 3600.0
    #: The flux record for the flux-weighted midpoint and count termination
    #: (:data:`FLUX_SOURCES`). ``"fau"``: the FAU guide frames the guiders
    #: report (``POST /flux``; :mod:`robofast.devices.kiwispec_flux`).
    #: ``"expmeter"``: the legacy exposure meter (PMT), which MINERVA does
    #: not use; only then is the meter reset, read or paused. ``"none"``:
    #: every exposure runs its fixed time. Without a flux record the
    #: spectrum's midpoint is the geometric one.
    flux_source: str = "fau"
    #: FAU flux: gaps between guide frames up to this (s) are bridged; a
    #: midpoint needs some telescope's frames to cover at least this
    #: fraction of the exposure; frames are kept this long (s).
    fau_max_gap: float = 10.0
    fau_min_coverage: float = 0.5
    fau_max_age: float = 4 * 3600.0
    expmeter_poll: float = 1.0
    #: The meter's own reading period (legacy: 1 reading/s).
    expmeter_period: float = 1.0
    expmeter_max_failures: int = 3
    #: Refuse an exposure with the CCD outside these (C); ccd_temp_max above
    #: ccd_setpoint.
    ccd_temp_min: float = -95.0
    ccd_temp_max: float = -78.0
    check_ccd_temp: bool = True
    #: Spectrograph pressure threshold (mbar; legacy 0.006), and whether a
    #: pressure above it is an ``"alarm"`` (``/status`` ``alarms``: the
    #: sequencer marks the spectrograph down) or only a ``"warning"``
    #: (``/status`` ``warnings``), e.g. while the spectrograph is isolated
    #: from its pump.
    max_spec_pressure: float = 0.006
    pressure_alarm: str = "alarm"
    #: The spectrograph PC's local time, hours from UTC (MST, no daylight
    #: saving: -7). SI Image stamps DATE-OBS in it.
    pc_utc_offset_hours: float = -7.0
    #: The raw file, as this host sees the spectrograph PC's data directory
    #: (a mount): ``{"dir": path, "rewrite": true, "wait": s}``. The file is
    #: ``<dir>/<night>/<file>``. With ``rewrite`` (default) each frame is made
    #: legal FITS in place, verified, after its header is written
    #: (:mod:`robofast.devices.kiwispec_fits`); ``wait`` bounds how long
    #: the file may take to appear. None: the raw file is not visible here
    #: and is left as the camera wrote it.
    raw_fits: dict[str, Any] | None = None
    #: SI Image restarts (docs/kiwispec.md, "Restarting SI Image"): after
    #: this many camera failures in a row the service kills and at once
    #: restarts the camera software (its CCD cooler stops while it is
    #: down), bounded by ``camera_restart_timeout``. False: never by itself
    #: (``POST /restart_camera`` still works).
    camera_restart: bool = True
    camera_restart_after: int = 2
    camera_restart_timeout: float = 60.0
    #: The spectrograph's chiller (telemetry only): None, ``{"kind": "sim",
    #: ...}`` or a driver's kind (:mod:`robofast.devices.kiwispec_aux`).
    chiller: dict[str, Any] | None = None
    #: The aux devices' Alpaca server (heater, iodine stage, backlight,
    #: pressures, chiller) instead of the legacy server for those devices:
    #: ``{"address": "127.0.0.1:11170", ...}``
    #: (:class:`~robofast.devices.kiwispec_aux.AuxConfig`). None: all on
    #: the legacy server.
    aux: dict[str, Any] | None = None
    abort_timeout: float = 30.0
    #: How long a finished frame may wait for the spectrograph server to come
    #: back (to move the raw frame into place).
    save_timeout: float = 60.0
    save_retry: float = 2.0
    status_interval: float = 10.0
    status_timeout: float = 15.0
    #: The claim queue (module doc): the longest a request may wait for the
    #: spectrograph, and how many may wait at once.
    max_queue_wait: float = 1800.0
    max_queue: int = 4
    #: Simulation only: the clock exposure start times and the default night
    #: label come from (:func:`robofast.clock.clock_from_config`, e.g.
    #: ``{"file": "clock.json"}``); default the host's clock.
    clock: dict[str, Any] | None = None
    #: The night rule for a request that names no night (module doc): the
    #: site's ``timezone``, ``night_rollover_hour`` (and ``longitude``);
    #: without it UTC with a noon rollover (logged at start).
    site: dict[str, Any] | None = None
    #: The SI camera set-up legacy sent at start (initialize_si).
    readout_mode: int = 3
    ccd_format: tuple[int, int, int, int, int, int] = (50, 2090, 1, 0, 2052, 1)
    #: The CCD's cooling setpoint and SI's "Operational Temp" (C, its warning
    #: and popup threshold), sent at every connection with the readout set-up
    #: (SI Configuration parameters, into the camera's RAM; the EEPROM keeps
    #: -90 and -85, and is never written). -80 since 2026-11-05: with the
    #: chiller at 21 C the CCD holds about -80.5 at full cooling (docs/kiwispec.md).
    ccd_setpoint: float = -80.0
    ccd_operational_temp: float = -75.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KiwiSpecConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}  # "_note" etc.
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown kiwispec config keys: {sorted(unknown)}")
        require_keys(d, ("spectrograph", "camera", "telescopes"))
        d["spectrograph"] = SpecEndpoint(**d["spectrograph"])
        d["camera"] = CameraEndpoint(**d["camera"])
        if "ccd_format" in d:
            d["ccd_format"] = tuple(d["ccd_format"])
        if d.get("stage_positions") is None:
            d.pop("stage_positions", None)
        lamps = d.get("lamp_frames") or []
        if not isinstance(lamps, list) or set(lamps) - set(LAMP_KINDS):
            raise ValueError(f"lamp_frames: a list of {list(LAMP_KINDS)}, not {lamps!r}")
        if not isinstance(d.get("slit_flat_led", False), bool):
            raise ValueError("slit_flat_led must be true or false")
        if not isinstance(d.get("backlight", True), bool):
            raise ValueError("backlight must be true or false")
        if d.get("flux_source", "fau") not in FLUX_SOURCES:
            raise ValueError(f"flux_source: one of {list(FLUX_SOURCES)}, not {d['flux_source']!r}")
        if d.get("pressure_alarm", "alarm") not in ("alarm", "warning"):
            raise ValueError(f"pressure_alarm: alarm or warning, not {d['pressure_alarm']!r}")
        raw = d.get("raw_fits")
        if raw is not None:
            if not isinstance(raw, dict) or not raw.get("dir"):
                raise ValueError("raw_fits: {'dir': path, 'rewrite'?: bool, 'wait'?: s}")
            extra = set(raw) - {"dir", "rewrite", "wait"}
            if extra:
                raise ValueError(f"raw_fits: unknown keys {sorted(extra)}")
        setpoint = float(d.get("ccd_setpoint", -80.0))
        if not setpoint < float(d.get("ccd_operational_temp", -75.0)):
            raise ValueError("ccd_operational_temp must be above ccd_setpoint")
        if not float(d.get("ccd_temp_min", -95.0)) < setpoint < float(d.get("ccd_temp_max", -78.0)):
            raise ValueError("ccd_setpoint must be inside ccd_temp_min..ccd_temp_max")
        if d.get("chiller") is not None:
            build_chiller(d["chiller"])  # fail at load, not at the first read
        if d.get("aux") is not None:
            aux = AuxConfig.from_dict(d["aux"])
            if aux.chiller and d.get("chiller") is not None:
                raise ValueError("chiller: configure it either here or as aux.chiller, not both")
        return cls(**d)

    def frame_kinds(self) -> list[str]:
        """The frame kinds this spectrograph can take (module doc)."""
        return [
            k
            for k in KINDS
            if (k not in LAMP_KINDS or k in self.lamp_frames)
            and (k != "slitflat" or self.slit_flat_led)
        ]

    def unavailable(self, kind: str) -> str | None:
        """Why this spectrograph cannot take ``kind`` frames (None: it can)."""
        if kind in LAMP_KINDS and kind not in self.lamp_frames:
            lamp = "ThAr" if kind == "thar" else "fiber-flat"
            return (
                f"{kind} frames are not available: this spectrograph has no {lamp} lamp"
                " (lamp_frames); MINERVA's KiwiSpec is iodine only (docs/kiwispec.md)"
            )
        if kind == "slitflat" and not self.slit_flat_led:
            return (
                "slitflat frames are not available: the slit-flat LED is not in use"
                " (slit_flat_led false; legacy stopped slit flats in 2023)"
            )
        return None


# -- jobs -----------------------------------------------------------------------------


class Refused(Exception):
    """A request refused before anything moved (409)."""


class Busy(Refused):
    """The spectrograph is held by another request (409, ``"busy": true``)."""


class Aborted(Exception):
    pass


@dataclass
class Frame:
    kind: str
    exptime: float
    object: str
    telescopes: list[str] = field(default_factory=list)
    target_counts: float | None = None
    iodine: str | float | None = None
    #: The night the raw file is named for (None: this service's clock).
    night: str | None = None


@dataclass
class Job:
    id: int
    kind: str  # "expose" or "calibrate"
    frames: list[Frame]
    state: str = "queued"  # queued, running, done, failed, aborted
    phase: str = ""
    error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    ended: float | None = None
    abort_reason: str | None = None
    #: Who asked for it (the request's ``client``).
    client: str = ""
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "client": self.client,
            "state": self.state,
            "phase": self.phase,
            "error": self.error,
            "frames": [dataclasses.asdict(f) for f in self.frames],
            "results": self.results,
            "abort_reason": self.abort_reason,
            "created": self.created,
            "ended": self.ended,
        }


def _num(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, not {value!r}") from None
    if not math.isfinite(x):
        raise ValueError(f"{what} must be finite")
    return x


class KiwiSpec:
    """The spectrograph and its one job at a time."""

    def __init__(
        self,
        config: KiwiSpecConfig,
        spec: SpecServerClient,
        camera: SICameraClient,
    ) -> None:
        self.config = config
        self.spec = spec
        self.camera = camera
        #: What holds the spectrograph now (a job, configure, backlight).
        self._held: str | None = None
        #: Requests waiting for it, first come first served: [future, what,
        #: client, time.time() when queued].
        self._waiters: deque[list[Any]] = deque()
        self.clock = clock_from_config(config.clock)
        self.night_rule = NightRule.from_site(config.site)
        if config.site is None:
            log.warning(
                "no 'site' night rule: a request without a night is named by UTC"
                " (robofast/clock.py; set site.timezone)"
            )
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}
        self.current: Job | None = None
        self._task: asyncio.Task | None = None
        self._abort = asyncio.Event()
        self._stop = asyncio.Event()
        self._acquisition: SIAcquisition | None = None
        self._camera_ready = False
        self.led_on_since: float | None = None
        self.led_on: bool | None = None
        self.backlight: bool | None = None
        self.last_stage_request: str | None = None
        #: The stage's named positions as the live server reports them (mm).
        self.stage_learned: dict[str, float] = {}
        self.heater_on: bool | None = None
        self.readings: dict[str, dict[str, Any]] = {}
        self.faults: list[str] = []
        self._status_task: asyncio.Task | None = None
        #: FAU guide-frame flux, per telescope (``POST /flux``).
        self.flux = FluxLog(max_age=config.fau_max_age)
        self.chiller: Chiller | None = build_chiller(config.chiller)
        #: Camera failures since the last good frame, and SI Image restarts.
        self.camera_failures = 0
        self.camera_restarts: list[dict[str, Any]] = []

    # -- status -------------------------------------------------------------------

    def _record(self, key: str, value: Any = None, error: str | None = None) -> None:
        r = self.readings.setdefault(key, {})
        r["at"] = time.time()
        if error is None:
            r["value"], r["error"], r["ok_at"] = value, None, r["at"]
        else:
            r["error"] = error

    async def _read(self, key: str, coro: Any) -> Any:
        try:
            value = await coro
        except (KiwiSpecError, TimeoutError) as exc:
            self._record(key, error=f"{type(exc).__name__}: {exc}")
            return None
        if isinstance(value, StagePosition):
            value = {"mm": value.mm, "name": value.name}
        self._record(key, value)
        return value

    async def refresh(self) -> None:
        """Read every sensor once (each read bounded)."""
        if self._acquisition is not None and not self._acquisition.finished:
            return  # keep the wire free for the exposure meter
        try:
            async with asyncio.timeout(self.config.status_timeout):
                await self._read("heater_temp", self.spec.cell_heater_temp())
                await self._read("heater_setpoint", self.spec.cell_heater_get_set_temp())
                await self._read("stage", self.spec.stage_position())
                await self._read("spec_pressure", self.spec.spec_pressure())
                await self._read("pump_pressure", self.spec.pump_pressure())
                if self.config.flux_source == "expmeter":
                    await self._read("expmeter_total", self.spec.expmeter_total())
                await self._read("ccd_temp", self.camera.ccd_temperature())
                if self.chiller is not None:
                    await self._read_chiller()
        except TimeoutError:
            self.faults.append("status refresh timed out")

    async def _read_chiller(self) -> None:
        assert self.chiller is not None
        try:
            async with asyncio.timeout(self.config.status_timeout):
                self._record("chiller", await self.chiller.read())
        except Exception as exc:  # a driver's error is telemetry, not a crash
            self._record("chiller", error=f"{type(exc).__name__}: {exc}")

    async def _status_loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.refresh()
            await asyncio.sleep(self.config.status_interval)

    def start_status_loop(self) -> None:
        self._status_task = asyncio.get_running_loop().create_task(self._status_loop())

    async def close(self) -> None:
        if self._status_task is not None:
            self._status_task.cancel()
        if self._task is not None and not self._task.done():
            await self.abort("service stopping")
        await self.camera.close()

    #: Readings whose errors are warnings, not alarms (telemetry only).
    WARNING_READINGS = ("chiller", "pump_pressure")

    def _checks(self) -> tuple[list[str], list[str]]:
        """``(alarms, warnings)`` from the last readings and the faults."""
        c, alarms, warns = self.config, [], []
        get = self.readings.get
        ccd = (get("ccd_temp") or {}).get("value")
        if ccd is not None and not c.ccd_temp_min <= ccd <= c.ccd_temp_max:
            alarms.append(f"CCD temperature {ccd} C outside {c.ccd_temp_min}..{c.ccd_temp_max}")
        p = (get("spec_pressure") or {}).get("value")
        if p is not None and p > c.max_spec_pressure:
            msg = f"spectrograph pressure {p} mbar > {c.max_spec_pressure}"
            (alarms if c.pressure_alarm == "alarm" else warns).append(msg)
        chill = (get("chiller") or {}).get("value")
        if chill and self.chiller is not None:
            if chill.get("fault"):
                alarms.append(f"chiller fault: {chill['fault']}")
            t, sp = chill.get("temp"), chill.get("setpoint")
            if t is not None and sp is not None and abs(t - sp) > self.chiller.tolerance:
                warns.append(f"chiller at {t} C, set point {sp} C")
        for key, r in self.readings.items():
            if r.get("error"):
                (warns if key in self.WARNING_READINGS else alarms).append(f"{key}: {r['error']}")
        return alarms + self.faults[-5:], warns

    def alarms(self) -> list[str]:
        return self._checks()[0]

    def status(self) -> dict[str, Any]:
        alarms, warns = self._checks()
        return {
            "name": self.config.name,
            "busy": self._held,
            "queue": [
                {"what": w[1], "client": w[2], "waiting": round(time.time() - w[3], 1)}
                for w in self._waiters
            ],
            "job": self.current.view() if self.current else None,
            "readings": self.readings,
            "led_on": self.led_on,
            "backlight_on": self.backlight,
            "heater_on": self.heater_on,
            "stage_request": self.last_stage_request,
            "stage_positions_live": dict(self.stage_learned),
            "frame_kinds": self.config.frame_kinds(),
            "flux_source": self.config.flux_source,
            "flux_reports": self.flux.status(),
            "camera_restarts": self.camera_restarts[-5:],
            "alarms": alarms,
            "warnings": warns,
        }

    # -- hardware steps (all bounded) ------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake (raising :class:`Aborted`) if the job is aborted."""
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._abort.wait()
        if self._abort.is_set():
            raise Aborted(self.current.abort_reason if self.current else "aborted")

    def stage_names(self) -> set[str]:
        return set(STAGE_NAMES) | set(self.config.stage_positions)

    async def move_stage(self, where: str | float) -> StagePosition:
        c = self.config
        target: float | None
        if isinstance(where, str):
            if where not in self.stage_names():
                raise ValueError(f"unknown iodine position {where!r}")
            # The server resolves the name with its own (live) ini.
            target = c.stage_positions.get(where)
            await self.spec.stage_move(where)
        else:
            target = where
            await self.spec.stage_move_mm(where)
        self.last_stage_request = str(where)
        deadline = time.monotonic() + c.stage_timeout
        # Arrived = within tolerance AND stopped: two consecutive readings
        # agree to a tenth of the tolerance. Within tolerance alone is not
        # enough: a stage still decelerating into place reads "unknown" (the
        # server names a position only at round(mm, 1)), and that reading
        # would go into the frame's header.
        # A named move has also arrived when the stopped stage is at the
        # position the live server itself names as the one asked for.
        prev: float | None = None
        while True:
            pos = await self.spec.stage_position()
            stopped = prev is not None and abs(pos.mm - prev) <= c.stage_tolerance / 10
            there = (target is not None and abs(pos.mm - target) <= c.stage_tolerance) or (
                isinstance(where, str) and pos.name == where
            )
            if stopped and there:
                if isinstance(where, str) and pos.name == where:
                    self._learn_stage(where, pos.mm)
                return pos
            prev = pos.mm
            if time.monotonic() >= deadline:
                want = where if target is None else f"{where} ({target} mm)"
                raise KiwiSpecTimeout(
                    f"iodine stage at {pos.mm} mm ({pos.name}), not {want}, after"
                    f" {c.stage_timeout:.0f} s (stuck?)"
                )
            await self._sleep(c.stage_poll)

    def _learn_stage(self, name: str, mm: float) -> None:
        """Record where the live server puts a named position; alarm if the
        config's cross-check value disagrees (the server is authoritative)."""
        self.stage_learned[name] = mm
        conf = self.config.stage_positions.get(name)
        if conf is not None and abs(conf - mm) > self.config.stage_tolerance:
            msg = (
                f"iodine stage: config stage_positions[{name!r}] = {conf} mm, but the live"
                f" spectrograph server puts {name!r} at {mm} mm (the server is authoritative;"
                " fix the config)"
            )
            if msg not in self.faults:
                log.error("%s", msg)
                self.faults.append(msg)

    async def set_led(self, on: bool) -> None:
        if on:
            await self.spec.call("led_turn_on")
            if not self.led_on:
                self.led_on_since = time.monotonic()
        else:
            await self.spec.call("led_turn_off")
            self.led_on_since = None
        self.led_on = on

    async def set_backlight(self, on: bool) -> None:
        """The FAU backlight: an LED disk on an arm that swings in over the
        fiber (on) and out again (off)."""
        if not self.config.backlight:
            raise Refused("no backlight (config backlight false)")
        meter = self.config.flux_source == "expmeter"
        if on:
            # The backlight would saturate the exposure meter: pause it first
            # (best effort: the legacy server answers 'fail' when the meter is
            # not running).
            if meter:
                with contextlib.suppress(KiwiSpecFailed):
                    await self.spec.call("stop_log_expmeter")
            await self.spec.call("backlight_on")
            self.backlight = True
        else:
            try:
                await self.spec.call("backlight_off")
                self.backlight = False
            finally:
                if meter:
                    with contextlib.suppress(KiwiSpecFailed):
                        await self.spec.call("start_log_expmeter")

    async def heater(self, on: bool | None, setpoint: float | None) -> None:
        if setpoint is not None:
            await self.spec.cell_heater_set_temp(setpoint)
        if on is True:
            await self.spec.cell_heater_on()
            self.heater_on = True
        elif on is False:
            await self.spec.cell_heater_off()
            self.heater_on = False

    async def wait_heater(self) -> str | None:
        """Wait up to ``heater_wait`` for the cell to reach its set point;
        then go on regardless (as legacy did) with a warning."""
        c = self.config
        deadline = time.monotonic() + c.heater_wait
        while True:
            temp = await self.spec.cell_heater_temp()
            if abs(temp - c.heater_setpoint) <= c.heater_tolerance:
                return None
            if time.monotonic() >= deadline:
                return f"iodine cell at {temp} C, set point {c.heater_setpoint} C"
            await self._sleep(min(1.0, c.heater_wait))

    async def _heater_readings(
        self, warnings: list[str], fresh_temp: bool
    ) -> tuple[float | None, float | None]:
        """The cell's actual and set temperatures for the header: the status
        loop's last reading when it is recent (no extra traffic on the wire),
        else a read now; ``fresh_temp`` always reads the temperature now (a
        science frame). None, with a warning, for a read that fails."""
        max_age = max(2 * self.config.status_interval, 30.0)
        out: list[float | None] = []
        for key, what, read in (
            ("heater_temp", "temperature", self.spec.cell_heater_temp),
            ("heater_setpoint", "set point", self.spec.cell_heater_get_set_temp),
        ):
            r = self.readings.get(key) or {}
            recent = r.get("ok_at") is not None and time.time() - r["ok_at"] <= max_age
            if recent and not (fresh_temp and key == "heater_temp"):
                out.append(r["value"])
                continue
            try:
                value = await read()
            except (KiwiSpecError, TimeoutError) as exc:
                out.append(None)
                warnings.append(f"iodine cell {what} unknown ({exc}); header says UNKNOWN")
                continue
            self._record(key, value)
            out.append(value)
        return out[0], out[1]

    async def ensure_camera(self) -> None:
        if self._camera_ready and self.camera.connected:
            return
        c = self.config
        await self.camera.set_readout_mode(c.readout_mode)
        await self.camera.set_ccd_format(*c.ccd_format)
        await self.camera.set_config_param("CCD Temperature Setpoint", si_temp_raw(c.ccd_setpoint))
        await self.camera.set_config_param("Operational Temp", si_temp_raw(c.ccd_operational_temp))
        await self.camera.set_cooler(True)
        self._camera_ready = True

    async def check_ccd(self) -> float | None:
        if not self.config.check_ccd_temp:
            return None
        temp = await self.camera.ccd_temperature()
        self._record("ccd_temp", temp)
        c = self.config
        if not c.ccd_temp_min <= temp <= c.ccd_temp_max:
            raise Refused(f"CCD temperature {temp} C is outside {c.ccd_temp_min}..{c.ccd_temp_max}")
        return temp

    # -- requests -------------------------------------------------------------------

    def _claim(self, what: str) -> None:
        """Take the spectrograph, synchronously (no await between the check
        and the claim, so two requests cannot both get it). Nobody jumps
        the queue."""
        if self._held is not None or self._waiters:
            raise Busy(f"busy ({self._held or 'requests queued'}); abort it first")
        self._held = what

    async def acquire(self, what: str, wait: float = 0.0, client: str = "") -> None:
        """Take the spectrograph, waiting (bounded by ``wait`` and
        ``max_queue_wait``) in a first-come, first-served queue while it is
        held. Raises :class:`Busy` when the wait runs out or the queue is
        full."""
        if self._held is None and not self._waiters:
            self._held = what
            return
        wait = min(max(0.0, wait), self.config.max_queue_wait)
        if wait <= 0:
            raise Busy(f"busy ({self._held or 'requests queued'})")
        if len(self._waiters) >= self.config.max_queue:
            raise Busy(f"busy ({self._held}); {len(self._waiters)} requests already waiting")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = [fut, what, client, time.time()]
        self._waiters.append(entry)
        log.info("%s (%s) waits for the spectrograph (held by %s)", what, client, self._held)
        try:
            async with asyncio.timeout(wait):
                await asyncio.shield(fut)
        except TimeoutError:
            if fut.done() and not fut.cancelled():
                return  # handed over just as the wait ran out: it is ours
            raise Busy(f"busy ({self._held}) for the {wait:.0f} s this request waited") from None
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release()  # handed over to a request that has gone
            raise
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)
            if not fut.done():
                fut.cancel()

    def _release(self) -> None:
        """Free the spectrograph, or hand it to the first request waiting."""
        self._held = None
        while self._waiters:
            fut, what, client, _ = self._waiters.popleft()
            if not fut.done():
                self._held = what
                fut.set_result(True)
                log.info("spectrograph handed to %s (%s)", what, client)
                return

    @contextlib.asynccontextmanager
    async def hold(self, what: str, wait: float = 0.0, client: str = ""):
        await self.acquire(what, wait, client)
        try:
            yield
        finally:
            self._release()

    async def configure(self, args: dict[str, Any]) -> dict[str, Any]:
        warnings: list[str] = []
        wait, client = queue_args(args)
        if args.get("led") and not self.config.slit_flat_led:
            raise ValueError(str(self.config.unavailable("slitflat")))
        async with self.hold("configure", wait, client):
            self._abort.clear()
            async with asyncio.timeout(self.config.stage_timeout + 60 + self.config.led_warmup):
                if "heater" in args or "heater_setpoint" in args:
                    sp = args.get("heater_setpoint")
                    await self.heater(
                        args.get("heater"), None if sp is None else _num(sp, "heater_setpoint")
                    )
                if "led" in args:
                    await self.set_led(bool(args["led"]))
                pos = None
                if "iodine" in args:
                    pos = await self.move_stage(self._iodine(args["iodine"]))
                if args.get("led") and self.led_on_since is not None:
                    await self._sleep(
                        self.config.led_warmup - (time.monotonic() - self.led_on_since)
                    )
        return {
            "stage": None if pos is None else {"mm": pos.mm, "name": pos.name},
            "led_on": self.led_on,
            "warnings": warnings,
        }

    def _iodine(self, value: Any) -> str | float:
        if isinstance(value, bool):
            return "in" if value else "out"
        if isinstance(value, str):
            if value not in self.stage_names():
                raise ValueError(f"unknown iodine position {value!r}")
            if value == "flat" and not self.config.slit_flat_led:
                # The stage's "flat" position puts the slit-flat lamp in the
                # beam; at MINERVA that lamp is dead (slit_flat_led false).
                raise ValueError(
                    "iodine position 'flat' (the slit-flat lamp) is not in use"
                    " (slit_flat_led is false)"
                )
            return value
        return _num(value, "iodine")

    def parse_frame(self, d: dict[str, Any]) -> Frame:
        c = self.config
        kind = str(d.get("kind", ""))
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
        why = c.unavailable(kind)
        if why is not None:
            raise ValueError(why)
        exptime = 0.0 if kind == "bias" else _num(d.get("exptime"), "exptime")
        if not 0 <= exptime <= c.max_exptime:
            raise ValueError(f"exptime {exptime} outside 0..{c.max_exptime}")
        teles = [str(t) for t in d.get("telescopes") or []]
        unknown = set(teles) - set(c.telescopes)
        if unknown:
            raise ValueError(f"unknown telescopes {sorted(unknown)}; known: {c.telescopes}")
        if len(set(teles)) != len(teles):
            raise ValueError("a telescope is listed twice")
        needs_teles = KINDS[kind][4]
        if needs_teles and not teles:
            raise ValueError("a science frame needs the telescopes feeding the fiber")
        if not needs_teles and teles:
            raise ValueError(f"a {kind} frame takes no telescopes")
        obj = str(d.get("object") or DEFAULT_OBJECT.get(kind, ""))
        if not obj:
            raise ValueError("a science frame needs an object name")
        if not _OBJECT.match(obj):
            raise ValueError(f"object name {obj!r} must match {_OBJECT.pattern}")
        counts = d.get("target_counts")
        if counts is not None:
            if not KINDS[kind][3]:
                raise ValueError(f"a {kind} frame does not use the exposure meter")
            counts = _num(counts, "target_counts")
            if counts <= 0:
                raise ValueError("target_counts must be positive")
        iodine = d.get("iodine")
        if iodine is not None:
            if kind not in ("science", "fiberflat"):
                raise ValueError(f"the iodine position of a {kind} frame is fixed")
            iodine = self._iodine(iodine)
        night_label = d.get("night")
        if night_label is not None and not _NIGHT.match(str(night_label)):
            raise ValueError(f"night {night_label!r} must look like n20261109")
        return Frame(
            kind,
            exptime,
            obj,
            teles,
            counts,
            iodine,
            None if night_label is None else str(night_label),
        )

    def submit(self, kind: str, frames: list[Frame], client: str = "") -> Job:
        job_id = next(self._ids)
        self._claim(f"job {job_id}")
        return self._start(job_id, kind, frames, client)

    async def submit_wait(
        self, kind: str, frames: list[Frame], wait: float = 0.0, client: str = ""
    ) -> Job:
        """:meth:`submit`, waiting (bounded) in the queue for the spectrograph."""
        job_id = next(self._ids)
        await self.acquire(f"job {job_id}", wait, client)
        return self._start(job_id, kind, frames, client)

    def _start(self, job_id: int, kind: str, frames: list[Frame], client: str) -> Job:
        job = Job(job_id, kind, frames, client=client)
        self.jobs[job.id] = job
        for old in sorted(self.jobs)[:-50]:
            if self.jobs[old].finished.is_set():
                del self.jobs[old]
        self.current = job
        self._abort.clear()
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        # The claim was taken in submit(); it is released in finally.
        job.state = "running"
        try:
            for i, frame in enumerate(job.frames):
                job.phase = f"frame {i + 1}/{len(job.frames)}"
                async with asyncio.timeout(self.frame_bound(frame)):
                    job.results.append(await self._take(job, frame))
                if self._abort.is_set():
                    raise Aborted(job.abort_reason or "aborted")
            job.state = "done"
        except Aborted as exc:
            job.state, job.error = "aborted", str(exc)
        except asyncio.CancelledError:
            job.state = "aborted"
            job.error = job.error or "cancelled"
            raise
        except TimeoutError:
            job.state, job.error = "failed", "frame exceeded its time bound"
            await self._drop_camera()
        except Refused as exc:
            job.state, job.error = "failed", f"refused: {exc}"
        except (KiwiSpecError, ValueError) as exc:
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # never leave a job "running"
            log.exception("job %d crashed", job.id)
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self._acquisition = None
            job.phase = ""
            job.ended = time.time()
            job.finished.set()
            if job.error:
                log.warning("job %d %s: %s", job.id, job.state, job.error)
            if job.state == "failed" and self._restart_due():
                # The job has ended; the spectrograph stays held until SI
                # Image is back. Shielded: a restart is never left halfway
                # (killed and not started again).
                self._held = "restarting SI Image"
                task = asyncio.get_running_loop().create_task(self._maybe_restart_camera())
                task.add_done_callback(lambda _t: self._release())
                await asyncio.shield(task)
            else:
                self._release()

    def frame_bound(self, f: Frame) -> float:
        c = self.config
        return (
            c.stage_timeout
            + c.heater_wait
            + c.led_warmup
            + f.exptime
            + c.readout_time
            + c.exposure_margin
            + c.save_timeout
            + c.abort_timeout
            # the raw file's rewrite: its wait to appear, and the rewrite
            + (float(c.raw_fits.get("wait", 30.0)) + 60.0 if c.raw_fits else 0.0)
        )

    async def _drop_camera(self) -> None:
        """Forget the camera connection after a camera failure (counted
        towards an SI Image restart)."""
        self._camera_ready = False
        self.camera_failures += 1
        await self.camera.close()

    async def restart_camera(self, reason: str) -> dict[str, Any]:
        """Kill SI Image and start it again **at once** (its CCD cooler is off
        while it is down, and the CCD's temperature stability matters), then
        reconnect and turn the cooler on. Bounded by
        ``camera_restart_timeout``. Raises :class:`KiwiSpecError` if SI
        Image could not be started again (an alarm: a human must start it).
        The caller holds the spectrograph."""
        c = self.config
        log.warning("restarting SI Image: %s", reason)
        record: dict[str, Any] = {"reason": reason, "at": time.time(), "ok": False}
        self.camera_restarts.append(record)
        del self.camera_restarts[:-20]
        self._camera_ready = False
        await self.camera.close()
        t0 = time.monotonic()
        deadline = t0 + c.camera_restart_timeout
        try:
            try:
                await self.spec.call("kill_si_image")
            except KiwiSpecError as exc:
                # It may be gone already; start it regardless.
                log.warning("kill_si_image: %s", exc)
            last: Exception | None = None
            while True:  # start again at once, retrying until the deadline
                try:
                    await self.spec.call("start_si_image")
                    break
                except KiwiSpecError as exc:
                    last = exc
                    if time.monotonic() >= deadline:
                        raise KiwiSpecError(f"start_si_image failed: {last}") from exc
                    await asyncio.sleep(1.0)
            while True:  # SI Image takes a moment to listen again
                try:
                    await self.ensure_camera()
                    break
                except (KiwiSpecError, TimeoutError) as exc:
                    self._camera_ready = False
                    await self.camera.close()
                    if time.monotonic() >= deadline:
                        raise KiwiSpecError(f"SI Image did not come back: {exc}") from exc
                    await asyncio.sleep(0.5)
        except KiwiSpecError as exc:
            msg = f"SI Image restart failed ({exc}): the CCD cooler may be off; start it by hand"
            record["error"] = msg
            self.faults.append(msg)
            log.error("%s", msg)
            raise
        record["ok"] = True
        record["seconds"] = round(time.monotonic() - t0, 2)
        self.camera_failures = 0
        log.warning("SI Image restarted in %.1f s", record["seconds"])
        return record

    def _restart_due(self) -> bool:
        c = self.config
        return c.camera_restart and self.camera_failures >= c.camera_restart_after

    async def _maybe_restart_camera(self) -> None:
        """After ``camera_restart_after`` camera failures in a row (module
        doc). Never raises."""
        c = self.config
        try:
            async with asyncio.timeout(c.camera_restart_timeout + 30):
                await self.restart_camera(f"{self.camera_failures} camera failures in a row")
        except (KiwiSpecError, TimeoutError) as exc:
            log.error("SI Image restart: %s", exc)

    async def _take(self, job: Job, f: Frame) -> dict[str, Any]:
        c = self.config
        dark, stage, led, uses_meter, _ = KINDS[f.kind]
        warnings: list[str] = []
        job.phase = f"{f.kind}: checking camera"
        try:
            await self.ensure_camera()
            ccd = await self.check_ccd()
        except (KiwiSpecError, TimeoutError):
            await self._drop_camera()
            raise

        job.phase = f"{f.kind}: configuring"
        if c.backlight and self.backlight is not False:
            await self.set_backlight(False)
        await self.set_led(led)
        if f.kind == "science":
            await self.heater(True, c.heater_setpoint)
        where: str | float = stage or (f.iodine if f.iodine is not None else c.default_iodine)
        pos = await self.move_stage(where)
        if f.kind == "science":
            w = await self.wait_heater()
            if w:
                warnings.append(w)
        # The cell's temperatures go into every frame's header: a failed
        # read is written as UNKNOWN (with a warning), never dropped.
        heater_temp, heater_setpoint = await self._heater_readings(
            warnings, fresh_temp=f.kind == "science"
        )
        if led and self.led_on_since is not None:
            job.phase = f"{f.kind}: LED warm-up"
            await self._sleep(c.led_warmup - (time.monotonic() - self.led_on_since))

        index = await self.spec.get_index()
        # One night label for the file name and its directory (module doc).
        night_label = f.night or night(self.clock(), self.night_rule)
        file_name = f"{night_label}.{f.object}.{index:04d}.fits"
        await self.spec.call("set_file_name", file_name)

        source = c.flux_source if uses_meter else "none"
        meter = source == "expmeter"
        fau = source == "fau"
        if meter:
            try:
                await self.spec.reset_expmeter()
            except KiwiSpecError as exc:
                meter = False
                warnings.append(f"exposure meter unavailable ({exc}); time-terminated")
        if f.target_counts is not None and source == "none" and uses_meter:
            warnings.append(
                "no flux source (flux_source none): target_counts ignored, time-terminated"
            )
        if self._abort.is_set():
            raise Aborted(job.abort_reason or "aborted")

        job.phase = f"{f.kind}: exposing"
        acq = await self.camera.start_acquisition(f.exptime, dark=dark)
        self._acquisition = acq
        t0 = time.monotonic()
        start_utc = self.clock()
        # The FAU frames carry the guiders' wall-clock (Unix, NTP) times: the
        # exposure window is matched against them in the same time, even
        # when a simulation runs this service on a scaled clock.
        start_unix = time.time()
        ended_by, samples, failures = "time", [(0.0, 0.0)], 0
        end = f.exptime
        try:
            while True:
                elapsed = time.monotonic() - t0
                if self._abort.is_set() or self._stop.is_set():
                    ended_by = "abort" if self._abort.is_set() else "stop"
                    end = elapsed
                    break
                if elapsed >= f.exptime or acq.finished:
                    end = min(elapsed, f.exptime)
                    break
                wait = min(c.expmeter_poll, f.exptime - elapsed)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await self._wake()
                if fau and f.target_counts is not None and not self._abort.is_set():
                    got = self._fau(f.telescopes, start_unix, time.time()).counts
                    if got >= f.target_counts:
                        ended_by = "counts"
                        end = time.monotonic() - t0
                        break
                if meter and not self._abort.is_set():
                    total = await self._meter(c.expmeter_poll)
                    if total is None:
                        failures += 1
                        if failures >= c.expmeter_max_failures:
                            meter = False
                            warnings.append("exposure meter stopped answering; time-terminated")
                    elif samples and total < samples[-1][1]:
                        meter = False
                        warnings.append("exposure meter total went backwards (server restart?)")
                    else:
                        failures = 0
                        samples.append((time.monotonic() - t0, total))
                        if f.target_counts is not None and total >= f.target_counts:
                            ended_by = "counts"
                            end = time.monotonic() - t0
                            break
            if ended_by != "time" and not acq.finished:
                await self.camera.terminate(timeout=c.abort_timeout)
            if ended_by == "abort":
                job.phase = f"{f.kind}: aborting (readout)"
                await acq.wait(c.readout_time + c.abort_timeout)
                raise Aborted(job.abort_reason or "aborted")
            if meter:
                # One more meter period, so the counts up to the shutter
                # closing have reached the total (see docs/kiwispec.md). Not
                # cut short by an abort: the frame is being read out anyway.
                await asyncio.sleep(c.expmeter_period / 2 + c.expmeter_poll / 2)
                total = await self._meter(c.expmeter_poll)
                if total is not None and total >= samples[-1][1]:
                    samples.append((time.monotonic() - t0, total))
            job.phase = f"{f.kind}: reading out"
            remaining = max(0.0, f.exptime - (time.monotonic() - t0))
            await acq.wait(remaining + c.readout_time + c.exposure_margin)
        except (KiwiSpecError, TimeoutError):
            await self._drop_camera()
            raise
        finally:
            self._acquisition = None

        job.phase = f"{f.kind}: saving"
        await self._save(file_name)
        self.camera_failures = 0
        midpoint = counts = fau_counts = None
        fau_detail: dict[str, Any] | None = None
        if meter and len(samples) > 1:
            lag = c.expmeter_period / 2
            shifted = [(samples[0][0], samples[0][1])] + [(t - lag, n) for t, n in samples[1:]]
            midpoint, counts = flux_weighted_midpoint(shifted, 0.0, end)
        if fau:
            # The readout gave the guiders time to report the last frames.
            t_start = start_unix
            res = self._fau(f.telescopes, t_start, t_start + end)
            fau_counts, fau_detail = res.counts, res.telescopes
            best = max((v["coverage"] for v in res.telescopes.values()), default=0.0)
            if res.midpoint is None:
                warnings.append("no FAU flux reported for this exposure: geometric midpoint")
            elif best < c.fau_min_coverage:
                warnings.append(
                    f"FAU flux covers {best:.0%} of the exposure (< {c.fau_min_coverage:.0%}):"
                    " geometric midpoint"
                )
            else:
                midpoint = res.midpoint - t_start
        if midpoint is None:
            source = "none"
        mid_utc = None if midpoint is None else start_utc + datetime.timedelta(seconds=midpoint)
        result: dict[str, Any] = {
            "file": file_name,
            "kind": f.kind,
            "object": f.object,
            "telescopes": f.telescopes,
            "dark": dark,
            "exptime_requested": f.exptime,
            "exptime": end,
            "ended_by": ended_by,
            "start_utc": start_utc.isoformat(),
            "flux_source": source,
            "expmeter_counts": counts,
            "fau_counts": fau_counts,
            "fau": fau_detail,
            "target_counts": f.target_counts,
            "flux_midpoint_s": midpoint,
            "flux_midpoint_utc": None if mid_utc is None else mid_utc.isoformat(),
            "flux_midpoint_jd": None if mid_utc is None else jd(mid_utc),
            # The midpoint for the barycentric correction: the flux-weighted
            # one, else the middle of the exposure.
            "midpoint_source": "geometric" if mid_utc is None else source,
            "iodine": {"requested": str(where), "mm": pos.mm, "name": pos.name},
            "led_on": led,
            "heater_temp": heater_temp,
            "heater_setpoint": heater_setpoint,
            "ccd_temp": ccd,
            "warnings": warnings,
        }
        job.phase = f"{f.kind}: header"
        try:
            await self.spec.write_header(self._header(result))
            result["header_written"] = True
        except KiwiSpecError as exc:
            result["header_written"] = False
            warnings.append(f"header not written: {exc}")
        if c.raw_fits is not None and c.raw_fits.get("rewrite", True):
            job.phase = f"{f.kind}: standardizing the raw file"
            result["fits"] = await self._standardize(night_label, file_name)
            if result["fits"].get("error"):
                warnings.append(f"raw file not rewritten: {result['fits']['error']}")
        return result

    def _fau(self, telescopes: list[str], start: float, end: float) -> Any:
        """The FAU flux of ``telescopes`` in ``[start, end]`` (Unix s)."""
        c = self.config
        return fau_flux(self.flux.window(telescopes, start, end), start, end, c.fau_max_gap)

    async def _standardize(self, night_label: str, file_name: str) -> dict[str, Any]:
        """Make the raw file legal FITS in place, verified
        (:func:`~robofast.devices.kiwispec_fits.standardize`). Never
        raises: a failure leaves the raw file untouched and raises an alarm."""
        from pathlib import Path

        raw = self.config.raw_fits or {}
        path = Path(raw["dir"]) / night_label / file_name
        deadline = time.monotonic() + float(raw.get("wait", 30.0))
        while not path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        try:
            return await asyncio.to_thread(
                standardize, path, utc_offset_hours=self.config.pc_utc_offset_hours
            )
        except FitsRewriteError as exc:
            msg = f"raw file {file_name} not rewritten (left untouched): {exc}"
            log.error("%s", msg)
            self.faults.append(msg)
            return {"file": file_name, "rewritten": False, "error": str(exc)}

    async def _wake(self) -> None:
        """Return when an abort or stop is requested."""
        a = asyncio.ensure_future(self._abort.wait())
        s = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({a, s}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            a.cancel()
            s.cancel()

    async def _meter(self, timeout: float) -> float | None:
        try:
            async with asyncio.timeout(max(timeout, 0.5) + self.spec.bound("get_expmeter_total")):
                return await self.spec.expmeter_total()
        except (KiwiSpecError, TimeoutError) as exc:
            log.warning("exposure meter read failed: %s", exc)
            return None

    async def _save(self, file_name: str) -> None:
        """Move the raw frame into place; while the spectrograph server is
        unreachable (restarting), retry until ``save_timeout``. The file name
        is sent again first: a restarted server has forgotten it (and
        ``save_image`` would then fail)."""
        c = self.config
        deadline = time.monotonic() + c.save_timeout
        while True:
            try:
                await self.spec.call("set_file_name", file_name)
                await self.spec.call("save_image")
                return
            except KiwiSpecUnreachable:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(c.save_retry)

    @staticmethod
    def _header(r: dict[str, Any]) -> str:
        """Header keywords for the raw file, legacy JSON ``{key: [value, comment]}``."""

        def clean(v: Any) -> Any:
            return v.replace("'", "") if isinstance(v, str) else v

        h: dict[str, Any] = {
            "IMAGETYP": [r["kind"], "Type of image"],
            "OBJECT": [r["object"], "Object name"],
            "EXPTERM": [r["ended_by"], "What ended the exposure (time/counts/stop)"],
            "FLUXSRC": [r["flux_source"], "Flux record for FLUXMID (fau/expmeter/none)"],
            "EXPFLUX": [
                "UNKNOWN" if r["expmeter_counts"] is None else r["expmeter_counts"],
                "Exposure meter flux during exposure in counts",
            ],
            "FAUFLUX": [
                "UNKNOWN" if r["fau_counts"] is None else round(r["fau_counts"], 1),
                "Summed FAU fiber-aperture flux (relative)",
            ],
            "MEXPFLUX": [
                "NA" if r["target_counts"] is None else r["target_counts"],
                "Maximum Exposure meter flux in counts",
            ],
            "FLUXMID": [
                "UNKNOWN" if r["flux_midpoint_jd"] is None else r["flux_midpoint_jd"],
                "Flux-weighted mid exposure time (JD_UTC)",
            ],
            "I2POSAF": [r["iodine"]["mm"], "Iodine Stage Actual Position (mm)"],
            "I2POSAS": [r["iodine"]["name"], "Iodine Stage Actual Position [string]"],
            "I2POSSS": [r["iodine"]["requested"], "Iodine Stage Set Position [string]"],
            "FIBFEED": [",".join(r["telescopes"]) or "NONE", "Telescopes feeding the fiber"],
            "SWCREATE": ["robofast kiwispec_service", "Software that drove the exposure"],
        }
        h["I2TEMPA"] = [
            "UNKNOWN" if r.get("heater_temp") is None else r["heater_temp"],
            "Iodine Cell Actual Temperature (C)",
        ]
        h["I2TEMPS"] = [
            "UNKNOWN" if r.get("heater_setpoint") is None else r["heater_setpoint"],
            "Iodine Cell Set Temperature (C)",
        ]
        return json.dumps({k: [clean(x) for x in v] for k, v in h.items()})

    # -- abort / stop -------------------------------------------------------------------

    async def abort(self, reason: str) -> dict[str, Any]:
        """Abort the running job (discarding the frame), bounded by
        ``abort_timeout``; then cancel it if it has not ended."""
        job, task = self.current, self._task
        if job is None or task is None or task.done() or job.finished.is_set():
            # (A finished job's task may still be restarting SI Image: that
            # is never cut short.)
            return {"aborted": False, "ended": True, "job": job.view() if job else None}
        job.abort_reason = reason
        self._abort.set()
        log.warning("aborting job %d: %s", job.id, reason)
        ended = True
        try:
            async with asyncio.timeout(self.config.abort_timeout):
                await job.finished.wait()
        except TimeoutError:
            ended = False
            log.error(
                "job %d did not end within %.0f s; cancelling", job.id, self.config.abort_timeout
            )
            # Bounded, passing our own cancellation on (robofast.cancel).
            await stop_task(task, 5)
            await self._drop_camera()
            self.faults.append(f"job {job.id} did not end after abort: camera unresponsive?")
        return {"aborted": True, "ended": ended, "job": job.view()}

    def stop(self) -> dict[str, Any]:
        """End the current exposure early, keeping the frame."""
        if self.current is None or self.current.finished.is_set():
            return {"stopped": False}
        self._stop.set()
        return {"stopped": True, "job": self.current.id}


# -- HTTP ---------------------------------------------------------------------------------

MAX_WAIT = 60.0


class _HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class KiwiSpecServer:
    """Serve a :class:`KiwiSpec` over HTTP/JSON (plain asyncio)."""

    def __init__(self, config: KiwiSpecConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.allow_hardware != hardware:
            log.info(
                "hardware use needs allow_hardware in the config AND --hardware;"
                " only a verified simulator spectrograph PC is allowed now"
            )
        s, cam = config.spectrograph, config.camera
        spec = SpecServerClient(
            s.host,
            s.port,
            allow_hardware=allow,
            connect_timeout=s.connect_timeout,
            send_timeout=s.send_timeout,
            max_attempts=s.max_attempts,
            reply_timeouts=s.reply_timeouts,
            max_reply_timeout=s.max_reply_timeout,
        )
        camera = SICameraClient(
            cam.host,
            cam.port,
            allow_hardware=allow,
            connect_timeout=cam.connect_timeout,
            command_timeout=cam.command_timeout,
        )
        if allow:
            log.warning("HARDWARE MODE: spectrograph PC at %s", spec.host)
        self.aux: AuxDevices | None = None
        routed: Any = spec
        if config.aux is not None:
            aux_cfg = AuxConfig.from_dict(config.aux)
            self.aux = AuxDevices(aux_cfg, allow_hardware=allow)
            routed = AuxRouted(spec, self.aux)
            log.info("aux devices through the Alpaca server at %s", aux_cfg.address)
        self.config = config
        self.kiwispec = KiwiSpec(config, routed, camera)
        if self.aux is not None and self.aux.cfg.chiller:
            self.kiwispec.chiller = AuxChiller(self.aux, self.aux.cfg.chiller_tolerance)
        host, _, port = config.listen.rpartition(":")
        self.host, self.port = host, int(port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self, *, status_loop: bool = True) -> KiwiSpecServer:
        self._server = await asyncio.start_server(self._connection, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if status_loop:
            self.kiwispec.start_status_loop()
        return self

    async def stop(self) -> None:
        await self.kiwispec.close()
        if self.aux is not None:
            await self.aux.close()
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await self._server.wait_closed()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                method, target, _ = line.decode("latin-1").split(" ", 2)
                headers = {}
                while (h := await reader.readline()) not in (b"\r\n", b"\n", b""):
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length") or 0)
                if n > 1 << 20:
                    break
                body = await reader.readexactly(n) if n else b""
                status, payload = await self._respond(method, target, body)
                data = json.dumps(payload).encode()
                head = (
                    f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n"
                )
                writer.write(head.encode() + data)
                await writer.drain()
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _respond(self, method: str, target: str, body: bytes) -> tuple[int, Any]:
        k = self.kiwispec
        url = urlparse(target)
        query = {key: v[0] for key, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            args = json.loads(body) if body else {}
            if not isinstance(args, dict):
                raise ValueError("the body must be a JSON object")
            if method == "GET" and parts == ["status"]:
                if query.get("refresh") in ("1", "true"):
                    await k.refresh()
                return 200, k.status()
            if method == "POST" and parts == ["configure"]:
                return 200, await k.configure(args)
            if method == "POST" and parts == ["expose"]:
                frame = k.parse_frame(args)
                wait, client = queue_args(args)
                return 202, (await k.submit_wait("expose", [frame], wait, client)).view()
            if method == "POST" and parts == ["calibrate"]:
                frames = []
                for spec in args.get("frames") or []:
                    n = int(spec.get("n", 1))
                    if not 1 <= n <= 100:
                        raise ValueError("n must be 1..100")
                    if spec.get("kind") == "science":
                        raise ValueError("a calibration has no science frames")
                    if "night" in args:
                        spec = {**spec, "night": args["night"]}
                    frames += [k.parse_frame(spec) for _ in range(n)]
                if not frames:
                    raise ValueError("no frames")
                wait, client = queue_args(args)
                return 202, (await k.submit_wait("calibrate", frames, wait, client)).view()
            if method == "GET" and parts[:1] == ["jobs"] and len(parts) == 2:
                job = k.jobs.get(int(parts[1])) if parts[1].isdigit() else None
                if job is None:
                    raise _HTTPError(404, f"no job {parts[1]}")
                wait = min(float(query.get("wait", 0)), MAX_WAIT)
                if wait > 0:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(wait):
                            await job.finished.wait()
                return 200, job.view()
            if method == "POST" and parts == ["abort"]:
                return 200, await k.abort(str(args.get("reason") or "requested"))
            if method == "POST" and parts == ["stop"]:
                return 200, k.stop()
            if method == "POST" and parts == ["flux"]:
                tel = str(args.get("telescope") or "")
                if tel not in self.config.telescopes:
                    raise ValueError(f"unknown telescope {tel!r}; known: {self.config.telescopes}")
                frames = parse_frames(args.get("frames"))
                kept = k.flux.add(tel, frames, time.time())
                return 200, {"telescope": tel, "accepted": len(frames), "kept": kept}
            if method == "POST" and parts == ["restart_camera"]:
                wait, client = queue_args(args)
                async with k.hold("restart_camera", wait, client):
                    return 200, await k.restart_camera(str(args.get("reason") or "requested"))
            if method == "POST" and parts == ["backlight"]:
                wait, client = queue_args(args)
                async with k.hold("backlight", wait, client):
                    await k.set_backlight(bool(args["on"]))
                return 200, {"backlight_on": k.backlight}
            raise _HTTPError(404, f"no {method} {url.path}")
        except _HTTPError as exc:
            return exc.status, {"error": str(exc)}
        except Busy as exc:
            return 409, {"error": str(exc), "busy": True}
        except Refused as exc:
            return 409, {"error": str(exc)}
        except Aborted as exc:
            return 409, {"error": f"aborted: {exc}"}
        except (KiwiSpecError, TimeoutError) as exc:
            return 503, {"error": f"{type(exc).__name__}: {exc}"}
        except (ValueError, TypeError, KeyError) as exc:
            return 400, {"error": f"bad request: {exc!r}"}


# -- the client the sequencer uses -----------------------------------------------------


class KiwiSpecRemote:
    """The service's API, for the sequencer and tools. :meth:`abort` is the
    cleanup hook: bounded, and it never raises."""

    #: Keys of a frame's result the sequencer copies to the top of each
    #: spectrum sidecar record (:class:`~robofast.sequencer.SpectrographHook`).
    record_keys: tuple[str, ...] = ("iodine", "flux_source", "fau_counts", "fits")

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        abort_timeout: float = 45.0,
        client: str = "",
        queue_wait: float = 0.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.abort_timeout = abort_timeout
        #: Who this is (the service's queue and job views show it), and how
        #: long ``expose`` and ``calibrate`` wait in the service's queue
        #: while another client holds the spectrograph (module doc).
        self.client = client
        self.queue_wait = queue_wait

    async def _call(self, method: str, path: str, body: Any = None, timeout: float | None = None):
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout, trust_env=False) as c:
                r = await isolated(c.request(method, self.url + path, json=body))
        except httpx.HTTPError as exc:
            raise KiwiSpecError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        data = r.json()
        if r.status_code == 409 and data.get("busy"):
            raise KiwiSpecBusy(f"{method} {path}: {data.get('error')}")
        if r.status_code >= 400:
            raise KiwiSpecError(f"{method} {path}: {r.status_code} {data.get('error')}")
        return data

    async def status(self, refresh: bool = False) -> dict[str, Any]:
        """The service's status (``refresh``: read every sensor now, bounded)."""
        return await self._call("GET", "/status?refresh=1" if refresh else "/status")

    def _queue(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        body = {**body}
        body.setdefault("client", self.client)
        body.setdefault("wait", self.queue_wait)
        return body, float(body["wait"]) + self.timeout

    async def expose(self, **frame: Any) -> dict[str, Any]:
        """Start one frame (raises :class:`KiwiSpecBusy` if another client
        still holds the spectrograph after ``wait``)."""
        body, timeout = self._queue(frame)
        return await self._call("POST", "/expose", body, timeout=timeout)

    async def calibrate(
        self, frames: list[dict[str, Any]], night: str | None = None
    ) -> dict[str, Any]:
        """Start a calibration sequence (``[{"kind", "exptime", "n"}, ...]``)."""
        body, timeout = self._queue({"frames": frames})
        if night is not None:
            body["night"] = night
        return await self._call("POST", "/calibrate", body, timeout=timeout)

    async def job(self, job_id: int, wait: float = 0.0) -> dict[str, Any]:
        return await self._call("GET", f"/jobs/{job_id}?wait={wait}", timeout=wait + self.timeout)

    async def stop(self) -> dict[str, Any]:
        """End the current exposure early, keeping the frame."""
        return await self._call("POST", "/stop", {})

    async def abort(self, reason: str, *, timeout: float | None = None) -> bool:
        """Abort whatever the spectrograph is doing; True if nothing is left
        running. Never raises (a cleanup must go on to park and close).
        Bounded by ``timeout`` (default ``abort_timeout``)."""
        timeout = self.abort_timeout if timeout is None else timeout
        try:
            async with asyncio.timeout(timeout):
                data = await self._call("POST", "/abort", {"reason": reason}, timeout=timeout)
        except Exception as exc:
            log.error("kiwispec abort (%s) failed: %r", reason, exc)
            return False
        return bool(data.get("ended", False))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real spectrograph PC (the config must also say allow_hardware)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_device_config(args.config, KiwiSpecConfig.from_dict)
    if args.listen:
        config.listen = args.listen

    async def run() -> None:
        try:
            server = KiwiSpecServer(config, hardware=args.hardware)
        except HardwareNotAllowed as exc:
            log.error("%s", exc)
            sys.exit(2)
        await server.start()
        print(json.dumps({"url": server.url}), flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await server.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
