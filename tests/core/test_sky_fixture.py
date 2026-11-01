"""The test sky (robofast_core/testing/sky_fixture.py): the synthetic sky in
the repository is what its generator writes, and the fixture .npz format
round-trips."""

from __future__ import annotations

import json

import numpy as np

from robofast_core import catalog as cat
from robofast_core.testing import sky_fixture
from tests.core.skydata import SYNTHETIC


def test_committed_synthetic_sky_is_the_generators(tmp_path):
    out = sky_fixture.synthetic_sky(tmp_path)
    ours = sorted(p.relative_to(out) for p in out.rglob("*.npy"))
    theirs = sorted(p.relative_to(SYNTHETIC) for p in SYNTHETIC.rglob("*.npy"))
    assert ours == [p for p in theirs if p.name != "wds.npy"]
    for rel in ours:
        a, b = np.load(out / rel), np.load(SYNTHETIC / rel)
        assert a.dtype == b.dtype and len(a) == len(b), rel
        for name in a.dtype.names:
            assert np.allclose(a[name], b[name], equal_nan=True), (rel, name)
    manifest = json.loads((SYNTHETIC / "gaia-dr3-g16-hpx7" / "manifest.json").read_text())
    assert manifest["catalog"].startswith("SYNTHETIC")


def test_synthetic_sky_has_the_twins():
    c = cat.catalog_at(SYNTHETIC)
    nu = c.cone(*sky_fixture.FIELDS["nu_dra"], 0.02, mag_limit=6.0)
    assert list(np.round(nu.g, 2)) == [4.79, 4.84]
    sep = cat.separation(nu.ra[0], nu.dec[0], nu.ra[1], nu.dec[1]) * 3600
    assert abs(sep - 62.2) < 0.01
    # Every tile is where its stars are, as with Gaia's source_id.
    tiles = cat.TileCache(SYNTHETIC)
    for p in sky_fixture.all_tiles():
        rec = tiles.tile(p)
        assert len(rec) and np.all(cat.ang2pix(7, rec["ra"], rec["dec"]) == p)


def test_npz_is_reproducible_and_unpacks(tmp_path):
    arrays = {"000002": np.zeros(3, cat.TILE_DTYPE), "000001": np.ones(2, cat.TILE_DTYPE)}
    a = sky_fixture.write_npz(tmp_path / "a.npz", arrays)
    b = sky_fixture.write_npz(tmp_path / "b.npz", dict(reversed(arrays.items())))
    assert a.read_bytes() == b.read_bytes()
    tiles = sky_fixture.unpack_tiles(a, tmp_path / "root")
    assert len(tiles.tile(2)) == 3 and len(tiles.tile(1)) == 2
    assert (tiles.directory / "manifest.json").is_file()
