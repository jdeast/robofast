"""Enclosure config files, shared by the supervisor and sequencer services.

One file describes one enclosure: its devices, its telescopes, and the
settings of the processes that run it. The safety supervisor
(:mod:`robofast.supervisor_service`) and the sequencer
(:mod:`robofast.sequencer_service`) read the same file, so they agree on
which telescopes the enclosure holds. ``sites/minerva/aqawan1.json`` is an
example; its shape::

    {
      "name": "aqawan1",                          # the enclosure
      "listen": "127.0.0.1:8100",                 # the supervisor's API
      "devices": {"dome": "...", "safetymonitor": "...", ...},
      "site": {"latitude": ..., "longitude": ..., "elevation": ...},
      "telescopes": {
        "t1": {"telescope": "host:port/N",
               "camera": "host:port/N",           # or {"address": ..., "cooling": ...}
               "filterwheel": "host:port/N",      # optional
               "calstage": "host:port/N",         # optional: TRES front end
               "lamps": {"switch": "host:port/N", ...},  # optional: docs/pulizzi.md
               "adc": "host:port/N",              # optional: docs/adc.md
               "limits": {"min_alt": 21, "max_alt": 83},  # manual-slew limits, deg
               "port_action": "..."},             # optional: M3 port Action
        "t2": {...}
      },
      "safety": {...SafetyConfig fields...},
      "sequencer": {...}                          # sequencer_service's settings
    }

A device address is Alpaca ``host:port``, optionally ``host:port/N`` for
device number N. Telescope names are the names the plan and the logs use.
``site`` is where the enclosure is (deg north, deg east, m), for
:mod:`robofast.when`; a dispatch plan entry carries its own.
A telescope's ``camera`` is its address, or the camera's own config
``{"address": "host:port/N", "cooling": {...}}`` (:func:`camera_entry`;
the cooler setpoint belongs to the camera: docs/cooling.md).
A telescope's ``limits`` are the altitudes the observer console allows a
manual slew between (:func:`altitude_limits`); without them (and without a
dispatch plan that lists the telescope) the console refuses manual slews.
Site values are never defaulted in core: a missing one is an error naming
the key and the file (docs/design.md 3.10).

The format is JSON. A file ending in ``.yaml`` or ``.yml`` is read as YAML
if PyYAML is installed (it is not yet a core dependency).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit(
                f"{path}: reading YAML needs PyYAML (pip install pyyaml), or use JSON"
            ) from exc
        config = yaml.safe_load(text)
    else:
        config = json.loads(text)
    if not isinstance(config, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return config


def device_address(spec: str) -> tuple[str, int]:
    """``"host:port/N"`` -> ``("host:port", N)``; N defaults to 0."""
    address, _, number = spec.partition("/")
    return address, int(number or 0)


def telescopes(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The enclosure's telescopes: name -> {"telescope": spec, "camera": spec,
    ...}."""
    entries = config.get("telescopes") or {}
    for name, entry in entries.items():
        if not isinstance(entry, dict) or "telescope" not in entry:
            raise ValueError(f"telescope {name!r}: expected {{'telescope': ..., 'camera': ...}}")
    return entries


#: The keys of a telescope's ``camera`` entry in its dict form.
CAMERA_KEYS = frozenset({"address", "cooling"})


def camera_entry(name: str, entry: dict[str, Any], where: str = "") -> tuple[str, Any]:
    """A telescope entry's camera: ``(address, cooling)``. ``camera`` is an
    Alpaca address, or the camera's own config ``{"address": "host:port/N",
    "cooling": {...}}``; ``cooling`` is its raw entry
    (:class:`~robofast.cooling.CoolingConfig`; docs/cooling.md), None when the
    camera has none. ``where`` (the config file) goes into the error
    messages."""
    pre = f"{where + ': ' if where else ''}telescopes.{name}"
    cam = entry.get("camera")
    if isinstance(cam, str) and cam:
        return cam, None
    if not isinstance(cam, dict) or not isinstance(cam.get("address"), str) or not cam["address"]:
        raise ValueError(
            f'{pre}.camera: an Alpaca address, or {{"address": "host:port/N", "cooling": {{...}}}}'
        )
    unknown = {k for k in cam if not str(k).startswith("_")} - CAMERA_KEYS
    if unknown:
        raise ValueError(f"{pre}.camera: unknown keys {sorted(unknown)}")
    return cam["address"], cam.get("cooling")


def altitude_limits(
    name: str, entry: dict[str, Any], where: str = ""
) -> tuple[float, float] | None:
    """A telescope entry's ``limits`` ``{"min_alt": deg, "max_alt": deg}``
    as ``(min_alt, max_alt)``; None when the entry has none. ``where`` (the
    config file) goes into the error messages."""
    lim = entry.get("limits")
    if lim is None:
        return None
    what = f"{where + ': ' if where else ''}telescopes.{name}.limits"
    if not isinstance(lim, dict) or set(lim) != {"min_alt", "max_alt"}:
        raise ValueError(f"{what}: expected {{'min_alt': deg, 'max_alt': deg}}")
    lo, hi = float(lim["min_alt"]), float(lim["max_alt"])
    if not -90.0 <= lo < hi <= 90.0:
        raise ValueError(f"{what}: need -90 <= min_alt < max_alt <= 90")
    return lo, hi


def require_keys(d: dict[str, Any], keys: Iterable[str], what: str = "") -> None:
    """Raise ValueError naming every key of ``keys`` missing from ``d``.

    Site, telescope and instrument values (coordinates, apertures, names,
    positions, ...) have no defaults in core: a site that leaves one out gets
    this error, never another site's number (docs/design.md 3.10). Callers
    that know the file prefix the message with its path."""
    missing = [k for k in keys if k not in d]
    if missing:
        where = f"{what}: " if what else ""
        raise ValueError(f"{where}missing required key(s) {', '.join(missing)}")


def load_device_config[T](path: str | Path, from_dict: Callable[[dict[str, Any]], T]) -> T:
    """A device server's config file through its ``from_dict``; any error
    (a missing required key, a bad value) exits with status 2, naming the
    file."""
    try:
        with open(path) as f:
            return from_dict(json.load(f))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
