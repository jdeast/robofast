"""The KiwiSpec spectrograph service: the one owner of the spectrograph.

This process is the only thing that talks to the spectrograph PC (its legacy
``spectrograph_server.py`` and its SI Image camera server, through
:mod:`robofast_core.devices.kiwispec_client`). Everything else -- the
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
  ``iodine`` default from config; exposure-meter terminated when
  ``target_counts`` is given, ``exptime`` then being the maximum),
  ``thar``, ``slitflat``, ``fiberflat``, ``dark``, ``bias`` (``exptime`` 0).
* ``POST /calibrate`` ``{"frames": [{"kind", "exptime", "n"}, ...]}`` -- a
  calibration sequence as one job; 202.
* ``GET /jobs/{id}?wait=S`` -- a job's state; waits up to ``S`` s for it to
  finish. Each frame's result has the file name, the actual exposure time,
  what ended it (``time``, ``counts``, ``stop``), the exposure-meter counts
  and the **flux-weighted midpoint** (seconds after the start, UTC, JD).
* ``POST /abort`` ``{"reason"}`` -- abort the running job: terminate the
  exposure and discard the frame; answers once the job has ended, or after
  ``abort_timeout`` (then ``"ended": false``; the job is cancelled and every
  lock released anyway). An abort that arrives once the shutter has closed
  lets that frame finish reading out and be saved, then ends the job.
  ``POST /stop`` ends the exposure early and keeps the frame.
* ``POST /backlight`` ``{"on": bool}`` -- the FAU fiber-acquisition backlight
  (the exposure meter is paused while it is on; the server itself turns the
  backlight off after 60 s).

One job at a time: ``configure``, ``expose``, ``calibrate`` and
``backlight`` answer 409 while a job runs. Every wait is bounded; locks are
released in ``finally``.

Safety
------
The spectrograph PC must be loopback (the simulator) unless the config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``.

Running
-------
::

    python -m robofast_core.devices.kiwispec_service --config kiwispec.json [--hardware]

with a JSON config (every key optional except the two hosts)::

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
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.devices.kiwispec_client import (
    CAMERA_PORT,
    SPEC_PORT,
    HardwareNotAllowed,
    KiwiSpecError,
    KiwiSpecFailed,
    KiwiSpecTimeout,
    KiwiSpecUnreachable,
    SIAcquisition,
    SICameraClient,
    SpecServerClient,
    StagePosition,
)

log = logging.getLogger(__name__)

#: Frame kinds: (SI dark frame?, iodine position or None for the request's,
#: slit-flat LED on?, uses the exposure meter?, needs telescopes?).
KINDS: dict[str, tuple[bool, str | None, bool, bool, bool]] = {
    # legacy control.spec_equipment_check, per objname
    "science": (False, None, False, True, True),
    "thar": (False, "out", False, False, False),
    "slitflat": (False, "flat", True, False, False),
    "fiberflat": (False, None, False, False, False),
    "dark": (True, "in", False, False, False),
    "bias": (True, "in", False, False, False),
}
#: Legacy object names in file names (``n20261031.<object>.0001.fits``).
DEFAULT_OBJECT = {
    "thar": "ThAr",
    "slitflat": "slitFlat",
    "fiberflat": "fiberFlat",
    "dark": "Dark",
    "bias": "Bias",
}
_OBJECT = re.compile(r"^[A-Za-z0-9_+-]{1,60}$")
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


def jd(t: datetime.datetime) -> float:
    return t.timestamp() / 86400.0 + JD_UNIX_EPOCH


def night(t: datetime.datetime | None = None) -> str:
    """Legacy night label: ``n`` + the UTC date."""
    return "n" + (t or datetime.datetime.now(datetime.UTC)).strftime("%Y%m%d")


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
    name: str = "kiwispec"
    listen: str = "127.0.0.1:8130"
    #: Must be true (and ``--hardware`` given) for a non-loopback PC.
    allow_hardware: bool = False
    #: Telescopes that can feed the fiber (legacy tele_list).
    telescopes: list[str] = field(default_factory=lambda: ["T1", "T2", "T3", "T4"])
    #: The server's named stage positions (mm; spectrograph_server.ini), used
    #: to check arrival. They must match the server's.
    stage_positions: dict[str, float] = field(
        default_factory=lambda: {"in": 11.0, "flat": 68.0, "out": 102.0}
    )
    #: Arrived: within this of the target (mm) and stopped (two consecutive
    #: polls agree to a tenth of it).
    stage_tolerance: float = 0.1
    stage_timeout: float = 60.0  # legacy ctrl_i2stage_move
    stage_poll: float = 1.0
    default_iodine: str = "in"
    heater_setpoint: float = 55.0  # legacy I2SETTEMP
    heater_tolerance: float = 0.501  # legacy I2TEMPTOL
    heater_wait: float = 10.0  # legacy: wait this long, then go on
    #: Seconds the slit-flat LED must have been on before a slit flat.
    led_warmup: float = 0.0  # legacy WARMUPMINUTES = 0
    readout_time: float = 22.0  # legacy ro_time
    #: Added to every exposure's bound (exposure + readout + this).
    exposure_margin: float = 60.0
    max_exptime: float = 3600.0
    expmeter_poll: float = 1.0
    #: The meter's own reading period (legacy: 1 reading/s).
    expmeter_period: float = 1.0
    expmeter_max_failures: int = 3
    #: Refuse an exposure with the CCD outside these (C). Legacy setpoint
    #: -90 C, "operational" -85 C (SI PARAM62, PARAM63).
    ccd_temp_min: float = -95.0
    ccd_temp_max: float = -85.0
    check_ccd_temp: bool = True
    #: Legacy spectrograph pressure alarm threshold (mbar).
    max_spec_pressure: float = 0.006
    abort_timeout: float = 30.0
    #: How long a finished frame may wait for the spectrograph server to come
    #: back (to move the raw frame into place).
    save_timeout: float = 60.0
    save_retry: float = 2.0
    status_interval: float = 10.0
    status_timeout: float = 15.0
    #: The SI camera set-up legacy sent at start (initialize_si).
    readout_mode: int = 3
    ccd_format: tuple[int, int, int, int, int, int] = (50, 2090, 1, 0, 2052, 1)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KiwiSpecConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}  # "_note" etc.
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown kiwispec config keys: {sorted(unknown)}")
        d["spectrograph"] = SpecEndpoint(**d["spectrograph"])
        d["camera"] = CameraEndpoint(**d["camera"])
        if "ccd_format" in d:
            d["ccd_format"] = tuple(d["ccd_format"])
        return cls(**d)


# -- jobs -----------------------------------------------------------------------------


class Refused(Exception):
    """A request refused before anything moved (409)."""


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
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
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
        self.readings: dict[str, dict[str, Any]] = {}
        self.faults: list[str] = []
        self._status_task: asyncio.Task | None = None

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
                await self._read("expmeter_total", self.spec.expmeter_total())
                await self._read("ccd_temp", self.camera.ccd_temperature())
        except TimeoutError:
            self.faults.append("status refresh timed out")

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

    def alarms(self) -> list[str]:
        c, out = self.config, []
        get = self.readings.get
        ccd = (get("ccd_temp") or {}).get("value")
        if ccd is not None and not c.ccd_temp_min <= ccd <= c.ccd_temp_max:
            out.append(f"CCD temperature {ccd} C outside {c.ccd_temp_min}..{c.ccd_temp_max}")
        p = (get("spec_pressure") or {}).get("value")
        if p is not None and p > c.max_spec_pressure:
            out.append(f"spectrograph pressure {p} mbar > {c.max_spec_pressure}")
        for key, r in self.readings.items():
            if r.get("error"):
                out.append(f"{key}: {r['error']}")
        return out + self.faults[-5:]

    def status(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "busy": self._held,
            "job": self.current.view() if self.current else None,
            "readings": self.readings,
            "led_on": self.led_on,
            "backlight_on": self.backlight,
            "stage_request": self.last_stage_request,
            "alarms": self.alarms(),
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

    async def move_stage(self, where: str | float) -> StagePosition:
        c = self.config
        if isinstance(where, str):
            if where not in c.stage_positions:
                raise ValueError(f"unknown iodine position {where!r}")
            target = c.stage_positions[where]
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
        prev: float | None = None
        while True:
            pos = await self.spec.stage_position()
            if (
                abs(pos.mm - target) <= c.stage_tolerance
                and prev is not None
                and abs(pos.mm - prev) <= c.stage_tolerance / 10
            ):
                return pos
            prev = pos.mm
            if time.monotonic() >= deadline:
                raise KiwiSpecTimeout(
                    f"iodine stage at {pos.mm} mm, not {where} ({target} mm), after"
                    f" {c.stage_timeout:.0f} s (stuck?)"
                )
            await self._sleep(c.stage_poll)

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
        if on:
            # The backlight would saturate the exposure meter: pause it first
            # (best effort: the legacy server answers 'fail' when the meter is
            # not running).
            with contextlib.suppress(KiwiSpecFailed):
                await self.spec.call("stop_log_expmeter")
            await self.spec.call("backlight_on")
            self.backlight = True
        else:
            try:
                await self.spec.call("backlight_off")
                self.backlight = False
            finally:
                with contextlib.suppress(KiwiSpecFailed):
                    await self.spec.call("start_log_expmeter")

    async def heater(self, on: bool | None, setpoint: float | None) -> None:
        if setpoint is not None:
            await self.spec.cell_heater_set_temp(setpoint)
        if on is True:
            await self.spec.cell_heater_on()
        elif on is False:
            await self.spec.cell_heater_off()

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

    async def ensure_camera(self) -> None:
        if self._camera_ready and self.camera.connected:
            return
        c = self.config
        await self.camera.set_readout_mode(c.readout_mode)
        await self.camera.set_ccd_format(*c.ccd_format)
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
        and the claim, so two requests cannot both get it)."""
        if self._held is not None:
            raise Refused(f"busy ({self._held}); abort it first")
        self._held = what

    @contextlib.asynccontextmanager
    async def hold(self, what: str):
        self._claim(what)
        try:
            yield
        finally:
            self._held = None

    async def configure(self, args: dict[str, Any]) -> dict[str, Any]:
        warnings: list[str] = []
        async with self.hold("configure"):
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
            if value not in self.config.stage_positions:
                raise ValueError(f"unknown iodine position {value!r}")
            return value
        return _num(value, "iodine")

    def parse_frame(self, d: dict[str, Any]) -> Frame:
        c = self.config
        kind = str(d.get("kind", ""))
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
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
        return Frame(kind, exptime, obj, teles, counts, iodine)

    def submit(self, kind: str, frames: list[Frame]) -> Job:
        job_id = next(self._ids)
        self._claim(f"job {job_id}")
        job = Job(job_id, kind, frames)
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
            self._held = None
            job.phase = ""
            job.ended = time.time()
            job.finished.set()
            if job.error:
                log.warning("job %d %s: %s", job.id, job.state, job.error)

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
        )

    async def _drop_camera(self) -> None:
        self._camera_ready = False
        await self.camera.close()

    async def _take(self, job: Job, f: Frame) -> dict[str, Any]:
        c = self.config
        dark, stage, led, uses_meter, _ = KINDS[f.kind]
        warnings: list[str] = []
        job.phase = f"{f.kind}: checking camera"
        await self.ensure_camera()
        ccd = await self.check_ccd()

        job.phase = f"{f.kind}: configuring"
        if self.backlight is not False:
            await self.set_backlight(False)
        await self.set_led(led)
        heater_temp = None
        if f.kind == "science":
            await self.heater(True, c.heater_setpoint)
        where: str | float = stage or (f.iodine if f.iodine is not None else c.default_iodine)
        pos = await self.move_stage(where)
        if f.kind == "science":
            w = await self.wait_heater()
            if w:
                warnings.append(w)
            heater_temp = await self.spec.cell_heater_temp()
        if led and self.led_on_since is not None:
            job.phase = f"{f.kind}: LED warm-up"
            await self._sleep(c.led_warmup - (time.monotonic() - self.led_on_since))

        index = await self.spec.get_index()
        file_name = f"{night()}.{f.object}.{index:04d}.fits"
        await self.spec.call("set_file_name", file_name)

        meter = uses_meter
        if meter:
            try:
                await self.spec.reset_expmeter()
            except KiwiSpecError as exc:
                meter = False
                warnings.append(f"exposure meter unavailable ({exc}); time-terminated")
        if self._abort.is_set():
            raise Aborted(job.abort_reason or "aborted")

        job.phase = f"{f.kind}: exposing"
        acq = await self.camera.start_acquisition(f.exptime, dark=dark)
        self._acquisition = acq
        t0 = time.monotonic()
        start_utc = datetime.datetime.now(datetime.UTC)
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
        midpoint = counts = None
        if meter and len(samples) > 1:
            lag = c.expmeter_period / 2
            shifted = [(samples[0][0], samples[0][1])] + [(t - lag, n) for t, n in samples[1:]]
            midpoint, counts = flux_weighted_midpoint(shifted, 0.0, end)
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
            "expmeter_counts": counts,
            "target_counts": f.target_counts,
            "flux_midpoint_s": midpoint,
            "flux_midpoint_utc": None if mid_utc is None else mid_utc.isoformat(),
            "flux_midpoint_jd": None if mid_utc is None else jd(mid_utc),
            "iodine": {"requested": str(where), "mm": pos.mm, "name": pos.name},
            "led_on": led,
            "heater_temp": heater_temp,
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
        return result

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
            "EXPFLUX": [
                "UNKNOWN" if r["expmeter_counts"] is None else r["expmeter_counts"],
                "Exposure meter flux during exposure in counts",
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
        if r["heater_temp"] is not None:
            h["I2TEMPA"] = [r["heater_temp"], "Iodine Cell Actual Temperature (C)"]
        return json.dumps({k: [clean(x) for x in v] for k, v in h.items()})

    # -- abort / stop -------------------------------------------------------------------

    async def abort(self, reason: str) -> dict[str, Any]:
        """Abort the running job (discarding the frame), bounded by
        ``abort_timeout``; then cancel it if it has not ended."""
        job, task = self.current, self._task
        if job is None or task is None or task.done():
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
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                async with asyncio.timeout(5):
                    await task
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
                " only a loopback spectrograph PC is allowed now"
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
        self.config = config
        self.kiwispec = KiwiSpec(config, spec, camera)
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
                return 202, k.submit("expose", [frame]).view()
            if method == "POST" and parts == ["calibrate"]:
                frames = []
                for spec in args.get("frames") or []:
                    n = int(spec.get("n", 1))
                    if not 1 <= n <= 100:
                        raise ValueError("n must be 1..100")
                    if spec.get("kind") == "science":
                        raise ValueError("a calibration has no science frames")
                    frames += [k.parse_frame(spec) for _ in range(n)]
                if not frames:
                    raise ValueError("no frames")
                return 202, k.submit("calibrate", frames).view()
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
            if method == "POST" and parts == ["backlight"]:
                async with k.hold("backlight"):
                    await k.set_backlight(bool(args["on"]))
                return 200, {"backlight_on": k.backlight}
            raise _HTTPError(404, f"no {method} {url.path}")
        except _HTTPError as exc:
            return exc.status, {"error": str(exc)}
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

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def _call(self, method: str, path: str, body: Any = None, timeout: float | None = None):
        import httpx

        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            r = await client.request(method, self.url + path, json=body)
        data = r.json()
        if r.status_code >= 400:
            raise KiwiSpecError(f"{method} {path}: {r.status_code} {data.get('error')}")
        return data

    async def status(self) -> dict[str, Any]:
        return await self._call("GET", "/status")

    async def expose(self, **frame: Any) -> dict[str, Any]:
        return await self._call("POST", "/expose", frame)

    async def job(self, job_id: int, wait: float = 0.0) -> dict[str, Any]:
        return await self._call("GET", f"/jobs/{job_id}?wait={wait}", timeout=wait + self.timeout)

    async def abort(self, reason: str, *, timeout: float = 45.0) -> bool:
        """Abort whatever the spectrograph is doing; True if nothing is left
        running. Never raises (a cleanup must go on to park and close)."""
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
        help="allow a non-loopback spectrograph PC (the config must also say allow_hardware)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = KiwiSpecConfig.from_dict(json.load(f))
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
