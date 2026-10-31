"""The Sun's altitude, from the Astronomical Almanac's low-precision formulae.

Good to about 0.01 deg in declination and right ascension for 1950-2050
(Astronomical Almanac, "Low precision formulas for the Sun", section C);
the altitude error is dominated by ignoring refraction (about 0.5 deg at the
horizon, 0.15 deg at 6 deg) and parallax (0.002 deg). That is far below what
a "Sun below N degrees" safety limit needs, and it costs no dependency
(astropy, or ephem as the legacy code used, would be heavy for one number).

The altitude returned is *geometric* (no refraction), of the Sun's centre.
The legacy MINERVA code used PyEphem's default apparent altitude, which
includes refraction, so near the horizon this reads up to ~0.5 deg lower; at
the legacy limit (+6 deg) the difference is ~0.15 deg, about 40 s of time.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

#: Julian date of the Unix epoch.
_JD_UNIX = 2440587.5
_J2000 = 2451545.0


def julian_date(when: datetime) -> float:
    """Julian date of an aware datetime (UTC is used as a stand-in for UT1/TT)."""
    if when.tzinfo is None:
        raise ValueError("julian_date needs an aware datetime")
    return when.timestamp() / 86400.0 + _JD_UNIX


def sun_ra_dec(when: datetime) -> tuple[float, float]:
    """Apparent right ascension and declination of the Sun, in degrees."""
    n = julian_date(when) - _J2000
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecl_lon = math.radians(mean_lon + 1.915 * math.sin(anomaly) + 0.020 * math.sin(2.0 * anomaly))
    obliquity = math.radians(23.439 - 0.0000004 * n)
    ra = math.degrees(math.atan2(math.cos(obliquity) * math.sin(ecl_lon), math.cos(ecl_lon)))
    dec = math.degrees(math.asin(math.sin(obliquity) * math.sin(ecl_lon)))
    return ra % 360.0, dec


def gmst_degrees(when: datetime) -> float:
    """Greenwich mean sidereal time, in degrees (USNO approximation)."""
    d = julian_date(when) - _J2000
    return (18.697374558 + 24.06570982441908 * d) % 24.0 * 15.0


def sun_altitude(when: datetime, latitude: float, longitude: float) -> float:
    """Geometric altitude of the Sun's centre in degrees.

    ``latitude`` is degrees north, ``longitude`` degrees *east* (MINERVA is
    at about -110.88).
    """
    ra, dec = sun_ra_dec(when)
    hour_angle = math.radians((gmst_degrees(when) + longitude - ra) % 360.0)
    lat, dec_r = math.radians(latitude), math.radians(dec)
    s = math.sin(lat) * math.sin(dec_r) + math.cos(lat) * math.cos(dec_r) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def now_utc() -> datetime:
    return datetime.now(UTC)
