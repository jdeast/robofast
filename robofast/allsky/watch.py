"""``python -m robofast.allsky watch --config <yaml>``: the shadow cloud map.

A small loop: every ``interval`` seconds take the newest frame of each
camera, analyse it (:func:`robofast.allsky.clouds.analyze`), pool the
cameras whose frames are within ``max_age`` of each other, and write
``latest.json`` atomically (a temporary file renamed over it). The weather
bridge's ``allsky`` source reads that file in *shadow* mode: logged with
every evaluation, never part of the safe/unsafe decision
(docs/allsky.md, docs/weather_bridge.md).

Frames come from a directory laid out like the site fetcher's,
``<dir>/<UTdate>/<camera>_<YYYYMMDDTHHMMSS>Z.<ext>``, or, only when the
config says ``fetch: true``, from each camera's ``url`` (saved under that
same layout in ``fetch_dir``, named by the server's Last-Modified).

Config (YAML)::

    output: ~/allsky/shadow/latest.json
    history: ~/allsky/shadow/history.jsonl   # optional: the trend's memory
    interval: 60          # s between passes
    max_age: 180          # s: pool cameras whose frames are this close
    stale: 600            # s: ignore a camera whose newest frame is older
    trend_frames: 10
    fetch: false          # true: download from the cameras' urls
    fetch_dir: ~/allsky   # where downloads go (fetch: true only)
    models: null          # directory of <camera>.json (default: the package's)
    cameras:
      mmto: {dir: ~/allsky/mmto}
      ridge: {dir: ~/allsky/ridge, url: "http://linmax.sao.arizona.edu/weather/sky/last_sky.jpg"}
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from robofast.allsky import cameras, clouds, frames
from robofast.allsky.model import CameraModel

log = logging.getLogger(__name__)

__all__ = ["WatchConfig", "Watcher", "latest_frame", "write_atomic"]

_KEYS = {
    "output",
    "history",
    "interval",
    "max_age",
    "stale",
    "trend_frames",
    "fetch",
    "fetch_dir",
    "models",
    "cameras",
}
_CAMERA_KEYS = {"dir", "url"}


@dataclass
class WatchConfig:
    output: Path
    cameras: dict[str, dict[str, Any]]
    history: Path | None = None
    interval: float = 60.0
    max_age: float = 180.0
    stale: float = 600.0
    trend_frames: int = 10
    fetch: bool = False
    fetch_dir: Path = field(default_factory=lambda: Path("~/allsky").expanduser())
    models: Path | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> WatchConfig:
        extra = set(raw) - _KEYS
        if extra:
            raise ValueError(f"unknown watch config keys: {sorted(extra)}")
        if not raw.get("cameras"):
            raise ValueError("watch config: no cameras")
        cams = {}
        for name, spec in raw["cameras"].items():
            spec = dict(spec or {})
            bad = set(spec) - _CAMERA_KEYS
            if bad:
                raise ValueError(f"camera {name}: unknown keys {sorted(bad)}")
            cameras.camera_spec(name)  # a known camera
            if "dir" in spec:
                spec["dir"] = Path(spec["dir"]).expanduser()
            cams[name] = spec

        def path(key: str) -> Path | None:
            v = raw.get(key)
            return None if v is None else Path(v).expanduser()

        cfg = cls(output=path("output") or Path("latest.json"), cameras=cams)
        cfg.history = path("history")
        cfg.models = path("models")
        if raw.get("fetch_dir"):
            cfg.fetch_dir = Path(raw["fetch_dir"]).expanduser()
        for key in ("interval", "max_age", "stale"):
            if key in raw:
                setattr(cfg, key, float(raw[key]))
        cfg.trend_frames = int(raw.get("trend_frames", cfg.trend_frames))
        fetch = raw.get("fetch", False)
        if not isinstance(fetch, bool):
            raise ValueError("watch config: fetch must be true or false")
        cfg.fetch = fetch
        for name, spec in cams.items():
            if "dir" not in spec and not (cfg.fetch and spec.get("url")):
                raise ValueError(f"camera {name}: needs a dir (or a url with fetch: true)")
        return cfg


def load_config(path: str | Path) -> WatchConfig:
    import yaml

    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: the top level must be a mapping")
    return WatchConfig.parse(raw)


def latest_frame(root: Path, camera: str) -> Path | None:
    """The newest ``<root>/<UTdate>/<camera>_*Z.*`` frame."""
    for d in sorted(root.glob("[0-9]" * 8), reverse=True):
        fs = sorted(p for p in d.glob(f"{camera}_*Z.*") if not p.name.startswith("."))
        if fs:
            return fs[-1]
    return None


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader sees the old or the new file, whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch_frame(url: str, root: Path, camera: str, timeout: float = 30.0) -> Path | None:
    """Download ``url`` into ``root/<UTdate>/<camera>_<Last-Modified>Z.<ext>``
    unless that file exists; returns its path."""
    import httpx

    with httpx.Client(trust_env=False, timeout=timeout) as http:
        head = http.head(url)
        head.raise_for_status()
        lm = head.headers.get("last-modified")
        when = parsedate_to_datetime(lm).astimezone(UTC) if lm else datetime.now(UTC)
        ext = Path(url.split("?")[0]).suffix or ".jpg"
        dest = root / f"{when:%Y%m%d}" / f"{camera}_{when:%Y%m%dT%H%M%S}Z{ext}"
        if dest.is_file():
            return dest
        r = http.get(url)
        r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("." + dest.name)
    tmp.write_bytes(r.content)
    os.replace(tmp, dest)
    return dest


class Watcher:
    """One pass: newest frames -> pooled map -> ``latest.json``."""

    def __init__(self, cfg: WatchConfig) -> None:
        self.cfg = cfg
        self.models: dict[str, CameraModel] = {}
        self._last: dict[str, tuple[Path, Any]] = {}
        self.history: list[clouds.CloudMap] = []
        if cfg.history and cfg.history.is_file():
            for line in cfg.history.read_text().splitlines()[-cfg.trend_frames :]:
                try:
                    self.history.append(clouds.map_from_json(json.loads(line)))
                except (ValueError, KeyError):
                    continue

    def model(self, camera: str) -> CameraModel:
        if camera not in self.models:
            d = self.cfg.models or cameras.MODELS_DIR
            self.models[camera] = CameraModel.load(d / f"{camera}.json")
        return self.models[camera]

    def newest(self, camera: str) -> Path | None:
        spec = self.cfg.cameras[camera]
        if self.cfg.fetch and spec.get("url"):
            try:
                fetch_frame(spec["url"], spec.get("dir") or self.cfg.fetch_dir / camera, camera)
            except Exception as exc:  # a camera down is not an error of the loop
                log.warning("%s: fetch failed: %s", camera, exc)
        root = spec.get("dir") or self.cfg.fetch_dir / camera
        return latest_frame(Path(root), camera)

    def step(self, now: datetime | None = None) -> dict[str, Any]:
        """Analyse the newest frames and write ``latest.json``; returns it."""
        now = now or datetime.now(UTC)
        items = []
        frame_info = []
        errors = []
        for cam in self.cfg.cameras:
            try:
                p = self.newest(cam)
                if p is None:
                    frame_info.append({"camera": cam, "frame": None, "age_s": None, "used": False})
                    continue
                t = frames.time_from_name(p)
                age = (now - t).total_seconds() if t else math.inf
                used = age <= self.cfg.stale
                frame_info.append(
                    {"camera": cam, "frame": p.name, "age_s": round(age, 1), "used": used}
                )
                if not used:
                    continue
                cached = self._last.get(cam)
                if cached and cached[0] == p:
                    meas = cached[1]
                else:
                    f = frames.load_frame(p, cam)
                    _, meas = clouds.analyze(f, cameras.camera_spec(cam), self.model(cam))
                    meas.bg = None  # keep memory down between passes
                    meas.valid = None
                    meas.frame.grey = None
                    meas.frame.rgb = None
                    self._last[cam] = (p, meas)
                items.append((meas, self.model(cam)))
            except Exception as exc:
                log.warning("%s: %s", cam, exc)
                errors.append(f"{cam}: {type(exc).__name__}: {exc}")
        doc: dict[str, Any] = {
            "written": now.isoformat(),
            "frames": frame_info,
            "errors": errors,
        }
        if not items:
            doc.update(time=None, whole_sky_clear=None, coverage=0.0, cells=[], trend=None)
        else:
            cmap = clouds.combine(items, max_age_s=self.cfg.max_age)
            if not self.history or cmap.time > self.history[-1].time:
                self.history = [*self.history, cmap][-self.cfg.trend_frames :]
                if self.cfg.history:
                    self.cfg.history.parent.mkdir(parents=True, exist_ok=True)
                    with self.cfg.history.open("a") as fh:
                        fh.write(json.dumps(cmap.to_json()) + "\n")
            m = cmap.to_json()
            z60 = cmap.clear_fraction(90.0, 0.0, 60.0)
            doc.update(
                time=m["time"],
                cameras=m["cameras"],
                whole_sky_clear=m["whole_sky_clear"],
                zenith60_clear=None if math.isnan(z60) else round(z60, 4),
                coverage=m["coverage"],
                sun_alt=m["sun_alt"],
                moon_alt=m["moon_alt"],
                moon_illum=m["moon_illum"],
                flags=m["flags"],
                cells=m["patches"],
                trend=clouds.trend(self.history, n=self.cfg.trend_frames).to_json(),
            )
        write_atomic(self.cfg.output, json.dumps(doc, indent=1) + "\n")
        return doc

    def run(self, passes: int | None = None) -> None:
        n = 0
        while passes is None or n < passes:
            t0 = time.monotonic()
            try:
                doc = self.step()
                log.info(
                    "clear %s coverage %s frames %s",
                    doc.get("whole_sky_clear"),
                    doc.get("coverage"),
                    [(f["camera"], f["age_s"]) for f in doc["frames"]],
                )
            except Exception:
                log.exception("watch pass failed")
            n += 1
            if passes is not None and n >= passes:
                break
            time.sleep(max(1.0, self.cfg.interval - (time.monotonic() - t0)))
