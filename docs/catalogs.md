# Catalog importers: TRES / FLWO 60-inch telshell catalogs

`robofast_core/catalogs/` reads the target lists of the FLWO 60-inch's
legacy TCS software ("telshell", by T. Groner, FLWO; proper-motion handling
by J. Irwin, 2022) into a small intermediate form, turns them into robofast
request groups, and writes them back in the legacy format so the legacy
software can keep reading catalogs during the transition. The format rules
below are ported from telshell's catalog loader, its catalog validator and
the routines that turn a catalog line into the command sent to the
telescope (FLWO code, used with FLWO's encouragement).

The target database is not built yet. Its design waits for a look at a
live TRES catalog directory. Until then, the intermediate form is what an
imported catalog is (section 4).

```
python -m robofast_core.catalogs import prog.mct                   # intermediate form (JSON)
python -m robofast_core.catalogs import prog.mct --to-requests     # request groups for the 60-inch
python -m robofast_core.catalogs check prog.mct box.off            # validate, with warnings
python -m robofast_core.catalogs export prog.json -o prog.mct      # back to the legacy format
```

`--to-requests` takes `--telescope` (default `tillinghast`), `--instrument`
(default `tres`), `--exposure-time` (default 300 s, a placeholder: TRES
catalogs carry no exposure times) and `--exposure-count` (default 1).
Errors name the file and line (`prog.mct:12: RA hours '12.5' must be a whole
number ...`), and the exit status is then 2.

## 1. Object catalogs (`.mct`)

One plain-text file per program (or per observer or night). Lines are
separated by newlines. There are three kinds of line:

```
!P.I.: A. Nobody
!Program: Synthetic test
STAR-A 05:26:17.50 +28:36:28.3 2000.0
STAR-B 12 30 00 -00 30 00 2000 120.5 -33.25 anything after this is a comment

STAR-C 18:36:56.3 +38:47:01
```

**Headers** start with `!`. Two are allowed, spelled exactly as shown,
including the blank after the colon: `!P.I.: ` and `!Program: `. Their
values may contain only ASCII letters, digits, blanks and `# - _ . , /`.
A header anywhere in the file applies to the whole list, and a second one
of the same key replaces the first (with a warning). Telshell copies the
P.I. and Program into the FITS headers of every object chosen from the
catalog. `!Epoch:` is read by telshell's loader, but its validator refuses
the catalog, so it is refused here too. So is any other `!` key.

**Object lines**: `NAME RA DEC [EPOCH [PMRA PMDEC]] [comment]`.

| Field | Rule (telshell's reading) |
|---|---|
| NAME | from the first character to the first blank; ASCII letters, digits, `+ - _ .`; at most 29 characters (telshell's OBJECT buffer). The manual advises against `+`/`-`, which confuse IRAF |
| RA | three parts `H M S`, separated by blanks or colons (mixed is fine): whole hours, whole minutes, seconds with a fraction. 0 ≤ RA < 24h; `24:00:00` is read as 0h (warning) |
| DEC | three parts `D M S`, same separators. The sign is the first character of `D` (so `-00 30 00` is -0.5 deg). Whole degrees and minutes. Minutes or seconds of exactly 60 are accepted, as telshell's validator does (warning); \|Dec\| ≤ 90 |
| EPOCH | a number, 1800 < EPOCH ≤ 2200 (telshell needs > 1800). It is both the **equinox** of RA/Dec and the **epoch** of the position for proper motion. Absent: 2000.0 |
| PMRA PMDEC | mas/yr, both or neither; PMRA is mu_alpha cos(dec). Only with an EPOCH before them. Since 2022 telshell applies them (rigorous space motion, no parallax or RV) to the coordinates it sends to the TCS; the FITS headers keep the catalog position |
| comment | whatever follows, ignored by telshell, kept here |

**Blank lines** are allowed. They take a place in the list, so they count
for `#n`, the observer's "line n" selection. Headers do not count. robofast
keeps each target's legacy position (`index`) and writes the blanks back.

**Limits**: at most 128 characters per line, at most 79 after the name
(telshell copies the rest of the line into an 80-byte buffer), and at most
10 000 entries.

### What is refused, and why

Telshell checks a catalog with a separate validator before loading it, but
the loader reads lines differently from the validator, and much that
passes the validator is misread when it is used. robofast accepts a line
only if telshell accepts it **and** reads it as it was meant. Everything
else is an error with the file and line:

| Input | What telshell does |
|---|---|
| `12.5 30 00` (fractional hours, degrees or minutes) | validator passes; the TCS command uses the whole part only (C `atoi`) |
| a word, `J2000`, or a number ≤ 1800 where the epoch goes | ignored, and the **previous target's epoch** is used (2000.0 only right after start-up) |
| fewer than six RA/Dec parts | the previous target's RA or Dec is kept |
| one number after the epoch, or a number following a non-numeric word there (`2000 # 12`, `2000 V= 8.5`) | read as proper motion (the missing or non-numeric one as 0) |
| a tab anywhere | not a separator: the tokens run together (the validator refuses tabs only in names and headers) |
| a leading blank | the validator passes it; `#n` takes an empty name and misreads the coordinates, and `!name` never matches |
| a line of only blanks, a name with no coordinates | the validator reads past the end of its buffer |
| a line over 128 characters | split into two list entries |
| more than 79 characters after the name | overflows the buffer the line is copied into |
| `!Epoch:`, an unknown `!` key, `!Program:` without the blank | the validator refuses the catalog |
| a carriage return (DOS line end) on a header or blank line | the validator refuses it (on an object line it is harmless: tolerated, with a warning) |
| more than 10 000 entries | the rest is dropped |
| bytes that are not UTF-8 | (robofast's own rule) |

Warnings (tolerated, read correctly): a DOS line end on an object line;
Dec minutes or seconds of 60; RA 24h; a second header of the same key; and
a name that **cannot be selected by name**. Telshell's `!name` picks the
first line whose name *starts with* what was typed, so after `HD100` a later
`HD10` or a second `HD100` is reachable only by `#n`. The OBJECT header
then gets what was typed, not the full name.

### Names to files

When the observer types a catalog name, telshell opens: an absolute path as
is; a name containing a dot inside the catalog directory, unchanged;
otherwise the catalog directory plus the name plus `.mct` (`.off` for
offset catalogs). Leading blanks are dropped. `catalogs.resolve()` follows
these rules, but it refuses a name that leaves the directory (`..`), which
telshell does not. The catalog directory is a site setting, not part of
this code.

## 2. Offset catalogs (`.off`)

```
0 0
30 0 east
-30 15
0 -15
LINK
```

Each line holds two whole numbers of arcsec, the RA and Dec offsets from
the target. They are separated by one blank, tab or comma, then any blanks.
Text after them is a comment. A line starting `LINK` ends the list and
makes it cycle. Each "Next offset" in telshell applies the current offset
and advances.

Refused: a blank line (telshell repeats the previous offset), fractional
or otherwise malformed numbers (`10.5 3` reads as RA 10, Dec 5; `10-20` as
10, 20), `LINK` first or text after `LINK`, an empty file (telshell
crashes on both), lines over 78 characters, and more than 1025 offsets.

Things to know about the legacy behaviour (not changed by the import):

- Without `LINK`, telshell never applies the **last** offset of the list:
  it moves only when there is a next entry.
- The RA offset is converted to seconds of time by dividing by 15, so it
  is in arcsec of RA, not of sky, unless the TCS scales it by cos(Dec).
  This is unconfirmed.
- In the telshell source examined, offsets go out as legacy TCS commands
  that the current TCS may never receive. Whether offset catalogs still
  work at the 60-inch is unconfirmed.

## 3. Confidential catalogs (`.sec`)

Telshell sends any catalog whose path contains `.sec` (anywhere, so even
`x.second.mct`) to an external helper. The helper asks for a password and
writes the decrypted catalog, which telshell then loads without
validation. The helper is not part of telshell's source, so its encryption
is unknown.

robofast does **not** decrypt these and ships no decryption. `import x.sec`
describes the file from outside: its size, SHA-256, and the container type
if its leading bytes name a common one (OpenSSL, PGP, gzip, zip, ...;
otherwise `unknown`). The result is a `kind: "private"` list with no
targets, the private targets of program `x`. `--to-requests` refuses it.
The way in for such a program is for its P.I. to export the catalog as a
plain `.mct` and import it with `--private`. That marks the list private,
and `--to-requests` then refuses it unless `--include-private` is given,
because robofast does not yet limit who can see a request's coordinates.

## 4. The intermediate form and the target model

`robofast_core/catalogs/model.py` holds dataclasses with a JSON form
(`"format": "robofast-catalog/1"`). They map one to one onto the proposed
target model:

| Legacy | Intermediate (`kind`) | Proposed target model |
|---|---|---|
| `!Program:` / `!P.I.:` | `Program` (`name`, `pi`) | Program (name, PI); members, allocation later |
| a `.mct` file | `TargetList` (`targets`), named after the file; `source` is the path | TargetList owned by the Program; file and digest as provenance |
| an object line | `CatalogTarget`: `name`, `ra`/`dec` (deg, catalog equinox), `equinox`, `epoch_given`, `pm_ra`/`pm_dec` (mas/yr), `comment`, `index` (legacy `#n`), `line`, `text` (tokens as written) | Target (sidereal; ICRS via `icrs()`), comment as a free-form property; `index` as the list position |
| a `.off` file | `OffsetPattern` (`offsets`: `ra`, `dec` arcsec, `comment`; `repeat`) | an offset/dither pattern a request can name |
| a `.sec` file | `PrivateCatalog` (`kind: "private"`, sealed: size, sha256, container) | a private TargetList, visible to the program only |

**To ICRS.** RA/Dec at equinox 2000.0 are taken as ICRS. FK5 J2000 and
ICRS differ by about 20 mas, far below the TRES fiber. Another equinox is
precessed as FK5 with a Julian equinox (astropy). A catalog meant as FK4
B1950 differs from that by up to about 1 arcsec. How the TCS itself
interprets the epoch it receives is unconfirmed. The position stays at
epoch `equinox`, and the proper motion is carried along.

**To requests** (`to_requests`, `--to-requests`). Each target becomes one
request group:

- a one-shot `EXPOSE` on `instrument_type` `tres`, at `location.telescope`
  `tillinghast`, guided;
- the ICRS target with `proper_motion_ra`/`_dec` and `epoch`;
- `proposal` = the Program (or the list's name);
- `observation_note` = catalog, `#n`, line, P.I., Program and the comment;
- ids `<list>-<n>`.

The output is validated by `request_model` before it is written, and loads
in the 60-inch's dispatch plan (`sites/flwo60/`). The P.I. has no field of
its own in the request language, so it travels in the note.

**Back to the legacy format** (`write_mct`, `write_off`, `export`). The
P.I. and Program headers come first, then the targets at their `index`
positions, with blank lines filling any gaps so `#n` still selects the
same target. The kept tokens are written back where they still match the
values. Edited or new targets are formatted to 1 ms in RA and 10 mas in
Dec. A canonical catalog (headers first, single blanks) round-trips byte
for byte. Any accepted catalog round-trips to the same targets. The writer
re-reads each line it writes and refuses anything telshell would misread.

## 5. Deferred to the target database

- Storage, ids and history: no database here. Programs, lists and targets
  get stable ids when the database exists (`docs/design.md` 3.7). Until
  then the request ids are derived from the list name and position.
- Members, allocation and visibility: who may see a private list, and
  enforcement in the request API, console and logs.
- Exposure times, priorities, cadence and constraints: TRES catalogs have
  none, and the TRES nights are planned by people today. The importer
  emits one-shot requests with a placeholder exposure time.
- Offset/dither patterns in requests: the request language has no field
  for them yet, so `.off` patterns are imported but not attached to
  requests.
- `.sec` catalogs: what the encryption is, and whether to migrate them by a
  one-time decryption with each program's P.I.
- Confirmation against the live system: the catalog directory's real
  contents and variations, the TCS's reading of the epoch (FK4 or FK5 for
  1950), whether offset catalogs still move the telescope, and the
  helper's container format.
