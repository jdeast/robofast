"""The test sky: five fields, as real Gaia DR3 tiles (from Zenodo) or as a
synthetic stand-in (in the repository).

The test fields (ra, dec deg; ``FIELDS``): a sparse high-latitude field, HD
185144 (sigma Draconis), a dense field near the Galactic plane, nu Draconis
(an equal pair 62 arcsec apart) and tau Ceti.

**Real.** ``FIXTURE_TILES`` are the HEALPix level-7 tiles around them. Their
Gaia DR3 contents (CC BY-NC 3.0 IGO, so not in the BSD repository) are one
file, ``test_fixture_tiles.npz``, of the Zenodo record ``gaia_extracts``
(:mod:`robofast_core.zenodo_assets`): one array per tile, keyed by the
tile's six-digit number. ``python scripts/build_catalog.py bundle`` rebuilds
it from the Gaia archive; :func:`unpack_tiles` lays it out as a tile cache.

**Synthetic.** :func:`synthetic_sky` writes the same tiles filled with a
seeded random catalog -- star counts, magnitude distribution and proper
motions like Gaia's in each field, ``source_id`` built from the HEALPix
index as Gaia's is -- plus hand-placed stars where the tests need them:
the nu Draconis pair in the geometry of its WDS entry (STFA 35, a US
Government work: the G 4.79 primary and, 62.2 arcsec away at position angle
308 deg, the G 4.84 star the tests call nu2),
sigma Draconis and tau Ceti (bright, fast-moving), and a bright pair in the
sparse field. Its ``bright_stars.npy`` holds those bright stars and a
random all-sky sprinkle. No Gaia data go into it (``python
scripts/build_catalog.py synthetic`` regenerates it; the test suite keeps a
copy in ``tests/core/data/catalog``).
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from robofast_core.catalog import (
    BRIGHT_FILE,
    TILE_DTYPE,
    TileCache,
    _untan,
    ang2pix,
    pix2ang,
    propagate,
    separation,
)

__all__ = [
    "FIELDS",
    "FIXTURE_FILE",
    "FIXTURE_TILES",
    "read_npz",
    "synthetic_sky",
    "unpack_tiles",
    "write_npz",
]

#: The test fields (ra, dec deg).
FIELDS: Mapping[str, tuple[float, float]] = {
    "sparse": (212.720875, 62.522197),
    "hd185144": (293.2035, 69.6611),
    "dense": (285.79486, 25.292175),
    "nu_dra": (263.056, 55.179),
    "tau_cet": (26.016, -15.9375),
}
#: The HEALPix (nested, level 7) tiles of the test fixture, by field.
FIXTURE_TILES: Mapping[str, tuple[int, ...]] = {
    "sparse": (47221, 47223, 47370, 47371, 47392, 47393, 47394, 47395),
    "hd185144": (64275, 64276, 64277, 64278, 64279, 64281, 64284, 64285, 64320, 64322),
    "dense": (128860, 128861, 128863),
    "nu_dra": (40384, 40385, 40386, 40387, 40392),
    "tau_cet": (69700, 69701, 69702, 69703),
}
#: The fixture file's name in the Zenodo record.
FIXTURE_FILE = "test_fixture_tiles.npz"
#: Fixed zip timestamps, so the same arrays always give the same bytes.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def all_tiles() -> list[int]:
    return sorted(p for ts in FIXTURE_TILES.values() for p in ts)


def write_npz(path: str | Path, arrays: Mapping[str, np.ndarray]) -> Path:
    """An ``.npz`` of ``arrays`` (sorted by key, fixed timestamps,
    uncompressed): byte-for-byte reproducible, unlike ``np.savez``, and
    readable by ``np.load(allow_pickle=False)``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for key in sorted(arrays):
            buf = io.BytesIO()
            np.lib.format.write_array(buf, np.ascontiguousarray(arrays[key]), allow_pickle=False)
            info = zipfile.ZipInfo(f"{key}.npy", date_time=_ZIP_TIME)
            info.external_attr = 0o644 << 16
            z.writestr(info, buf.getvalue())
    return path


def read_npz(path: str | Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def unpack_tiles(npz: str | Path, root: str | Path) -> TileCache:
    """Lay the tiles of ``npz`` out as a tile cache under ``root``."""
    tiles = TileCache(root)
    for key, rec in read_npz(npz).items():
        tiles.save(int(key), rec.astype(TILE_DTYPE))
    return tiles


# -- the synthetic sky -----------------------------------------------------------------

#: Synthetic stars (G < 16) per tile, by field: like Gaia DR3's there, a
#: little sparser in the two densest fields to keep the files small.
_PER_TILE = {"sparse": 80, "hd185144": 160, "dense": 700, "nu_dra": 130, "tau_cet": 55}
SEED = 20261031


def _hand_placed() -> list[tuple[str, float, float, float, float, float, float]]:
    """(name, ra, dec at J2016, pmra, pmdec, g, bp_rp) of the stars the
    tests rely on (module doc)."""
    stars = []

    def at2016(ra2000, dec2000, pmra, pmdec):
        r, d = propagate(ra2000, dec2000, pmra, pmdec, 16.0)
        return float(r), float(d)

    # The primary (G 4.79) at the WDS position of STFA 35 (J2000), the
    # secondary (G 4.84, "nu2" in the tests) 62.2 arcsec away at position
    # angle 308 deg; a common proper motion.
    pm = (148.0, 62.0)
    r2, d2 = at2016(263.06616667, 55.17280556, *pm)
    rho, theta = 62.2 / 3600.0, math.radians(308.0)
    r1, d1 = _untan(
        r2,
        d2,
        math.tan(math.radians(rho)) * math.sin(theta),
        math.tan(math.radians(rho)) * math.cos(theta),
    )
    stars.append(("nu Dra A", r2, d2, *pm, 4.79, 0.29))
    stars.append(("nu Dra B", float(r1), float(d1), *pm, 4.84, 0.28))
    # sigma Draconis (HD 185144) and tau Ceti: bright and fast.
    stars.append(("sigma Dra", *at2016(293.08995833, 69.66116667, 598.5, -1738.6), 598.5, -1738.6,
                  4.47, 0.95))  # fmt: skip
    stars.append(("tau Cet", *at2016(26.01701, -15.93748, -1721.0, 854.2), -1721.0, 854.2,
                  3.30, 0.89))  # fmt: skip
    # The sparse field: a bright star near the centre and a companion.
    ra0, dec0 = FIELDS["sparse"]
    ra, dec = _untan(ra0, dec0, math.radians(30 / 3600), math.radians(20 / 3600))
    stars.append(("sparse A", float(ra), float(dec), -12.0, 8.0, 9.6, 0.7))
    ra, dec = _untan(ra0, dec0, math.radians(-20 / 3600), math.radians(55 / 3600))
    stars.append(("sparse B", float(ra), float(dec), 4.0, -3.0, 12.1, 1.1))
    return stars


def _source_ids(ra: np.ndarray, dec: np.ndarray, start: int) -> np.ndarray:
    # As Gaia's: the level-12 nested HEALPix index in the top bits.
    return (ang2pix(12, ra, dec).astype(np.int64) << 35) + start + np.arange(len(ra))


def _random_stars(
    rng: np.random.Generator, n: int, ra0: float, dec0: float, radius: float, gmin: float
) -> np.ndarray:
    """``n`` stars uniform within ``radius`` deg of (ra0, dec0), G from
    ``gmin`` to 16 with dN/dG ~ 10**(0.28 G)."""
    r = np.tan(np.radians(radius)) * np.sqrt(rng.uniform(0, 1, n))
    phi = rng.uniform(0, 2 * np.pi, n)
    ra, dec = _untan(ra0, dec0, r * np.cos(phi), r * np.sin(phi))
    a = 0.28
    lo, hi = 10 ** (a * gmin), 10 ** (a * 16.0)
    g = np.log10(lo + rng.uniform(0, 1, n) * (hi - lo)) / a
    rec = np.zeros(n, TILE_DTYPE)
    rec["ra"], rec["dec"], rec["g"] = ra, dec, np.minimum(g, 15.999)
    rec["bp_rp"] = np.clip(rng.normal(1.0, 0.35, n), 0.1, 3.5)
    fast = rng.uniform(0, 1, n) < 0.03
    sigma = np.where(fast, 80.0, 10.0)
    rec["pmra"], rec["pmdec"] = rng.normal(0, sigma), rng.normal(0, sigma)
    return rec


def synthetic_sky(out: str | Path, seed: int = SEED) -> Path:
    """Write the synthetic test sky under ``out`` (module doc): the
    fixture tiles, their manifest and ``bright_stars.npy``."""
    out = Path(out)
    rng = np.random.default_rng(seed)
    hand = _hand_placed()
    tiles = TileCache(out)
    per_tile: dict[int, list[np.ndarray]] = {p: [] for p in all_tiles()}
    start = 1
    for name, pixels in FIXTURE_TILES.items():
        # Sample a cap around the field's tiles, keep what lands in them.
        ra, dec = pix2ang(7, np.array(pixels))
        ra0, dec0 = float(ra[0]), float(dec[0])
        radius = float(np.max(separation(ra0, dec0, ra, dec))) + 0.35
        want = _PER_TILE[name] * len(pixels)
        rec = _random_stars(rng, 8 * want + 400, ra0, dec0, radius, gmin=7.0)
        pix = ang2pix(7, rec["ra"], rec["dec"])
        rec = rec[np.isin(pix, pixels)]
        # Keep no random star brighter than G 8 within 0.3 deg of the
        # field centre: the hand-placed ones must stay the brightest.
        cra, cdec = FIELDS[name]
        near = separation(cra, cdec, rec["ra"], rec["dec"]) < 0.3
        rec = rec[~(near & (rec["g"] < 8.0))]
        rec = rec[: _PER_TILE[name] * len(pixels)]
        rec["source_id"] = _source_ids(rec["ra"], rec["dec"], start)
        start += len(rec)
        for p in pixels:
            per_tile[p].append(rec[ang2pix(7, rec["ra"], rec["dec"]) == p])
    bright = []
    for k, (_, ra, dec, pmra, pmdec, g, bp_rp) in enumerate(hand):
        one = np.array([(0, ra, dec, pmra, pmdec, g, bp_rp)], TILE_DTYPE)
        one["source_id"] = _source_ids(one["ra"], one["dec"], 10**9 + k)
        p = int(ang2pix(7, one["ra"], one["dec"])[0])
        if p in per_tile:
            per_tile[p].append(one)
        if g < 6.5:
            bright.append(one)
    for p, parts in per_tile.items():
        rec = np.concatenate(parts) if parts else np.zeros(0, TILE_DTYPE)
        tiles.save(p, np.sort(rec, order="source_id"))
    # The synthetic manifest replaces the Gaia one TileCache wrote.
    (tiles.directory / "manifest.json").write_text(
        json.dumps(
            {
                "catalog": "SYNTHETIC (not Gaia): robofast_core.testing.sky_fixture",
                "licence": "BSD 3-Clause, like the code",
                "seed": seed,
                "epoch": 2016.0,
                "healpix": {"scheme": "nested", "level": tiles.level},
                "mag_limit": {"band": "G", "value": tiles.mag_limit},
                "columns": {n: str(TILE_DTYPE[n]) for n in TILE_DTYPE.names},
            },
            indent=1,
        )
        + "\n"
    )
    # A random all-sky sprinkle of bright stars, away from the fields.
    sky = np.zeros(0, TILE_DTYPE)
    while len(sky) < 300:
        n = 400
        z = rng.uniform(-1, 1, n)
        rec = np.zeros(n, TILE_DTYPE)
        rec["ra"] = rng.uniform(0, 360, n)
        rec["dec"] = np.degrees(np.arcsin(z))
        rec["g"] = rng.uniform(1.5, 6.5, n)
        rec["bp_rp"] = np.clip(rng.normal(0.8, 0.5, n), -0.2, 3.0)
        rec["pmra"], rec["pmdec"] = rng.normal(0, 30, n), rng.normal(0, 30, n)
        far = np.ones(n, bool)
        for ra0, dec0 in FIELDS.values():
            far &= separation(ra0, dec0, rec["ra"], rec["dec"]) > 1.0
        sky = np.concatenate([sky, rec[far]])
    sky = sky[:300]
    sky["source_id"] = _source_ids(sky["ra"], sky["dec"], 2 * 10**9)
    b = np.concatenate([*bright, sky])
    np.save(out / BRIGHT_FILE, np.sort(b, order=["g", "source_id"]), allow_pickle=False)
    return out
