"""FITS files for the sequencer's frames: the header and the file on disk.

Layout (``docs/data.md``)::

    <data_root>/<site>/<telescope>/n<YYYYMMDD>/n<YYYYMMDD>.<telescope>.<object>.<seq:04d>.fits

``.fits.fz`` when Rice-compressed. The night is the local date of the
*evening* (:func:`robofast_core.plan_record.night_of`, the plan record's
convention: before ``night_rollover_hour`` local time is the previous
night), from the sequencer's clock when ``StartExposure`` was sent
(``DATE-CMD``; :mod:`robofast_core.clock`: the one clock every night name
comes from). ``seq`` counts up per telescope
and night (the legacy ``getIndex``: one more than the highest number in the
directory). Beside the frames, ``n<YYYYMMDD>.<telescope>.frames.jsonl``
indexes them, one JSON line per frame.

Writing is atomic and never overwrites: the file is written under a
temporary name in the same directory, flushed to disk, then *hard-linked*
to its final name (which fails if that name exists: then the next number
is taken, the header updated, and the file written again), and the
temporary name removed. A reader never sees a partial frame, and nothing
-- not even a second writer -- replaces one. Every HDU gets ``DATASUM`` and
``CHECKSUM``.

Times are UTC from the camera: ``DATE-OBS`` is the camera's
``LastExposureStartTime`` (ASCOM: UTC), written with milliseconds, and
``EXPTIME`` its ``LastExposureDuration``. Nothing is ever derived from
local time plus an offset (the legacy ``fix_fits.py`` assumed UTC = local +
7 h and rewrote raw files in place; neither happens here). If the camera
does not report a start time, the host's UTC time of ``StartExposure`` is
used and ``TIMESRC`` says so.

Registry values (:mod:`robofast_core.registry`) go in under their own
keywords, units in the comments. A stale value is written *undefined*, with
``STALE`` and its old value and age in the comment, and listed in a
``COMMENT`` card; it is never written as current.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from robofast_core.clock import Clock, real_clock
from robofast_core.imagebytes import ImageArray, fits_order
from robofast_core.plan_record import night_of
from robofast_core.registry import Snapshot, Value

log = logging.getLogger(__name__)

#: Order of the registry scopes in a header (then the per-telescope ones).
_SCOPE_ORDER = ("site", "software", "enclosure", "weather", "sky")
_SAFE = re.compile(r"[^A-Za-z0-9+\-_]")


@dataclass(frozen=True)
class Optics:
    """A telescope's nominal optics, for the approximate WCS and the header.

    ``plate_scale`` arcsec per unbinned pixel; ``position_angle`` deg, of the
    image +y axis east of north; ``flip``: east is to the right (odd
    parity) instead of the left. Aperture and focal length in mm."""

    plate_scale: float | None = None
    position_angle: float = 0.0
    flip: bool = False
    aperture: float | None = None
    focal_length: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Optics:
        d = dict(d or {})
        known = {"plate_scale", "position_angle", "flip", "aperture", "focal_length"}
        if set(d) - known:
            raise ValueError(f"optics: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


@dataclass
class FrameInfo:
    """Everything about one frame except its pixels."""

    telescope: str
    object: str
    obs_name: str
    imagetyp: str
    light: bool
    exptime_requested: float
    #: Host UTC time just before ``StartExposure`` was sent.
    host_start: datetime
    #: Frame number within its observation (0-based).
    index: int
    sequencer: str = ""
    generation: int | None = None
    #: Request ids from the plan (group, request, configuration).
    ids: dict[str, Any] = field(default_factory=dict)
    #: Requested target (RA hours, Dec deg), None for dark frames.
    target: tuple[float, float] | None = None
    filter: str | None = None
    port: str | None = None
    #: Registry snapshots at exposure start and end (end: camera values).
    start: Snapshot | None = None
    end: Snapshot | None = None
    optics: Optics = field(default_factory=Optics)


@dataclass
class WrittenFrame:
    path: Path
    #: Relative to the data root (what the request API reports).
    relpath: str
    seq: int
    night: date
    date_obs: str
    nbytes: int
    record: dict[str, Any]


# -- helpers ------------------------------------------------------------------------


def parse_camera_time(text: Any) -> datetime | None:
    """ASCOM ``LastExposureStartTime`` (``CCYY-MM-DDThh:mm:ss[.fff...]``,
    UTC) as an aware datetime; None if absent or unparseable."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().removesuffix("Z").removesuffix("+00:00")
    main, _, frac = s.partition(".")
    try:
        dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if frac:
        if not frac.isdigit():
            return None
        dt += timedelta(microseconds=int((frac + "000000")[:6]))
    return dt.replace(tzinfo=UTC)


def iso_ms(dt: datetime) -> str:
    """UTC ISO 8601 with milliseconds, no zone suffix (FITS convention)."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def safe_name(text: str, limit: int = 64) -> str:
    """A filename field: letters, digits, ``+ - _`` only (dots separate fields)."""
    s = _SAFE.sub("_", text.strip())[:limit].strip("_")
    return s or "unknown"


def airmass(alt_deg: float) -> float | None:
    """Kasten & Young (1989); None below the horizon."""
    if alt_deg <= 0:
        return None
    return 1.0 / (math.sin(math.radians(alt_deg)) + 0.50572 * (alt_deg + 6.07995) ** -1.6364)


def _sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def mount_to_icrs(
    ra_hours: float, dec_deg: float, eqsys: int | None, when: datetime, site: dict | None
) -> tuple[float, float, str, float | None]:
    """Mount coordinates -> (RA deg, Dec deg, RADESYS, EQUINOX).

    ASCOM ``EquatorialSystem``: 2 = J2000 (written as FK5, equinox 2000),
    1 = topocentric apparent (converted to ICRS with astropy's TETE frame:
    approximate, it ignores refraction), anything else is written as the
    mount gives it, labelled FK5 at that equinox's best guess (J2000)."""
    if eqsys == 1 and site is not None:
        from astropy import units as u
        from astropy.coordinates import TETE, EarthLocation, SkyCoord
        from astropy.time import Time

        from robofast_core.sky import offline_iers

        offline_iers()
        loc = EarthLocation.from_geodetic(
            site["longitude"] * u.deg, site["latitude"] * u.deg, site.get("elevation", 0) * u.m
        )
        c = SkyCoord(
            ra=ra_hours * 15 * u.deg,
            dec=dec_deg * u.deg,
            frame=TETE(obstime=Time(when), location=loc),
        ).icrs
        return float(c.ra.deg), float(c.dec.deg), "ICRS", None
    return (ra_hours * 15.0) % 360.0, dec_deg, "FK5", 2000.0


def storage_array(image: ImageArray) -> np.ndarray:
    """The pixels in FITS order and the type to store them in: unsigned
    16-bit for integers that fit (the usual camera case, BZERO 32768),
    otherwise 32- or 64-bit integers, or floats as sent. One copy."""
    data = fits_order(image.data)
    kind = data.dtype.kind
    if kind == "f":
        dtype = np.dtype(">f4") if data.dtype.itemsize <= 4 else np.dtype(">f8")
    elif data.dtype in (np.dtype("u1"), np.dtype("<u2")):
        dtype = np.dtype("uint16")
    else:
        lo, hi = (int(data.min()), int(data.max())) if data.size else (0, 0)
        if lo >= 0 and hi <= 0xFFFF:
            dtype = np.dtype("uint16")
        elif -(2**31) <= lo and hi < 2**31:
            dtype = np.dtype(">i4")
        else:
            dtype = np.dtype(">i8")
    out = np.empty(data.shape, dtype=dtype)
    out[...] = data
    return out


def _card(header: fits.Header, key: str, value: Any, comment: str = "") -> None:
    """Set a card, shortening the comment to fit 80 characters (no warnings)."""
    if isinstance(value, float) and not math.isfinite(value):
        comment = f"{comment} (non-finite: {value})"
        value = None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str) and len(value) > 68:
        value = value[:68]
    if comment:
        width = len(fits.Card(key, value).image.rstrip())
        room = 80 - width - 3
        comment = comment[: max(0, room)] if room > 0 else ""
    header[key] = (value, comment)


def _fmt(v: Any, unit: str) -> str:
    if isinstance(v, float):
        v = f"{v:.6g}"
    return f"{v}{' ' + unit if unit else ''}"


# -- the header ----------------------------------------------------------------------


def build_header(
    info: FrameInfo, image_shape: tuple[int, ...], written: datetime | None = None
) -> tuple[fits.Header, datetime]:
    """The frame's header (no data-structure keywords) and its ``DATE-OBS``;
    ``written`` (default: now) is ``DATE``."""
    h = fits.Header()
    start = info.start or Snapshot(info.host_start, {})
    end = info.end or Snapshot(info.host_start, {})
    tel = info.telescope
    cam = f"{tel}.camera."
    stale: list[str] = []

    def get(name: str, snap: Snapshot = start) -> Any:
        return snap.current(name)

    # Times: the camera's, in UTC.
    cam_start = parse_camera_time(get(cam + "last_start", end))
    if cam_start is not None:
        date_obs, timesrc = cam_start, "camera"
    else:
        date_obs, timesrc = info.host_start.astimezone(UTC), "host"
    exptime = get(cam + "last_duration", end)
    if not isinstance(exptime, int | float) or exptime < 0:
        exptime = float(info.exptime_requested)
    from astropy.time import Time

    t_obs = Time(date_obs)
    _card(h, "DATE", iso_ms(written or datetime.now(UTC)), "UTC date this file was written")
    _card(h, "DATE-OBS", iso_ms(date_obs), "UTC start of exposure")
    _card(h, "DATE-END", iso_ms(date_obs + timedelta(seconds=exptime)), "UTC end of exposure")
    _card(h, "TIMESYS", "UTC", "Time scale of DATE keywords")
    _card(h, "TIMESRC", timesrc, "DATE-OBS from the camera, or host clock")
    _card(h, "MJD-OBS", round(float(t_obs.mjd), 8), "MJD (UTC) at start of exposure")
    _card(h, "JD", round(float(t_obs.jd), 8), "JD (UTC) at start of exposure")
    _card(h, "DATE-CMD", iso_ms(info.host_start), "Host UTC when StartExposure was sent")
    _card(h, "EXPTIME", float(exptime), "[s] Exposure time (camera)")
    _card(h, "EXPREQ", float(info.exptime_requested), "[s] Requested exposure time")
    if cam_start is not None:
        skew = (cam_start - info.host_start).total_seconds()
        _card(h, "CLKSKEW", round(skew, 3), "[s] DATE-OBS minus DATE-CMD")

    # What it is.
    _card(h, "OBJECT", info.object, "Target name")
    _card(h, "IMAGETYP", info.imagetyp, "Frame type")
    _card(h, "TELESCOP", tel, "Telescope")
    _card(h, "ORIGIN", "robofast", "Software that wrote this file")
    if info.filter is not None:
        _card(h, "FILTER", info.filter, "Filter (requested and selected)")
    if info.port is not None:
        _card(h, "PORT", info.port, "M3 port (requested and selected)")
    if info.optics.aperture is not None:
        _card(h, "APTDIA", info.optics.aperture, "[mm] Telescope aperture")
    if info.optics.focal_length is not None:
        _card(h, "FOCALLEN", info.optics.focal_length, "[mm] Focal length")

    # Identifiers.
    for key, kw, comment in (
        ("group", "REQGRP", "Request group id"),
        ("request", "REQUEST", "Request id"),
        ("configuration", "CONFIGID", "Configuration id"),
    ):
        if info.ids.get(key) is not None:
            _card(h, kw, str(info.ids[key]), comment)
    _card(h, "OBSNAME", info.obs_name, "Sequencer observation name")
    _card(h, "FRAMENUM", info.index, "Frame number within the observation (0-based)")
    if info.sequencer:
        _card(h, "SEQUENCR", info.sequencer, "Sequencer (lease holder)")
    if info.generation is not None:
        _card(h, "GENERATN", info.generation, "Sequencer generation")

    # Pointing (light frames only).
    site = None
    if start.current("site.latitude") is not None:
        site = {
            "latitude": start.current("site.latitude"),
            "longitude": start.current("site.longitude"),
            "elevation": start.current("site.elevation", 0.0),
        }
    ra = dec = None
    if info.light:
        if info.target is not None:
            _card(h, "OBJRA", round(info.target[0] * 15.0, 7), "[deg] Requested RA (ICRS)")
            _card(h, "OBJDEC", round(info.target[1], 7), "[deg] Requested Dec (ICRS)")
        raw_ra, raw_dec = start.get(f"{tel}.tel.ra"), start.get(f"{tel}.tel.dec")
        if raw_ra is not None and raw_dec is not None:
            if raw_ra.stale(start.taken) or raw_dec.stale(start.taken):
                for kw, v in (("RA", raw_ra), ("DEC", raw_dec)):
                    _card(h, kw, None, f"STALE {v.age(start.taken):.0f}s: mount {v.value}")
                    stale.append(kw)
            else:
                ra, dec, radesys, equinox = mount_to_icrs(
                    float(raw_ra.value),
                    float(raw_dec.value),
                    start.current(f"{tel}.tel.eqsys"),
                    date_obs,
                    site,
                )
                _card(h, "RA", round(ra, 7), "[deg] Mount RA at exposure start")
                _card(h, "DEC", round(dec, 7), "[deg] Mount Dec at exposure start")
                _card(h, "RADESYS", radesys, "Reference frame of RA/DEC")
                if equinox is not None:
                    _card(h, "EQUINOX", equinox, "Equinox of RA/DEC")
                lst = start.current(f"{tel}.tel.lst")
                if lst is not None:
                    ha = (float(lst) - float(raw_ra.value) + 12.0) % 24.0 - 12.0
                    _card(h, "HA", round(ha, 6), "[h] Hour angle at exposure start")
        alt = start.current(f"{tel}.tel.alt")
        if alt is not None:
            am = airmass(float(alt))
            if am is not None:
                _card(h, "AIRMASS", round(am, 5), "Airmass at start (Kasten & Young 1989)")
        mra, mdec = start.current("sky.moon_ra"), start.current("sky.moon_dec")
        if ra is not None and mra is not None and mdec is not None:
            _card(h, "MOONSEP", round(_sep_deg(ra, dec, mra, mdec), 3), "[deg] Moon separation")

    # Registry values, start snapshot (the camera's from the end snapshot).
    values: list[tuple[Value, Snapshot]] = []
    for snap, keep in (
        (start, lambda n: not n.startswith(cam)),
        (end, lambda n: n.startswith(cam)),
    ):
        for name, v in snap.values.items():
            scope = name.split(".", 1)[0]
            if not info.light and name.startswith(
                (f"{tel}.tel.", f"{tel}.focuser.", f"{tel}.rotator.")
            ):
                continue  # the mount's state means nothing for a dark frame
            if v.keyword and keep(name) and (scope == tel or scope in _SCOPE_ORDER):
                values.append((v, snap))
    rank = {s: i for i, s in enumerate(_SCOPE_ORDER)}
    values.sort(key=lambda p: rank.get(p[0].name.split(".", 1)[0], len(rank)))
    for v, snap in values:
        assert v.keyword is not None
        if v.keyword in h:
            continue  # written above from the same value
        unit = f"[{v.unit}] " if v.unit else ""
        if v.stale(snap.taken):
            why = "read failed" if v.error else f"{v.age(snap.taken):.0f}s old"
            _card(h, v.keyword, None, f"STALE ({why}): was {_fmt(v.value, v.unit)}")
            stale.append(v.keyword)
        elif v.value != "":  # a device's empty string says nothing
            _card(h, v.keyword, v.value, f"{unit}{v.comment}")

    # Approximate WCS from the mount and the nominal plate scale.
    if info.light and ra is not None and info.optics.plate_scale and len(image_shape) >= 2:
        _wcs(h, info, end, ra, dec, image_shape)

    if stale:
        h.add_comment(f"Stale values written as undefined: {', '.join(stale)}"[:72])
    return h, date_obs


def _wcs(
    h: fits.Header,
    info: FrameInfo,
    end: Snapshot,
    ra: float,
    dec: float,
    shape: tuple[int, ...],
) -> None:
    cam = f"{info.telescope}.camera."
    binx = end.current(cam + "binx", 1) or 1
    biny = end.current(cam + "biny", binx) or binx
    ny, nx = shape[-2], shape[-1]
    full_x, full_y = end.current(cam + "xsize"), end.current(cam + "ysize")
    sx, sy = end.current(cam + "startx", 0) or 0, end.current(cam + "starty", 0) or 0
    # Reference pixel: the optical axis, taken as the centre of the full chip.
    if full_x and full_y:
        crpix1 = (full_x / binx + 1) / 2.0 - sx
        crpix2 = (full_y / biny + 1) / 2.0 - sy
    else:
        crpix1, crpix2 = (nx + 1) / 2.0, (ny + 1) / 2.0
    scale = float(info.optics.plate_scale or 0.0) / 3600.0
    pa = math.radians(info.optics.position_angle)
    parity = -1.0 if not info.optics.flip else 1.0  # east left: RA decreases with x
    _card(h, "WCSAXES", 2, "Approximate WCS: mount pointing, nominal scale")
    _card(h, "CTYPE1", "RA---TAN", "TAN projection")
    _card(h, "CTYPE2", "DEC--TAN", "TAN projection")
    _card(h, "CUNIT1", "deg", "")
    _card(h, "CUNIT2", "deg", "")
    _card(h, "CRVAL1", round(ra, 7), "[deg] RA at the reference pixel (mount)")
    _card(h, "CRVAL2", round(dec, 7), "[deg] Dec at the reference pixel (mount)")
    _card(h, "CRPIX1", crpix1, "Reference pixel x (full-chip centre)")
    _card(h, "CRPIX2", crpix2, "Reference pixel y (full-chip centre)")
    _card(h, "CD1_1", parity * scale * binx * math.cos(pa), "[deg/px]")
    _card(h, "CD1_2", -parity * scale * biny * math.sin(pa), "[deg/px]")
    _card(h, "CD2_1", scale * binx * math.sin(pa), "[deg/px]")
    _card(h, "CD2_2", scale * biny * math.cos(pa), "[deg/px]")
    _card(h, "PIXSCALE", round(float(info.optics.plate_scale) * binx, 5), "[arcsec/px] as binned")
    _card(h, "WCSSRC", "mount", "WCS from mount pointing (not solved)")


#: The approximate WCS's cards (replaced by a solved WCS).
WCS_KEYS = (
    "WCSAXES",
    "CTYPE1",
    "CTYPE2",
    "CUNIT1",
    "CUNIT2",
    "CRVAL1",
    "CRVAL2",
    "CRPIX1",
    "CRPIX2",
    "CD1_1",
    "CD1_2",
    "CD2_1",
    "CD2_2",
    "PC1_1",
    "PC1_2",
    "PC2_1",
    "PC2_2",
    "CDELT1",
    "CDELT2",
    "WCSSRC",
)


def replace_wcs(h: fits.Header, cards: list[tuple[str, Any, str]]) -> None:
    """Swap the header's WCS for ``cards`` (a solved one, with its SIP
    terms and provenance): the old cards are removed, the new ones written
    where the old ``WCSAXES`` was (else at the end)."""
    at = h.index("WCSAXES") if "WCSAXES" in h else len(h)
    for key in WCS_KEYS:
        while key in h:
            h.remove(key)
    pos = min(at, len(h))
    for key, value, comment in cards:
        if key in h:
            h[key] = (value, comment)
            continue
        h.insert(pos, fits.Card(key, value, comment[:47] if comment else ""))
        pos += 1


def imagetyp(light: bool, exptime: float, name: str) -> str:
    if not light:
        return "BIAS" if exptime <= 0 else "DARK"
    return "FLAT" if name.lower().startswith(("flat", "skyflat", "domeflat")) else "LIGHT"


# -- the file -----------------------------------------------------------------------


class FrameStore:
    """Where frames go, and the writing itself (module doc). Thread-safe
    per telescope: one writer per telescope at a time (the pipeline's)."""

    def __init__(
        self,
        data_root: str | Path,
        site: str,
        *,
        compress: bool = False,
        rollover_hour: float = 12.0,
        timezone: str | None = None,
        longitude: float | None = None,
        index: bool = True,
        clock: Clock = real_clock,
        solver: Any = None,
    ) -> None:
        self.root = Path(data_root)
        #: Optional plate solver (``robofast_core.framesolve.FrameSolver``):
        #: called with the pixels, the header and the frame's info before
        #: the file is written; it replaces the approximate WCS when it
        #: solves (``WCSSRC``) and adds the photometric zero point, and
        #: returns a summary for the index (docs/sky.md).
        self.solver = solver
        #: Where ``DATE`` and the index's ``written`` come from (the
        #: sequencer's clock, :mod:`robofast_core.clock`).
        self.clock = clock
        self.site = safe_name(site)
        self.compress = compress
        self.rollover_hour = rollover_hour
        self.timezone = timezone
        self.longitude = longitude
        self.index = index
        self._next: dict[tuple[str, date], int] = {}

    def night(self, when: datetime) -> date:
        return night_of(
            when,
            rollover_hour=self.rollover_hour,
            timezone=self.timezone,
            longitude=self.longitude,
        )

    def directory(self, telescope: str, night: date) -> Path:
        return self.root / self.site / safe_name(telescope) / f"n{night:%Y%m%d}"

    def filename(
        self, telescope: str, night: date, obj: str, seq: int, compressed: bool = False
    ) -> str:
        ext = ".fits.fz" if compressed else ".fits"
        return f"n{night:%Y%m%d}.{safe_name(telescope)}.{safe_name(obj)}.{seq:04d}{ext}"

    def _scan(self, d: Path, telescope: str, night: date) -> int:
        pat = re.compile(
            rf"n{night:%Y%m%d}\.{re.escape(safe_name(telescope))}\..+\.(\d{{4,}})\.fits(\.fz)?"
        )
        best = 0
        if d.is_dir():
            for p in d.iterdir():
                m = pat.fullmatch(p.name)
                if m:
                    best = max(best, int(m.group(1)))
        return best + 1

    def write(self, info: FrameInfo, image: ImageArray) -> WrittenFrame:
        """Build the header and write the frame (atomic, never overwrites)."""
        data = storage_array(image)
        header, _ = build_header(info, data.shape, self.clock())
        solved: dict[str, Any] | None = None
        if self.solver is not None and info.light:
            try:
                solved = self.solver(data, header, info)
            except Exception as exc:  # a solve never costs a frame
                log.warning("%s: plate solve failed: %r", info.telescope, exc)
                solved = {"error": f"{type(exc).__name__}: {exc}"}
        # The night: the sequencer's clock at StartExposure, the same clock
        # as every other night name (robofast_core.clock).
        night = self.night(info.host_start)
        d = self.directory(info.telescope, night)
        d.mkdir(parents=True, exist_ok=True)
        key = (info.telescope, night)
        seq = self._next.get(key) or self._scan(d, info.telescope, night)
        _card(header, "NIGHT", f"n{night:%Y%m%d}", "Night (local date of the evening)")
        compressed = self.compress and data.dtype.kind in "iu"  # Rice: integers only
        tmp = d / f".{uuid.uuid4().hex}.tmp"
        try:
            for _ in range(1000):
                name = self.filename(info.telescope, night, info.object, seq, compressed)
                _card(header, "FILENAME", name, "Original file name")
                _card(header, "SEQNUM", seq, "Frame number this telescope and night")
                self._write_tmp(tmp, header, data, compressed)
                final = d / name
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    seq += 1
                    continue
                break
            else:
                raise RuntimeError(f"{d}: no free frame number")
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(d)
        self._next[key] = seq + 1
        rel = final.relative_to(self.root).as_posix()
        record = {
            "file": rel,
            "telescope": info.telescope,
            "object": info.object,
            "imagetyp": info.imagetyp,
            "date_obs": header["DATE-OBS"],
            "exptime": header["EXPTIME"],
            "filter": info.filter,
            "observation": info.obs_name,
            "frame": info.index,
            "seq": seq,
            **{k: v for k, v in info.ids.items() if v is not None},
            "shape": list(data.shape),
            "bytes": final.stat().st_size,
            "written": self.clock().isoformat(),
        }
        if solved:
            record["solve"] = solved
        if self.index:
            self._append_index(
                d / f"n{night:%Y%m%d}.{safe_name(info.telescope)}.frames.jsonl", record
            )
        return WrittenFrame(final, rel, seq, night, header["DATE-OBS"], record["bytes"], record)

    @staticmethod
    def _write_tmp(tmp: Path, header: fits.Header, data: np.ndarray, compressed: bool) -> None:
        if compressed:
            hdus = fits.HDUList(
                [fits.PrimaryHDU(), fits.CompImageHDU(data, header, compression_type="RICE_1")]
            )
        else:
            hdus = fits.HDUList([fits.PrimaryHDU(data, header)])
        with open(tmp, "wb") as f:
            hdus.writeto(f, checksum=True)
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _append_index(path: Path, record: dict[str, Any]) -> None:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
