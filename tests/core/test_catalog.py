"""The star catalog: HEALPix tiles, cone queries, proper motion, offline use.

The test sky (tests/core/skydata.py) is real Gaia DR3 tiles (G < 16, HEALPix
level 7) around a few test fields once the Zenodo record is pinned, else a
synthetic stand-in; nothing here touches the network beyond that one
cached download.
"""

from __future__ import annotations

import math
import shutil
from datetime import UTC, datetime

import numpy as np
import pytest

from robofast_core import catalog as cat
from tests.core.skydata import require_gaia, sky_dir

#: The test fields (ra, dec deg): a sparse high-latitude field, HD 185144,
#: a dense field near the plane, nu Draconis (an equal pair), tau Ceti.
SPARSE = (212.720875, 62.522197)
HD185144 = (293.2035, 69.6611)
DENSE = (285.79486, 25.292175)
NU_DRA = (263.056, 55.179)
TAU_CET = (26.016, -15.9375)


@pytest.fixture(scope="module")
def catalog() -> cat.Catalog:
    return cat.catalog_at(sky_dir())


def test_healpix_matches_gaia_source_ids(catalog):
    # Gaia's source_id >> 35 is the level-12 nested HEALPix index of the
    # star's (J2016) position: an independent check of ang2pix (the
    # synthetic sky builds its ids with ang2pix, so only real data check it).
    require_gaia()
    s = catalog.tiles.cone(*SPARSE, 0.3)
    assert len(s) > 50
    assert np.array_equal(cat.ang2pix(12, s.ra, s.dec), s.source_id >> 35)


@pytest.mark.parametrize("level", [0, 2, 5])
def test_pix2ang_inverts_ang2pix(level):
    pix = np.arange(12 * 4**level)
    ra, dec = cat.pix2ang(level, pix)
    assert np.array_equal(cat.ang2pix(level, ra, dec), pix)


@pytest.mark.parametrize(
    "field", [(150.0, 30.0, 0.3), (45.0, 41.8, 0.2), (0.0, 89.9, 0.3), (300.0, -60.0, 0.15)]
)
def test_tiles_in_cone_covers_the_cone(field):
    ra, dec, r = field
    tiles = set(cat.tiles_in_cone(7, ra, dec, r))
    rng = np.random.default_rng(0)
    n = 50000
    th = np.radians(r) * np.sqrt(rng.uniform(0, 1, n))
    ph = rng.uniform(0, 2 * np.pi, n)
    pra, pdec = cat._untan(ra, dec, np.tan(th) * np.cos(ph), np.tan(th) * np.sin(ph))
    assert set(np.unique(cat.ang2pix(7, pra, pdec)).tolist()) <= tiles


def test_source_id_range_is_the_tile():
    lo, hi = cat.source_id_range(7, 40624)
    assert lo >> (35 + 10) == 40624 and hi >> (35 + 10) == 40624
    assert (hi + 1) >> (35 + 10) == 40625


@pytest.mark.parametrize("field", [SPARSE, HD185144, DENSE, NU_DRA, TAU_CET])
def test_cone_queries_on_fixtures(catalog, field):
    s = catalog.cone(*field, 0.1)
    assert len(s) > 0
    assert np.all(cat.separation(*field, s.ra, s.dec) <= 0.1 + 1e-9)
    assert np.all(np.diff(s.g) >= 0)  # brightest first
    assert np.all(s.g < 16.0)


def test_denser_near_the_galactic_plane(catalog):
    assert len(catalog.cone(*DENSE, 0.1)) > 5 * len(catalog.cone(*SPARSE, 0.1))


def test_mag_limit_and_bright_stars(catalog):
    s = catalog.cone(*NU_DRA, 0.05, mag_limit=6.0)
    # nu1 and nu2 Draconis: G 4.79 and 4.84, 62 arcsec apart.
    assert len(s) == 2
    assert abs(s.g[1] - s.g[0]) < 0.1
    assert 55 < cat.separation(s.ra[0], s.dec[0], s.ra[1], s.dec[1]) * 3600 < 70


def test_missing_tile_offline_fails_clearly(tmp_path):
    # A bright-star file dropped into the cache directory is used (how an
    # offline host is pre-filled, docs/sky.md).
    shutil.copyfile(sky_dir() / cat.BRIGHT_FILE, tmp_path / cat.BRIGHT_FILE)
    c = cat.catalog_at(tmp_path)
    with pytest.raises(cat.CatalogUnavailable, match="not cached"):
        c.cone(10.0, 10.0, 0.1)
    # The bright stars answer with no tile (tau Ceti, G 3.3).
    assert len(c.cone(*TAU_CET, 0.1, bright_only=True)) == 1


def test_no_bright_stars_is_a_warning_not_a_failure(tmp_path, monkeypatch, caplog):
    # Nothing in the cache directory, the record not pinned (or offline):
    # the catalog still works from its tiles; bright_only says why it cannot.
    monkeypatch.setenv("ROBOFAST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROBOFAST_OFFLINE", "1")
    monkeypatch.setattr(cat, "_BRIGHT", None)
    monkeypatch.setattr(cat, "_WARNED", set())
    tiles = cat.TileCache(sky_dir())
    c = cat.Catalog(tiles)
    assert c.bright is None and "bright-star" in caplog.text
    assert len(c.cone(*SPARSE, 0.1)) > 0
    with pytest.raises(cat.CatalogUnavailable, match="bright"):
        c.cone(*TAU_CET, 0.1, bright_only=True)


def test_missing_tile_is_fetched_once_and_cached(tmp_path):
    calls = []
    src = cat.TileCache(sky_dir())

    def fetch(level, pix, mag):
        calls.append(pix)
        return src.tile(pix)

    tiles = cat.TileCache(tmp_path, fetch=fetch)
    c = cat.Catalog(tiles, bright=None)
    a = c.cone(*SPARSE, 0.1)
    n = len(calls)
    assert n >= 1 and (tiles.directory / "manifest.json").is_file()
    # A second cache object on the same directory needs no fetch.
    tiles2 = cat.TileCache(tmp_path, fetch=None)
    b = cat.Catalog(tiles2, bright=None).cone(*SPARSE, 0.1)
    assert len(calls) == n
    assert np.array_equal(a.source_id, b.source_id)


def test_gaia_bright_stars():
    require_gaia()
    b = cat.bright_stars(sky_dir() / cat.BRIGHT_FILE)
    assert 9000 < len(b) < 15000
    assert b.g.max() < 6.5
    # tau Ceti is there; Vega (G ~ 0) is not: Gaia lacks the very
    # brightest stars (the Hipparcos supplement, when cached, adds them).
    assert len(b.cone(*TAU_CET, 0.1)) == 1
    assert len(b.cone(279.2347, 38.7837, 0.05)) == 0


def test_bright_supplement_only_where_gaia_has_nothing():
    gaia = cat.Stars.from_records(np.array([(1, 10.0, 10.0, 0, 0, 5.0, 0.5)], dtype=cat.TILE_DTYPE))
    bright = cat.Stars.from_records(
        np.array(
            [
                (-1, 10.0, 10.0 + 0.5 / 3600, 0, 0, 5.1, np.nan),
                (-2, 10.01, 10.0, 0, 0, 1.0, np.nan),
            ],
            dtype=cat.TILE_DTYPE,
        ),
        source="bright",
    )

    class Tiles:
        mag_limit = 16.0

        def cone(self, ra, dec, r):
            return gaia.cone(ra, dec, r)

    c = cat.Catalog(Tiles(), bright=bright)  # type: ignore[arg-type]
    s = c.cone(10.0, 10.0, 0.1)
    assert list(s.source_id) == [-2, 1]
    assert list(s.source) == ["bright", "gaia"]


def test_proper_motion_propagation():
    # Barnard's star: ~10.4 arcsec/yr, mostly north.
    ra, dec = 269.4486, 4.7397
    pmra, pmdec = -801.551, 10362.394
    r2, d2 = cat.propagate(ra, dec, pmra, pmdec, 10.0)
    moved = cat.separation(ra, dec, r2, d2) * 3600
    assert moved == pytest.approx(math.hypot(pmra, pmdec) * 10 / 1000, rel=1e-4)
    assert d2 > dec and r2 < ra
    # And back.
    r3, d3 = cat.propagate(r2, d2, pmra, pmdec, -10.0)
    assert cat.separation(ra, dec, r3, d3) * 3600 < 0.01
    # Near the pole, the sphere is handled (no RA blow-up).
    r4, d4 = cat.propagate(0.0, 89.999, 1000.0, 0.0, 10.0)
    assert cat.separation(0.0, 89.999, r4, d4) * 3600 == pytest.approx(10.0, rel=1e-3)


def test_cone_at_epoch_moves_the_stars(catalog):
    s16 = catalog.cone(*SPARSE, 0.2)
    s26 = catalog.cone(*SPARSE, 0.2, epoch=datetime(2026, 1, 1, tzinfo=UTC))
    assert s26.epoch == pytest.approx(2026.0, abs=0.01)
    _, i16, i26 = np.intersect1d(s16.source_id, s26.source_id, return_indices=True)
    k = int(np.argmax(np.hypot(s16.pmra[i16], s16.pmdec[i16])))
    moved = cat.separation(s16.ra[i16][k], s16.dec[i16][k], s26.ra[i26][k], s26.dec[i26][k])
    pm = math.hypot(s16.pmra[i16][k], s16.pmdec[i16][k])
    assert moved * 3600 == pytest.approx(pm * (s26.epoch - 2016.0) / 1000, rel=1e-3)


def test_g_to_v():
    # A solar-type star (BP-RP 0.82): V - G ~ +0.15 (Riello et al. 2021).
    assert cat.g_to_v(10.0, 0.82) - 10.0 == pytest.approx(0.15, abs=0.02)
    assert np.isfinite(cat.g_to_v(10.0, np.nan))


def test_parse_csv_with_nulls():
    rec = cat.parse_csv("source_id,ra,dec,pmra,pmdec,g,bp_rp\n5,1.5,2.5,,,12.0,\n")
    assert rec["source_id"][0] == 5 and np.isnan(rec["pmra"][0]) and rec["g"][0] == 12.0
    s = cat.Stars.from_records(rec)
    assert s.pmra[0] == 0.0  # unknown proper motion: none


def test_wds_fixture_and_parser(catalog):
    assert catalog.doubles is not None
    pairs = catalog.doubles.cone(*NU_DRA, 0.05)
    assert any(p["rho"] > 60 and abs(p["mag1"] - p["mag2"]) < 0.1 for p in pairs)
    line = (
        "01535+1918STF 180AB    1800 2025  413   0 359   8.9   7.2  4.52  4.58 A2IV+B9V  "
        "+078-097 +077-107 +18  243 NT   015331.76+191738.6"
    )
    r = cat.parse_wds(line)
    assert r["disc"][0] == "STF 180" and r["comp"][0] == "AB"
    assert r["rho"][0] == pytest.approx(7.2) and r["theta"][0] == pytest.approx(359)
    assert r["ra"][0] == pytest.approx(15 * (1 + 53 / 60 + 31.76 / 3600))
    assert r["dec"][0] == pytest.approx(19 + 17 / 60 + 38.6 / 3600)


def test_catalog_from_config(tmp_path, monkeypatch):
    c = cat.catalog_from_config({"cache_dir": str(sky_dir()), "fetch": False})
    assert c.tiles is not None and c.tiles.fetch is None
    assert len(c.cone(*SPARSE, 0.05)) > 0
    with pytest.raises(ValueError, match="unknown keys"):
        cat.catalog_from_config({"cache": "x"})
    monkeypatch.setenv("ROBOFAST_CATALOG_DIR", str(tmp_path))
    assert cat.catalog_from_config({"fetch": False}).tiles.root == tmp_path
