"""The console's images: each guider's last frame, each telescope's latest
saved frame, and the spectrograph's latest raw frame (docs/console.md,
"Images").

Sources, per enclosure:

* ``guider-<tel>`` -- the guider service's ``GET /frame`` (its last frame,
  binned on the guide computer to at most ``guider_frame_max`` pixels a
  side), with what to draw over it: the target (fiber) and its radius, the
  last centroid, the guide window;
* ``imager-<tel>`` -- the newest frame in the telescope's nightly frame
  index (``<data_root>/<site>/<tel>/n<night>/...frames.jsonl``,
  docs/data.md), read from disk;
* ``spectrograph`` -- the newest ``*.fits`` in the console's
  ``spectrograph_frames`` directory (where the spectrograph PC's raw frames
  land; its night subdirectories are searched too).

A source is read at most every ``min_interval`` (guiders) or ``file_poll``
(files) seconds however many browsers watch it, and the latest frame is
kept, so a click for a pixel value or a FITS download reads that same
frame. Frames are scaled for the browser by :func:`robofast_core.frameview.render`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from robofast_core.console.aggregate import ConsoleEnclosure
from robofast_core.frameview import decode_array, scale_limits

log = logging.getLogger(__name__)

#: Largest frame read from disk (pixels); bigger ones are refused, not read.
MAX_PIXELS = 80_000_000


class NoFrame(Exception):
    """The source has no frame (yet), or it could not be read."""


@dataclass
class ViewFrame:
    """One frame as the console holds it."""

    id: str
    data: np.ndarray
    meta: dict[str, Any]
    #: The file it came from (None: a guider frame, built on demand for FITS).
    path: Path | None = None
    fetched: float = field(default_factory=time.monotonic)
    limits: dict[str, tuple[float, float]] = field(default_factory=dict)

    def limits_for(self, scale: str) -> tuple[float, float]:
        """Display limits from the whole frame (cached), so panning and
        zooming keep the same stretch."""
        if scale not in self.limits:
            self.limits[scale] = scale_limits(self.data, scale)
        return self.limits[scale]

    def fits_bytes(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        from astropy.io import fits

        hdu = fits.PrimaryHDU(self.data)
        for key, card in (
            ("EXPTIME", "exposure"),
            ("DATE-END", "utc_end"),
            ("BINNING", "binning"),
        ):
            v = self.meta.get(card)
            if isinstance(v, int | float | str):
                hdu.header[key] = v
        origin = self.meta.get("origin") or [0, 0]
        hdu.header["LTV1"] = -float(origin[0])
        hdu.header["LTV2"] = -float(origin[1])
        hdu.header["COMMENT"] = "robofast console: a display copy of a guider frame"
        buf = io.BytesIO()
        hdu.writeto(buf)
        return buf.getvalue()


def read_fits(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """The first 2-D image in ``path`` and a few header values."""
    from astropy.io import fits

    with fits.open(path, memmap=False, lazy_load_hdus=True) as hdul:
        for hdu in hdul:
            shape = getattr(hdu, "shape", None) or ()
            if len(shape) == 2:
                if shape[0] * shape[1] > MAX_PIXELS:
                    raise NoFrame(f"{path.name}: {shape} is too large to show")
                data = np.asarray(hdu.data, dtype=np.float32)
                h = hdu.header
                meta = {
                    k: h.get(key)
                    for k, key in (
                        ("object", "OBJECT"),
                        ("exposure", "EXPTIME"),
                        ("date_obs", "DATE-OBS"),
                        ("imagetyp", "IMAGETYP"),
                        ("filter", "FILTER"),
                    )
                    if isinstance(h.get(key), str | int | float)
                }
                return data, meta
    raise NoFrame(f"{path.name}: no 2-D image")


def _last_line(path: Path, tail: int = 65536) -> dict[str, Any] | None:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            lines = f.read().split(b"\n")
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            return e if isinstance(e, dict) else None
    return None


def newest_indexed_frame(root: Path, site: str, telescope: str) -> Path | None:
    """The newest frame in ``telescope``'s newest nightly index (docs/data.md)."""
    nights = sorted((root / site / telescope).glob("n[0-9]*"))
    for night in reversed(nights[-2:]):
        entry = _last_line(night / f"{night.name}.{telescope}.frames.jsonl")
        if entry is not None and isinstance(entry.get("file"), str):
            p = root / entry["file"]
            if p.resolve().is_relative_to(root.resolve()):
                return p
    return None


def newest_fits(directory: Path) -> Path | None:
    """The newest ``*.fits`` in ``directory`` or its newest subdirectory."""
    try:
        subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
    except OSError:
        return None
    cands = list(directory.glob("*.fits")) + list(directory.glob("*.fits.fz"))
    for d in subdirs[-1:]:
        cands += list(d.glob("*.fits")) + list(d.glob("*.fits.fz"))
    best, best_t = None, -1.0
    for p in cands:
        try:
            t = p.stat().st_mtime
        except OSError:
            continue
        if t > best_t:
            best, best_t = p, t
    return best


class ImageSources:
    """Every enclosure's image sources and their latest frames."""

    def __init__(
        self,
        enclosures: dict[str, ConsoleEnclosure],
        http: httpx.AsyncClient,
        *,
        timeout: float = 2.0,
        min_interval: float = 0.4,
        file_poll: float = 2.0,
        guider_frame_max: int = 256,
    ) -> None:
        self.enclosures = enclosures
        self.http = http
        self.timeout = timeout
        self.min_interval = min_interval
        self.file_poll = file_poll
        self.guider_frame_max = guider_frame_max
        self._frames: dict[tuple[str, str], ViewFrame] = {}
        self._checked: dict[tuple[str, str], float] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def sources(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name, enc in self.enclosures.items():
            srcs = []
            for tel in enc.telescopes:
                if tel in enc.guiders:
                    srcs.append(
                        {
                            "id": f"guider-{tel}",
                            "kind": "guider",
                            "telescope": tel,
                            "label": f"{tel} guider",
                        }
                    )
            for tel in enc.telescopes:
                if enc.images_root is not None:
                    srcs.append(
                        {
                            "id": f"imager-{tel}",
                            "kind": "imager",
                            "telescope": tel,
                            "label": f"{tel} latest frame",
                        }
                    )
            if enc.spectrograph_frames is not None:
                srcs.append(
                    {
                        "id": "spectrograph",
                        "kind": "spectrograph",
                        "telescope": None,
                        "label": "spectrograph latest frame",
                    }
                )
            out[name] = srcs
        return out

    def _known(self, enclosure: str, source: str) -> tuple[ConsoleEnclosure, str, str | None]:
        enc = self.enclosures.get(enclosure)
        if enc is None:
            raise KeyError(enclosure)
        if source == "spectrograph" and enc.spectrograph_frames is not None:
            return enc, "spectrograph", None
        kind, _, tel = source.partition("-")
        if kind == "guider" and tel in enc.guiders:
            return enc, kind, tel
        if kind == "imager" and tel in enc.telescopes and enc.images_root is not None:
            return enc, kind, tel
        raise KeyError(source)

    async def frame(self, enclosure: str, source: str) -> ViewFrame:
        """The source's latest frame (fetched or re-checked if due).
        Raises KeyError (no such source) or NoFrame."""
        enc, kind, tel = self._known(enclosure, source)
        key = (enclosure, source)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            have = self._frames.get(key)
            every = self.min_interval if kind == "guider" else self.file_poll
            if have is not None and now - self._checked.get(key, 0.0) < every:
                return have
            self._checked[key] = now
            if kind == "guider":
                assert tel is not None
                fresh = await self._guider(enc, tel)
            else:
                path = (
                    newest_fits(enc.spectrograph_frames)  # type: ignore[arg-type]
                    if kind == "spectrograph"
                    else newest_indexed_frame(enc.images_root, enc.images_site, tel)  # type: ignore[arg-type]
                )
                if path is None:
                    raise NoFrame(f"{source}: no frame yet")
                if have is not None and have.path == path:
                    return have
                fresh = await asyncio.to_thread(self._file, path, kind)
            self._frames[key] = fresh
            return fresh

    async def _guider(self, enc: ConsoleEnclosure, tel: str) -> ViewFrame:
        url = f"{enc.guiders[tel]}/frame?max={self.guider_frame_max}"
        try:
            r = await self.http.get(url, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise NoFrame(f"{tel} guider unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise NoFrame(f"{tel} guider: {r.status_code} {r.text[:100]}")
        try:
            doc = r.json()
            data = decode_array(doc)
        except (ValueError, KeyError, TypeError) as exc:
            raise NoFrame(f"{tel} guider: bad frame ({exc})") from exc
        b = float(doc.get("binning") or 1)
        roi = doc.get("roi") or {"x": 0, "y": 0}
        ox, oy = float(roi.get("x", 0)), float(roi.get("y", 0))

        def to_frame(x: Any, y: Any) -> list[float] | None:
            """Detector pixel -> frame pixel (binned, within the ROI)."""
            if not isinstance(x, int | float) or not isinstance(y, int | float):
                return None
            return [(x - ox) / b, (y - oy) / b]

        overlays: dict[str, Any] = {}
        t = doc.get("target")
        if isinstance(t, list) and len(t) == 2 and (p := to_frame(*t)) is not None:
            r = float(doc.get("fiber_radius") or 0) / b
            overlays["target"] = {"x": p[0], "y": p[1], "r": r}
        s = doc.get("star")
        if isinstance(s, dict) and (p := to_frame(s.get("x"), s.get("y"))) is not None:
            overlays["star"] = {"x": p[0], "y": p[1], "fwhm": s.get("fwhm")}
        w = doc.get("guide_roi")
        if isinstance(w, dict) and (p := to_frame(w.get("x"), w.get("y"))) is not None:
            overlays["window"] = {
                "x0": p[0],
                "y0": p[1],
                "x1": p[0] + float(w.get("width", 0)) / b,
                "y1": p[1] + float(w.get("height", 0)) / b,
            }
        meta = {
            "kind": "guider",
            "telescope": tel,
            "count": doc.get("count"),
            "exposure": doc.get("exposure"),
            "utc_end": doc.get("utc_end"),
            "state": doc.get("state"),
            "binning": b,
            "origin": [ox, oy],
            "overlays": overlays,
        }
        fid = hashlib.sha256(f"{tel}|{doc.get('count')}|{doc.get('utc_end')}".encode()).hexdigest()
        return ViewFrame(fid[:16], data, meta)

    @staticmethod
    def _file(path: Path, kind: str) -> ViewFrame:
        try:
            data, meta = read_fits(path)
            st = path.stat()
        except NoFrame:
            raise
        except Exception as exc:  # a broken or half-copied file: say so
            raise NoFrame(f"{path.name}: {type(exc).__name__}: {exc}") from exc
        meta.update(kind=kind, file=path.name, binning=1, origin=[0, 0], overlays={})
        fid = hashlib.sha256(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
        return ViewFrame(fid[:16], data, meta, path=path)
