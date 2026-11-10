"""The Sun gate: first light runs only in the dark, and never points near
the Sun (docs/first_light.md, "The Sun").

Two rules, applied whether or not the telescope has an enclosure, before
anything moves and for as long as first light runs:

* **The Sun is down**: its altitude at the site is at most ``max_sun_alt``
  (default :data:`MAX_SUN_ALT`, -12 deg: the end of nautical twilight, the
  dispatch scheduler's default too). First light is refused above it, and
  stops (the sequencer's cleanup parks) when the Sun rises to it.
* **No target near the Sun**: every place first light points (the focus
  target, the pointing calibration's fields, the zenith frame) is at least
  ``min_sun_separation`` (default :data:`MIN_SUN_SEPARATION`, 30 deg) from
  the Sun.

The limits can be changed, within :data:`MAX_SUN_ALT_CEILING` (the Sun
below the horizon) and :data:`MIN_SUN_SEPARATION_FLOOR`: no setting turns
either rule off. Sunlight in a telescope can burn a camera or an eye; a
first light, by someone new to the telescope, is when it is likeliest.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

import numpy as np

#: Default: the Sun's highest altitude (deg) for first light (nautical twilight).
MAX_SUN_ALT = -12.0
#: Default: the least angle (deg) from the Sun to anything first light points at.
MIN_SUN_SEPARATION = 30.0
#: No setting allows the Sun above the horizon...
MAX_SUN_ALT_CEILING = 0.0
#: ...or a target nearer the Sun than this (deg).
MIN_SUN_SEPARATION_FLOOR = 15.0
#: The Sun's altitude is sampled this often (s) to find when it crosses a limit.
STEP = 120.0


class SunGateError(RuntimeError):
    """The Sun is up, or a target is too near it (one line: what and when)."""


def check_limits(max_sun_alt: float, min_sun_separation: float) -> None:
    """ValueError for a limit that would turn a rule off."""
    if not -90.0 <= max_sun_alt <= MAX_SUN_ALT_CEILING:
        raise ValueError(
            f"max_sun_alt {max_sun_alt:g}: at most {MAX_SUN_ALT_CEILING:g} deg (the Sun down)"
        )
    if not MIN_SUN_SEPARATION_FLOOR <= min_sun_separation <= 180.0:
        raise ValueError(
            f"min_sun_separation {min_sun_separation:g}: at least {MIN_SUN_SEPARATION_FLOOR:g} deg"
        )


def _location(site: tuple[float, float, float]):
    from robofast.sky import Site, offline_iers

    offline_iers()
    return Site(site[0], site[1], site[2]).location


def sun_altitudes(site: tuple[float, float, float], times: list[datetime]) -> np.ndarray:
    """The Sun's altitude (deg) at ``site`` (lat, lon east, elev m) at each time."""
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time

    loc = _location(site)
    t = Time(times)
    sun = get_body("sun", t, loc).transform_to(AltAz(obstime=t, location=loc))
    return np.atleast_1d(np.asarray(sun.alt.deg, dtype=float))


def _crossing(
    site: tuple[float, float, float], when: datetime, limit: float, rising: bool, hours: float
) -> datetime | None:
    """The first time after ``when``, within ``hours``, that the Sun's
    altitude goes above (``rising``) or to at most (not ``rising``) ``limit``."""
    n = int(hours * 3600.0 / STEP) + 1
    times = [when + timedelta(seconds=STEP * i) for i in range(n)]
    alt = sun_altitudes(site, times)
    hit = alt > limit if rising else alt <= limit
    if not hit.any():
        return None
    i = int(np.argmax(hit))
    if i == 0:
        return when
    # Between two samples the altitude is near enough a straight line.
    a0, a1 = alt[i - 1], alt[i]
    frac = (limit - a0) / (a1 - a0) if a1 != a0 else 1.0
    return times[i - 1] + timedelta(seconds=STEP * min(1.0, max(0.0, frac)))


def check_dark(
    site: tuple[float, float, float], when: datetime, max_sun_alt: float = MAX_SUN_ALT
) -> datetime | None:
    """:class:`SunGateError` if the Sun at ``site`` is above ``max_sun_alt``
    at ``when`` (saying when it next is not); else when it next rises to it
    (None: not within a day, a polar night)."""
    (alt,) = sun_altitudes(site, [when])
    if alt > max_sun_alt:
        dark = _crossing(site, when, max_sun_alt, rising=False, hours=24.0)
        then = (
            f"; it is next at {max_sun_alt:g} deg at {dark:%Y-%m-%d %H:%M} UTC"
            if dark is not None
            else "; not within a day here"
        )
        raise SunGateError(
            f"the Sun is up: {alt:.1f} deg, above {max_sun_alt:g} deg (--max-sun-alt);"
            f" first light runs only in the dark{then}"
        )
    return _crossing(site, when, max_sun_alt, rising=True, hours=24.0)


def sun_separations(
    site: tuple[float, float, float],
    when: datetime,
    targets: Iterable[tuple[float, float]],
) -> np.ndarray:
    """Angle (deg) from the Sun at ``when`` to each target (RA, Dec deg, ICRS)."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord, get_body
    from astropy.time import Time

    pts = list(targets)
    if not pts:
        return np.zeros(0)
    sun = get_body("sun", Time(when), _location(site))
    sun = SkyCoord(sun.ra, sun.dec)  # its direction (aberration aside, as for a star)
    ra, dec = np.array(pts, dtype=float).T
    return np.asarray(sun.separation(SkyCoord(ra * u.deg, dec * u.deg)).deg, dtype=float)


def check_targets(
    site: tuple[float, float, float],
    when: datetime,
    targets: Iterable[tuple[str, float, float]],
    min_sun_separation: float = MIN_SUN_SEPARATION,
    margin: float = 0.0,
) -> None:
    """:class:`SunGateError` naming the first target (name, RA, Dec deg)
    nearer the Sun than ``min_sun_separation`` + ``margin`` deg at ``when``
    (``margin``: what the Sun moves, and the fields drift, until it is
    observed)."""
    named = list(targets)
    seps = sun_separations(site, when, [(ra, dec) for _, ra, dec in named])
    for (name, _, _), sep in zip(named, seps, strict=True):
        if sep < min_sun_separation + margin:
            raise SunGateError(
                f"{name} is {sep:.0f} deg from the Sun, nearer than {min_sun_separation:g} deg"
                " (--min-sun-separation): first light does not point there"
            )
