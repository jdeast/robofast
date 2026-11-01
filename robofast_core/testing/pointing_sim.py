"""An all-sky synthetic star catalog and a mount on the simulated sky, for
tests that point all over the sky (pointing runs, calibrations).

The test sky (``tests/core/skydata.py``) covers five fields;
a pointing grid needs stars everywhere. :class:`SyntheticCatalog` makes them
up, deterministically: each 1 x 1 deg cell (in RA and sin Dec) gets its own
random stars (a Poisson number for ``density`` per square degree, uniform in
the cell, magnitudes with the Galactic-cap slope dN/dm ~ 10**(0.3 m)), seeded
by the cell, so the same sky is rendered by the simulator and found by the
solver. It answers :meth:`~robofast_core.catalog.Catalog.cone` like the real
catalog.

:class:`SkyMount` is a :class:`~robofast_core.testing.fakes.FakeTelescope`
whose sidereal time, altitude and azimuth follow a clock at a site (the
fake's own sidereal time runs from the process's monotonic clock).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from typing import Any

import numpy as np

from robofast_core.catalog import Stars, separation
from robofast_core.pointing import hadec_to_altaz
from robofast_core.testing.fakes import FakeDevice, FakeTelescope
from robofast_core.testing.skysim import lst_deg

__all__ = ["SimRotator", "SkyMount", "SyntheticCatalog"]

_NZ = 115  # cells in sin(dec): about 1 deg near the equator


class SyntheticCatalog:
    """Random stars over the whole sky (module doc)."""

    def __init__(
        self,
        density: float = 400.0,
        bright: float = 10.0,
        faint: float = 16.0,
        seed: int = 0,
    ) -> None:
        self.density = density
        self.bright = bright
        self.faint = faint
        self.seed = seed
        self.mag_limit = faint
        self.doubles = None

    @lru_cache(maxsize=4096)  # noqa: B019 - a test helper; the cache is the point
    def _cell(self, i: int, j: int) -> Stars:
        z0 = -1.0 + 2.0 * j / _NZ
        z1 = -1.0 + 2.0 * (j + 1) / _NZ
        n_ra = 360
        area = (360.0 / n_ra) * (z1 - z0) * 180.0 / math.pi  # deg^2
        rng = np.random.default_rng([self.seed, i, j])
        n = rng.poisson(self.density * area)
        ra = (i + rng.uniform(0, 1, n)) * (360.0 / n_ra)
        dec = np.degrees(np.arcsin(rng.uniform(z0, z1, n)))
        # dN/dm ~ 10**(0.3 m) between bright and faint.
        a, b = 10 ** (0.3 * self.bright), 10 ** (0.3 * self.faint)
        g = np.log10(a + (b - a) * rng.uniform(0, 1, n)) / 0.3
        sid = (i * 1000 + j) * 100000 + np.arange(n)
        return Stars(
            ra,
            dec,
            np.zeros(n),
            np.zeros(n),
            g,
            np.full(n, 0.8),
            sid.astype(np.int64),
        )

    def cone(
        self,
        ra: float,
        dec: float,
        radius: float,
        *,
        epoch: Any = None,
        mag_limit: float | None = None,
        bright_only: bool = False,
    ) -> Stars:
        d0, d1 = max(-90.0, dec - radius), min(90.0, dec + radius)
        j0 = int((math.sin(math.radians(d0)) + 1.0) / 2.0 * _NZ)
        j1 = min(_NZ - 1, int((math.sin(math.radians(d1)) + 1.0) / 2.0 * _NZ))
        cosd = math.cos(math.radians(min(abs(d0), abs(d1)) if d0 * d1 > 0 else 0.0))
        if d1 >= 89.9 or d0 <= -89.9 or cosd < 1e-3:
            cols = range(360)
        else:
            half = min(180.0, radius / cosd + 1.0)
            cols = [int(c) % 360 for c in np.arange(math.floor(ra - half), math.ceil(ra + half))]
        parts = [self._cell(i, j) for j in range(j0, j1 + 1) for i in sorted(set(cols))]
        s = Stars.concat(parts)
        s = s.take(separation(ra, dec, s.ra, s.dec) <= radius)
        if mag_limit is not None:
            s = s.brighter(mag_limit)
        return s.sorted()


class SkyMount(FakeTelescope):
    """A fake mount whose ``SiderealTime`` (hours), ``Altitude`` and
    ``Azimuth`` follow ``clock`` at (``latitude``, east ``longitude``)."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        latitude: float = 31.680407,
        longitude: float = -110.878977,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.SiteLatitude = latitude
        self.SiteLongitude = longitude
        self.slews: list[tuple[float, float]] = []
        self.actions: list[tuple[str, str]] = []

    @property
    def SiderealTime(self) -> float:  # type: ignore[override]
        return lst_deg(self.clock(), self.SiteLongitude) / 15.0

    def _altaz(self) -> tuple[float, float]:
        ha = self.SiderealTime * 15.0 - self.ra * 15.0
        az, alt = hadec_to_altaz(ha, self.dec, self.SiteLatitude)
        return float(az), float(alt)

    @property
    def Altitude(self) -> float:  # type: ignore[override]
        return self._altaz()[1]

    @Altitude.setter
    def Altitude(self, value: float) -> None:
        pass  # FakeTelescope.__init__ sets it

    @property
    def Azimuth(self) -> float:  # type: ignore[override]
        return self._altaz()[0]

    @Azimuth.setter
    def Azimuth(self, value: float) -> None:
        pass

    def SlewToCoordinatesAsync(self, ra: float, dec: float) -> None:
        super().SlewToCoordinatesAsync(ra, dec)
        self.slews.append((ra, dec))

    def Action(self, action: str, parameters: str = "") -> str:
        if self.port_action is not None and action == self.port_action:
            return super().Action(action, parameters)
        self.actions.append((action, parameters))
        return ""


class SimRotator(FakeDevice):
    """An Alpaca rotator: ``MoveMechanical`` takes ``move_time`` s;
    ``Position`` (the sky PA it reports) is the mechanical angle plus
    ``sky_offset``. ``image_pa(offset, sense)`` gives a scene's PA."""

    def __init__(self, position: float = 0.0, move_time: float = 0.05, sky_offset: float = 0.0):
        super().__init__()
        self._mech = float(position) % 360.0
        self._until = 0.0
        self.move_time = move_time
        self.sky_offset = sky_offset
        self.moves: list[float] = []

    @property
    def MechanicalPosition(self) -> float:
        return self._mech

    @property
    def Position(self) -> float:
        return (self._mech + self.sky_offset) % 360.0

    @property
    def IsMoving(self) -> bool:
        return self._now() < self._until

    def MoveMechanical(self, angle: float) -> None:
        self._mech = float(angle) % 360.0
        self._until = self._now() + self.move_time
        self.moves.append(self._mech)

    def Halt(self) -> None:
        self._until = 0.0

    def image_pa(self, offset: float, sense: int = 1) -> Callable[[], float]:
        return lambda: (offset + sense * self._mech) % 360.0
