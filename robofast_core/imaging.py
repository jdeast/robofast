"""The sequencer's frame pipeline: telemetry, download, and a background writer.

For each exposure the sequencer (:mod:`robofast_core.sequencer`) calls:

1. :meth:`Imaging.exposure_started`, right after ``StartExposure`` -- reads
   the telescope's pointing (light frames), focuser and rotator into the
   value registry and takes the *start* snapshot;
2. :meth:`Imaging.exposure_done`, once ``ImageReady`` -- reads the camera's
   state (``LastExposureStartTime``, ``LastExposureDuration``, temperature,
   binning, ...) into the registry, takes the *end* snapshot, **downloads
   the image** (ImageBytes, else JSON; the timeout sized by the image), and
   queues it for writing;
3. :meth:`Imaging.flush` before reporting an observation complete: waits
   for that telescope's queued frames to be on disk, so the plan's record of
   the observation carries real file paths.

The download happens in the observing task, with the generation's camera
grant: the camera holds only one image, and the next ``StartExposure``
discards it, so it must be read out before the next exposure starts (and
the grant fence covers it). Building the header and writing the file
happen in a **background task per telescope** (in a worker thread), so they
overlap the next exposure. The queue is **bounded** (``queue_size``
frames): if the disk falls behind, the next exposure waits for room rather
than piling up images in memory.

**A frame is never dropped silently.** If writing a frame fails, the
pipeline alarms (a critical ``frame_write_failed`` event and log line,
with everything known about the frame), and that telescope's next
:meth:`exposure_started` / :meth:`flush` raises :class:`FrameWriteFailed`:
the sequencer stops that telescope for the night (it gives up, an alarm)
while the others observe on. Frames already queued are still attempted.

Telemetry reads go through the same grants as the observation (so a fenced
generation sends nothing), each bounded by the owner's command timeout and
the whole batch by ``telemetry_timeout``. A read that fails marks its value
stale (written undefined in the header); it never fails the exposure. A
member the device does not implement (``NotImplementedException``) is not
asked again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from robofast_core.fits_writer import FrameInfo, FrameStore, Optics, WrittenFrame, imagetyp
from robofast_core.imagebytes import ImageArray, wire_bytes_per_pixel
from robofast_core.owner import DeviceError, DeviceOwner, Grant, GrantRevoked
from robofast_core.plan import Observation
from robofast_core.registry import Snapshot, Telemetry, ValueRegistry, publish_static, utcnow
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

__all__ = [
    "FrameWriteFailed",
    "Imaging",
    "ImagingConfig",
    "PendingFrame",
    "download_timeout",
]


class FrameWriteFailed(Exception):
    """A frame of this telescope could not be written; stop the telescope."""


@dataclass
class ImagingConfig:
    """``images`` in the enclosure config (``docs/data.md``)."""

    data_root: str
    #: Site name: a directory level, and ``OBSERVAT``.
    site: str = "site"
    #: Rice-compress integer images (``.fits.fz``).
    compress: bool = False
    #: Frames per telescope waiting to be written before the next exposure waits.
    queue_size: int = 4
    #: Ask cameras for the ImageBytes transfer (else JSON).
    imagebytes: bool = True
    #: Download timeout: this much plus the image's (generous) size over
    #: ``download_min_rate`` bytes/s.
    download_timeout_base: float = 30.0
    download_min_rate: float = 2e6
    #: Night convention (as the plan record's): local time from ``timezone``
    #: (IANA), else mean solar time at the site's longitude.
    night_rollover_hour: float = 12.0
    timezone: str | None = None
    #: How long telescope and camera readings stay current in the registry.
    pointing_max_age: float = 60.0
    camera_max_age: float = 60.0
    #: Bound on one exposure's batch of telemetry reads.
    telemetry_timeout: float = 15.0
    #: Bound on waiting for a telescope's queued frames to be written.
    flush_timeout: float = 120.0
    #: Write the nightly JSON-lines index of frames.
    index: bool = True
    #: Background telemetry (:class:`~robofast_core.registry.Telemetry`).
    weather_interval: float = 30.0
    weather_max_age: float = 300.0
    enclosure_interval: float = 10.0
    sky_interval: float = 60.0
    #: Per telescope: :class:`~robofast_core.fits_writer.Optics` fields.
    telescopes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImagingConfig:
        d = dict(d)
        d.pop("_note", None)
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"images: unknown keys {sorted(set(d) - known)}")
        if "data_root" not in d:
            raise ValueError("images: data_root is required")
        cfg = cls(**d)
        if cfg.queue_size < 1:
            raise ValueError("images: queue_size must be at least 1")
        for name, optics in cfg.telescopes.items():
            Optics.from_dict(optics)  # fail at startup
            if not isinstance(name, str):
                raise ValueError("images.telescopes: names must be strings")
        return cfg


def download_timeout(
    pixels: int, transfer: str = "imagebytes", base: float = 30.0, min_rate: float = 2e6
) -> float:
    """A timeout for downloading an image of ``pixels`` pixels."""
    return base + pixels * wire_bytes_per_pixel(transfer) / min_rate


@dataclass
class PendingFrame:
    """A frame handed to the writer; ``written`` is set once it is on disk."""

    telescope: str
    index: int
    written: WrittenFrame | None = None
    error: str | None = None
    #: The downloaded image, kept only when asked (``keep_image``: the
    #: autofocus measures it); the caller drops it when done.
    image: ImageArray | None = field(default=None, repr=False)

    @property
    def path(self) -> str | None:
        return None if self.written is None else self.written.relpath


@dataclass
class _Exposure:
    info: FrameInfo
    t0: float


@dataclass
class _Writer:
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    failed: str | None = None


#: Telescope members read at a light exposure's start:
#: member -> (registry name, keyword, unit, comment).
TELESCOPE_MEMBERS = {
    "RightAscension": ("tel.ra", None, "h", "Mount RA"),
    "Declination": ("tel.dec", None, "deg", "Mount Dec"),
    "Altitude": ("tel.alt", "ALT", "deg", "Altitude at exposure start"),
    "Azimuth": ("tel.az", "AZ", "deg", "Azimuth at exposure start"),
    "SiderealTime": ("tel.lst", "LST", "h", "Local sidereal time at start"),
    "Tracking": ("tel.tracking", "TRACKING", "", "Mount tracking"),
}
FOCUSER_MEMBERS = {
    "Position": ("focuser.position", "FOCPOS", "step", "Focuser position"),
    "Temperature": ("focuser.temperature", "FOCTEMP", "C", "Focuser temperature"),
}
ROTATOR_MEMBERS = {
    "Position": ("rotator.position", "ROTPOS", "deg", "Rotator sky position angle"),
    "MechanicalPosition": ("rotator.mechanical", "ROTMECH", "deg", "Rotator mechanical angle"),
}
#: Camera members read after each exposure (``camera.`` names).
CAMERA_MEMBERS = {
    "LastExposureStartTime": ("last_start", None, "", "Camera exposure start (UTC)"),
    "LastExposureDuration": ("last_duration", None, "s", "Camera exposure duration"),
    "CCDTemperature": ("ccd_temp", "CCD-TEMP", "C", "CCD temperature"),
    "SetCCDTemperature": ("set_temp", "SET-TEMP", "C", "CCD temperature setpoint"),
    "CoolerOn": ("cooler_on", "COOLERON", "", "Cooler on"),
    "CoolerPower": ("cooler_power", "COOLPOW", "%", "Cooler power"),
    "BinX": ("binx", "XBINNING", "", "Binning, x"),
    "BinY": ("biny", "YBINNING", "", "Binning, y"),
    "StartX": ("startx", "XORGSUBF", "px", "Subframe origin, x (binned)"),
    "StartY": ("starty", "YORGSUBF", "px", "Subframe origin, y (binned)"),
    "NumX": ("numx", None, "px", "Subframe width"),
    "NumY": ("numy", None, "px", "Subframe height"),
    "ReadoutMode": ("readout_mode", None, "", "Readout mode index"),
    "Gain": ("gain", "GAIN", "", "Camera gain setting"),
    "Offset": ("offset", "OFFSET", "", "Camera offset setting"),
}
#: Camera members that do not change: read once per camera.
CAMERA_STATIC = {
    "Name": ("name", "INSTRUME", "", "Camera"),
    "SensorName": ("sensor", "DETECTOR", "", "Sensor"),
    "PixelSizeX": ("pixsize_x", "PIXSIZE1", "um", "Pixel width, unbinned"),
    "PixelSizeY": ("pixsize_y", "PIXSIZE2", "um", "Pixel height, unbinned"),
    "ElectronsPerADU": ("egain", "EGAIN", "e-/ADU", "Electrons per ADU"),
    "MaxADU": ("max_adu", "MAXADU", "ADU", "Maximum ADU"),
    "CameraXSize": ("xsize", None, "px", "Chip width, unbinned"),
    "CameraYSize": ("ysize", None, "px", "Chip height, unbinned"),
    "ReadoutModes": ("readout_modes", None, "", "Readout modes"),
}


class Imaging:
    """The frame pipeline of one sequencer (module doc).

    ``focusers`` and ``rotators`` map telescope names to read-only owners
    (optional). ``site`` is the enclosure's (latitude, longitude,
    elevation); ``weather`` an ObservingConditions owner and ``supervisor``
    the safety supervisor, for the background :class:`Telemetry`.
    """

    def __init__(
        self,
        config: ImagingConfig,
        *,
        timeline: Timeline | None = None,
        registry: ValueRegistry | None = None,
        site: dict[str, float] | None = None,
        focusers: dict[str, DeviceOwner] | None = None,
        rotators: dict[str, DeviceOwner] | None = None,
        weather: DeviceOwner | None = None,
        supervisor: Any = None,
        version: str | None = None,
    ) -> None:
        self.config = config
        self.timeline = timeline
        self.registry = registry or ValueRegistry()
        self.site = site
        self.store = FrameStore(
            config.data_root,
            config.site,
            compress=config.compress,
            rollover_hour=config.night_rollover_hour,
            timezone=config.timezone,
            longitude=None if site is None else site.get("longitude"),
            index=config.index,
        )
        publish_static(self.registry, site_name=config.site, site=site, version=version)
        self.telemetry = Telemetry(
            self.registry,
            weather=weather,
            supervisor=supervisor,
            site=site,
            weather_interval=config.weather_interval,
            weather_max_age=config.weather_max_age,
            enclosure_interval=config.enclosure_interval,
            sky_interval=config.sky_interval,
        )
        self._aux = {
            name: [
                (owner.grant(f"telemetry/{name}"), members, kind)
                for owners, members, kind in (
                    (focusers or {}, FOCUSER_MEMBERS, "focuser"),
                    (rotators or {}, ROTATOR_MEMBERS, "rotator"),
                )
                if (owner := owners.get(name)) is not None
            ]
            for name in {*(focusers or {}), *(rotators or {})}
        }
        #: Owners only the pipeline uses (closed by the service at exit).
        self.owners: list[DeviceOwner] = []
        self._writers: dict[str, _Writer] = {}
        self._unsupported: set[tuple[str, str]] = set()
        self._mount_static: set[str] = set()
        self._camera_static: set[str] = set()
        #: Every frame written: telescope -> list of WrittenFrame (bounded).
        self.written: list[WrittenFrame] = []
        #: Download timings: (telescope, transfer, bytes, seconds).
        self.downloads: list[tuple[str, str, int, float]] = []

    # -- the sequencer's calls -------------------------------------------------

    async def exposure_started(
        self,
        telescope: str,
        tel: Grant,
        obs: Observation,
        *,
        index: int,
        host_start: datetime,
        sequencer: str = "",
        generation: int | None = None,
        ids: dict[str, Any] | None = None,
    ) -> _Exposure:
        if obs.light:
            if telescope not in self._mount_static:
                await self._read_one(
                    telescope, tel, "EquatorialSystem", "tel.eqsys", None, "", "", max_age=None
                )
                self._mount_static.add(telescope)
            reads = [(tel, TELESCOPE_MEMBERS, "")]
            reads += [(grant, members, "") for grant, members, _ in self._aux.get(telescope, [])]
            await self._read_batch(telescope, reads, self.config.pointing_max_age)
        optics = Optics.from_dict(self.config.telescopes.get(telescope))
        info = FrameInfo(
            telescope=telescope,
            object=obs.object or obs.name,
            obs_name=obs.name,
            imagetyp="FOCUS"
            if obs.kind == "autofocus"
            else imagetyp(obs.light, obs.exptime, obs.object or obs.name),
            light=obs.light,
            exptime_requested=obs.exptime,
            host_start=host_start,
            index=index,
            sequencer=sequencer,
            generation=generation,
            ids=dict(ids or {}),
            target=(obs.ra_hours, obs.dec_deg)
            if obs.light and obs.ra_hours is not None and obs.dec_deg is not None
            else None,
            filter=obs.filter,
            port=obs.port,
            optics=optics,
        )
        info.start = self.registry.snapshot(self._scopes(telescope))
        return _Exposure(info, time.monotonic())

    async def exposure_done(
        self, telescope: str, cam: Grant, exp: _Exposure, *, keep_image: bool = False
    ) -> PendingFrame:
        """Read the camera's state, download the image, queue it (module doc).
        With ``keep_image`` the returned frame also carries the image."""
        if telescope not in self._camera_static:
            await self._read_batch(telescope, [(cam, CAMERA_STATIC, "camera.")], None)
            self._camera_static.add(telescope)
        await self._read_batch(
            telescope, [(cam, CAMERA_MEMBERS, "camera.")], self.config.camera_max_age
        )
        end = self.registry.snapshot(self._scopes(telescope))
        exp.info.end = end
        modes = end.current(f"{telescope}.camera.readout_modes")
        mode = end.current(f"{telescope}.camera.readout_mode")
        if isinstance(modes, list) and isinstance(mode, int) and 0 <= mode < len(modes):
            self.registry.publish(
                f"{telescope}.camera.readout_name",
                str(modes[mode]),
                max_age=self.config.camera_max_age,
                keyword="READMODE",
                comment="Readout mode",
            )
            exp.info.end = self.registry.snapshot(self._scopes(telescope))
        pixels = self._pixels(telescope, end)
        transfer = "imagebytes" if self.config.imagebytes else "json"
        timeout = download_timeout(
            pixels, transfer, self.config.download_timeout_base, self.config.download_min_rate
        )
        t0 = time.monotonic()
        image = await cam.get("ImageArray", timeout=timeout)
        dt = time.monotonic() - t0
        if not isinstance(image, ImageArray):
            raise DeviceError(f"{telescope}: ImageArray returned {type(image).__name__}")
        self.downloads.append((telescope, image.transfer, image.nbytes_wire, dt))
        del self.downloads[:-1000]
        self._event(
            "image_downloaded",
            telescope=telescope,
            transfer=image.transfer,
            shape=list(image.data.shape),
            bytes=image.nbytes_wire,
            seconds=round(dt, 4),
        )
        frame = PendingFrame(telescope, exp.info.index)
        if keep_image:
            frame.image = image
        writer = self._writer(telescope)
        await writer.queue.put((frame, exp.info, image))
        return frame

    async def flush(self, telescope: str) -> None:
        """Wait until ``telescope``'s queued frames are written; raise
        :class:`FrameWriteFailed` if any failed."""
        w = self._writers.get(telescope)
        if w is not None:
            try:
                await asyncio.wait_for(w.queue.join(), self.config.flush_timeout)
            except TimeoutError:
                w.failed = w.failed or f"frames not written within {self.config.flush_timeout} s"
                self._alarm(telescope, w.failed)
        self._check(telescope)

    def check(self, telescope: str) -> None:
        """Raise :class:`FrameWriteFailed` if a frame of ``telescope`` was lost."""
        self._check(telescope)

    def failed(self, telescope: str) -> str | None:
        w = self._writers.get(telescope)
        return None if w is None else w.failed

    async def aclose(self, timeout: float | None = None) -> None:
        """Write what is queued (bounded), then stop the writers."""
        timeout = self.config.flush_timeout if timeout is None else timeout
        writers = [w for w in self._writers.values() if w.task is not None]
        joins = [asyncio.ensure_future(w.queue.join()) for w in writers]
        if joins:
            _, pending = await asyncio.wait(joins, timeout=timeout)
            for j in pending:
                j.cancel()
            for name, w in self._writers.items():
                if w.queue.qsize():
                    self._alarm(name, f"{w.queue.qsize()} frames not written at shutdown")
        for w in writers:
            assert w.task is not None
            w.task.cancel()
        await asyncio.gather(*(w.task for w in writers if w.task), return_exceptions=True)

    def filter_selected(self, telescope: str, name: str, position: int) -> None:
        self.registry.publish(
            f"{telescope}.filter.name",
            name,
            max_age=None,
            keyword="FILTNAME",
            comment="Filter wheel: selected filter",
        )
        self.registry.publish(
            f"{telescope}.filter.position",
            position,
            max_age=None,
            keyword="FILTPOS",
            comment="Filter wheel position",
        )

    def port_selected(self, telescope: str, port: str) -> None:
        self.registry.publish(
            f"{telescope}.tel.port",
            port,
            max_age=None,
            keyword="M3PORT",
            comment="M3 port last selected",
        )

    # -- internals -------------------------------------------------------------

    def _scopes(self, telescope: str) -> list[str]:
        return [telescope, "site", "software", "enclosure", "weather", "sky"]

    def _pixels(self, telescope: str, snap: Snapshot) -> int:
        nx = snap.current(f"{telescope}.camera.numx") or snap.current(f"{telescope}.camera.xsize")
        ny = snap.current(f"{telescope}.camera.numy") or snap.current(f"{telescope}.camera.ysize")
        if isinstance(nx, int) and isinstance(ny, int) and nx > 0 and ny > 0:
            return nx * ny * 3  # allow for colour planes
        return 8192 * 8192

    def _check(self, telescope: str) -> None:
        w = self._writers.get(telescope)
        if w is not None and w.failed:
            raise FrameWriteFailed(f"{telescope}: {w.failed}")

    async def _read_batch(
        self, telescope: str, reads: list[tuple[Grant, dict, str]], max_age: float | None
    ) -> None:
        todo = [
            (grant, member, spec, prefix)
            for grant, members, prefix in reads
            for member, spec in members.items()
        ]
        t_batch = utcnow()
        try:
            async with asyncio.timeout(self.config.telemetry_timeout):
                for grant, member, (name, kw, unit, comment), prefix in todo:
                    await self._read_one(
                        telescope, grant, member, prefix + name, kw, unit, comment, max_age=max_age
                    )
        except TimeoutError:
            for _, _, (name, *_), prefix in todo:
                v = self.registry.get(f"{telescope}.{prefix}{name}")
                if v is not None and v.measured < t_batch:
                    self.registry.failed(f"{telescope}.{prefix}{name}", "telemetry timeout")
            log.warning(
                "%s: telemetry reads exceeded %s s", telescope, self.config.telemetry_timeout
            )

    async def _read_one(
        self,
        telescope: str,
        grant: Grant,
        member: str,
        name: str,
        keyword: str | None,
        unit: str,
        comment: str,
        *,
        max_age: float | None,
    ) -> None:
        from robofast_core.alpaca_client import NotImplementedException

        key = (telescope, f"{grant.owner.name}.{member}")
        if key in self._unsupported:
            return
        full = f"{telescope}.{name}"
        try:
            value = await grant.get(member)
        except GrantRevoked:
            raise
        except NotImplementedException:
            self._unsupported.add(key)
            return
        except DeviceError as exc:
            self.registry.failed(full, f"{type(exc).__name__}: {exc}")
            return
        if isinstance(value, float):
            value = float(f"{value:.10g}")  # a device's float noise is not precision
        elif isinstance(value, int | bool | str):
            pass
        elif isinstance(value, list):
            value = [str(x) for x in value]
        else:
            value = str(value)
        self.registry.publish(
            full, value, max_age=max_age, unit=unit, keyword=keyword, comment=comment
        )

    def _writer(self, telescope: str) -> _Writer:
        w = self._writers.get(telescope)
        if w is None:
            w = _Writer(asyncio.Queue(self.config.queue_size))
            self._writers[telescope] = w
        if w.task is None or w.task.done():
            w.task = asyncio.create_task(self._write_loop(telescope, w), name=f"writer-{telescope}")
        return w

    async def _write_loop(self, telescope: str, w: _Writer) -> None:
        while True:
            frame, info, image = await w.queue.get()
            try:
                t0 = time.monotonic()
                written = await asyncio.to_thread(self.store.write, info, image)
                frame.written = written
                self.written.append(written)
                del self.written[:-10000]
                self._event(
                    "frame_written",
                    telescope=telescope,
                    file=written.relpath,
                    seconds=round(time.monotonic() - t0, 4),
                )
            except Exception as exc:
                frame.error = f"{type(exc).__name__}: {exc}"
                w.failed = w.failed or frame.error
                self._alarm(
                    telescope,
                    f"frame {info.obs_name} #{info.index} ({info.object}, started "
                    f"{info.host_start.isoformat()}) NOT WRITTEN: {frame.error}",
                )
            finally:
                del image
                w.queue.task_done()

    def _alarm(self, telescope: str, message: str) -> None:
        log.critical("%s: %s", telescope, message)
        self._event("frame_write_failed", telescope=telescope, error=message, level="critical")

    def _event(self, what: str, **detail: Any) -> None:
        if self.timeline is not None:
            self.timeline.event("imaging", what, **detail)


def imaging_from_config(
    config: dict[str, Any],
    timeline: Timeline,
    *,
    supervisor: Any = None,
    weather: DeviceOwner | None = None,
    focusers: dict[str, DeviceOwner] | None = None,
    rotators: dict[str, DeviceOwner] | None = None,
    base: Path | None = None,
) -> Imaging | None:
    """An :class:`Imaging` from the enclosure config's ``images`` entry
    (None if there is none). A relative ``data_root`` is relative to
    ``base`` (the config file's directory)."""
    entry = config.get("images")
    if entry is None:
        return None
    cfg = ImagingConfig.from_dict(entry)
    root = Path(cfg.data_root)
    if base is not None and not root.is_absolute():
        cfg.data_root = str(base / root)
    return Imaging(
        cfg,
        timeline=timeline,
        site=config.get("site"),
        focusers=focusers,
        rotators=rotators,
        weather=weather,
        supervisor=supervisor,
    )
