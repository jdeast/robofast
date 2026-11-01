# Data on Zenodo

robofast's repository is BSD 3-Clause, so data under other licences -- the
Gaia DR3 extracts, under ESA's CC BY-NC 3.0 IGO -- are not in it. They live
in a Zenodo record and are fetched on first use (`docs/sky.md`, "What is
downloaded, from where"). Three pieces, adapted from EXOZIPPy's tooling (BSD
3-Clause, same author):

| Piece | What it does |
|---|---|
| `robofast_core/zenodo.py` | the download side: fetch, check size and md5, cache in the data directory; `python -m robofast_core.zenodo fetch` / `status` |
| `robofast_core/zenodo_assets.py` | the ONE table of records and pins (`RECORDS`), and the records the code expects (`EXPECTED`; `PENDING` = expected but not pinned) |
| `scripts/zenodo_publish.py` | the upload side: creates a **draft**, uploads, verifies, and **stops** |

Records:

| Registry key | Files | Built by | Metadata |
|---|---|---|---|
| `gaia_extracts` | `bright_stars.npy`, `test_fixture_tiles.npz`, `README.md`, `MANIFEST.txt` | `python scripts/build_catalog.py bundle --out DIR` | `scripts/zenodo/gaia_extracts.yaml` |

## One-time setup

Create a personal access token at
<https://zenodo.org/account/settings/applications/> with the scopes
`deposit:write` and `deposit:actions`, and save it alone on one line in
`~/.config/zenodo/token` with `chmod 600` (the script refuses a file that
is group or world readable). `--token-file` or `$ZENODO_TOKEN_FILE`
override the path. The token is never accepted on the command line, it
travels only in an Authorization header that is not forwarded across
redirects, and every message the script prints passes through a redactor.
`--sandbox` targets sandbox.zenodo.org with `~/.config/zenodo/sandbox_token`.

## The workflow

1. **Build the bundle**: a flat directory holding exactly the files of the
   record (Zenodo records have no subdirectories; dotfiles are ignored).

   ```sh
   python scripts/build_catalog.py bundle --out /scratch/gaia_extracts
   ```

   queries the Gaia archive (one query for the bright stars, one per test
   tile) and writes byte-for-byte reproducible files. `zenodo_publish.py`
   writes `MANIFEST.txt` (`name size md5` per line) into the bundle if it is
   absent, and refuses a stale one.

2. **Create the draft and upload**. Always `--dry-run` first: it prints the
   plan and makes no HTTP request at all (it does not even read the token).

   ```sh
   # a brand-new record
   python scripts/zenodo_publish.py new-record --bundle /scratch/gaia_extracts \
       --metadata scripts/zenodo/gaia_extracts.yaml --dry-run
   python scripts/zenodo_publish.py new-record --bundle /scratch/gaia_extracts \
       --metadata scripts/zenodo/gaia_extracts.yaml
   # a new version of a published record (replaces same-named files, keeps
   # the others; --prune makes the draft hold exactly the bundle)
   python scripts/zenodo_publish.py new-version --record <id> --bundle DIR
   ```

   `new-version --metadata changes.yaml` merges keys (e.g. `version`) into
   the carried-over metadata. Uploads stream from disk and retry on 5xx and
   connection errors; an interrupted run resumes with
   `upload --draft <id> --bundle DIR`, which skips files already on the
   draft with a matching md5. After uploading, the draft is read back and
   every file's size and md5 compared with the local copy; a mismatch fails
   loudly and says not to publish.

3. **Review and publish** the draft at the URL the script prints. Check
   the title, creators, description, licence, files (sizes and md5s against
   `MANIFEST.txt`), then click Publish: that mints a permanent DOI, so a
   human always does it. (`--publish` exists, but needs the exact title
   typed at a terminal.) Zenodo has no licence id for CC BY-NC 3.0 IGO, so
   the metadata says "Other (Non-Commercial)" and the description names the
   licence; on the draft's page it can be replaced by a custom licence
   entry. robofast has no Zenodo community; if one is made, add its slug
   to the metadata's `communities` and run
   `zenodo_publish.py community --record <id> --community <slug>` after
   publishing (Zenodo ignores `communities` on publish).

4. **Pin**: every pin lives in `robofast_core/zenodo_assets.py`.

   ```sh
   python scripts/zenodo_publish.py pin --record <id> --name gaia_extracts --update-registry
   ```

   reads the PUBLISHED record's public API (no token) and writes its entry
   into `RECORDS` (a new version of a pinned dataset is matched by its
   concept record and rewritten in place; `--name` is only needed the first
   time and must be one of `EXPECTED`). The new text is read back and
   checked against the record before the file is replaced. Without
   `--update-registry` it only prints the entry. `--bundle DIR` also checks
   the record against the local files.

5. **Test and open a PR**:

   ```sh
   ROBOFAST_NETWORK_TESTS=1 pytest tests/core/test_zenodo_assets.py
   pytest tests/core
   ```

   The first re-reads the record from Zenodo and compares it with the
   table; the second now runs the catalog tests on the real Gaia sky (the
   two real-data-only tests stop skipping). CI caches the data directory
   keyed on `zenodo_assets.py`, so it downloads each pinned file once per
   pin change. A new version gets a new record id; files already cached
   at another size are fetched again.

Tests: `tests/core/test_zenodo_publish.py` drives every subcommand against a
fake in-process Zenodo, and `tests/core/test_zenodo.py` the downloader
against a loopback server; nothing in the suite talks to zenodo.org
(`test_zenodo_assets.py`'s live check only with `ROBOFAST_NETWORK_TESTS=1`).
