"""The bright stars, and where they (and the Sun, Moon and planets) are.

The catalogue is the Yale Bright Star Catalogue, 5th revised edition
(Hoffleit & Warren 1991, NASA ADC / CDS V/50): 9 110 objects to V ~ 6.5,
with J2000 positions, proper motions and V. It is small (600 kB), public
and photometric in V, which is what an all-sky camera's limit is quoted in.
:func:`fetch_bsc5` downloads it once into the robofast data directory; the
parsed table is cached beside it as ``bsc5.npy``.

Positions on the sky (:func:`star_altaz`) come from one astropy transform
per frame time, offline (bundled IERS tables, :func:`robofast.sky.offline_iers`):
an arcminute is far below a pixel of these cameras (6-10 arcmin).
"""

from __future__ import annotations

import gzip
import math
import os
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

__all__ = [
    "BSC5_URL",
    "Ephemeris",
    "bright_stars",
    "ephemeris",
    "fetch_bsc5",
    "parse_bsc5",
    "refraction_deg",
    "star_altaz",
]

#: Public copy of the catalogue (Harvard TDC mirror of NASA ADC V/50).
BSC5_URL = "http://tdc-www.harvard.edu/catalogs/bsc5.dat.gz"
#: A site: (latitude deg N, longitude deg E, elevation m); a camera's
#: comes from its spec (:attr:`robofast.allsky.cameras.CameraSpec.site`).
Site = tuple[float, float, float]

STAR_DTYPE = np.dtype(
    [("hr", "<i4"), ("ra", "<f8"), ("dec", "<f8"), ("pmra", "<f4"), ("pmdec", "<f4"), ("v", "<f4")]
)


def data_dir() -> Path:
    """Where the catalogue lives: ``$ROBOFAST_DATA_DIR/bsc5`` or
    ``~/.cache/robofast/data/bsc5``."""
    base = os.environ.get("ROBOFAST_DATA_DIR") or Path.home() / ".cache" / "robofast" / "data"
    return Path(base) / "bsc5"


def _f(s: str) -> float:
    s = s.strip()
    return float(s) if s else math.nan


def parse_bsc5(lines: list[str]) -> np.ndarray:
    """The catalogue's fixed-width records (ReadMe of V/50) as
    :data:`STAR_DTYPE`; objects without a J2000 position or V are dropped.
    ``pmra`` is the proper motion in RA times cos(Dec), arcsec/yr."""
    out = []
    for line in lines:
        line = line.rstrip("\n").ljust(197)
        if not line[75:77].strip() or not line[102:107].strip():
            continue
        ra = 15.0 * (int(line[75:77]) + int(line[77:79]) / 60 + float(line[79:83]) / 3600)
        sign = -1.0 if line[83] == "-" else 1.0
        dec = sign * (int(line[84:86]) + int(line[86:88]) / 60 + int(line[88:90]) / 3600)
        pmra = _f(line[148:154])
        pmdec = _f(line[154:160])
        out.append(
            (
                int(line[0:4]),
                ra,
                dec,
                0.0 if math.isnan(pmra) else pmra,
                0.0 if math.isnan(pmdec) else pmdec,
                float(line[102:107]),
            )
        )
    return np.array(out, dtype=STAR_DTYPE)


def fetch_bsc5(dest: Path | None = None, url: str = BSC5_URL) -> Path:
    """Download the catalogue (once) and cache the parsed table; returns
    the ``.npy`` path."""
    d = dest or data_dir()
    d.mkdir(parents=True, exist_ok=True)
    raw = d / "bsc5.dat.gz"
    if not raw.is_file():
        tmp = raw.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as r:
            tmp.write_bytes(r.read())
        tmp.rename(raw)
    return _cache(raw)


def _cache(raw: Path) -> Path:
    npy = raw.parent / "bsc5.npy"
    if not npy.is_file() or npy.stat().st_mtime < raw.stat().st_mtime:
        with gzip.open(raw, "rt", encoding="latin-1") as f:
            np.save(npy, parse_bsc5(f.readlines()))
    return npy


_CACHE: dict[str, np.ndarray] = {}


def bright_stars(path: str | Path | None = None, vmax: float = 6.5) -> np.ndarray:
    """The catalogue to ``vmax``, brightest first. ``path``: a ``.npy`` of
    :data:`STAR_DTYPE` or the ``bsc5.dat.gz``; default the data directory
    (raises FileNotFoundError naming :func:`fetch_bsc5` if absent)."""
    if path is None:
        raw = data_dir() / "bsc5.dat.gz"
        npy = data_dir() / "bsc5.npy"
        if raw.is_file():
            npy = _cache(raw)
        elif not npy.is_file():
            raise FileNotFoundError(
                f"no bright-star catalogue in {data_dir()}: run "
                "`python -m robofast.allsky fetch-catalog`"
            )
        p = npy
    else:
        p = Path(path)
        if p.suffix == ".gz":
            p = _cache(p)
    key = str(p)
    if key not in _CACHE:
        _CACHE[key] = np.sort(np.load(p, allow_pickle=False), order="v")
    stars = _CACHE[key]
    return stars[stars["v"] <= vmax]


def refraction_deg(alt: np.ndarray, pressure_hpa: float = 740.0, temp_c: float = 5.0) -> np.ndarray:
    """Bennett's refraction (degrees) for apparent altitude, scaled for
    pressure and temperature; fine to ~0.1 arcmin above 5 deg."""
    a = np.maximum(np.asarray(alt, dtype=float), -1.0)
    r = 1.0 / np.tan(np.radians(a + 7.31 / (a + 4.4))) / 60.0
    return r * (pressure_hpa / 1010.0) * (283.0 / (273.0 + temp_c))


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def star_altaz(stars: np.ndarray, when: datetime, site: Site) -> tuple[np.ndarray, np.ndarray]:
    """Apparent (refracted) altitude and azimuth (deg, azimuth east of
    north) of ``stars`` at ``when``."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    from robofast.sky import offline_iers

    offline_iers()
    when = _as_utc(when)
    years = (Time(when).jyear - 2000.0) if stars.size else 0.0
    dec = stars["dec"] + stars["pmdec"] * years / 3600.0
    cosd = np.maximum(np.cos(np.radians(stars["dec"])), 1e-6)
    ra = stars["ra"] + stars["pmra"] * years / 3600.0 / cosd
    loc = EarthLocation.from_geodetic(site[1] * u.deg, site[0] * u.deg, site[2] * u.m)
    frame = AltAz(obstime=Time(when), location=loc)
    aa = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").transform_to(frame)
    alt = aa.alt.deg
    return alt + refraction_deg(alt), aa.az.deg


@dataclass(frozen=True)
class Ephemeris:
    """Sun and Moon (and the bright planets) at one time, degrees."""

    sun_alt: float
    sun_az: float
    moon_alt: float
    moon_az: float
    #: Fraction of the Moon's disc lit, 0-1.
    moon_illum: float
    #: name -> (alt, az) of Venus, Mars, Jupiter, Saturn.
    planets: dict[str, tuple[float, float]]


def ephemeris(when: datetime, site: Site) -> Ephemeris:
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body
    from astropy.time import Time

    from robofast.sky import offline_iers

    offline_iers()
    t = Time(_as_utc(when))
    loc = EarthLocation.from_geodetic(site[1] * u.deg, site[0] * u.deg, site[2] * u.m)
    frame = AltAz(obstime=t, location=loc)
    out = {}
    for body in ("sun", "moon", "venus", "mars", "jupiter", "saturn"):
        c = get_body(body, t, loc)
        aa = c.transform_to(frame)
        out[body] = (float(aa.alt.deg), float(aa.az.deg), c)
    sun_c, moon_c = out["sun"][2], out["moon"][2]
    elong = float(sun_c.separation(moon_c).rad)
    illum = (1 - math.cos(elong)) / 2
    return Ephemeris(
        sun_alt=out["sun"][0],
        sun_az=out["sun"][1],
        moon_alt=out["moon"][0],
        moon_az=out["moon"][1],
        moon_illum=illum,
        planets={k: out[k][:2] for k in ("venus", "mars", "jupiter", "saturn")},
    )
