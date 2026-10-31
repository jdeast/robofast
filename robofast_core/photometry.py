"""How many electrons a star and the sky give: zero points, throughput, sky.

Per telescope and **port** (the imager; the FAU behind a 2 % beamsplitter;
the fiber), a :class:`PortPhotometry` gives the **zero point** ``zp``: the
catalog magnitude of a star that gives 1 e-/s through that port, so a
star of magnitude ``m`` gives ``10**(-0.4 (m - zp))`` e-/s above the
atmosphere. It is either measured (``zero_point``, from plate-solved
frames: :func:`robofast_core.astrometry.zero_point`) or estimated from an
**efficiency chain**::

    zp = 2.5 log10(photon_rate(band) * collecting_area_cm2 * prod(efficiency))

``efficiency`` is a mapping of named factors (``beamsplitter``,
``mirrors``, ``corrector``, ``filter``, ``qe``, ...) so a site config says
where each number came from and which are guesses (sites/minerva/README.md).
A measured ``zero_point`` wins over the chain.

The sky: :func:`sky_brightness` is the V surface brightness (mag/arcsec2)
of the dark sky, plus twilight (an exponential in the Sun's depression),
plus moonlight (Krisciunas & Schaefer 1991, PASP 103, 1033). Good to a few
tenths of a magnitude, which is what exposure estimates and the simulator
need.

Pure numpy; :func:`sky_conditions` uses astropy for the Sun and Moon.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "PHOTON_RATE",
    "PortPhotometry",
    "SkyConditions",
    "electrons",
    "sky_brightness",
    "sky_conditions",
    "snr",
]

#: Photons s^-1 cm^-2 above the atmosphere from a magnitude-0 star in each
#: band (Vega system): Bessell, Castelli & Plez 1998's photons/s/cm2/A at
#: magnitude 0 times the band's effective width (U 756 x 660 A, B 1392 x
#: 940, V 996 x 880, R 702 x 1380, I 452 x 1490). ``G``, Gaia's broad band
#: (~330-1050 nm, the closest match to an unfiltered silicon camera): about
#: 780 photons/s/cm2/A at 620 nm over ~4000 A (an estimate, +-20 %).
PHOTON_RATE = {
    "U": 5.0e5,
    "B": 1.31e6,
    "V": 8.76e5,
    "R": 9.7e5,
    "I": 6.7e5,
    "G": 3.1e6,
    "clear": 3.1e6,
}


@dataclass(frozen=True)
class PortPhotometry:
    """One port's photometry (module doc). ``aperture`` mm; ``obstruction``
    the central obstruction as a fraction of the diameter."""

    band: str = "G"
    aperture: float = 700.0
    obstruction: float = 0.47
    #: Named throughput factors; their product is the efficiency.
    efficiency: Mapping[str, float] = field(default_factory=dict)
    #: Measured zero point (mag giving 1 e-/s), with its error and when.
    zero_point: float | None = None
    zero_point_error: float | None = None
    zero_point_date: str | None = None
    #: Detector (for S/N and saturation): e-/ADU, e- rms, ADU.
    gain: float = 1.0
    read_noise: float = 5.0
    saturation: float = 65535.0
    bias: float = 0.0
    #: e-/px/s.
    dark: float = 0.0
    #: Atmospheric extinction, mag per airmass.
    extinction: float = 0.15

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> PortPhotometry:
        d = {k: v for k, v in dict(d or {}).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"photometry: unknown keys {sorted(set(d) - known)}")
        out = cls(**d)
        if out.band not in PHOTON_RATE:
            raise ValueError(f"photometry: band {out.band!r} not one of {sorted(PHOTON_RATE)}")
        for k, v in out.efficiency.items():
            if not 0 < float(v) <= 1:
                raise ValueError(f"photometry: efficiency {k}={v} must be in (0, 1]")
        return out

    @property
    def area_cm2(self) -> float:
        r = self.aperture / 20.0
        return math.pi * r * r * (1.0 - self.obstruction**2)

    @property
    def throughput(self) -> float:
        p = 1.0
        for v in self.efficiency.values():
            p *= float(v)
        return p

    @property
    def zp_estimated(self) -> float:
        """The zero point from the efficiency chain alone."""
        return 2.5 * math.log10(PHOTON_RATE[self.band] * self.area_cm2 * self.throughput)

    @property
    def zp(self) -> float:
        return self.zero_point if self.zero_point is not None else self.zp_estimated

    def rate(self, mag: float, airmass: float = 1.0) -> float:
        """e-/s from a star of ``mag`` at ``airmass``."""
        return 10.0 ** (-0.4 * (mag + self.extinction * airmass - self.zp))

    def sky_rate(self, mu: float, pixel_scale: float) -> float:
        """e-/s per pixel from a sky of ``mu`` mag/arcsec2 (taken in this
        port's band; the V sky is close enough for G or clear)."""
        return 10.0 ** (-0.4 * (mu - self.zp)) * pixel_scale**2

    def with_fraction(self, name: str, fraction: float) -> PortPhotometry:
        """The same port with one more efficiency factor (e.g. the fiber's
        98 % behind the FAU's 2 % beamsplitter)."""
        eff = dict(self.efficiency)
        eff[name] = fraction
        return PortPhotometry(**{**self.__dict__, "efficiency": eff, "zero_point": None})


def electrons(phot: PortPhotometry, mag: float, exptime: float, airmass: float = 1.0) -> float:
    return phot.rate(mag, airmass) * exptime


def snr(
    phot: PortPhotometry,
    mag: float,
    exptime: float,
    *,
    sky_per_pixel: float,
    npix: float,
    airmass: float = 1.0,
) -> float:
    """CCD-equation S/N of a star in an aperture of ``npix`` pixels."""
    s = electrons(phot, mag, exptime, airmass)
    noise2 = s + npix * ((sky_per_pixel + phot.dark) * exptime + phot.read_noise**2)
    return s / math.sqrt(noise2) if noise2 > 0 else 0.0


# -- the sky ---------------------------------------------------------------------


@dataclass(frozen=True)
class SkyConditions:
    """What the sky brightness depends on (angles deg)."""

    sun_alt: float = -30.0
    moon_alt: float = -30.0
    #: Moon phase angle: 0 full, 180 new.
    moon_phase: float = 180.0
    #: Angular distance from the target to the Moon.
    moon_sep: float = 90.0
    target_alt: float = 60.0


def _airmass_ks(z_deg: float) -> float:
    """Krisciunas & Schaefer's airmass (their eq. 3)."""
    s = math.sin(math.radians(min(z_deg, 89.9)))
    return 1.0 / math.sqrt(1.0 - 0.96 * s * s)


def sky_brightness(c: SkyConditions, *, dark: float = 21.7, k: float = 0.172) -> float:
    """V surface brightness (mag/arcsec2) of the sky (module doc).

    ``dark`` is the zenith dark-sky brightness (21.7: a good dark site,
    solar minimum). Twilight: ``m = 11 + 1.2 (-6 - sun_alt)`` for the Sun
    between -18 and 0 deg below the horizon, an empirical ramp (about
    1.2 mag per degree of depression; Tyson & Gal 1993). Moonlight:
    Krisciunas & Schaefer 1991, eqs. 15-21."""
    xz = _airmass_ks(90.0 - max(c.target_alt, 1.0))
    # Dark sky brightens towards the horizon (K&S eq. 2), in nanolamberts.
    b_dark = 34.08 * math.exp(20.7233 - 0.92104 * dark) * 10 ** (-0.4 * k * (xz - 1)) * xz
    b = b_dark
    if c.sun_alt > -18.0:
        m_twi = 11.0 + 1.2 * (-6.0 - min(c.sun_alt, 0.0))
        b += 34.08 * math.exp(20.7233 - 0.92104 * m_twi)
    if c.moon_alt > 0 and c.moon_phase < 180.0:
        alpha = abs(c.moon_phase)
        istar = 10 ** (-0.4 * (3.84 + 0.026 * alpha + 4e-9 * alpha**4))
        rho = max(c.moon_sep, 1.0)
        f = 10**5.36 * (1.06 + math.cos(math.radians(rho)) ** 2) + 10 ** (6.15 - rho / 40.0)
        xm = _airmass_ks(90.0 - c.moon_alt)
        b += f * istar * 10 ** (-0.4 * k * xm) * (1 - 10 ** (-0.4 * k * xz))
    return (20.7233 - math.log(b / 34.08)) / 0.92104


def sky_conditions(
    latitude: float, longitude: float, when: datetime, ra: float, dec: float
) -> SkyConditions:
    """The Sun, the Moon and the target (ra, dec deg) at ``when`` (astropy;
    no downloads: :func:`robofast_core.sky.offline_iers`)."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body
    from astropy.time import Time

    from robofast_core.sky import offline_iers

    offline_iers()
    loc = EarthLocation.from_geodetic(longitude * u.deg, latitude * u.deg)
    t = Time(when)
    frame = AltAz(obstime=t, location=loc)
    sun = get_body("sun", t, loc)
    moon = get_body("moon", t, loc)
    target = SkyCoord(ra * u.deg, dec * u.deg)
    elong = sun.separation(moon).deg
    return SkyConditions(
        sun_alt=float(sun.transform_to(frame).alt.deg),
        moon_alt=float(moon.transform_to(frame).alt.deg),
        moon_phase=float(180.0 - elong),
        moon_sep=float(moon.separation(target.transform_to(moon.frame)).deg),
        target_alt=float(target.transform_to(frame).alt.deg),
    )
