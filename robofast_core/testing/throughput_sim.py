"""Months of zero points with a known throughput history (docs/throughput.md).

:func:`simulate` makes the store records a telescope would leave: each
night some frames (or none: the roof stayed shut), each frame's zero point
what the sky simulator (:mod:`robofast_core.testing.skysim`) renders and the
frame solver measures -- the port's zero point
(:class:`~robofast_core.photometry.PortPhotometry`) times the injected
throughput and the cloud transparency, less extinction at the frame's
airmass, plus measurement noise -- without rendering every frame (a year of
nights in milliseconds). ``tests/core/test_throughput.py`` checks one path
through real rendered and plate-solved frames as well.

The truth is in :class:`History` (a slow decline, cleanings and recoats that
restore it, sudden losses) and :class:`Weather` (closed nights, patchy
cloud, uniform thin cirrus, runs of bad nights).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from robofast_core.photometry import PortPhotometry

__all__ = ["History", "Weather", "simulate"]


@dataclass
class History:
    """The injected throughput (1 = as new) against the night index."""

    #: Fractional loss per 30 days (multiplicative: dust on the mirrors).
    decline_per_30d: float = 0.025
    #: Night index -> throughput just after a cleaning or recoat.
    restores: dict[int, float] = field(default_factory=dict)
    #: (first night, last night, factor): a sudden loss for a while (dew, a cover).
    losses: list[tuple[int, int, float]] = field(default_factory=list)
    start: float = 1.0

    def __call__(self, i: int) -> float:
        level, since = self.start, 0
        for n in sorted(self.restores):
            if n <= i:
                level, since = self.restores[n], n
        t = level * (1.0 - self.decline_per_30d) ** ((i - since) / 30.0)
        for a, b, f in self.losses:
            if a <= i <= b:
                t *= f
        return t


@dataclass
class Weather:
    """Which nights are clear (fractions of nights)."""

    closed: float = 0.15
    patchy: float = 0.25
    cirrus: float = 0.10
    #: (first night, length): runs of patchy nights (a monsoon week).
    runs: list[tuple[int, int]] = field(default_factory=list)

    def kind(self, i: int, rng: np.random.Generator) -> str:
        if any(a <= i < a + n for a, n in self.runs):
            return "patchy"
        u = rng.random()
        if u < self.closed:
            return "closed"
        if u < self.closed + self.patchy:
            return "patchy"
        if u < self.closed + self.patchy + self.cirrus:
            return "cirrus"
        return "clear"

    @staticmethod
    def transparency(kind: str, n: int, rng: np.random.Generator) -> np.ndarray:
        if kind == "patchy":  # clouds come and go: some frames lose most of the light
            c = rng.uniform(0.3, 0.9)
            return np.clip(1.0 - c * rng.uniform(0.3, 1.3, n), 0.05, 1.0)
        if kind == "cirrus":  # a uniform veil all night
            return np.full(n, rng.uniform(0.8, 0.95)) * rng.uniform(0.99, 1.0, n)
        return np.ones(n)


def simulate(
    photometry: PortPhotometry,
    history: History,
    weather: Weather,
    *,
    nights: int,
    start: date = date(2026, 1, 1),
    frames_per_night: int = 12,
    airmass: tuple[float, float] = (1.0, 2.0),
    noise: float = 0.03,
    telescope: str = "t2",
    port: str | None = "fau",
    filt: str | None = None,
    unit: str = "e-/s",
    seed: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(store records, truth per night: ``night``, ``throughput``, ``weather``)."""
    rng = np.random.default_rng(seed)
    recs, truth = [], []
    for i in range(nights):
        d = start + timedelta(days=i)
        night = f"n{d:%Y%m%d}"
        kind = weather.kind(i, rng)
        thr = history(i)
        truth.append({"night": night, "i": i, "throughput": thr, "weather": kind})
        if kind == "closed":
            continue
        tr = weather.transparency(kind, frames_per_night, rng)
        x = rng.uniform(*airmass, frames_per_night)
        t0 = datetime(d.year, d.month, d.day, 3, 0, tzinfo=UTC) + timedelta(days=1)
        for f in range(frames_per_night):
            zp = (
                photometry.zp
                + 2.5 * math.log10(thr * tr[f])
                - photometry.extinction * x[f]
                + rng.normal(0.0, noise)
            )
            t = t0 + timedelta(minutes=40 * f)
            recs.append(
                {
                    "kind": "zp",
                    "key": f"sim/{telescope}/{night}/{f}",
                    "night": night,
                    "t": t.isoformat(),
                    "telescope": telescope,
                    "port": port,
                    "filter": filt,
                    "zp": round(zp, 4),
                    "zp_err": 0.01,
                    "zp_scatter": 0.05,
                    "n_stars": 12,
                    "unit": unit,
                    "airmass": round(float(x[f]), 4),
                    "exptime": 10.0,
                    "source": "sim",
                }
            )
    return recs, truth
