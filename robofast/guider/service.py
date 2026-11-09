"""The guider service: one guide camera's loop, run next to the camera.

Runs on the computer the guide camera is plugged into, so frames never
cross the network in the loop: only commands come in and low-rate
telemetry goes out (docs/guider.md). Plain asyncio HTTP/JSON, like the
other robofast services (design.md 3.1).

The API
-------
All bodies are JSON; errors are ``{"error": ...}`` with 400 (bad request),
404, 409 (refused in this state: already guiding, no target, not
calibrated) or 503 (a device failed).

* ``GET /status`` -- state (``idle``, ``locating``, ``acquiring``,
  ``guiding``, ``lost``, ``failed``, ``stopped``) and why, the target,
  window, controller, tip/tilt, telemetry over the last ``window`` frames
  (residual rms, correction, flux, FWHM, loop rate, latency from exposure
  end to command sent) and alarms.
* ``GET /telemetry?n=N`` -- the last N per-frame records.
* ``GET /values`` -- the service's value registry (``<scope>.guide_*``).
* ``GET /frame?max=N`` -- the last frame the camera took (guide window or
  acquisition frame), binned to at most N pixels a side (default 128, at
  most 1024), as :func:`robofast.frameview.encode_array` JSON, with
  what to draw over it: the target (fiber) and its radius, the last
  centroid and FWHM, the guide window, the state. For people to look at
  (the observer console); 404 before the first frame.
* ``POST /start`` ``{"target"?: [x, y], "mode"?: "closed"|"open",
  "exposure"?: s, "dec"?: deg, "target_radec"?: [ra, dec], "unambiguous"?:
  bool}`` -- acquire and guide; 409 if running. ``dec`` is the target's
  declination (pulse-guide east lengths). ``target_radec`` (deg) lets
  ``select: "catalog"`` find the target by plate solving the acquisition
  frame (needs ``catalog`` and ``solver`` in the config; docs/sky.md);
  ``unambiguous`` allows the brightest-star fallback when it cannot.
  ``"offset_guide"?: {"target": {"ra", "dec", "pmra"?, "pmdec"?, "epoch"?},
  "star": "auto" | {"east", "north"} | {"ra", "dec", "pmra"?, "pmdec"?,
  "epoch"?, "g"?}, "site"?: {"latitude", "longitude", "elevation", ...},
  "pa"?: deg}`` guides on an offset star (docs/offset_guiding.md): the
  guide star is held where the target falls on the fiber, and acquisition
  looks for the guide star (``target_radec`` is ignored). ``"auto"``
  chooses it from ``catalog``. ``GET /status`` reports it
  (``guide_mode``, ``offset_guide``).
* ``POST /stop`` ``{"reason"?}`` -- stop (bounded); always answers.
* ``POST /mode`` ``{"mode"}``, ``POST /target`` ``{"x", "y"}`` (moves the
  setpoint without resetting the controller), ``POST /gains`` ``{"kp",
  "ki", "kd", "deadband", "i_max", "max_step", "max_rate"}`` (any subset),
  ``POST /roi`` ``{"size"}``, ``POST /exposure`` ``{"exposure", "gain"?}``.
* ``POST /locate`` -- find the fiber with the backlight; it becomes the
  target. 409 while guiding.
* ``POST /reconnect`` -- stop guiding (if it is), close the guide camera and
  open it again (the recovery ladder's ``reconnect`` rung for the
  ``guider`` device, docs/recovery.md; legacy ``imager.recover()``'s
  disconnect and reconnect); the state becomes ``idle``. 503 if the camera
  does not open.
* ``POST /calibrate`` ``{"device": "tiptilt"|"mount", "step", "exposure"?,
  "frames"?, "settle"?}`` -- measure and save that device's matrix (for
  this ``port``). 409 while guiding.

Config (JSON; every key optional except as noted)::

    {"name": "t2-guider", "scope": "t2", "listen": "127.0.0.1:8140",
     "allow_hardware": false, "port": "fau",
     "calibration_dir": "/var/lib/robofast/guider",
     "camera": {"kind": "sim" | "alpaca" | "zwo" | "framegrabber", ...},
     "tiptilt": null | {"kind": "sim" | "sxao" | "sxao-sim" | "pi" | "pi-sim", ...},
     "mount": null | {"kind": "alpaca" | "sim", "address", "method": "offset" | "pulseguide",
               "guide_rate": arcsec/s (required with "pulseguide"), ...},
     "offload": null | {"threshold", "period", "min_pixels", "window", "settle"},
     "backlight": null | {"kind": "sim"} | {"kind": <a plug-in's kind>, ...},
     "guide": {...GuideConfig...},
     "sim": {...the simulated sky, when camera.kind is "sim"; an optional "sky"
             section re-points it at each start's target_radec with real catalog
             stars (robofast.testing.skysim.GuiderSky; docs/demo.md)...},
     "redis": null | {"host", "port", "channel"},
     "flux_report": null | {"url", "telescope", "interval"?, "timeout"?}}

Each ``kind`` is a backend registered in :mod:`robofast.guider.hardware`
(a plug-in adds more: docs/plugins.md). MINERVA's FAU backlight is
KiwiSpec's, ``{"kind": "kiwispec", "url": "http://host:port"}``, registered
by the built-in :mod:`robofast.devices` plug-in; the earlier form
``{"<kind>": url}`` still works.

``flux_report`` sends the counts inside the fiber's footprint on every guide
frame to the spectrograph service (``POST /flux``), for the spectrum's
flux-weighted midpoint (:mod:`robofast.guider.flux_report`; it turns
on ``guide.fiber_flux``). ``GET /status`` shows it (``flux_report``).

Safety
------
Every real device backend -- a serial port (SX AO), PI's USB/TCP, a ZWO or
frame-grabber camera, a non-loopback Alpaca camera or telescope -- is
refused unless the config says ``"allow_hardware": true`` **and** the
command line passes ``--hardware`` (as the Aqawan and TCSGO servers).

Running
-------
::

    python -m robofast.guider.service --config guider.json [--hardware]

It prints one JSON line, ``{"url": ...}``, once listening.

The sequencer reaches it through :class:`GuiderRemote`: ``start`` (waits,
bounded, for ``guiding``), ``check`` (raises if the guider has failed),
``stop`` (bounded; never raises) and, while guiding, polling the status into
the sequencer's value registry for the FITS headers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from robofast.cancel import isolated
from robofast.frameview import bin_to, encode_array
from robofast.guider import calibration as calib
from robofast.guider import hardware as hw
from robofast.guider.base import (
    ROI,
    Camera,
    Clock,
    Frame,
    GuiderError,
    HardwareNotAllowed,
    RealClock,
    TipTilt,
    TipTiltError,
)
from robofast.guider.centroid import WindowedCOM
from robofast.guider.flux_report import FluxReportConfig, FluxReporter
from robofast.guider.loop import (
    Backlight,
    GuideConfig,
    Guider,
    publish_values,
)
from robofast.guider.offload import OffloadConfig, Offloader
from robofast.guider.redis_pub import TrackingPublisher
from robofast.guider.sim import (
    Fiber,
    Jitter,
    SimWorld,
)
from robofast.guider.tiptilt import (
    MountOffset,
)
from robofast.registry import ValueRegistry

log = logging.getLogger(__name__)

MAX_BODY = 1 << 16


@dataclass
class GuiderServiceConfig:
    name: str = "guider"
    scope: str = "guider"
    listen: str = "127.0.0.1:8140"
    allow_hardware: bool = False
    port: str = "default"
    calibration_dir: str | None = None
    camera: dict[str, Any] = field(default_factory=lambda: {"kind": "sim"})
    tiptilt: dict[str, Any] | None = None
    mount: dict[str, Any] | None = None
    offload: dict[str, Any] | None = None
    backlight: dict[str, Any] | None = None
    guide: dict[str, Any] = field(default_factory=dict)
    sim: dict[str, Any] = field(default_factory=dict)
    redis: dict[str, Any] | None = None
    #: Inline calibrations (``{"tiptilt": {...}, "mount": {...}}``), used
    #: when the calibration directory has none.
    calibrations: dict[str, Any] = field(default_factory=dict)
    #: For ``guide.select: "catalog"`` (docs/sky.md): the star catalog
    #: (:func:`robofast.catalog.catalog_from_config`) and the plate
    #: solver's settings for this camera (:class:`robofast.astrometry.SolverConfig`).
    catalog: dict[str, Any] | None = None
    solver: dict[str, Any] | None = None
    #: The port's PA calibration (docs/rotcal.md): ``{"store": path,
    #: "telescope": name}``. Without a measured mount calibration, the
    #: mount's matrix is seeded from it (PA, parity, plate scale), and it
    #: gives the Redis telemetry its PA.
    pa_calibration: dict[str, Any] | None = None
    #: The acquisition cascade's thresholds (docs/acquisition.md;
    #: :class:`~robofast.guider.acquire.AcqConfig`). Every start then
    #: runs the cascade (``mode`` from the target, default AUTO).
    acquisition: dict[str, Any] | None = None
    #: Offset guiding (docs/offset_guiding.md): ``update`` (s between
    #: setpoint updates), ``refraction`` (correct differential refraction,
    #: default true), ``field_rotation`` (``"none"``: an equatorial mount or
    #: a derotated port; ``"altaz"`` with ``sense``), ``wavelength`` (um,
    #: the guide camera's band) and ``auto`` (choosing the guide star:
    #: :class:`~robofast.guider.offset.AutoConfig`).
    offset_guide: dict[str, Any] = field(default_factory=dict)
    #: Report the fiber flux to the spectrograph service
    #: (:class:`~robofast.guider.flux_report.FluxReportConfig`).
    flux_report: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuiderServiceConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"guider config: unknown keys {sorted(unknown)}")
        return cls(**d)


# -- building the parts ------------------------------------------------------------------


def build_world(spec: dict[str, Any], clock: Clock) -> SimWorld:
    """The simulated sky from the ``sim`` section (:class:`SimWorld`
    fields; ``jitter`` and ``fiber`` as dicts of their fields)."""
    s = dict(spec)
    s.pop("sky", None)  # the catalog sky (GuiderService; robofast.testing.skysim.GuiderSky)
    jit = s.pop("jitter", None)
    fib = s.pop("fiber", None)
    ttm = s.pop("tt_matrix", None)
    for k in ("sensor", "star", "rotation"):
        if k in s:
            s[k] = tuple(s[k])
    world = SimWorld(
        clock=clock,
        jitter=Jitter(**jit) if jit else None,
        fiber=Fiber(**fib) if fib else None,
        **s,
    )
    if ttm is not None:
        world.tt_matrix = np.asarray(ttm, dtype=float)
    return world


@dataclass
class Parts:
    camera: Camera
    tiptilt: TipTilt | None
    mount: MountOffset | None
    world: SimWorld | None
    backlight: Backlight | None
    closers: list[Any] = field(default_factory=list)


def build_parts(cfg: GuiderServiceConfig, *, allowed: bool, clock: Clock) -> Parts:
    """Every part, each from its registered backend
    (:mod:`robofast.guider.hardware`), with the hardware gate applied
    (module doc). The camera first: a simulated one makes the simulated sky
    the other parts join."""
    ctx = hw.BuildContext(allowed=allowed, clock=clock, sim=cfg.sim)

    def build(registry: Any, part: str, spec: Any, default: str | None = None) -> Any:
        kind, rest = hw.spec_kind(spec, part, default)
        return registry.lookup(kind, f"guider config: {part}.kind")(rest, ctx)

    camera: Camera = build(hw.CAMERAS, "camera", cfg.camera, "sim")
    tiptilt: TipTilt | None = build(hw.TIPTILTS, "tiptilt", cfg.tiptilt) if cfg.tiptilt else None
    mount: MountOffset | None = None
    if cfg.mount:
        mk, m = hw.spec_kind(cfg.mount, "mount", "alpaca")
        driver = hw.MOUNTS.lookup(mk, "guider config: mount.kind")(m, ctx)
        mount = MountOffset(
            driver,
            method=str(m.get("method", "offset")),
            action=str(m.get("action", "Offset")),
            guide_rate=None if m.get("guide_rate") is None else float(m["guide_rate"]),
            max_offset=float(m.get("max_offset", 30.0)),
            timeout=float(m.get("timeout", 5.0)),
        )
        mount.clock = clock
    backlight: Backlight | None = (
        build(hw.BACKLIGHTS, "backlight", cfg.backlight) if cfg.backlight else None
    )
    return Parts(camera, tiptilt, mount, ctx.world, backlight)


# -- the service ------------------------------------------------------------------------


class FrameTap:
    """A camera that keeps its last frame (for ``GET /frame``); otherwise
    the camera itself."""

    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self.last: Frame | None = None
        self.count = 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._camera, name)

    async def expose(self, exposure: float, roi: ROI | None, gain: float | None) -> Frame:
        frame = await self._camera.expose(exposure, roi, gain)
        self.last = frame
        self.count += 1
        return frame


class _HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GuiderService:
    """The parts, the :class:`~robofast.guider.loop.Guider` and the
    HTTP API (module doc)."""

    def __init__(
        self,
        config: GuiderServiceConfig,
        *,
        hardware: bool = False,
        clock: Clock | None = None,
        redis_client: Any = None,
    ) -> None:
        self.config = config
        allowed = config.allow_hardware and hardware
        if config.allow_hardware != hardware:
            log.info(
                "hardware use needs allow_hardware in the config AND --hardware;"
                " only simulators and loopback devices are allowed now"
            )
        self.clock = clock or RealClock()
        self.parts = build_parts(config, allowed=allowed, clock=self.clock)
        if allowed:
            log.warning("HARDWARE MODE: guider %s", config.name)
        self.registry = ValueRegistry()
        self.store = (
            calib.CalibrationStore(config.calibration_dir) if config.calibration_dir else None
        )
        gcfg = GuideConfig.from_dict({"scope": config.scope, **config.guide})
        flux_cfg = (
            None if config.flux_report is None else FluxReportConfig.from_dict(config.flux_report)
        )
        if flux_cfg is not None:
            gcfg.fiber_flux = True
        self.pa_cal = self._pa_calibration()
        publisher = None
        if redis_client is not None or config.redis:
            r = config.redis or {}
            kw = {
                "channel": r.get("channel", "tracking_data"),
                "plate_scale": gcfg.plate_scale,
                "pa": float(r.get("pa", self.pa_cal.guider_pa(0.0) if self.pa_cal else 0.0)),
            }
            publisher = (
                TrackingPublisher(redis_client, **kw)
                if redis_client is not None
                else TrackingPublisher.connect(
                    r.get("host", "127.0.0.1"), int(r.get("port", 6379)), **kw
                )
            )
        p = self.parts
        #: The camera, keeping its last frame (``GET /frame``).
        self.tap = FrameTap(p.camera)
        p.camera = self.tap  # type: ignore[assignment]
        self.guider = Guider(
            p.camera,
            gcfg,
            tiptilt=p.tiptilt,
            mount=p.mount,
            backlight=p.backlight,
            clock=self.clock,
            registry=self.registry,
            publisher=publisher,
        )
        self.flux_reporter = None if flux_cfg is None else FluxReporter(self.guider, flux_cfg)
        self._load_calibrations()
        self._catalog: Any = None
        self._solver: Any = None
        if config.solver is not None:
            from robofast.astrometry import SolverConfig
            from robofast.catalog import catalog_from_config

            self._solver = SolverConfig.from_dict(config.solver)
            self._catalog = catalog_from_config(config.catalog)
        #: Opt-in simulator sky from a real catalog (``sim.sky``): re-pointed
        #: at each start's ``target_radec`` (robofast.testing.skysim.GuiderSky).
        self._sky: Any = None
        self.sim_sky: dict[str, Any] | None = None
        if p.world is not None and config.sim.get("sky"):
            from robofast.testing.skysim import GuiderSky

            fib = p.world.fiber
            self._sky = GuiderSky.from_config(
                config.sim["sky"],
                sensor=p.world.sensor,
                fiber=None if fib is None else (fib.x, fib.y),
            )
        host, _, port = config.listen.rpartition(":")
        self.host, self.port = host, int(port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._busy: str | None = None

    def _load_calibrations(self) -> None:
        g = self.guider
        for role, dev in (("tiptilt", g.tiptilt), ("mount", g.mount)):
            if dev is None:
                continue
            cal = self.store.load(dev.name, self.config.port) if self.store else None
            if cal is None and role in self.config.calibrations:
                cal = calib.Calibration.from_dict(
                    {"device": dev.name, "port": self.config.port, **self.config.calibrations[role]}
                )
            if cal is None and role == "mount" and self.pa_cal is not None:
                cal = self._seed_mount(dev.name)
            if role == "tiptilt":
                g.tt_cal = cal
            else:
                g.mount_cal = cal
        self._rebuild_offloader()

    def _pa_calibration(self) -> Any:
        """The port's PA calibration, if configured and measured."""
        pc = self.config.pa_calibration
        if not pc:
            return None
        from robofast.rotcal import PAStore

        try:
            return PAStore(pc["store"]).get(pc["telescope"], self.config.port)
        except (OSError, ValueError, KeyError) as exc:
            log.error("PA calibration unreadable: %s", exc)
            return None

    def _seed_mount(self, device: str) -> calib.Calibration | None:
        """A mount calibration from the PA calibration (docs/rotcal.md)."""
        from robofast.rotcal import seed_mount_matrix

        cal = self.pa_cal
        ps = self.guider.config.plate_scale or cal.plate_scale
        if not ps:
            return None
        return calib.Calibration(
            device,
            self.config.port,
            seed_mount_matrix(cal, ps).tolist(),
            units="arcsec",
            plate_scale=ps,
            pa=cal.guider_pa(0.0),
            parity=cal.parity,
            notes=f"seeded from the PA calibration measured {cal.measured}",
        )

    def _rebuild_offloader(self) -> None:
        g = self.guider
        if (
            self.config.offload is not None
            and g.tiptilt is not None
            and g.mount is not None
            and g.tt_cal is not None
            and g.mount_cal is not None
        ):
            g.offloader = Offloader(
                g.tiptilt,
                g.tt_cal,
                g.mount,
                g.mount_cal,
                OffloadConfig(**self.config.offload),
                clock=self.clock,
            )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> GuiderService:
        p = self.parts
        await p.camera.open()
        for dev in (p.tiptilt, p.mount):
            if dev is not None:
                await dev.open()
        self._server = await asyncio.start_server(self._connection, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.flux_reporter is not None:
            self.flux_reporter.start()
        return self

    async def reconnect_camera(self) -> None:
        """Stop guiding, close the camera and open it again (``POST
        /reconnect``); raises :class:`GuiderError` if it does not open."""
        g = self.guider
        if g.running:
            await g.stop("reconnecting the camera")
        cam = self.parts.camera
        with contextlib.suppress(Exception):
            await cam.close()
        try:
            await cam.open()
        except GuiderError:
            g._set_state("failed", "camera did not reopen")
            raise
        except Exception as exc:
            g._set_state("failed", f"camera did not reopen: {exc!r}")
            raise GuiderError(f"camera did not reopen: {type(exc).__name__}: {exc}") from exc
        g._set_state("idle", "camera reconnected")
        log.warning("guider %s: camera reconnected", self.config.scope)

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.guider.stop("service stopping")
        if self.flux_reporter is not None:
            await self.flux_reporter.stop()
        p = self.parts
        for dev in (p.tiptilt, p.mount, p.camera):
            if dev is not None:
                with contextlib.suppress(Exception):
                    await dev.close()
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await self._server.wait_closed()

    # -- HTTP ---------------------------------------------------------------------------

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
                if n > MAX_BODY:
                    break
                body = await reader.readexactly(n) if n else b""
                status, payload = await self._respond(method, target, body)
                data = json.dumps(payload, default=_json_default).encode()
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

    def status(self) -> dict[str, Any]:
        s = self.guider.status()
        s["name"] = self.config.name
        s["scope"] = self.config.scope
        s["port"] = self.config.port
        s["busy"] = self._busy
        if self._sky is not None:
            s["sim_sky"] = self.sim_sky
        s["flux_report"] = None if self.flux_reporter is None else self.flux_reporter.status()
        s["calibrations"] = {
            "tiptilt": None if self.guider.tt_cal is None else self.guider.tt_cal.as_json(),
            "mount": None if self.guider.mount_cal is None else self.guider.mount_cal.as_json(),
        }
        return s

    def frame(self, max_side: int = 128) -> dict[str, Any]:
        """The last frame, binned, with its overlays (module doc)."""
        f = self.tap.last
        if f is None:
            raise _HTTPError(404, "no frame yet")
        g = self.guider
        binned, factor = bin_to(f.data, max(8, min(int(max_side), 1024)))
        m = g.last
        star = None
        if m is not None and m.valid and math.isfinite(m.x):
            star = {"x": m.x, "y": m.y, "fwhm": m.fwhm if math.isfinite(m.fwhm) else None}
        return {
            **encode_array(binned),
            "binning": factor,
            "roi": f.roi.as_json(),
            "count": self.tap.count,
            "exposure": f.exposure,
            "utc_end": None if f.utc_end is None else f.utc_end.isoformat(),
            "state": g.state,
            "target": None if g.config.target is None else list(g.config.target),
            "fiber_radius": g.config.fiber_radius,
            "star": star,
            "guide_roi": None if g._roi is None else g._roi.as_json(),
            "scope": self.config.scope,
        }

    async def _respond(self, method: str, target: str, body: bytes) -> tuple[int, Any]:
        g = self.guider
        url = urlparse(target)
        query = {key: v[0] for key, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            args = json.loads(body) if body else {}
            if not isinstance(args, dict):
                raise ValueError("the body must be a JSON object")
            if method == "GET" and parts == ["status"]:
                return 200, self.status()
            if method == "GET" and parts == ["telemetry"]:
                n = max(1, min(int(query.get("n", 100)), len(g.records) or 1))
                return 200, {"records": [asdict(r) for r in list(g.records)[-n:]]}
            if method == "GET" and parts == ["values"]:
                return 200, self.registry.as_json()
            if method == "GET" and parts == ["frame"]:
                return 200, self.frame(int(query.get("max", 128)))
            if method != "POST":
                raise _HTTPError(404, f"no {method} {url.path}")
            if parts == ["stop"]:
                stopped = await g.stop(str(args.get("reason") or "requested"))
                return 200, {"stopped": stopped, "state": g.state}
            if parts == ["start"]:
                self._refuse_if_busy()
                if g.running:
                    raise _HTTPError(409, "already guiding")
                if "target" in args:
                    x, y = (float(v) for v in args["target"])
                    g.set_target(x, y)
                if "mode" in args:
                    g.set_mode(str(args["mode"]))
                if "exposure" in args:
                    g.config.exposure = _positive(args["exposure"], 60.0)
                if "dec" in args and g.mount is not None:
                    g.mount.dec_deg = float(args["dec"])
                g.clear_offset()
                if self._sky is not None and args.get("target_radec") is not None:
                    ra, dec = (float(v) for v in args["target_radec"])
                    await asyncio.to_thread(self._point_sky, ra, dec)
                if args.get("offset_guide"):
                    args = self._set_offset(args)
                self._set_identifier(args)
                try:
                    g.start()
                except GuiderError as exc:
                    raise _HTTPError(409, str(exc)) from exc
                return 200, self.status()
            if parts == ["mode"]:
                g.set_mode(str(args["mode"]))
                return 200, {"mode": g.config.mode}
            if parts == ["target"]:
                g.set_target(float(args["x"]), float(args["y"]))
                return 200, {"target": list(g.config.target or ())}
            if parts == ["gains"]:
                g.set_gains({k: (None if v is None else float(v)) for k, v in args.items()})
                return 200, {"gains": g.controller.gains.as_json()}
            if parts == ["roi"]:
                g.set_roi_size(int(args["size"]))
                return 200, {"roi_size": g.config.roi_size}
            if parts == ["exposure"]:
                g.config.exposure = _positive(args["exposure"], 60.0)
                if "gain" in args:
                    g.config.gain = None if args["gain"] is None else float(args["gain"])
                return 200, {"exposure": g.config.exposure, "gain": g.config.gain}
            if parts == ["reconnect"]:
                self._refuse_if_busy()
                async with self._hold("reconnect"):
                    await self.reconnect_camera()
                return 200, self.status()
            if parts == ["locate"]:
                self._refuse_if_busy()
                if g.running:
                    raise _HTTPError(409, "stop guiding before locating the fiber")
                async with self._hold("locate"):
                    x, y = await g.locate_fiber()
                return 200, {"target": [x, y]}
            if parts == ["calibrate"]:
                self._refuse_if_busy()
                if g.running:
                    raise _HTTPError(409, "stop guiding before calibrating")
                async with self._hold("calibrate"):
                    cal = await self.calibrate(args)
                return 200, cal.as_json()
            raise _HTTPError(404, f"no {method} {url.path}")
        except _HTTPError as exc:
            return exc.status, {"error": str(exc)}
        except HardwareNotAllowed as exc:
            return 409, {"error": str(exc)}
        except (GuiderError, TipTiltError, TimeoutError) as exc:
            return 503, {"error": f"{type(exc).__name__}: {exc}"}
        except (ValueError, TypeError, KeyError) as exc:
            return 400, {"error": f"bad request: {exc!r}"}

    def _point_sky(self, ra: float, dec: float) -> None:
        """The simulated sky's star field around the target (``sim.sky``).
        If the catalog cannot answer, a lone star at the same place (the
        synthetic field), logged."""
        world = self.parts.world
        assert world is not None and self._sky is not None
        try:
            main, others, info = self._sky.field(ra, dec, datetime.now(UTC), world.flux)
        except Exception as exc:
            log.warning("sim sky: no catalog field at RA %.5f Dec %+.5f (%r)", ra, dec, exc)
            main, others = (*self._sky.centre, world.flux), []
            info = {"ra": ra, "dec": dec, "error": repr(exc), "field_stars": 0}
        world.star = (main[0], main[1])
        world.others = others
        self.sim_sky = info
        log.info("sim sky: %s", info)

    def _set_offset(self, args: dict[str, Any]) -> dict[str, Any]:
        """Offset guiding for this start (module doc): choose or take the
        guide star, set the setpoint, and acquire the guide star instead of
        the target (returns ``args`` with ``target_radec`` the guide star's)."""
        from robofast.guider import offset as og

        g = self.guider
        spec = dict(args["offset_guide"])
        known = {"target", "star", "site", "pa"}
        if set(spec) - known:
            raise ValueError(f"offset_guide: unknown keys {sorted(set(spec) - known)}")
        conf = dict(self.config.offset_guide)
        target = og.TargetPosition.from_dict(spec["target"])
        site = None
        if spec.get("site"):
            site = og.OffsetSite.from_dict(
                {"wavelength": conf.get("wavelength", 0.65), **spec["site"]}
            )
        pa_cal = self.pa_cal
        scale = g.config.plate_scale or (pa_cal.plate_scale if pa_cal else None)
        if not scale:
            raise _HTTPError(409, "offset guiding needs the guide camera's plate_scale")
        pa = (
            float(spec["pa"])
            if spec.get("pa") is not None
            else (pa_cal.pa(0.0) if pa_cal else None)
        )
        if pa is None:
            raise _HTTPError(409, "offset guiding needs the port's PA (pa_calibration or 'pa')")
        flip = False if pa_cal is None else bool(pa_cal.flip)
        fiber = g.fiber or g.config.target
        if fiber is None:
            raise _HTTPError(409, "no fiber pixel: configure it or locate the fiber")
        now = datetime.now(UTC)
        star = og.guide_star_from_spec(spec.get("star", "auto"))
        if star is None:
            if self._catalog is None:
                raise _HTTPError(409, "offset_star 'auto' needs a catalog in the guider config")
            star, notes = self._choose_star(target, now, fiber, scale, pa, flip, conf)
            if star is None:
                raise _HTTPError(
                    409,
                    "no usable offset guide star in the field" + (f": {notes}" if notes else ""),
                )
        offset = og.OffsetGuide(
            target,
            star,
            float(scale),
            float(pa),
            flip,
            site=site,
            refraction=bool(conf.get("refraction", True)),
            field_rotation=str(conf.get("field_rotation", "none")),
            sense=int(conf.get("sense", 1)),
            update=float(conf.get("update", 10.0)),
            start=now,
        )
        t0 = self.clock.now()
        g.set_offset(offset, lambda t: now + timedelta(seconds=t - t0))
        ra, dec = star.radec(target, now)
        return {**args, "target_radec": [ra, dec]}

    def _choose_star(
        self, target: Any, now: datetime, fiber: Any, scale: float, pa: float, flip: bool, conf: Any
    ) -> tuple[Any, list[str]]:
        """``offset_star: "auto"``: the best catalog star in the field."""
        from robofast.catalog import CatalogUnavailable
        from robofast.guider import offset as og

        nx, ny = self.guider.camera.sensor
        radius = math.hypot(nx, ny) * scale / 3600.0
        ra, dec = target.at(now)
        auto = og.AutoConfig.from_dict(conf.get("auto"))
        try:
            stars = self._catalog.cone(ra, dec, radius, epoch=now, mag_limit=auto.g_faint)
        except CatalogUnavailable as exc:
            raise _HTTPError(503, f"offset_star 'auto': no catalog ({exc})") from exc
        star, notes = og.choose_guide_star(
            stars,
            target,
            now,
            fiber=(float(fiber[0]), float(fiber[1])),
            sensor=(nx, ny),
            plate_scale=scale,
            pa=pa,
            flip=flip,
            config=auto,
        )
        for n in notes:
            log.info("offset star candidate %s", n)
        return star, notes

    def _set_identifier(self, args: dict[str, Any]) -> None:
        """Catalog acquisition (``select: "catalog"``) for this start's
        target: ``target_radec`` [ra, dec] deg and ``unambiguous`` (from
        vetting: brightest-star fallback allowed)."""
        g = self.guider
        g.unambiguous = bool(args.get("unambiguous", False))
        g.identify = None
        g.acquirer = None
        radec = args.get("target_radec")
        if radec is not None and self._solver is not None:
            from robofast.guider.identify import catalog_identifier

            ra, dec = (float(v) for v in radec)
            g.identify = catalog_identifier(self._catalog, self._solver, ra, dec)
        acq = args.get("acquisition")
        if self.config.acquisition is not None or acq is not None:
            g.acquirer = self._acquirer(dict(acq or {}), radec, args.get("pointing_sigma"))

    def _acquirer(self, acq: dict[str, Any], radec: Any, pointing_sigma: Any) -> Any:
        """The acquisition cascade for this target (docs/acquisition.md)."""
        from robofast.guider.acquire import AcqConfig, Acquirer, Neighbour

        g = self.guider
        cfg = AcqConfig.from_dict(self.config.acquisition)
        if acq.get("tolerance") is not None:
            cfg.tolerance = float(acq["tolerance"])
        pa_cal = self.pa_cal
        scale = g.config.plate_scale or (pa_cal.plate_scale if pa_cal else None)
        neighbours: list[Neighbour] = []
        target_g = acq.get("target_g")
        if acq.get("companions"):
            # Given with the target: offsets east/north (arcsec) or positions.
            for c in acq["companions"]:
                if "east" in c:
                    neighbours.append(Neighbour(float(c["east"]), float(c["north"]), float(c["g"])))
                elif radec is not None:
                    from robofast.catalog import Stars
                    from robofast.guider.acquire import neighbours_from_catalog

                    s = Stars.from_records(_records([c]))
                    neighbours += neighbours_from_catalog(s, float(radec[0]), float(radec[1]), None)
        elif radec is not None and self._catalog is not None and scale:
            neighbours, target_g = self._catalog_neighbours(radec, scale, target_g)
        return Acquirer(
            predicted=tuple(g.config.target) if g.config.target else (0.0, 0.0),
            target_g=None if target_g is None else float(target_g),
            neighbours=neighbours,
            scale=scale,
            pa=None if pa_cal is None else pa_cal.pa(0.0),
            flip=False if pa_cal is None else pa_cal.flip,
            pointing_sigma=None if pointing_sigma is None else float(pointing_sigma),
            solve=g.identify,
            mode=str(acq.get("mode") or "AUTO"),
            fallback=bool(acq.get("fallback", False)),
            config=cfg,
        )

    def _catalog_neighbours(self, radec: Any, scale: float, target_g: Any) -> tuple[list, Any]:
        """Catalog stars around the target within the camera's reach."""
        import math as _m

        from robofast.catalog import CatalogUnavailable, separation
        from robofast.guider.acquire import neighbours_from_catalog

        ra, dec = float(radec[0]), float(radec[1])
        nx, ny = self.guider.camera.sensor
        radius = _m.hypot(nx, ny) * scale / 3600.0
        try:
            s = self._catalog.cone(ra, dec, radius, epoch=datetime.now(UTC))
        except CatalogUnavailable as exc:
            log.warning("acquisition: no catalog (%s)", exc)
            return [], target_g
        if not len(s):
            return [], target_g
        d = separation(ra, dec, s.ra, s.dec) * 3600.0
        k = int(np.argmin(d))
        if d[k] <= 3.0:
            if target_g is None:
                target_g = float(s.g[k])
            s = s.take(np.arange(len(s)) != k)
        return neighbours_from_catalog(s, ra, dec, target_g), target_g

    def _refuse_if_busy(self) -> None:
        if self._busy is not None:
            raise _HTTPError(409, f"busy: {self._busy}")

    @contextlib.asynccontextmanager
    async def _hold(self, what: str):
        self._busy = what
        try:
            yield
        finally:
            self._busy = None

    async def calibrate(self, args: dict[str, Any]) -> calib.Calibration:
        """Measure one device's matrix around the current star (module doc)."""
        g = self.guider
        which = str(args.get("device", "tiptilt"))
        dev = g.tiptilt if which == "tiptilt" else g.mount if which == "mount" else None
        if dev is None:
            raise _HTTPError(409, f"no {which} to calibrate")
        c = g.config
        size = int(args.get("roi", max(c.roi_size * 2, 64)))
        centre = c.target
        if centre is None:
            frame = await g.camera.expose(c.acq_exposure, g._acq_roi(), c.gain)
            m = WindowedCOM().measure(frame, None)
            if not m.valid:
                raise GuiderError(f"no star to calibrate on: {m.reason}")
            centre = (m.x, m.y)
        roi = ROI.centred(centre[0], centre[1], size, g.camera.sensor)
        cal = await calib.measure(
            g.camera,
            WindowedCOM(),
            dev,
            step=float(args["step"]),
            exposure=_positive(args.get("exposure", c.exposure * 5), 60.0),
            roi=roi,
            frames=int(args.get("frames", 10)),
            settle=float(args.get("settle", 0.2 if which == "tiptilt" else 3.0)),
            port=self.config.port,
            clock=self.clock,
            gain=c.gain,
        )
        if which == "tiptilt":
            g.tt_cal = cal
        else:
            g.mount_cal = cal
        if self.store is not None:
            path = self.store.save(cal)
            log.info("calibration of %s saved to %s", dev.name, path)
        self._rebuild_offloader()
        return cal


def _positive(v: Any, top: float) -> float:
    x = float(v)
    if not (0 < x <= top):
        raise ValueError(f"{x} not in (0, {top}]")
    return x


def _json_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return str(o)


# -- the client the sequencer uses ------------------------------------------------------


class GuiderTimeout(GuiderError):
    """The guider service did not answer in time (a transport timeout,
    not a refusal): transient while it is busy, so :class:`GuiderRemote`
    retries status reads before giving up."""


class GuiderRemote:
    """The service's API, for the sequencer (its ``guider`` hook).

    * :meth:`start` -- start guiding and wait (``acquire_timeout``) for
      ``guiding``; raises :class:`GuiderError` otherwise (after stopping
      it), which is that telescope's ordinary error.
    * :meth:`check` -- raises if the guider has failed or stopped on its
      own since :meth:`start`.
    * a status read that times out is transient (a busy service, a loaded
      computer; night 1107): while acquiring, :meth:`start` keeps asking
      until ``acquire_timeout``; while guiding, :meth:`check` passes until
      ``status_failures`` reads in a row have timed out. A refusal, a
      connection refused or a ``failed`` state still fails at once.
    * :meth:`stop` -- bounded and never raises (cleanup must go on to park).
    * with a ``registry``, a background task copies the guider's state into
      it every ``poll_interval`` while guiding (the FITS headers).
    """

    def __init__(
        self,
        url: str,
        *,
        scope: str,
        registry: Any = None,
        timeout: float = 5.0,
        acquire_timeout: float = 120.0,
        poll_interval: float = 2.0,
        status_failures: int = 3,
    ) -> None:
        self.url = url.rstrip("/")
        self.scope = scope
        self.registry = registry
        self.timeout = timeout
        self.acquire_timeout = acquire_timeout
        self.poll_interval = poll_interval
        self.status_failures = max(1, int(status_failures))
        self._status_misses = 0
        self._poll: asyncio.Task | None = None
        self.started = False

    async def _call(
        self, method: str, path: str, body: Any = None, timeout: float | None = None
    ) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=timeout or self.timeout, trust_env=False
            ) as client:
                r = await isolated(client.request(method, self.url + path, json=body))
        except httpx.TimeoutException as exc:
            raise GuiderTimeout(f"guider {method} {path}: {type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GuiderError(f"guider {method} {path}: {type(exc).__name__}: {exc}") from exc
        data = r.json()
        if r.status_code >= 400:
            raise GuiderError(f"guider {method} {path}: {r.status_code} {data.get('error')}")
        return data

    async def status(self) -> dict[str, Any]:
        return await self._call("GET", "/status")

    async def start(self, **params: Any) -> dict[str, Any]:
        await self._call("POST", "/start", params)
        self.started = True
        self._status_misses = 0
        deadline = asyncio.get_running_loop().time() + self.acquire_timeout
        misses = 0
        while True:
            try:
                st = await self.status()
            except GuiderTimeout as exc:
                # Busy acquiring (or a loaded computer): ask again, until
                # acquire_timeout says it is not coming.
                misses += 1
                log.warning("guider %s status (acquiring, %d): %s", self.scope, misses, exc)
                if asyncio.get_running_loop().time() > deadline:
                    await self.stop("acquisition timed out")
                    raise GuiderError(
                        f"guider {self.scope}: not guiding after {self.acquire_timeout} s"
                        f" (status unanswered: {exc})"
                    ) from exc
                await asyncio.sleep(0.2)
                continue
            if st["state"] == "guiding":
                break
            if st["state"] in ("failed", "stopped", "idle"):
                await self.stop("acquisition failed")
                raise GuiderError(f"guider {self.scope}: {st['state']}: {st.get('reason')}")
            if asyncio.get_running_loop().time() > deadline:
                await self.stop("acquisition timed out")
                raise GuiderError(
                    f"guider {self.scope}: not guiding after {self.acquire_timeout} s"
                    f" ({st['state']}: {st.get('reason')})"
                )
            await asyncio.sleep(0.2)
        self._publish(st)
        if self.registry is not None:
            self._poll = asyncio.create_task(self._poll_loop(), name=f"guider-poll-{self.scope}")
        return st

    async def reconnect(self) -> dict[str, Any]:
        """Close and open the guide camera again (``POST /reconnect``; the
        recovery ladder's ``guider`` reconnect rung). Raises
        :class:`GuiderError` (bounded by ``acquire_timeout``)."""
        self.started = False
        bound = max(self.timeout, self.acquire_timeout)
        try:
            async with asyncio.timeout(bound):
                return await self._call("POST", "/reconnect", {}, timeout=bound)
        except TimeoutError as exc:
            raise GuiderError(f"guider {self.scope}: camera not reconnected in {bound} s") from exc

    async def locate(self, timeout: float | None = None) -> tuple[float, float]:
        """Find the fiber with the backlight (``POST /locate``); it becomes
        the guide target. Raises :class:`GuiderError` (bounded)."""
        bound = timeout if timeout is not None else self.acquire_timeout
        try:
            async with asyncio.timeout(bound):
                data = await self._call("POST", "/locate", {}, timeout=bound)
        except TimeoutError as exc:
            raise GuiderError(f"guider {self.scope}: fiber not located in {bound} s") from exc
        x, y = data["target"]
        return float(x), float(y)

    async def check(self) -> None:
        """Raise :class:`GuiderError` if the guider gave up (failed) or
        stopped by itself since :meth:`start`; ``lost`` and ``acquiring``
        are its own re-acquisition and pass."""
        if not self.started:
            return
        try:
            st = await self.status()
        except GuiderTimeout as exc:
            self._status_misses += 1
            if self._status_misses >= self.status_failures:
                raise GuiderError(
                    f"guider {self.scope}: status unanswered"
                    f" {self._status_misses} times in a row ({exc})"
                ) from exc
            log.warning(
                "guider %s status (%d of %d): %s",
                self.scope,
                self._status_misses,
                self.status_failures,
                exc,
            )
            return
        self._status_misses = 0
        self._publish(st)
        if st["state"] in ("failed", "stopped", "idle"):
            raise GuiderError(f"guider {self.scope}: {st['state']}: {st.get('reason')}")

    async def stop(self, reason: str = "requested") -> bool:
        """Stop guiding. Bounded; never raises; True if it stopped."""
        self.started = False
        poll, self._poll = self._poll, None
        if poll is not None:
            # Bounded, and never swallowing our own cancellation: a cancel
            # that lands inside an httpx request can be lost (the request
            # returns normally), so the poll task may outlive its cancel;
            # it also ends by itself once it is no longer ``self._poll``.
            poll.cancel()
            await asyncio.wait({poll}, timeout=self.timeout)
            if poll.done() and not poll.cancelled():
                poll.exception()  # retrieved (it only ever logs)
        try:
            async with asyncio.timeout(self.timeout):
                data = await self._call("POST", "/stop", {"reason": reason})
        except Exception as exc:
            log.error("guider %s stop (%s) failed: %r", self.scope, reason, exc)
            return False
        if self.registry is not None:
            with contextlib.suppress(Exception):
                self._publish({"state": data.get("state", "stopped"), "telemetry": {}})
        return bool(data.get("stopped", False))

    async def _poll_loop(self) -> None:
        me = asyncio.current_task()
        # Ends when stop() replaces it, even if its cancel was lost (stop()).
        while self._poll is me:
            await asyncio.sleep(self.poll_interval)
            if self._poll is not me:
                return
            try:
                self._publish(await self.status())
            except Exception as exc:
                log.warning("guider %s status: %r", self.scope, exc)
                if self.registry is not None:
                    self.registry.failed(f"{self.scope}.guide_state", repr(exc))

    def _publish(self, st: dict[str, Any]) -> None:
        if self.registry is None:
            return
        tt = st.get("tiptilt")
        offset = {"guide_mode": st.get("guide_mode") or "target", **(st.get("offset_guide") or {})}
        publish_values(
            self.registry,
            self.scope,
            st.get("state", "?"),
            st.get("telemetry") or {},
            target=st.get("fiber") or st.get("target"),
            offset=offset if "guide_mode" in st else None,
            exposure=st.get("exposure"),
            tiptilt_position=None if not tt else tt.get("position"),
            max_age=max(3 * self.poll_interval, 10.0),
            measured=datetime.now(UTC),
        )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow real devices (the config must also say allow_hardware)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = GuiderServiceConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen

    async def run() -> None:
        try:
            service = GuiderService(config, hardware=args.hardware)
        except HardwareNotAllowed as exc:
            log.error("%s", exc)
            sys.exit(2)
        await service.start()
        print(json.dumps({"url": service.url}), flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await service.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()


def _records(cs: list[dict[str, Any]]) -> np.ndarray:
    """Companions given as ``{"ra", "dec", "g"}`` (deg) as catalog records."""
    from robofast.catalog import TILE_DTYPE

    rec = np.zeros(len(cs), TILE_DTYPE)
    for i, c in enumerate(cs):
        rec[i]["ra"], rec[i]["dec"], rec[i]["g"] = float(c["ra"]), float(c["dec"]), float(c["g"])
        rec[i]["source_id"] = -1 - i
    return rec
