#!/usr/bin/env python3
"""Build robofast's star-catalog caches and data bundles from their original
sources (docs/sky.md, docs/zenodo.md).

    # Gaia DR3 tiles (G < 16, HEALPix level 7) covering fields (ra dec radius, deg):
    python scripts/build_catalog.py tiles --cache ~/.cache/robofast/catalog \\
        --field 212.72 62.52 0.3 --field 285.79 25.29 0.3

    # ... or every target of a targets file (RA hours, Dec deg):
    python scripts/build_catalog.py tiles --cache DIR --targets sites/minerva/targets.example.json

    # The bright-star list (every Gaia DR3 star with G < 6.5) into a cache
    # directory (catalog_at() looks there before downloading it from Zenodo):
    python scripts/build_catalog.py bright --cache DIR

    # The Hipparcos stars Gaia lacks (fetched and cached, not bundled):
    python scripts/build_catalog.py hipparcos --cache DIR

    # The Washington Double Star catalog, parsed (target vetting):
    python scripts/build_catalog.py wds --cache DIR

    # The Zenodo bundle of Gaia DR3 extracts (docs/zenodo.md): bright_stars.npy,
    # test_fixture_tiles.npz and README.md, from the Gaia archive:
    python scripts/build_catalog.py bundle --out DIR

    # The synthetic test sky (no Gaia data) the test suite keeps in the repository:
    python scripts/build_catalog.py synthetic --out tests/core/data/catalog

The Gaia archive answers one query in a minute or two, so tiles are fetched
``--workers`` at a time. Tiles already cached are skipped; nothing is
fetched with ``--dry-run``.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robofast_core import catalog as cat
from robofast_core.testing import sky_fixture


def _fields(args: argparse.Namespace) -> list[tuple[float, float, float]]:
    fields = [tuple(map(float, f)) for f in args.field or []]
    for path in args.targets or []:
        data = json.loads(Path(path).read_text())
        items = data if isinstance(data, list) else data.get("targets", [])
        for t in items:
            fields.append((15.0 * float(t["ra_hours"]), float(t["dec_deg"]), args.radius))
    return fields


def cmd_tiles(args: argparse.Namespace) -> int:
    tiles = cat.TileCache(
        args.cache, level=args.level, mag_limit=args.mag_limit, fetch=cat.GaiaArchive()
    )
    todo = cat.fill_cache(tiles, _fields(args), dry_run=True)
    print(f"{len(todo)} tiles to fetch into {tiles.directory}")
    if args.dry_run or not todo:
        return 0

    def one(p: int) -> str:
        rec = tiles.tile(p)
        return f"tile {p}: {len(rec)} stars"

    with ThreadPoolExecutor(args.workers) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    return 0


BRIGHT_ADQL = (
    "SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag AS g, bp_rp "
    "FROM gaiadr3.gaia_source WHERE phot_g_mean_mag < {mag:g}"
)


def build_bright(out: Path, mag_limit: float = 6.5) -> np.ndarray:
    """Every Gaia DR3 star with G < ``mag_limit``, brightest first (ties by
    source_id, so the file is reproducible)."""
    rec = cat.GaiaArchive().query(BRIGHT_ADQL.format(mag=mag_limit)).astype(cat.TILE_DTYPE)
    rec = np.sort(rec, order=["g", "source_id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, rec, allow_pickle=False)
    print(f"{len(rec)} stars (G < {mag_limit:g}) -> {out} ({out.stat().st_size} bytes)")
    return rec


def cmd_bright(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path(args.cache) / cat.BRIGHT_FILE
    build_bright(out, args.mag_limit)
    return 0


def build_fixture_tiles(out: Path, workers: int = 6) -> dict[str, np.ndarray]:
    """The real Gaia DR3 tiles (G < 16, level 7) of the test fields, sorted
    by source_id, as one reproducible .npz (sky_fixture.FIXTURE_TILES)."""
    archive = cat.GaiaArchive()
    pixels = sky_fixture.all_tiles()

    def one(p: int) -> tuple[str, np.ndarray]:
        rec = np.asarray(archive(7, p, 16.0)).astype(cat.TILE_DTYPE)
        print(f"tile {p}: {len(rec)} stars", flush=True)
        return f"{p:06d}", np.sort(rec, order="source_id")

    with ThreadPoolExecutor(workers) as pool:
        arrays = dict(pool.map(one, pixels))
    sky_fixture.write_npz(out, arrays)
    print(f"{len(arrays)} tiles -> {out} ({out.stat().st_size} bytes)")
    return arrays


BUNDLE_README = """\
# Gaia DR3 star-catalog extracts for robofast

Data files read by robofast (https://github.com/jdeast/robofast), an
open-source robotic observatory control system. They are extracts of
**Gaia DR3** (`gaiadr3.gaia_source`), queried from the ESA Gaia archive
(https://gea.esac.esa.int/archive/) and rebuilt reproducibly with
`python scripts/build_catalog.py bundle --out DIR` in the robofast
repository. robofast downloads them on first use, pinned by size and md5.

## Files

* `bright_stars.npy` -- every Gaia DR3 star with `phot_g_mean_mag < 6.5`
  ({n_bright} stars), brightest first. ADQL:
  `{bright_adql}`.
  robofast uses it for offline cone queries, pointing models and the few
  bright stars a field's tiles lack.
* `test_fixture_tiles.npz` -- {n_tiles} HEALPix tiles (nested scheme, level 7;
  Gaia `source_id >> 35` is the level-12 index, so a tile is a `source_id`
  range) with every Gaia DR3 star of `G < 16` ({n_tile_stars} stars), around five
  test fields of robofast's test suite: a sparse high-latitude field
  (212.72, +62.52), HD 185144 (sigma Dra), a dense field near the Galactic
  plane (285.79, +25.29), nu Draconis and tau Ceti. One array per tile,
  keyed by the six-digit tile number. ADQL per tile:
  `SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag AS g, bp_rp FROM
  gaiadr3.gaia_source WHERE source_id BETWEEN <lo> AND <hi> AND
  phot_g_mean_mag < 16`.
* `README.md` -- this file; `MANIFEST.txt` -- name, size and md5 of every
  other file.

`bright_stars.npy` and every array in `test_fixture_tiles.npz` are numpy
structured arrays, readable with `numpy.load(path, allow_pickle=False)`, with the
columns `source_id` (int64), `ra`, `dec` (float64, deg, ICRS, epoch
J2016.0), `pmra` (times cos dec), `pmdec` (float32, mas/yr), `g`
(float32, G mag) and `bp_rp` (float32, BP-RP colour; NaN if unknown).

## Licence and acknowledgement

Gaia data are distributed by ESA under the Creative Commons
Attribution-NonCommercial 3.0 IGO licence (CC BY-NC 3.0 IGO,
https://creativecommons.org/licenses/by-nc/3.0/igo/; terms at
https://www.cosmos.esa.int/web/gaia-users/license). These extracts are
redistributed under the same licence. Credit: ESA/Gaia/DPAC.

This work has made use of data from the European Space Agency (ESA) mission
Gaia (https://www.cosmos.esa.int/gaia), processed by the Gaia Data
Processing and Analysis Consortium (DPAC,
https://www.cosmos.esa.int/web/gaia/dpac/consortium). Funding for the DPAC
has been provided by national institutions, in particular the institutions
participating in the Gaia Multilateral Agreement.

Please cite Gaia Collaboration, Prusti et al. 2016, A&A 595, A1 (the
mission) and Gaia Collaboration, Vallenari et al. 2023, A&A 674, A1
(Data Release 3).

robofast's code is under the BSD 3-Clause licence; these data are not.
"""


def bundle_readme(bright: np.ndarray, tiles: dict[str, np.ndarray]) -> str:
    return BUNDLE_README.format(
        n_bright=len(bright),
        bright_adql=BRIGHT_ADQL.format(mag=6.5),
        n_tiles=len(tiles),
        n_tile_stars=sum(len(t) for t in tiles.values()),
    )


def cmd_bundle(args: argparse.Namespace) -> int:
    """Build the Zenodo bundle of Gaia extracts (docs/zenodo.md)."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stale = sorted(p.name for p in out.iterdir() if not p.name.startswith("."))
    if stale and not args.force:
        print(f"{out} is not empty ({', '.join(stale)}); use --force to rebuild over it")
        return 1
    (out / "MANIFEST.txt").unlink(missing_ok=True)  # zenodo_publish.py writes it
    bright = build_bright(out / cat.BRIGHT_FILE)
    tiles = build_fixture_tiles(out / sky_fixture.FIXTURE_FILE, args.workers)
    (out / "README.md").write_text(bundle_readme(bright, tiles))
    print(f"bundle ready in {out}; next: scripts/zenodo_publish.py (docs/zenodo.md)")
    return 0


def cmd_synthetic(args: argparse.Namespace) -> int:
    out = sky_fixture.synthetic_sky(args.out, seed=args.seed)
    print(f"synthetic test sky -> {out}")
    return 0


def cmd_hipparcos(args: argparse.Namespace) -> int:
    """Hipparcos (van Leeuwen 2007) stars brighter than V ~ 6.5 with no
    Gaia DR3 counterpart (``gaiadr3.hipparcos2_best_neighbour``), at epoch
    J1991.25 moved to J2016.0; Hp is stored in the ``g`` column."""
    rec = cat.GaiaArchive().query(
        "SELECT -h.hip AS source_id, h.ra, h.dec, h.pm_ra AS pmra, h.pm_de AS pmdec, "
        "h.hp_mag AS g, h.b_v AS bp_rp FROM public.hipparcos_newreduction AS h "
        "LEFT OUTER JOIN gaiadr3.hipparcos2_best_neighbour AS n "
        "ON n.original_ext_source_id = h.hip "
        f"WHERE h.hp_mag < {args.mag_limit:g} AND n.source_id IS NULL"
    )
    ra, dec = cat.propagate(rec["ra"], rec["dec"], rec["pmra"], rec["pmdec"], 2016.0 - 1991.25)
    rec["ra"], rec["dec"] = ra, dec
    rec["bp_rp"] = np.nan  # B-V is not BP-RP
    out = Path(args.cache) / "hipparcos-not-in-gaia.npy"
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, rec.astype(cat.TILE_DTYPE), allow_pickle=False)
    print(f"{len(rec)} Hipparcos stars without a Gaia DR3 counterpart -> {out}")
    return 0


def cmd_wds(args: argparse.Namespace) -> int:
    import httpx

    text = httpx.get(args.url, timeout=300, follow_redirects=True).text
    rec = cat.parse_wds(text)
    out = Path(args.cache) / "wds.npy"
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, rec, allow_pickle=False)
    print(f"{len(rec)} WDS pairs -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tiles", help="fetch Gaia DR3 tiles for fields")
    t.add_argument("--cache", required=True)
    t.add_argument("--level", type=int, default=7)
    t.add_argument("--mag-limit", type=float, default=16.0)
    t.add_argument("--field", nargs=3, action="append", metavar=("RA", "DEC", "RADIUS"))
    t.add_argument("--targets", action="append", help="a targets JSON (ra_hours, dec_deg)")
    t.add_argument("--radius", type=float, default=0.3, help="deg, for --targets")
    t.add_argument("--workers", type=int, default=6)
    t.add_argument("--dry-run", action="store_true")
    b = sub.add_parser("bright", help="build the bright-star list into a cache directory")
    where = b.add_mutually_exclusive_group(required=True)
    where.add_argument("--cache", help="writes CACHE/bright_stars.npy")
    where.add_argument("--out", help="the file to write")
    b.add_argument("--mag-limit", type=float, default=6.5)
    bu = sub.add_parser("bundle", help="build the Zenodo bundle of Gaia extracts")
    bu.add_argument("--out", required=True)
    bu.add_argument("--workers", type=int, default=6)
    bu.add_argument("--force", action="store_true")
    sy = sub.add_parser("synthetic", help="write the synthetic test sky (no Gaia data)")
    sy.add_argument("--out", default="tests/core/data/catalog")
    sy.add_argument("--seed", type=int, default=sky_fixture.SEED)
    h = sub.add_parser("hipparcos", help="cache the Hipparcos stars Gaia lacks")
    h.add_argument("--cache", required=True)
    h.add_argument("--mag-limit", type=float, default=6.5)
    w = sub.add_parser("wds", help="cache the Washington Double Star catalog")
    w.add_argument("--cache", required=True)
    w.add_argument("--url", default=cat.WDS_URL)
    args = ap.parse_args(argv)
    cmds = {
        "tiles": cmd_tiles,
        "bright": cmd_bright,
        "hipparcos": cmd_hipparcos,
        "wds": cmd_wds,
        "bundle": cmd_bundle,
        "synthetic": cmd_synthetic,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
