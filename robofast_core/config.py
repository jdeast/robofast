"""Enclosure config files, shared by the supervisor and sequencer services.

One file describes one enclosure: its devices, its telescopes, and the
settings of the processes that run it. The safety supervisor
(:mod:`robofast_core.supervisor_service`) and the sequencer
(:mod:`robofast_core.sequencer_service`) read the same file, so they agree on
which telescopes the enclosure holds. ``sites/minerva/aqawan1.json`` is an
example; its shape::

    {
      "name": "aqawan1",                          # the enclosure
      "listen": "127.0.0.1:8100",                 # the supervisor's API
      "devices": {"dome": "...", "safetymonitor": "...", ...},
      "site": {"latitude": ..., "longitude": ..., "elevation": ...},
      "telescopes": {
        "t1": {"telescope": "host:port/N", "camera": "host:port/N",
               "filterwheel": "host:port/N",      # optional
               "calstage": "host:port/N",         # optional: TRES front end
               "lamps": {"switch": "host:port/N", ...},  # optional: docs/pulizzi.md
               "adc": "host:port/N",              # optional: docs/adc.md
               "port_action": "..."},             # optional: M3 port Action
        "t2": {...}
      },
      "safety": {...SafetyConfig fields...},
      "sequencer": {...}                          # sequencer_service's settings
    }

A device address is Alpaca ``host:port``, optionally ``host:port/N`` for
device number N. Telescope names are the names the plan and the logs use.
``site`` is where the enclosure is (deg north, deg east, m), for
:mod:`robofast_core.when`; a dispatch plan entry carries its own.

The format is JSON. A file ending in ``.yaml`` or ``.yml`` is read as YAML
if PyYAML is installed (it is not yet a core dependency).
"""

from __future__ import annotations

import json
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
