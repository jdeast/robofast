"""Spectrograph calibration lamps, as the sequencer uses them (docs/pulizzi.md).

A telescope whose spectrograph has calibration lamps (the FLWO 60-inch's
TRES: ThAr and incandescent lamps on a Pulizzi power switch) has a
``lamps`` entry in the enclosure config::

    "lamps": {
      "switch": "127.0.0.1:11170/0",   # robofast's lamp Switch server
      "frame_overhead": 30,            # s per frame beyond its exposure (readout)
      "lamps": {
        "ThAr1": {"warmup": 60, "calstage": "in"},
        "Incand": {"warmup": 30, "calstage": "in"}
      }
    }

The names are the switch's outlet names (``Action("Status")``). ``warmup``
is how long the lamp burns before the first exposure; ``calstage`` the
calibration-stage position the lamp light needs (selected **before** the
lamp is switched on; an observation's own ``calstage`` wins; null: leave
the stage alone).

The switch is :mod:`robofast_core.devices.pulizzi_server` (an Alpaca
Switch with the ``Status`` and ``LampsOff`` actions); this module reads and
switches it through a device owner's grant.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

#: Observation kinds that switch a lamp (request types ARC and LAMP_FLAT).
LAMP_KINDS = ("arc", "lamp_flat")


@dataclass
class LampSpec:
    #: Seconds the lamp burns before the first exposure.
    warmup: float = 0.0
    #: Calibration-stage position for this lamp's light (None: leave it).
    calstage: str | None = None


@dataclass
class LampSetup:
    switch: str
    lamps: dict[str, LampSpec] = field(default_factory=dict)
    #: Per-frame time beyond the exposure (readout, saving), for checking
    #: that an observation fits in the lamp's max-on time before starting.
    frame_overhead: float = 30.0
    #: Bound on one switch command (the server reads the unit back).
    switch_timeout: float = 15.0

    @classmethod
    def from_dict(cls, d: Any) -> LampSetup:
        if not isinstance(d, dict):
            raise ValueError("lamps: expected an object")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = {"switch", "lamps", "frame_overhead", "switch_timeout"}
        if set(d) - known:
            raise ValueError(f"lamps: unknown keys {sorted(set(d) - known)}")
        if not isinstance(d.get("switch"), str) or not d["switch"]:
            raise ValueError("lamps.switch: the lamp switch's Alpaca address")
        raw = d.get("lamps") or {}
        if not isinstance(raw, dict) or not raw:
            raise ValueError("lamps.lamps: at least one lamp")
        lamps = {}
        for name, spec in raw.items():
            if name.startswith("_"):
                continue
            spec = {k: v for k, v in dict(spec or {}).items() if not k.startswith("_")}
            if set(spec) - {"warmup", "calstage"}:
                raise ValueError(
                    f"lamps.lamps.{name}: unknown keys {sorted(set(spec) - {'warmup', 'calstage'})}"
                )
            ls = LampSpec(**spec)
            if not isinstance(ls.warmup, int | float) or not 0 <= ls.warmup <= 3600:
                raise ValueError(f"lamps.lamps.{name}.warmup: 0 to 3600 s")
            if ls.calstage is not None and not isinstance(ls.calstage, str):
                raise ValueError(f"lamps.lamps.{name}.calstage: a position name or null")
            lamps[str(name)] = ls
        setup = cls(
            d["switch"],
            lamps,
            float(d.get("frame_overhead", 30.0)),
            float(d.get("switch_timeout", 15.0)),
        )
        for k in ("frame_overhead", "switch_timeout"):
            v = getattr(setup, k)
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"lamps.{k} must be positive")
        return setup

    def duration(self, name: str, exptime: float, count: int) -> float:
        """How long ``count`` frames with lamp ``name`` keep it on (s)."""
        return self.lamps[name].warmup + count * (exptime + self.frame_overhead)


async def switch_status(grant: Any, timeout: float) -> dict[str, dict[str, Any]]:
    """The switch's outlets by name (its ``Action("Status")``)."""
    raw = await grant.call("Action", "Status", "", timeout=timeout)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return {o["name"]: o for o in data["outlets"]}


async def lamps_off(grant: Any, timeout: float) -> list[str]:
    """Every lamp off (``Action("LampsOff")``); the names switched off."""
    raw = await grant.call("Action", "LampsOff", "", timeout=timeout)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return list(data.get("off", []))
