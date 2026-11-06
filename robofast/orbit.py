"""Places and rates of comets and asteroids from their orbital elements.

An OCS ``ORBITAL_ELEMENTS`` target (docs/request_language.md) carries
heliocentric osculating elements, ecliptic and equinox J2000, as the Minor
Planet Center publishes them. :class:`Orbit` holds them and gives the
target's place as seen from a site at a time: the *astrometric* ICRS RA and
Dec (light-time corrected, topocentric), the same kind of place as a star's
catalog coordinates, so a mount that takes J2000 coordinates points at it
the way it points at a star (it applies aberration, precession and nutation
itself). The rate is that place's derivative, in arcsec per second of time.

The orbit is propagated as a two-body (Sun only) conic with universal
variables, so one path serves ellipses, parabolas and hyperbolas
(an interstellar object's ``e > 1``). The planets are ignored: within a few
weeks of the elements' epoch (the MPC's are re-osculated every few months)
that moves a main-belt or comet place by hundredths of an arcsecond. An old
epoch, or a close approach to a planet, can move it by much more; the
request is then the place to send fresh elements.

The Sun and the Earth come from astropy's built-in ephemeris (no download)
and the site from :class:`astropy.coordinates.EarthLocation`.

Schemes (the OCS's names and fields):

* ``MPC_MINOR_PLANET``: ``epochofel`` (MJD), ``orbinc``, ``longascnode``,
  ``argofperih`` (deg), ``meandist`` (au), ``eccentricity`` (< 1) and
  ``meananom`` (deg, at ``epochofel``);
* ``MPC_COMET`` (and ``JPL_MINOR_PLANET``, which has the same fields):
  ``epochofel``, ``orbinc``, ``longascnode``, ``argofperih``,
  ``perihdist`` (au), ``eccentricity`` (any, >= 0) and ``epochofperih``
  (MJD).

Times of the elements are TT (the MPC's); the target's clock time is UTC.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

import numpy as np
from astropy import units as u
from astropy.coordinates import EarthLocation, get_body_barycentric
from astropy.time import Time

#: The Gaussian gravitational constant (rad/day; au, days, solar masses).
GAUSS_K = 0.01720209895
MU = GAUSS_K**2
#: The speed of light in au per day.
C_AU_PER_DAY = 173.1446326742403
#: The obliquity of the ecliptic at J2000 (IAU 1976, as the MPC uses), rad.
OBLIQUITY_J2000 = math.radians(84381.448 / 3600.0)
#: The step of the central difference for the rate (seconds of time).
RATE_STEP_S = 30.0

SCHEMES = ("MPC_MINOR_PLANET", "MPC_COMET", "JPL_MINOR_PLANET")
#: OCS schemes refused, with the reason.
REFUSED_SCHEMES = {
    "JPL_MAJOR_PLANET": "a major planet; robofast takes minor planets and comets",
    "ASA_MAJOR_PLANET": "a major planet; robofast takes minor planets and comets",
    "ASA_MINOR_PLANET": "the Astronomical Almanac's form; send MPC_MINOR_PLANET",
    "ASA_COMET": "the Astronomical Almanac's form; send MPC_COMET",
}
#: The OCS's eccentricity limit for a non-comet scheme (observation-portal's
#: OrbitalElementsTargetHelper.validate).
NON_COMET_MAX_E = 0.9


def _stumpff(z: float) -> tuple[float, float]:
    """Stumpff functions C(z), S(z)."""
    if z > 1e-6:
        s = math.sqrt(z)
        return (1.0 - math.cos(s)) / z, (s - math.sin(s)) / (s * z)
    if z < -1e-6:
        s = math.sqrt(-z)
        return (math.cosh(s) - 1.0) / -z, (math.sinh(s) - s) / (s * -z)
    # Series near z = 0 (the parabola).
    return 0.5 - z / 24.0 + z * z / 720.0, 1.0 / 6.0 - z / 120.0 + z * z / 5040.0


@dataclass(frozen=True)
class Orbit:
    """Heliocentric osculating elements (ecliptic J2000), the OCS's names."""

    scheme: str
    epochofel: float
    orbinc: float
    longascnode: float
    argofperih: float
    eccentricity: float
    meandist: float | None = None
    meananom: float | None = None
    perihdist: float | None = None
    epochofperih: float | None = None

    #: scheme: its own fields (beyond the common five), as the OCS requires.
    FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "MPC_MINOR_PLANET": ("meandist", "meananom"),
        "MPC_COMET": ("perihdist", "epochofperih"),
        "JPL_MINOR_PLANET": ("perihdist", "epochofperih"),
    }
    COMMON: ClassVar[tuple[str, ...]] = (
        "epochofel",
        "orbinc",
        "longascnode",
        "argofperih",
        "eccentricity",
    )
    #: field: (lo, hi)
    RANGES: ClassVar[dict[str, tuple[float, float]]] = {
        "epochofel": (15020.0, 88069.0),  # MJD 1900-2100
        "epochofperih": (15020.0, 88069.0),
        "orbinc": (0.0, 180.0),
        "longascnode": (0.0, 360.0),
        "argofperih": (0.0, 360.0),
        "eccentricity": (0.0, 100.0),
        "meandist": (1e-3, 1e6),
        "perihdist": (1e-4, 1e4),
        "meananom": (-360.0, 360.0),
    }

    def __post_init__(self) -> None:
        if self.scheme not in SCHEMES:
            why = REFUSED_SCHEMES.get(self.scheme, f"one of {', '.join(SCHEMES)}")
            raise ValueError(f"scheme {self.scheme!r}: {why}")
        for k in self.FIELDS[self.scheme]:
            if getattr(self, k) is None:
                raise ValueError(f"scheme {self.scheme}: {k} is required")
        e = self.eccentricity
        if self.scheme == "MPC_MINOR_PLANET":
            if e >= 1.0:
                raise ValueError(
                    f"MPC_MINOR_PLANET: eccentricity {e!r} must be < 1 (send MPC_COMET)"
                )
        if "COMET" not in self.scheme and e > NON_COMET_MAX_E:
            raise ValueError(
                f"{self.scheme}: eccentricity {e!r} is over {NON_COMET_MAX_E};"
                " send MPC_COMET (as the OCS asks)"
            )

    # -- the conic at perihelion ------------------------------------------------------

    @property
    def q(self) -> float:
        """Perihelion distance, au."""
        if self.perihdist is not None:
            return self.perihdist
        assert self.meandist is not None
        return self.meandist * (1.0 - self.eccentricity)

    @property
    def tp_mjd(self) -> float:
        """Time of perihelion, MJD (TT). For MPC_MINOR_PLANET, the
        perihelion nearest the epoch (the mean anomaly taken in (-180, 180])."""
        if self.epochofperih is not None:
            return self.epochofperih
        assert self.meandist is not None and self.meananom is not None
        n = GAUSS_K / self.meandist**1.5  # rad/day
        m = math.radians(((self.meananom + 180.0) % 360.0) - 180.0)
        return self.epochofel - m / n

    def _axes(self) -> tuple[np.ndarray, np.ndarray]:
        """Unit vectors toward perihelion (P) and 90 deg ahead of it (Q), ICRS."""
        om, w, i = (math.radians(x) for x in (self.longascnode, self.argofperih, self.orbinc))
        co, so, cw, sw, ci, si = (
            math.cos(om),
            math.sin(om),
            math.cos(w),
            math.sin(w),
            math.cos(i),
            math.sin(i),
        )
        p = np.array([co * cw - so * sw * ci, so * cw + co * sw * ci, sw * si])
        q = np.array([-co * sw - so * cw * ci, -so * sw + co * cw * ci, cw * si])
        ce, se = math.cos(OBLIQUITY_J2000), math.sin(OBLIQUITY_J2000)
        rot = np.array([[1.0, 0.0, 0.0], [0.0, ce, -se], [0.0, se, ce]])  # ecliptic -> equatorial
        return rot @ p, rot @ q

    def heliocentric(self, tt_mjd: float) -> np.ndarray:
        """Heliocentric position (au, ICRS axes) at ``tt_mjd`` (MJD, TT)."""
        q, e = self.q, self.eccentricity
        alpha = (1.0 - e) / q  # 1/a; 0 for the parabola, < 0 for a hyperbola
        dt = tt_mjd - self.tp_mjd
        if alpha > 0:  # an ellipse: within one period of perihelion
            period = 2.0 * math.pi / (GAUSS_K * alpha**1.5)
            dt = math.remainder(dt, period)
        chi = self._universal(dt, q, alpha)
        c, s = _stumpff(alpha * chi * chi)
        f = 1.0 - chi * chi / q * c
        g = dt - chi**3 * s / math.sqrt(MU)
        vp = math.sqrt(MU * (1.0 + e) / q)
        p, qv = self._axes()
        return f * q * p + g * vp * qv

    @staticmethod
    def _universal(dt: float, r0: float, alpha: float) -> float:
        """The universal anomaly for time ``dt`` (days) from perihelion,
        Newton's method kept inside a bisection bracket (the time is
        monotonic in it, so this always converges)."""
        sq = math.sqrt(MU)

        def time_of(chi: float) -> tuple[float, float]:
            z = alpha * chi * chi
            c, s = _stumpff(z)
            t = ((1.0 - alpha * r0) * chi**3 * s + r0 * chi) / sq
            r = chi * chi * c + r0 * (1.0 - z * s)  # dt/dchi * sqrt(mu)
            return t, r / sq

        if dt == 0.0:
            return 0.0
        lo, hi = 0.0, 1.0
        while time_of(hi)[0] < abs(dt):
            lo, hi = hi, hi * 2.0
            if hi > 1e9:
                raise ValueError("orbit: no universal anomaly for this time")
        chi = 0.5 * (lo + hi)
        for _ in range(200):
            t, dtdchi = time_of(chi)
            err = t - abs(dt)
            if err > 0:
                hi = chi
            else:
                lo = chi
            if abs(err) < 1e-13 * max(1.0, abs(dt)):
                break
            nxt = chi - err / dtdchi
            chi = nxt if lo < nxt < hi else 0.5 * (lo + hi)
        return math.copysign(chi, dt)

    # -- the place from a site ---------------------------------------------------------

    def places(
        self, when: Sequence[datetime], location: EarthLocation | None = None
    ) -> list[tuple[float, float]]:
        """Astrometric ICRS ``(ra, dec)`` in degrees at each UTC time, from
        ``location`` (None: the geocenter), light-time corrected."""
        t = Time(list(when), scale="utc")
        earth = get_body_barycentric("earth", t).xyz.to_value(u.au).T
        sun = get_body_barycentric("sun", t).xyz.to_value(u.au).T
        obs = earth.copy()
        if location is not None:
            gcrs, _ = location.get_gcrs_posvel(t)
            obs = obs + gcrs.xyz.to_value(u.au).T
        tt = t.tt.mjd
        out = []
        for k in range(len(t)):
            tau = 0.0
            for _ in range(4):
                # The Sun moves under 1e-5 au/day around the barycenter, a
                # few km in a light time: its place now serves.
                d = sun[k] + self.heliocentric(float(tt[k]) - tau) - obs[k]
                tau = float(np.linalg.norm(d)) / C_AU_PER_DAY
            x, y, z = d
            ra = math.degrees(math.atan2(y, x)) % 360.0
            dec = math.degrees(math.atan2(z, math.hypot(x, y)))
            out.append((ra, dec))
        return out

    def place(self, when: datetime, location: EarthLocation | None = None) -> tuple[float, float]:
        """:meth:`places` at one time."""
        return self.places([when], location)[0]

    def rate(self, when: datetime, location: EarthLocation | None = None) -> tuple[float, float]:
        """The place's motion at ``when``: ``(dra_cosdec, ddec)`` in arcsec
        per second of time, a central difference over :data:`RATE_STEP_S`."""
        h = timedelta(seconds=RATE_STEP_S)
        (ra0, de0), (ra1, de1) = self.places([when - h, when + h], location)
        dra = ((ra1 - ra0 + 180.0) % 360.0) - 180.0
        dec = 0.5 * (de0 + de1)
        k = 3600.0 / (2.0 * RATE_STEP_S)
        return dra * math.cos(math.radians(dec)) * k, (de1 - de0) * k

    # -- the OCS form ------------------------------------------------------------------

    @classmethod
    def from_target(cls, d: dict[str, Any], what: str, number: Any) -> Orbit:
        """From an OCS target object (its keys already checked); ``number``
        is the request model's checked-number reader."""
        scheme = d.get("scheme")
        if scheme not in SCHEMES:
            why = REFUSED_SCHEMES.get(str(scheme), f"expected one of {', '.join(SCHEMES)}")
            raise ValueError(f"{what}.scheme: {scheme!r}: {why}")
        need = cls.COMMON + cls.FIELDS[scheme]
        missing = [k for k in need if d.get(k) is None]
        if missing:
            raise ValueError(f"{what}: scheme {scheme} needs {missing}")
        extra = [k for k in cls.RANGES if k not in need and d.get(k) is not None]
        if extra:
            raise ValueError(f"{what}: {extra} not used by scheme {scheme}")
        kw = {k: number(d[k], f"{what}.{k}", *cls.RANGES[k]) for k in need}
        try:
            return cls(scheme, **kw)
        except ValueError as exc:
            raise ValueError(f"{what}: {exc}") from None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"scheme": self.scheme}
        for k in self.COMMON + self.FIELDS[self.scheme]:
            d[k] = getattr(self, k)
        return d
