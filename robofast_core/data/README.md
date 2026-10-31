# Bundled data

## `bright_stars.npy`: the bright-star catalog

Every star in **Gaia DR3** (`gaiadr3.gaia_source`) with `phot_g_mean_mag < 6.5`
(12 119 stars), at epoch J2016.0, brightest first: `source_id`, `ra`, `dec`
(deg, ICRS), `pmra` (with cos dec), `pmdec` (mas/yr), `g`, `bp_rp`. Built by
`python scripts/build_catalog.py bright` (docs/sky.md). Read it with
`robofast_core.catalog.bright_stars()`.

Gaia lacks some of the very brightest stars (roughly G < 3, e.g. Vega,
Sirius); `scripts/build_catalog.py hipparcos` fetches the Hipparcos stars
without a Gaia counterpart into the local cache (not bundled: see below).

**Licence.** Gaia data are distributed by ESA under the
[CC BY-NC 3.0 IGO](https://creativecommons.org/licenses/by-nc/3.0/igo/)
licence (<https://www.cosmos.esa.int/web/gaia-users/license>), which allows
redistribution for non-commercial use with attribution. This file, and the
Gaia fixture tiles under `tests/core/data/catalog/`, are therefore under
CC BY-NC 3.0 IGO, **not** the code's BSD 3-Clause licence (`LICENSE`). Attribution:

> This work has made use of data from the European Space Agency (ESA)
> mission Gaia (<https://www.cosmos.esa.int/gaia>), processed by the Gaia Data
> Processing and Analysis Consortium (DPAC,
> <https://www.cosmos.esa.int/web/gaia/dpac/consortium>). Funding for the DPAC
> has been provided by national institutions, in particular the institutions
> participating in the Gaia Multilateral Agreement. Gaia Collaboration,
> Vallenari et al. 2023, A&A 674, A1.

A commercial user should delete this file (robofast then works from the
local cache it builds, `docs/sky.md`) or check ESA's terms for commercial use.

The Hipparcos catalogue's redistribution terms are not stated as clearly,
so it is fetched and cached locally, never bundled. The Washington Double
Star catalog (US Naval Observatory; a US Government work) is also fetched
and cached (`scripts/build_catalog.py wds`); a few of its lines are in the
test fixtures.
