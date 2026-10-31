#!/usr/bin/env python3
"""Build robofast's star-catalog caches from the Gaia archive (docs/sky.md).

    # Gaia DR3 tiles (G < 16, HEALPix level 7) covering fields (ra dec radius, deg):
    python scripts/build_catalog.py tiles --cache ~/.cache/robofast/catalog \\
        --field 212.72 62.52 0.3 --field 285.79 25.29 0.3

    # ... or every target of a targets file (RA hours, Dec deg):
    python scripts/build_catalog.py tiles --cache DIR --targets sites/minerva/targets.example.json

    # The bundled bright-star catalog (every Gaia DR3 star with G < 6.5):
    python scripts/build_catalog.py bright --out robofast_core/data/bright_stars.npy

    # The Hipparcos stars Gaia lacks (fetched and cached, not bundled):
    python scripts/build_catalog.py hipparcos --cache DIR

    # The Washington Double Star catalog, parsed (target vetting):
    python scripts/build_catalog.py wds --cache DIR

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


def cmd_bright(args: argparse.Namespace) -> int:
    rec = cat.GaiaArchive().query(
        "SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag AS g, bp_rp "
        f"FROM gaiadr3.gaia_source WHERE phot_g_mean_mag < {args.mag_limit:g}"
    )
    rec = np.sort(rec, order="g")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, rec.astype(cat.TILE_DTYPE), allow_pickle=False)
    print(f"{len(rec)} stars (G < {args.mag_limit:g}) -> {out} ({out.stat().st_size} bytes)")
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
    b = sub.add_parser("bright", help="build the bundled bright-star catalog")
    b.add_argument("--out", default="robofast_core/data/bright_stars.npy")
    b.add_argument("--mag-limit", type=float, default=6.5)
    h = sub.add_parser("hipparcos", help="cache the Hipparcos stars Gaia lacks")
    h.add_argument("--cache", required=True)
    h.add_argument("--mag-limit", type=float, default=6.5)
    w = sub.add_parser("wds", help="cache the Washington Double Star catalog")
    w.add_argument("--cache", required=True)
    w.add_argument("--url", default=cat.WDS_URL)
    args = ap.parse_args(argv)
    cmds = {"tiles": cmd_tiles, "bright": cmd_bright, "hipparcos": cmd_hipparcos, "wds": cmd_wds}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
