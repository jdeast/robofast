# The sky: catalog, simulator, plate solving

robofast knows where the stars are (`robofast_core/catalog.py`), can render
what a camera on a mis-pointing telescope sees (`robofast_core/testing/skysim.py`),
and can work out from an image where the telescope really points
(`robofast_core/astrometry.py`). Every light frame can be solved as it is
written, with its WCS and photometric zero point in the header
(`robofast_core/framesolve.py`). The pieces are pure numpy plus astropy
(already a dependency); nothing needs the network once the catalog cache is
filled.

## 1. The catalog

`Catalog.cone(ra, dec, radius, epoch=..., mag_limit=...)` returns a `Stars`
table: RA/Dec (ICRS, deg) moved to `epoch` by proper motion, `pmra`/`pmdec`
(mas/yr, `pmra` with cos dec), Gaia `g`, `bp_rp`, an estimated Johnson `v`
(Riello et al. 2021), `source_id` and the `source` of each row, brightest
first. Sources, in order:

| Source | What | Where | Network |
|---|---|---|---|
| Gaia DR3 tiles (`TileCache`) | every star with G < 16 | the cache directory | fetched on demand, then never |
| bright stars (`bright_stars()`) | every Gaia DR3 star with G < 6.5 (12 119) | bundled: `robofast_core/data/bright_stars.npy` (485 kB) | never |
| Hipparcos supplement | the Hipparcos stars Gaia lacks (the very brightest, G < ~3) | the cache directory | `build_catalog.py hipparcos` |
| WDS (`DoubleStars`) | the Washington Double Star catalog (158 000 pairs) | the cache directory | `build_catalog.py wds` |
| VizieR (`VizierSource`) | Gaia DR3 for a field nothing cached covers | -- | every query; needs astroquery (optional) |

Bright-catalog stars are added to a Gaia answer only where Gaia has no star
within 3 arcsec. A cone is widened by 3 arcsec/yr times the epoch difference
before it is cut, so a fast-moving star that has moved into the field is
found (`Catalog.max_pm`).

Offline: a query whose tiles are not cached and cannot be fetched raises
`CatalogUnavailable` (never a silent empty field); `cone(...,
bright_only=True)` uses only the bundled bright stars and always answers.

### Cache layout

```
<cache_dir>/gaia-dr3-g16-hpx7/manifest.json          what, licence, epoch, columns
<cache_dir>/gaia-dr3-g16-hpx7/<pix // 1024:03d>/<pix:06d>.npy
<cache_dir>/hipparcos-not-in-gaia.npy                optional
<cache_dir>/wds.npy                                  optional
```

Tiles are HEALPix pixels, nested scheme, level 7 (nside 128: 196 608 tiles
of 0.21 deg2, about 0.46 deg across). Gaia's `source_id >> 35` is the
star's level-12 nested HEALPix index, so a tile is one `source_id` range
query on the Gaia archive (clustered on it). Each file is a numpy structured
array (`source_id` i8, `ra`, `dec` f8, `pmra`, `pmdec`, `g`, `bp_rp` f4; 36
bytes a star), written atomically. Sizes (G < 16): a high-latitude tile
holds ~60-200 stars (2-7 kB), one near the Galactic plane ~1000-3000 (36-100
kB); the whole sky would be ~2-3 GB, but only the tiles of fields actually
observed are ever fetched. The Gaia archive takes 1-2 minutes per query
(fixed overhead), so `build_catalog.py` fetches several tiles at once;
filling a night's targets ahead of time is the intended use.

`cache_dir` comes from the `catalog` config entry, else
`$ROBOFAST_CATALOG_DIR`, else `~/.cache/robofast/catalog`:

```json
"catalog": {"cache_dir": "/var/lib/robofast/catalog", "level": 7, "mag_limit": 16,
            "fetch": true, "live": false}
```

```sh
python scripts/build_catalog.py tiles --cache DIR --targets sites/minerva/targets.example.json
python scripts/build_catalog.py tiles --cache DIR --field 212.72 62.52 0.4
python scripts/build_catalog.py wds --cache DIR
python scripts/build_catalog.py hipparcos --cache DIR
python scripts/build_catalog.py bright --out robofast_core/data/bright_stars.npy
```

### Licences

Gaia DR3 is distributed by ESA under **CC BY-NC 3.0 IGO**
(<https://www.cosmos.esa.int/web/gaia-users/license>): redistribution is
allowed for non-commercial use, with attribution. The bundled bright-star
file and the test fixture tiles are Gaia data under that licence, not the
code's; `robofast_core/data/README.md` has the attribution and what a
commercial user should do. The Hipparcos catalogue's redistribution terms
are not stated as plainly, so it is fetched and cached, not bundled. The WDS
is a US Naval Observatory product (a US Government work), fetched and cached
from its home at Georgia State; a few lines are in the fixtures.

### Tests

`tests/core/data/catalog/` holds real Gaia DR3 tiles (27 tiles, 290 kB) around
five test fields -- a sparse high-latitude field (212.72, +62.52), HD 185144
(sigma Dra), a dense field near the Galactic plane (285.79, +25.29), nu
Draconis (an equal pair 62 arcsec apart) and tau Ceti -- plus their WDS
lines. CI never downloads a catalog. The HEALPix code is checked against
Gaia's own `source_id`s.

## 2. Pointing-error models (`robofast_core/pointing.py`)

`PointingModel(terms, mount, latitude)` holds TPOINT terms (arcsec):
equatorial IH, ID, CH, NP, MA, ME, TF, FO, DAF, HCES/HCEC, DCES/DCEC; alt-az
IA, IE, CA, NPAE, AN, AW, TF, TX, ACES/ACEC, ECES/ECEC (formulae in the
module doc). Convention: the model gives **mount minus true**.
`mount_from_true` is what to command to land on a star; `true_from_mount`
(fixed-point inverse, < 1 mas) is where a commanded telescope really points.
Every term is linear in its coefficient: `fit(names, true..., mount...)`
fits them by least squares (residuals weighted by cos dec) with formal
errors and the rms before and after -- what a pointing-model run does with
plate-solved images.

## 3. The simulator (`robofast_core/testing/skysim.py`)

`SkyScene(catalog, camera, photometry, site, pointing, seeing, ...)`:

1. the mount is commanded to (RA, Dec); the scene's *true* pointing model
   (hour angle from the local sidereal time; azimuth/elevation for alt-az)
   says where it really points;
2. catalog stars around the true pointing, moved to the date;
3. projected (TAN) with the camera's plate scale, rotator position angle,
   parity, optical axis (`crpix`) and optional radial distortion `k1`;
4. fluxes from G through the port's zero point and extinction at the
   airmass; the sky from the Sun, Moon and dark sky (Krisciunas & Schaefer
   1991; a twilight ramp); seeing, defocus, Poisson and read noise, bias,
   gain, saturation, subframes and binning by the shared renderer
   (`robofast_core/testing/starfield.py`).

`frame.truth` carries the commanded and true pointing, the PA, and every
catalog star with its exact pixel. Commanded coordinates are taken as ICRS
(no precession, nutation, aberration or refraction: the mount software's
business).

Opt-in users (the default random star fields are unchanged):
`SkyFocusScene(sky, mount, clock)` is a `FocusScene` for `StarfieldCamera`
whose stars follow a mount's `RightAscension`/`Declination` (the simulated
night's imagers); `sky_world_stars(sky, ra, dec, when)` gives the guider
simulator's `SimWorld` its target and field stars.

## 4. Photometry (`robofast_core/photometry.py`)

`PortPhotometry` per telescope and port: `zp` is the catalog magnitude
giving 1 e-/s through that port, measured (`zero_point`, with its error and
date) or estimated from an efficiency chain (`efficiency`: named factors
such as `mirrors`, `qe`, `filter`, `beamsplitter`) times the collecting area
and the band's photon rate. The FAU sits behind a 2 % beamsplitter, so its
zero point is the imager-like optics' minus 4.25 mag; the fiber gets the
other 98 % (`with_fraction("beamsplitter", 0.98)`, for exposure-meter and
spectrum S/N estimates). Also: the CCD-equation `snr`, and
`sky_brightness`/`sky_conditions` for the sky.

## 5. Plate solving (`robofast_core/astrometry.py`)

**Near-blind** (`solve_near`, the default for acquisition, pointing models
and every frame): given the approximate pointing (`search_radius`, default
0.25 deg) and the plate scale (`scale_tolerance`, default 3 %):

1. detect stars (`detect_stars`: block-median background, 3x3 smoothing,
   8-connected pieces of at least 4 pixels, flux-weighted centroids);
2. take the catalog around the guess, to about the depth of the detections,
   onto the tangent plane in pixel units;
3. form triangles of each star with pairs of its nearest neighbours, in
   both lists; since the scale is known, the three side lengths themselves
   are the invariants (rotation and parity are free unless `pa`/`flip` are
   given);
4. each matched triangle gives a candidate similarity transform; keep the
   one most stars agree with;
5. match every detection through it, fit a TAN WCS (SIP with `sip_order`
   2-3 for a distorted field) by linear least squares, re-match tighter,
   refit;
6. accept only if at least `min_matches` stars (default 4) and a quarter
   of the stars that should match did (else `SolveError`: "a chance
   alignment").

Few stars: a pair is accepted only with `min_matches=2` and a known parity
(the brighter detection is the brighter catalog star); a single star only
with `min_matches=1` and a known PA (an offset); both are flagged
`degenerate`.

Accuracy and speed, measured by the tests (render with a pointing error of
1-3 arcmin, solve from the commanded position): MINERVA imager (0.61"/px,
2048x2048) centre within 0.1-0.3 arcsec, PA within 0.01 deg, scale within
0.05 %, ~100-200 stars matched, rms ~0.2-0.3 arcsec; FAU (0.265"/px,
1936x1216) dense to sparse fields within 0.1-0.3 arcsec with 3-60 stars.
About 1 s per 2048x2048 frame (detection dominates) on a loaded server.

**Blind** (`solve_field`): astrometry.net's `solve-field` if installed,
with its index files in a local directory (`index_dir`). For MINERVA's
fields: the imager (21 arcmin) needs the 4100/5200-series indexes with
quads of 2-30 arcmin (4107-4110 or 5204-5206), the FAU (8.5 x 5.4 arcmin)
5203-5205 (`http://data.astrometry.net/`). Not installed in CI; the test
skips.

`Solution`: the astropy WCS (1-based in FITS, as FITS requires), `ra`/`dec`
of the image centre, `pa` (+y east of north), `scale`, `flip`, `rms`,
`n_matched`, the matched stars, `method`, `seconds`, `degenerate`.

## 6. Solved frames and zero points (`robofast_core/framesolve.py`)

With `images.solve` in the enclosure config,

```json
"solve": {"catalog": {"cache_dir": "/var/lib/robofast/catalog"},
          "telescopes": {"t1": {"search_radius": 0.2}}}
```

the frame writer solves every light frame near its mount WCS (in the
writer thread, bounded by `timeout`). On success the approximate WCS is
replaced: `WCSSRC = 'robofast'`, `CTYPE` `RA---TAN` (or `-SIP`), the CD/PC
cards, `WCSRMS`, `WCSNSTAR`, `WCSPA`, `WCSSCALE`, `WCSFLIP`, `WCSMETH`. The
frame's photometric zero point from the matched stars (3-arcsec-radius
apertures, isolated and unsaturated stars) goes in as `ZPMAG` (G giving 1
e-/s, or 1 ADU/s without `EGAIN`), `ZPERR`, `ZPSCAT`, `ZPNSTAR`, `ZPUNIT`
(next to `AIRMASS`); the nightly index gets a `solve` entry (with the
mount's pointing offset), and the registry `{tel}.image.zp`,
`{tel}.image.wcs_rms`, `wcs_offset_arcsec`, `wcs_pa`. A failure keeps the
mount WCS (`WCSSRC = 'mount'`), adds `WCSFAIL`, and never costs the frame.

## 7. What is left

- Measured plate scales, PAs and parities for each MINERVA port; the
  zero points from real frames (section 4).
- A pointing-model builder on top of `pointing.fit` and the solver.
- astrometry.net index files on the control hosts, if a blind fallback is
  wanted.
- The Gaia cache filled for the target list on each host.
