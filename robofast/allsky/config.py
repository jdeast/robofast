"""The ``allsky`` section of a site's weather config: which all-sky cameras
the site uses (docs/allsky.md).

Listing a camera is what turns the cloud map on. The same section drives
both halves:

* the watcher (``python -m robofast.allsky watch --config <weather.yaml>``)
  analyses each listed camera's newest frame and writes ``latest.json``
  (:mod:`robofast.allsky.watch`);
* the weather bridge makes one **shadow** source per listed camera,
  ``allsky_<camera>``, plus ``allsky``, the pooled map, when two or more
  are listed (:func:`bridge_sources`). Shadow sources are logged with every
  evaluation and never take part in the verdict (docs/weather_bridge.md).

No camera listed (or no section) means no watcher work and no all-sky
source. Adding a camera is a config change: its entry here, plus its
definition and fitted model in the models directory (``cameras.yaml`` and
``<camera>.json``).

This module only knows the section's shape; it imports nothing heavy, so
the weather bridge can use it without the analysis code's dependencies.
"""

from __future__ import annotations

import copy
import os
from typing import Any

__all__ = [
    "BRIDGE_KEYS",
    "CAMERA_KEYS",
    "POOLED",
    "SECTION_KEYS",
    "WATCH_KEYS",
    "bridge_sources",
    "camera_source",
    "check_section",
]

#: The watcher's keys (robofast.allsky.watch).
WATCH_KEYS = frozenset(
    {
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
)
#: The weather bridge's keys: the shadow sources' staleness and the limits
#: their "would" verdicts are judged by (the same for every camera).
BRIDGE_KEYS = frozenset({"stale_after", "limits"})
SECTION_KEYS = WATCH_KEYS | BRIDGE_KEYS
#: Each camera's keys: where its frames are (and, only with ``fetch: true``,
#: where to download them from).
CAMERA_KEYS = frozenset({"dir", "url"})
#: The pooled map's source name.
POOLED = "allsky"


def camera_source(camera: str) -> str:
    """The weather-bridge source name of one camera's map."""
    return f"{POOLED}_{camera}"


def check_section(section: Any, where: str = "allsky") -> dict[str, Any]:
    """The section as a dict, its keys checked (strictly: unknown keys are
    errors). ``None`` is an empty section."""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{where}: must be a mapping")
    extra = set(section) - SECTION_KEYS
    if extra:
        raise ValueError(f"{where}: unknown keys {sorted(extra)}")
    cams = section.get("cameras") or {}
    if not isinstance(cams, dict):
        raise ValueError(f"{where}.cameras: must be a mapping of camera name -> settings")
    for name, spec in cams.items():
        if spec is not None and not isinstance(spec, dict):
            raise ValueError(f"{where}.cameras.{name}: must be a mapping")
        bad = set(spec or {}) - CAMERA_KEYS
        if bad:
            raise ValueError(f"{where}.cameras.{name}: unknown keys {sorted(bad)}")
    return section


def bridge_sources(section: Any, where: str = "allsky") -> dict[str, dict[str, Any]]:
    """The weather-bridge source specs for the section: ``allsky_<camera>``
    for each listed camera and, with two or more, ``allsky`` (the pooled
    map). Empty when no camera is listed. Every one is type ``allsky``
    (shadow only)."""
    section = check_section(section, where)
    cams = list(section.get("cameras") or {})
    if not cams:
        return {}
    out = section.get("output")
    if not out:
        raise ValueError(f"{where}.output: needed (the watcher's latest.json)")
    path = os.path.expandvars(os.path.expanduser(str(out)))
    if not os.path.isabs(path):
        raise ValueError(f"{where}.output: must be absolute (or start with ~): {out}")
    base: dict[str, Any] = {"type": "allsky", "path": path}
    if "stale_after" in section:
        base["stale_after"] = section["stale_after"]
    if section.get("limits"):
        base["limits"] = section["limits"]
    specs = {camera_source(c): {**copy.deepcopy(base), "camera": c} for c in cams}
    if len(cams) > 1:
        specs[POOLED] = copy.deepcopy(base)
    return specs
