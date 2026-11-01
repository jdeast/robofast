# The synthetic test sky

**Not Gaia data.** A stand-in for the real Gaia DR3 test fixture, which is
under ESA's CC BY-NC 3.0 IGO licence and so not in this BSD repository (it
is downloaded from Zenodo once its record is pinned; `docs/sky.md`,
"Tests"):

* `gaia-dr3-g16-hpx7/`: the 30 HEALPix tiles of the five test fields, in
  the tile-cache layout, filled with a seeded random catalog (Gaia-like star
  counts, magnitudes and proper motions per field) plus hand-placed stars
  where the tests need them. `manifest.json` says `SYNTHETIC`.
* `bright_stars.npy`: those hand-placed bright stars and a random all-sky
  sprinkle.

Regenerate with `python scripts/build_catalog.py synthetic` (generator:
`robofast_core/testing/sky_fixture.py`; `tests/core/test_sky_fixture.py`
checks these files are what it writes).

* `wds.npy`: 15 real lines of the Washington Double Star catalog (US Naval
  Observatory, a US Government work, public domain), parsed by
  `robofast_core.catalog.parse_wds`. Used with either sky.
