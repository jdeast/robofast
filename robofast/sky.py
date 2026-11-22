"""Positions on the sky for the scheduler, from astropy.

Everything is vectorized over targets and none of it runs an astropy
coordinate transform per target: the dispatch scheduler calls these on its
event loop, for every candidate, and a slow decision holds the loop (and the
GIL) -- lease renewals, guider polls and heartbeats wait. On the MINERVA
gateway (night 2026-11-21) a re-evaluation of ~220 requests held the GIL for
about 3 s, one astropy transform per candidate in :func:`moon_separation`.

* :func:`altitudes` rotates the J2000 directions to the true equator and
  equinox of date with one bias-precession-nutation matrix and takes the
  apparent sidereal time at one reference instant, advancing it at the
  sidereal rate; the rest is numpy. Against astropy's AltAz it differs by
  annual aberration (up to 20 arcsec) and polar motion, far below what the
  altitude limits need.
* :func:`moon_separation` is plain spherical trigonometry against the Moon's
  topocentric (GCRS) RA/Dec, read once per :class:`SkyNow`: GCRS axes are
  ICRS-aligned, so for a star the two directions differ only by aberration.

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

    @cached_property
    def moon_radec(self) -> tuple[float, float]:
        """The Moon's topocentric RA and Dec (deg), read once."""
        return float(self.moon.ra.deg), float(self.moon.dec.deg)


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
    import erfa

    ra = np.radians(np.asarray(ra_hours, dtype=float) * 15.0)
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    rows = [list(r) for r in times]
    if len(rows) != len(ra) or len(ra) != len(dec):
        raise ValueError("ra_hours, dec_deg and times must have one entry per target")
    ref = next((t for r in rows for t in r), None)
    if ref is None:
        return np.zeros((len(rows), len(rows[0]) if rows else 0))
    # Hours of each time after the reference instant.
    dt = np.array([[(t - ref).total_seconds() / 3600.0 for t in r] for r in rows], dtype=float)
    t_ref = Time(ref)
    tt = t_ref.tt
    # J2000 (ICRS) -> true equator and equinox of date; one matrix for the
    # whole evaluation (it changes by ~0.1 arcsec in an hour).
    rbpn = erfa.pnm06a(tt.jd1, tt.jd2)
    v = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
    x, y, z = rbpn @ v
    ra_app = np.arctan2(y, x)
    dec_app = np.arcsin(np.clip(z, -1.0, 1.0))
    # Apparent sidereal time at the site, advanced at the sidereal rate.
    lst0 = t_ref.sidereal_time("apparent", longitude=site.longitude * u.deg).hour
    lst = np.radians((lst0 + dt / SIDEREAL) * 15.0)
    ha = lst - ra_app[:, None]
    phi = math.radians(site.latitude)
    sin_alt = math.sin(phi) * np.sin(dec_app)[:, None] + math.cos(phi) * np.cos(dec_app)[
        :, None
    ] * np.cos(ha)
    return np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))


def moon_separation(
    moon: SkyNow | SkyCoord | tuple[float, float],
    ra_hours: Sequence[float],
    dec_deg: Sequence[float],
) -> np.ndarray:
    """Angular distance (deg) from the Moon to each target (as seen from the
    site; the Moon's parallax matters, so this uses its topocentric place).
    ``moon`` is a :class:`SkyNow` (its RA/Dec read once), the Moon's GCRS
    SkyCoord, or its (RA, Dec) in degrees. Plain spherical trigonometry: GCRS
    axes are ICRS-aligned, so a star's ICRS direction differs from its GCRS
    one only by aberration (20 arcsec), which a Moon-avoidance limit ignores."""
    if isinstance(moon, SkyNow):
        m_ra, m_dec = moon.moon_radec
    elif isinstance(moon, SkyCoord):
        m_ra, m_dec = float(moon.ra.deg), float(moon.dec.deg)
    else:
        m_ra, m_dec = moon
    ra = np.radians(np.asarray(ra_hours, dtype=float) * 15.0)
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    m_ra, m_dec = math.radians(m_ra), math.radians(m_dec)
    # Vincenty's formula: accurate at every separation.
    d_ra = ra - m_ra
    num = np.hypot(
        np.cos(dec) * np.sin(d_ra),
        math.cos(m_dec) * np.sin(dec) - math.sin(m_dec) * np.cos(dec) * np.cos(d_ra),
    )
    den = math.sin(m_dec) * np.sin(dec) + math.cos(m_dec) * np.cos(dec) * np.cos(d_ra)
    return np.degrees(np.arctan2(num, den))


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
