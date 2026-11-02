# MINERVA's target lists: the Google Sheet

MINERVA's RV targets and its B stars live in a Google Sheet, the "MINERVA
target list": tab `targets` (the RV stars) and tab `B stars` (rapidly
rotating B stars observed through the iodine cell, docs/kiwispec.md
section 4). A red-side list is in a sheet of its own. Legacy
`minerva-control` (`minerva_library/targetlist.py`) exported each tab to
CSV with a Google service account (`downloadList`) and read the CSV
(`rdlist`, `mkdict`) for its scheduler (`scheduler.py`) and its nightly
schedule (`rv_control.mkschedule`).

`robofast_core/catalogs/minerva_sheet.py` reads the same tabs with the
same service account and a **read-only** scope, keeps a timestamped
snapshot of every fetch, and turns each active row into one request group
in the request language (`docs/request_language.md`), ready for the
dispatch scheduler. It follows the TRES importer's stance
(`docs/catalogs.md`): what legacy reads is read the way legacy meant it; a
row that legacy would misread, or that robofast cannot honour, is
**refused** with its sheet row number and the reason; nothing is guessed.

```
python -m robofast_core.catalogs.minerva_sheet fetch  --config target_sheet.json   # live -> snapshot
python -m robofast_core.catalogs.minerva_sheet check  --config target_sheet.json   # validate, list refusals
python -m robofast_core.catalogs.minerva_sheet export --config target_sheet.json -o minerva-requests.json
python -m robofast_core.catalogs.minerva_sheet check  --csv targets=targets.csv --csv bstars=bstars.csv
python -m robofast_core.catalogs.minerva_sheet export --snapshot /var/lib/robofast/data/minerva_sheet/20261101T120000Z
```

`check` and `export` take one source: `--config` (fetch live; if the Sheet
cannot be read, fall back to the newest snapshot; `--offline` skips the
fetch), `--snapshot DIR` (that snapshot), or `--csv LIST=PATH` (a CSV
export of a tab, "File > Download > CSV" in the Sheet, or legacy's
`targets.csv` / `bstar.csv`). `check` exits 2 if any row is refused.
`export` writes the accepted rows (`{"groups": [...]}`, validated by the
request model) and lists the refused ones on stderr; `--strict` makes any
refusal an error. `fetch` only fetches; it never falls back. Errors exit 2.

## Configuration

`sites/minerva/target_sheet.example.json`, copied to the control host and
filled in there:

| Key | Meaning |
|---|---|
| `key_file` | the service-account key (JSON), e.g. `/etc/robofast/secrets/minerva_sheets.json`, mode 600 (a warning otherwise). `$ROBOFAST_MINERVA_SHEETS_KEY` overrides it |
| `lists.<name>.sheet` | the spreadsheet key (the id in the Sheet's URL). **Site config only**, never in a repository; the example has placeholders, and `fetch` refuses them |
| `lists.<name>.tab` | the worksheet: `targets`, `B stars` |
| `lists.<name>.kind` | `targets` (RV stars) or `bstars` |
| `lists.<name>.proposal`, `id_prefix`, `instrument` | the requests' `proposal`, id prefix (`RV-`, `BSTAR-`) and `instrument_type` (default `spectrograph`) |
| `lists.<name>.pool`, `per_night` | B stars: the cadence pool and how many of it a night (default the list's name, 1) |
| `lists.<name>.enabled` | `false` skips the list (the red-side list, until it is confirmed wanted) |
| `snapshot_dir`, `keep` | where snapshots go (default `$ROBOFAST_DATA_DIR/minerva_sheet`, see `robofast_core.zenodo.data_dir`) and how many are kept (30) |

The live fetch needs `gspread` and `google-auth`: `pip install --group
sheets`. They are optional imports; nothing else in robofast needs them.
Cells are read unformatted (the stored numbers, not the Sheet's display
rounding of them), so coordinates keep their full precision.

## Snapshots and the fallback

Every live fetch writes `<snapshot_dir>/<UTC time>/` (for example
`20261101T120000Z/`) holding `targets.csv`, `bstars.csv` (one CSV per list,
as fetched) and `meta.json` (fetch time; each list's tab, kind, row count
and SHA-256). It is written to a temporary directory and renamed into
place, so a snapshot is never half written; a CSV that no longer matches
its digest is refused. The newest `keep` are kept. Snapshots hold the
collaboration's targets: the directory is created mode 700.

When the Sheet cannot be read (network, Google, a missing or revoked key,
gspread not installed), the lists come from the newest snapshot, with a
warning naming the reason and the snapshot's age. Either way one line
`MINERVA target lists from <snapshot path>` is logged, so every load
names exactly where its targets came from. With neither a Sheet nor a
snapshot it is an error. `ROBOFAST_OFFLINE=1` skips the fetch.

## The columns

Headers are matched as legacy matched them: the text before any `(`,
stripped (`ra (J2000 hrs)` is `ra`; `seplimit` and `seplimit (sec)` are
the same). A tab without one of the columns legacy's `mkdict` reads is
refused as a whole (legacy stopped with a `KeyError`), as is a column read
here that appears twice. A column not listed below is ignored with a
warning.

Only rows with `active` 1 are converted; the others are counted as
inactive and not checked (legacy skipped them). Blank rows are skipped.

| Column (tab) | Legacy reading | robofast |
|---|---|---|
| `name` | the target's name | the target's `name`; the group id is `<id_prefix>-<name>` (characters outside `A-Za-z0-9._+-` become `_`), stable across fetches so the dispatch state's visit history carries over. Blank, or a name or id already on an earlier active row: **refused** (legacy's `mkdict(name=)` took the first) |
| `ra (J2000 hrs)` | RA in hours, J2000, at epoch 2000.0 (`cdk700.starmotion`) | `target.ra` = 15 x ra, degrees. Required, 0 <= ra < 24 |
| `radeg (J2000 deg)` | not read | if given, must agree with 15 x `ra` within 1 arcsec, or the row is **refused** (the sheet disagrees with itself; legacy used `ra`) |
| `dec (J2000 deg)` | Dec in degrees, J2000 | `target.dec`. Required, within +-90 |
| `pmra (mas/yr)`, `pmdec (mas/yr)` | mu_alpha cos(dec) and mu_delta, applied from J2000.0 to now (`starmotion`) | `proper_motion_ra`/`_dec` with `epoch` 2000.0. Blank: **refused** (legacy read NaN and pointed at NaN); write 0 when negligible |
| `parallax (mas)` | FITS header only (`starmotion` looks for a `px` key that `mkdict` never sets, so parallax was never applied) | `target.parallax` (barycentric correction); blank: absent |
| `rv (km/s)` | FITS header; in `starmotion` only together with the (missing) parallax | `target.extra_params.radial_velocity`; blank: absent |
| `vmag (Johnson V)` | FITS header | in the request's `observation_note` |
| `priority` | multiplies the scheduler's weight (1-10 in practice) | the group's `ipp_value`. Must be > 0: a priority of 0 or less on an active row is **refused** (set `active` 0 instead) |
| `exptime (sec)` | the spectrum's exposure time, one exposure | `exposure_time`, `exposure_count` 1. Must be > 0 |
| `maxobs` (`targets`) | visits a night; blank or unreadable: 2. (The scheduler's test `observed > maxobs` let one more through) | `extra_params.cadence.max_per_night`, as written; blank: 2. Not a whole number >= 1: **refused** (legacy silently used 2) |
| `seplimit (sec)` | minimum time between visits; blank: 3 h | `cadence.min_interval` (start to start); blank: 10800 s |
| `fauexptime (sec)` | the acquisition camera's exposure time | `acquisition_config.exposure_time` (accepted by the request model; the guider sets its own exposure today). Blank: absent |
| `acquisition_offset_north`, `_east (arcsec)` | offset guiding: the target is this far from the star the guider locks on (applied only when **both** are non-zero) | robofast has no acquisition offset yet, so a non-zero offset is **refused**: it would guide on the wrong star. Blank or 0: none |
| `iodine` (`targets`; the red list) | **not read**: `mkdict` sets `i2` true for every target | 1 or blank: iodine in (as legacy). 0: **refused**, since legacy observed such a row through the cell anyway and robofast will not guess which was meant (set 1, or ask for a template, a `SPECTRUM` with iodine false, separately) |
| `active` | 1: observe; anything else: skip | 1: convert; 0 or blank: inactive; anything else (`yes`, `TRUE`, 2): **refused** |
| `comment` | FITS header | in `observation_note` |
| `maxobs` (`B stars`) | as for targets, but the scheduler's "one B star a night" flag dominated | not used: the B stars share a pool (below) |
| `distance`, `Teff`, `sptype`, `bad_priority`, `vsini`, `pmodeperiod`, `Amp P-mode`, `dtactive`, `tc`, `period`, `t14`, `Howard (2010)`, `finder`, `mstar`, `rstar`, `rpearth`, `mpearth`, `k` | not read | ignored (kept in the snapshot) |
| red list: `Common Name`, `Sloan I band`, `MEarth Image`, `MEarth Mags`, `Companion`, `Type and Seperation`, `Planets`, `Mass`, `Period`, `Paper` | not read | ignored |

A row that passes these checks is also validated by the request model
(`RequestGroup.from_dict`): whatever that refuses refuses the row, with
its reason.

## The requests

Every accepted row becomes one group with one standing request
(`extra_params.cadence`) and one `SPECTRUM` configuration: guided, iodine
in (`extra_params.iodine: true`), fed by every free telescope with the
`spectrograph` (no `location.telescope`, as legacy's `doSpectra` used the
telescopes it had), no constraints of its own beyond the telescope's and
site's limits (legacy had none).

- **RV targets** (`kind: targets`): cadence `min_interval` = `seplimit`,
  `max_per_night` = `maxobs`.
- **B stars** (`kind: bstars`): cadence `min_interval` = `seplimit`,
  `max_per_night` 1 and `pool: "bstars"`. A pool shares the nightly count
  among its requests (`docs/request_language.md`), so at most one B star,
  whichever, is taken a night. Legacy's rule was softer: after the night's
  B star it divided the other B stars' weights by 10^6 ("unless we have
  nothing better to do"), and before it raised them as the night ran out
  (`mkschedule` put one at the start of the night). robofast does neither:
  a B star competes on its `ipp_value` like any request, and a second one
  is never taken. Raise `per_night` for more.

The output loads in the dispatch plan as a `requests` file
(`sites/minerva/dispatch.example.json`); run `export` from a timer and
point the plan at its output. The request API reloads a request file when
it changes.

## Not done

- The red-side list: the reader handles it (`kind: targets`, its own
  sheet), but it is disabled in the example until it is confirmed wanted,
  and robofast has no instrument for it. Its `iodine` column is probably
  the red spectrograph's own setting, not KiwiSpec's cell: rows with
  `iodine` 0 are refused until that is settled.
- Acquisition offsets (offset guiding), see above.
- Legacy's B-star weighting through the night, and its weighting of three
  visits a night by hour angle (`calc_weight_multi`): the dispatch score
  has its own terms (design.md 3.7).
- Writing to the Sheet: never; the scope is read only.
