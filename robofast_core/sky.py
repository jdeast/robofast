"""Positions on the sky for the scheduler, from astropy.

Everything is vectorized over targets: :func:`altitudes` takes arrays of
coordinates and an array of times and does one coordinate transform, so a
dispatch decision over a few hundred configurations costs tens of
milliseconds, not seconds.

Altitudes are geometric (no refraction): the limits they are compared with
(20 deg and up) are far from the horizon, where refraction is a few arcmin.

**No network.** By default astropy downloads Earth-orientation tables (IERS)
when a time is newer than its bundled ones, and refuses to transform without
them. An observatory must not stall on a download, and scheduling does not
need UT1 to the millisecond (an arcsecond is plenty), so
:func:`offline_iers` (called by the scheduler) turns the downloads off and
lets astropy fall back to its bundled tables silently.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body
from astropy.time import Time

#: A sidereal day in solar hours.
SIDEREAL = 0.99726957


def offline_iers() -> None:
    """Never download IERS tables; use the bundled ones, silently."""
    from astropy.utils import iers

    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    iers.conf.iers_degraded_accuracy = "ignore"


@dataclass(frozen=True)
class Site:
    """``latitude`` degrees north, ``longitude`` degrees *east*, ``elevation``
    metres."""

    latitude: float
    longitude: float
    elevation: float = 0.0

    @cached_property
    def location(self) -> EarthLocation:
        return EarthLocation.from_geodetic(
            self.longitude * u.deg, self.latitude * u.deg, self.elevation * u.m
        )

    def night_start(self, when: datetime) -> datetime:
        """The local mean-solar noon before ``when``: the start of "tonight"
        for nightly counts."""
        local = when + timedelta(hours=self.longitude / 15.0)
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        if local < noon:
            noon -= timedelta(days=1)
        return noon - timedelta(hours=self.longitude / 15.0)


@dataclass(frozen=True)
class SkyNow:
    """The Sun, the Moon and sidereal time at one instant."""

    when: datetime
    lst_hours: float
    sun_alt: float
    #: Sun altitude ten minutes later (to tell evening from morning).
    sun_alt_later: float
    moon: SkyCoord  # GCRS

    @property
    def sun_rising(self) -> bool:
        return self.sun_alt_later > self.sun_alt


def sky_now(site: Site, when: datetime) -> SkyNow:
    t = Time([when, when + timedelta(minutes=10)])
    frame = AltAz(obstime=t, location=site.location)
    sun = get_body("sun", t, site.location).transform_to(frame)
    lst = Time(when).sidereal_time("apparent", longitude=site.longitude * u.deg).hour
    return SkyNow(
        when,
        float(lst),
        float(sun.alt.deg[0]),
        float(sun.alt.deg[1]),
        get_body("moon", Time(when), site.location),
    )


def altitudes(
    site: Site,
    ra_hours: Sequence[float],
    dec_deg: Sequence[float],
    times: Sequence[Sequence[datetime]],
) -> np.ndarray:
    """Altitude (deg) of target ``i`` at each of ``times[i]``; shape
    ``(len(ra_hours), len(times[0]))``. Each row of ``times`` has the same
    length."""
    ra = np.asarray(ra_hours, dtype=float)[:, None] * 15.0
    dec = np.asarray(dec_deg, dtype=float)[:, None]
    t = Time(np.asarray(times, dtype=object))
    coords = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    frame = AltAz(obstime=t, location=site.location)
    return np.asarray(coords.transform_to(frame).alt.deg)


def moon_separation(
    moon: SkyCoord, ra_hours: Sequence[float], dec_deg: Sequence[float]
) -> np.ndarray:
    """Angular distance (deg) from the Moon to each target (as seen from the
    site; the Moon's parallax matters, so this uses its topocentric place)."""
    coords = SkyCoord(
        ra=np.asarray(ra_hours, dtype=float) * 15.0 * u.deg,
        dec=np.asarray(dec_deg, dtype=float) * u.deg,
        frame="icrs",
    )
    # Compare in the Moon's (GCRS) frame; for a star at infinity GCRS and
    # ICRS directions differ only by aberration (20 arcsec).
    return np.asarray(moon.separation(coords.transform_to(moon.frame)).deg)


def hour_angle(lst_hours: float, ra_hours: float) -> float:
    """Hour angle in hours, in [-12, 12). (Apparent sidereal time against
    J2000 RA: precession makes this off by up to a couple of minutes, which
    does not matter for hour-angle limits.)"""
    return (lst_hours - ra_hours + 12.0) % 24.0 - 12.0


def setting_hour_angle(latitude: float, dec_deg: float, alt_deg: float) -> float:
    """Hour angle (hours, >= 0) at which a target of declination ``dec_deg``
    sinks below ``alt_deg``; ``inf`` if it never does, ``-inf`` if it never
    gets that high."""
    phi, dec, alt = (math.radians(x) for x in (latitude, dec_deg, alt_deg))
    denom = math.cos(phi) * math.cos(dec)
    if denom <= 1e-12:  # at a pole: altitude is constant
        return math.inf if math.sin(phi) * math.sin(dec) >= math.sin(alt) else -math.inf
    c = (math.sin(alt) - math.sin(phi) * math.sin(dec)) / denom
    if c <= -1.0:
        return math.inf
    if c >= 1.0:
        return -math.inf
    return math.degrees(math.acos(c)) / 15.0


def transit_altitude(latitude: float, dec_deg: float) -> float:
    return 90.0 - abs(latitude - dec_deg)


def airmass(alt_deg: float) -> float:
    """Plane-parallel airmass, sec z (inf at or below the horizon)."""
    return 1.0 / math.sin(math.radians(alt_deg)) if alt_deg > 0 else math.inf
