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
  most 1024), as :func:`robofast_core.frameview.encode_array` JSON, with
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
* ``POST /stop`` ``{"reason"?}`` -- stop (bounded); always answers.
* ``POST /mode`` ``{"mode"}``, ``POST /target`` ``{"x", "y"}`` (moves the
  setpoint without resetting the controller), ``POST /gains`` ``{"kp",
  "ki", "kd", "deadband", "i_max", "max_step", "max_rate"}`` (any subset),
  ``POST /roi`` ``{"size"}``, ``POST /exposure`` ``{"exposure", "gain"?}``.
* ``POST /locate`` -- find the fiber with the backlight; it becomes the
  target. 409 while guiding.
* ``POST /calibrate`` ``{"device": "tiptilt"|"mount", "step", "exposure"?,
  "frames"?, "settle"?}`` -- measure and save that device's matrix (for
  this ``port``). 409 while guiding.

Config (JSON; every key optional except as noted)::

    {"name": "t2-guider", "scope": "t2", "listen": "127.0.0.1:8140",
     "allow_hardware": false, "port": "fau",
     "calibration_dir": "/var/lib/robofast/guider",
     "camera": {"kind": "sim" | "alpaca" | "zwo" | "framegrabber", ...},
     "tiptilt": null | {"kind": "sim" | "sxao" | "sxao-sim" | "pi" | "pi-sim", ...},
     "mount": null | {"kind": "alpaca" | "sim", "address", "method": "offset" | "pulseguide", ...},
     "offload": null | {"threshold", "period", "min_pixels", "window", "settle"},
     "backlight": null | {"kiwispec": "http://host:port"} | {"sim": true},
     "guide": {...GuideConfig...},
     "sim": {...the simulated sky, when camera.kind is "sim"...},
     "redis": null | {"host", "port", "channel"}}

Safety
------
Every real device backend -- a serial port (SX AO), PI's USB/TCP, a ZWO or
frame-grabber camera, a non-loopback Alpaca camera or telescope -- is
refused unless the config says ``"allow_hardware": true`` **and** the
command line passes ``--hardware`` (as the Aqawan and TCSGO servers).

Running
-------
::

    python -m robofast_core.guider.service --config guider.json [--hardware]

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
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from robofast_core.frameview import bin_to, encode_array
from robofast_core.guider import calibration as calib
from robofast_core.guider.base import (
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
from robofast_core.guider.camera import AlpacaCamera, FrameGrabberCamera, ZWOCamera
from robofast_core.guider.centroid import WindowedCOM
from robofast_core.guider.loop import (
    Backlight,
    GuideConfig,
    Guider,
    kiwispec_backlight,
    publish_values,
)
from robofast_core.guider.offload import OffloadConfig, Offloader
from robofast_core.guider.redis_pub import TrackingPublisher
from robofast_core.guider.sim import (
    Fiber,
    Jitter,
    PlantView,
    SimCamera,
    SimMount,
    SimTipTilt,
    SimWorld,
)
from robofast_core.guider.tiptilt import (
    PIGCS,
    SXAO,
    MountOffset,
    SerialTransport,
    SimGCS,
    SXAOSim,
    alpaca_telescope,
    open_pi_gcs,
)
from robofast_core.registry import ValueRegistry

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
    #: (:func:`robofast_core.catalog.catalog_from_config`) and the plate
    #: solver's settings for this camera (:class:`robofast_core.astrometry.SolverConfig`).
    catalog: dict[str, Any] | None = None
    solver: dict[str, Any] | None = None
    #: The port's PA calibration (docs/rotcal.md): ``{"store": path,
    #: "telescope": name}``. Without a measured mount calibration, the
    #: mount's matrix is seeded from it (PA, parity, plate scale), and it
    #: gives the Redis telemetry its PA.
    pa_calibration: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuiderServiceConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"guider config: unknown keys {sorted(unknown)}")
        return cls(**d)


# -- building the parts ------------------------------------------------------------------


def _limits(v: Any) -> Any:
    return (
        None if v is None else ((float(v[0][0]), float(v[0][1])), (float(v[1][0]), float(v[1][1])))
    )


def build_world(spec: dict[str, Any], clock: Clock) -> SimWorld:
    """The simulated sky from the ``sim`` section (:class:`SimWorld`
    fields; ``jitter`` and ``fiber`` as dicts of their fields)."""
    s = dict(spec)
    jit = s.pop("jitter", None)
    fib = s.pop("fiber", None)
    ttm = s.pop("tt_matrix", None)
    for k in ("sensor", "star"):
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
    """Every part, with the hardware gate applied (module doc)."""
    cam_spec = dict(cfg.camera)
    kind = cam_spec.pop("kind", "sim")
    world: SimWorld | None = None
    camera: Camera
    if kind == "sim":
        world = build_world(cfg.sim, clock)
        camera = SimCamera(world, readout=float(cam_spec.get("readout", 0.0)))
    elif kind == "alpaca":
        camera = AlpacaCamera.at(
            cam_spec["address"], int(cam_spec.get("number", 0)), allow_hardware=allowed, clock=clock
        )
    elif kind == "zwo":
        camera = ZWOCamera(
            index=int(cam_spec.get("index", 0)),
            library=cam_spec.get("library"),
            allow_hardware=allowed,
            clock=clock,
        )
    elif kind == "framegrabber":
        camera = FrameGrabberCamera(device=str(cam_spec.get("device", "")), allow_hardware=allowed)
    else:
        raise ValueError(f"camera kind {kind!r}")

    tiptilt: TipTilt | None = None
    if cfg.tiptilt:
        t = dict(cfg.tiptilt)
        tk = t.pop("kind")
        if tk == "sim":
            tt = SimTipTilt(
                limits=_limits(t.get("limits", [[-100, 100], [-100, 100]])),
                tau=float(t.get("tau", 2e-3)),
                delay=float(t.get("delay", 0.0)),
                latency=float(t.get("latency", 0.0)),
                clock=clock,
            )
            if world is not None:
                world.tiptilt = tt
            tiptilt = tt
        elif tk in ("sxao", "sxao-sim"):
            term = str(t.get("terminator", "")).encode().decode("unicode_escape").encode("latin-1")
            if tk == "sxao":
                transport: Any = SerialTransport(
                    t["port"], baudrate=int(t.get("baudrate", 9600)), allow_hardware=allowed
                )
            else:
                sim = SXAOSim(travel=int(t.get("travel", 1000)), terminator=term, clock=clock)
                transport = sim
                if world is not None:
                    world.tiptilt = PlantView(lambda _t, s=sim: s.position)
            sx = SXAO(
                transport,
                limits=_limits(t.get("limits", [[-500, 500], [-500, 500]])),
                terminator=term,
                timeout=float(t.get("timeout", 0.5)),
            )
            sx.clock = clock
            tiptilt = sx
        elif tk in ("pi", "pi-sim"):
            if tk == "pi":
                gcs: Any = open_pi_gcs(
                    usb_serial=t.get("usb_serial"),
                    host=t.get("host"),
                    port=int(t.get("tcp_port", 50000)),
                    allow_hardware=allowed,
                )
            else:
                gcs = SimGCS(tau=float(t.get("tau", 1e-3)), clock=clock)
                gcs.ConnectUSB("simulated")
                if world is not None:
                    world.tiptilt = PlantView(gcs.position_at)
            pi = PIGCS(gcs, margin=float(t.get("margin", 0.0)), in_thread=(tk == "pi"))
            pi.clock = clock
            tiptilt = pi
        else:
            raise ValueError(f"tiptilt kind {tk!r}")

    mount: MountOffset | None = None
    if cfg.mount:
        m = dict(cfg.mount)
        mk = m.pop("kind", "alpaca")
        if mk == "sim":
            driver: Any = SimMount(
                tau=float(m.get("tau", 0.5)),
                delay=float(m.get("delay", 0.1)),
                guide_rate=float(m.get("guide_rate", 7.5)),
                clock=clock,
            )
            if world is not None:
                world.mount = driver
        elif mk == "alpaca":
            driver = alpaca_telescope(m["address"], int(m.get("number", 0)), allow_hardware=allowed)
        else:
            raise ValueError(f"mount kind {mk!r}")
        mount = MountOffset(
            driver,
            method=str(m.get("method", "offset")),
            action=str(m.get("action", "Offset")),
            guide_rate=float(m.get("guide_rate", 7.5)),
            max_offset=float(m.get("max_offset", 30.0)),
            timeout=float(m.get("timeout", 5.0)),
        )
        mount.clock = clock

    backlight: Backlight | None = None
    if cfg.backlight:
        if cfg.backlight.get("kiwispec"):
            backlight = kiwispec_backlight(str(cfg.backlight["kiwispec"]))
        elif cfg.backlight.get("sim") and world is not None:
            w = world

            async def _set(on: bool) -> None:
                w.backlit = on

            backlight = Backlight(_set)
    return Parts(camera, tiptilt, mount, world, backlight)


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
    """The parts, the :class:`~robofast_core.guider.loop.Guider` and the
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
        self.pa_cal = self._pa_calibration()
        publisher = None
        if redis_client is not None or config.redis:
            r = config.redis or {}
            kw = {
                "channel": r.get("channel", "tracking_data"),
                "plate_scale": gcfg.plate_scale,
                "pa": float(r.get("pa", self.pa_cal.pa(0.0) if self.pa_cal else 0.0)),
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
        self._load_calibrations()
        self._catalog: Any = None
        self._solver: Any = None
        if config.solver is not None:
            from robofast_core.astrometry import SolverConfig
            from robofast_core.catalog import catalog_from_config

            self._solver = SolverConfig.from_dict(config.solver)
            self._catalog = catalog_from_config(config.catalog)
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
        from robofast_core.rotcal import PAStore

        try:
            return PAStore(pc["store"]).get(pc["telescope"], self.config.port)
        except (OSError, ValueError, KeyError) as exc:
            log.error("PA calibration unreadable: %s", exc)
            return None

    def _seed_mount(self, device: str) -> calib.Calibration | None:
        """A mount calibration from the PA calibration (docs/rotcal.md)."""
        from robofast_core.rotcal import seed_mount_matrix

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
            pa=cal.pa(0.0),
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
        return self

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.guider.stop("service stopping")
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

    def _set_identifier(self, args: dict[str, Any]) -> None:
        """Catalog acquisition (``select: "catalog"``) for this start's
        target: ``target_radec`` [ra, dec] deg and ``unambiguous`` (from
        vetting: brightest-star fallback allowed)."""
        g = self.guider
        g.unambiguous = bool(args.get("unambiguous", False))
        g.identify = None
        radec = args.get("target_radec")
        if radec is not None and self._solver is not None:
            from robofast_core.guider.identify import catalog_identifier

            ra, dec = (float(v) for v in radec)
            g.identify = catalog_identifier(self._catalog, self._solver, ra, dec)

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


class GuiderRemote:
    """The service's API, for the sequencer (its ``guider`` hook).

    * :meth:`start` -- start guiding and wait (``acquire_timeout``) for
      ``guiding``; raises :class:`GuiderError` otherwise (after stopping
      it), which is that telescope's ordinary error.
    * :meth:`check` -- raises if the guider has failed or stopped on its
      own since :meth:`start`.
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
    ) -> None:
        self.url = url.rstrip("/")
        self.scope = scope
        self.registry = registry
        self.timeout = timeout
        self.acquire_timeout = acquire_timeout
        self.poll_interval = poll_interval
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
                r = await client.request(method, self.url + path, json=body)
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
        deadline = asyncio.get_running_loop().time() + self.acquire_timeout
        while True:
            st = await self.status()
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
        st = await self.status()
        self._publish(st)
        if st["state"] in ("failed", "stopped", "idle"):
            raise GuiderError(f"guider {self.scope}: {st['state']}: {st.get('reason')}")

    async def stop(self, reason: str = "requested") -> bool:
        """Stop guiding. Bounded; never raises; True if it stopped."""
        self.started = False
        poll, self._poll = self._poll, None
        if poll is not None:
            poll.cancel()
            with contextlib.suppress(BaseException):
                await poll
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
        while True:
            await asyncio.sleep(self.poll_interval)
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
        publish_values(
            self.registry,
            self.scope,
            st.get("state", "?"),
            st.get("telemetry") or {},
            target=st.get("target"),
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
