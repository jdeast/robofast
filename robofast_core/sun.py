"""The Sun's altitude at a site, from astropy.

JDE (2026-10-31): use astropy, so that the bridge's number agrees exactly
with the same calculation made elsewhere (a scheduler, an analysis script)
with the same parameters.

**Refraction.** The legacy MINERVA code (``minerva_library/env.py``,
``site.sunalt``) used ``ephem.Observer`` without setting ``pressure``, so
PyEphem applied refraction with its defaults: 1010 mbar, 15 degC. To match
it, :func:`sun_altitude` takes the same pressure and temperature (the MINERVA
config passes 1010 hPa and 15 degC) and returns the *apparent* altitude of
the Sun's centre; ``pressure_hpa=0`` gives the geometric altitude. Measured
against PyEphem 4.2.1: within 0.002 deg from +6 deg up (the MINERVA limit),
but astropy's refraction model (ERFA ``refco``, ``A tan z + B tan^3 z``) is
not meant for the horizon: 0.01 deg apart at +4.5 deg, 0.04 at +3.3, 0.15 at
+1, and below the horizon astropy keeps adding ~0.17 deg where PyEphem
stops refracting. Nothing near a safety limit lives
there; the Sun source also reports the geometric altitude for the record.

**Offline.** Importing this module sets ``astropy.utils.iers.conf``
``auto_download = False`` (no IERS-A or leap-second downloads: they could
stall or fail an evaluation on a machine without internet) and
``auto_max_age = None`` (accept the bundled IERS-A predictions however old;
beyond them astropy warns and uses mean polar motion). UT1-UTC errors of
under a second move the Sun by under 0.005 deg. These are process-wide
settings; anything in the same process that wants fresh IERS data must ask
for it explicitly.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from functools import lru_cache

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, get_body
from astropy.time import Time
from astropy.utils import iers
from astropy.utils.exceptions import AstropyWarning
from erfa import ErfaWarning

iers.conf.auto_download = False
iers.conf.auto_max_age = None

#: PyEphem's Observer defaults, which the legacy code's refraction used.
LEGACY_PRESSURE_HPA = 1010.0
LEGACY_TEMPERATURE_C = 15.0


@lru_cache(maxsize=16)
def _location(latitude: float, longitude: float, elevation: float) -> EarthLocation:
    return EarthLocation.from_geodetic(longitude * u.deg, latitude * u.deg, elevation * u.m)


def sun_altitude(
    when: datetime,
    latitude: float,
    longitude: float,
    elevation: float = 0.0,
    *,
    pressure_hpa: float = 0.0,
    temperature_c: float = LEGACY_TEMPERATURE_C,
    relative_humidity: float = 0.0,
    wavelength_um: float = 0.55,
) -> float:
    """Topocentric altitude of the Sun's centre, in degrees.

    ``latitude`` is degrees north, ``longitude`` degrees *east* (MINERVA is
    at about -110.88), ``elevation`` metres. ``pressure_hpa=0`` (the
    default) is the geometric altitude; a pressure gives the apparent
    (refracted) altitude for that pressure, temperature, humidity
    (0..1) and wavelength.
    """
    if when.tzinfo is None:
        raise ValueError("sun_altitude needs an aware datetime")
    location = _location(float(latitude), float(longitude), float(elevation))
    with warnings.catch_warnings():
        # Beyond the bundled IERS tables (or the leap-second table) astropy
        # warns on every call; the degradation is far below what matters here.
        warnings.simplefilter("ignore", AstropyWarning)
        warnings.simplefilter("ignore", ErfaWarning)
        t = Time(when.astimezone(UTC))
        frame = AltAz(
            obstime=t,
            location=location,
            pressure=pressure_hpa * u.hPa,
            temperature=temperature_c * u.deg_C,
            relative_humidity=relative_humidity,
            obswl=wavelength_um * u.micron,
        )
        return float(get_body("sun", t, location).transform_to(frame).alt.deg)


def now_utc() -> datetime:
    return datetime.now(UTC)
