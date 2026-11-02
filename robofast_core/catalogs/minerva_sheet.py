"""MINERVA's target lists: the RV targets and the B stars, from their Google
Sheet (or a CSV export of each tab), as request groups.

Legacy MINERVA (``minerva-control``, ``minerva_library/targetlist.py``)
keeps its target lists in a Google Sheet: the RV targets on tab
``targets``, the B stars observed through the iodine cell on tab
``B stars``, and, in a sheet of its own, a red-side list. Its
``downloadList`` exported each tab to CSV with a service account, and
``rdlist``/``mkdict`` read the CSV. This module reads the same tabs, with
the same service account and a read-only scope, and turns each row into
one request group (docs/minerva_sheet.md has every column and its
mapping):

* an RV target: a standing ``SPECTRUM`` (iodine in) with
  ``extra_params.cadence`` from ``seplimit`` and ``maxobs``;
* a B star: a standing ``SPECTRUM`` with ``extra_params.iodine: true`` and
  a cadence pool, so at most one B star, whichever, is taken a night
  (legacy's "1 Bstar per night").

Same stance as the TRES importer (:mod:`.tres`): what legacy reads is read
the way legacy meant it; a row legacy would misread, or that robofast
cannot honour, is refused with its sheet row number and the reason; nothing
is guessed. Inactive rows are skipped, not checked.

Every live fetch is written as a timestamped snapshot (a CSV per tab and a
``meta.json``) under :func:`robofast_core.zenodo.data_dir`
``/minerva_sheet``. When the Sheet cannot be reached, :func:`load` falls
back to the newest snapshot, with a warning that gives its age; either way
it logs the one snapshot path the lists came from. Command line::

    python -m robofast_core.catalogs.minerva_sheet fetch --config sheet.json
    python -m robofast_core.catalogs.minerva_sheet check --config sheet.json
    python -m robofast_core.catalogs.minerva_sheet export --config sheet.json -o rv.json
    python -m robofast_core.catalogs.minerva_sheet check --csv targets=targets.csv

gspread and google-auth are optional imports (``pip install --group
sheets``), needed only for a live fetch. The key file and the sheet ids
belong in the site config, never in a repository.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robofast_core.catalogs.model import _ident
from robofast_core.request_model import RequestGroup, load_request_groups

log = logging.getLogger(__name__)

__all__ = [
    "KEY_ENV",
    "SCOPES",
    "ListSpec",
    "Loaded",
    "SheetConfig",
    "SheetError",
    "SheetList",
    "SheetUnavailable",
    "convert",
    "fetch",
    "load",
    "newest_snapshot",
    "read_snapshot",
    "write_snapshot",
]

#: Environment variable that overrides the config's ``key_file``.
KEY_ENV = "ROBOFAST_MINERVA_SHEETS_KEY"
#: Read only: robofast never writes to the Sheet.
SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
SNAPSHOT_FORMAT = "robofast-minerva-sheet/1"
#: Snapshots kept (the newest); older ones are deleted after a fetch.
DEFAULT_KEEP = 30
_STAMP = re.compile(r"\d{8}T\d{6}Z(-\d+)?")

KINDS = ("targets", "bstars")

#: Legacy's defaults when ``seplimit`` / ``maxobs`` is blank (``mkdict``).
DEFAULT_SEPLIMIT = 3 * 3600.0
DEFAULT_MAXOBS = 2
#: ``radeg`` must agree with ``ra`` x 15 to this (arcsec).
RADEG_TOLERANCE = 1.0

#: Columns legacy's ``mkdict`` reads for every row: a tab without one of
#: them is refused (legacy would stop with a KeyError).
REQUIRED = (
    "name",
    "ra",
    "dec",
    "pmra",
    "pmdec",
    "parallax",
    "rv",
    "vmag",
    "priority",
    "exptime",
    "fauexptime",
    "acquisition_offset_north",
    "acquisition_offset_east",
    "active",
    "comment",
)
#: Read when present (legacy: ``maxobs``/``seplimit`` with a default;
#: ``radeg`` and ``iodine`` are checked here, legacy ignored them).
OPTIONAL = ("radeg", "maxobs", "seplimit", "iodine")
#: Columns in the live tabs that legacy never read: kept in the snapshot,
#: ignored here (docs/minerva_sheet.md). Any other column gets a warning.
INFORMATIONAL = frozenset(
    {
        "distance",
        "Teff",
        "sptype",
        "bad_priority",
        "vsini",
        "pmodeperiod",
        "Amp P-mode",
        "dtactive",
        "tc",
        "period",
        "t14",
        "Howard",
        "finder",
        "mstar",
        "rstar",
        "rpearth",
        "mpearth",
        "k",
        # the red-side sheet
        "Common Name",
        "Sloan I band",
        "MEarth Image",
        "MEarth Mags",
        "Companion",
        "Type and Seperation",
        "Planets",
        "Mass",
        "Period",
        "Paper",
    }
)


class SheetError(ValueError):
    """A tab or config that cannot be read at all (a missing column, a
    duplicated header, a bad config entry)."""


class SheetUnavailable(RuntimeError):
    """No live Sheet and no snapshot to fall back to."""


# ------------------------------------------------------------------ config


def _placeholder(value: str) -> bool:
    return not value.strip() or (value.startswith("<") and value.endswith(">"))


@dataclass
class ListSpec:
    """One target list: which sheet and tab, and how its rows become
    requests. ``sheet`` is the spreadsheet key (site config only)."""

    name: str
    kind: str
    tab: str
    sheet: str = ""
    proposal: str = ""
    id_prefix: str = ""
    instrument: str = "spectrograph"
    #: B stars: the cadence pool that shares ``max_per_night``.
    pool: str = ""
    #: B stars: how many of the pool a night.
    per_night: int = 1
    enabled: bool = True

    KEYS = frozenset(
        {"kind", "tab", "sheet", "proposal", "id_prefix", "instrument", "pool", "per_night"}
        | {"enabled"}
    )

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise SheetError(f"list {self.name!r}: kind {self.kind!r}; expected one of {KINDS}")
        self.proposal = self.proposal or f"minerva-{self.name}"
        self.id_prefix = self.id_prefix or self.name
        self.pool = self.pool or self.name
        if (
            isinstance(self.per_night, bool)
            or not isinstance(self.per_night, int)
            or self.per_night < 1
        ):
            raise SheetError(f"list {self.name!r}: per_night must be a positive integer")

    @classmethod
    def from_dict(cls, name: str, d: Any) -> ListSpec:
        if not isinstance(d, dict):
            raise SheetError(f"list {name!r}: expected an object")
        unknown = set(d) - cls.KEYS - {"_note"}
        if unknown:
            raise SheetError(f"list {name!r}: unknown keys {sorted(unknown)}")
        if "kind" not in d or "tab" not in d:
            raise SheetError(f"list {name!r}: 'kind' and 'tab' are required")
        return cls(name=name, **{k: v for k, v in d.items() if k != "_note"})


#: The lists as legacy has them, without the sheet keys (those come from
#: the site config). Used for ``--csv`` without a config.
DEFAULT_LISTS = {
    "targets": ListSpec("targets", "targets", "targets", proposal="minerva-rv", id_prefix="RV"),
    "bstars": ListSpec(
        "bstars", "bstars", "B stars", proposal="iodine-calibration", id_prefix="BSTAR"
    ),
}


@dataclass
class SheetConfig:
    """The site's sheet config (``sites/minerva/target_sheet.example.json``)."""

    lists: dict[str, ListSpec]
    key_file: Path | None = None
    snapshot_dir: Path | None = None
    keep: int = DEFAULT_KEEP

    KEYS = frozenset({"key_file", "snapshot_dir", "keep", "lists"})

    @classmethod
    def from_dict(cls, d: Any, base: Path | None = None) -> SheetConfig:
        if not isinstance(d, dict):
            raise SheetError("sheet config: expected an object")
        unknown = set(d) - cls.KEYS - {"_note"}
        if unknown:
            raise SheetError(f"sheet config: unknown keys {sorted(unknown)}")
        lists = d.get("lists")
        if not isinstance(lists, dict) or not lists:
            raise SheetError("sheet config: 'lists' must name at least one list")

        def path(v: Any) -> Path | None:
            if v in (None, ""):
                return None
            p = Path(str(v)).expanduser()
            return base / p if base is not None and not p.is_absolute() else p

        keep = d.get("keep", DEFAULT_KEEP)
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise SheetError("sheet config: 'keep' must be a positive integer")
        return cls(
            lists={k: ListSpec.from_dict(k, v) for k, v in lists.items()},
            key_file=path(d.get("key_file")),
            snapshot_dir=path(d.get("snapshot_dir")),
            keep=keep,
        )

    @classmethod
    def load(cls, path: str | Path) -> SheetConfig:
        p = Path(path)
        return cls.from_dict(json.loads(p.read_text()), base=p.parent)

    def enabled(self) -> dict[str, ListSpec]:
        return {k: v for k, v in self.lists.items() if v.enabled}

    def key_path(self) -> Path | None:
        """The service-account key: ``$ROBOFAST_MINERVA_SHEETS_KEY``, else the config's."""
        env = os.environ.get(KEY_ENV, "").strip()
        return Path(env).expanduser() if env else self.key_file

    def snapshots(self) -> Path:
        if self.snapshot_dir is not None:
            return self.snapshot_dir
        from robofast_core.zenodo import data_dir

        return data_dir() / "minerva_sheet"


# ------------------------------------------------------------------ parsing


@dataclass
class SheetList:
    """One tab, converted: the request groups of its active rows, and what
    was refused (``"<list> row <n>: <reason>"``), skipped or warned about."""

    name: str
    kind: str
    groups: list[dict[str, Any]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    inactive: int = 0
    blank: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.name}: {len(self.groups)} requests, {self.inactive} inactive, "
            f"{len(self.refused)} refused"
        )


class _Refused(ValueError):
    pass


def _header(h: str) -> str:
    """Legacy's column name: the text before any '(' (its unit), stripped."""
    return h.split("(")[0].strip()


def _number(text: str, col: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    try:
        x = float(text)
    except ValueError:
        raise _Refused(f"{col} {text!r} is not a number") from None
    if not math.isfinite(x):
        raise _Refused(f"{col} {text!r} is not a finite number")
    if not lo <= x <= hi:
        raise _Refused(f"{col} {text!r} is outside [{lo:g}, {hi:g}]")
    return x


def _whole(text: str, col: str, lo: int = 1) -> int:
    x = _number(text, col)
    if x != int(x) or x < lo:
        raise _Refused(f"{col} {text!r} must be a whole number >= {lo}")
    return int(x)


def _flag(text: str, col: str) -> bool | None:
    """1 -> True, 0 -> False, blank -> None; anything else is refused."""
    if text == "":
        return None
    try:
        x = float(text)
    except ValueError:
        x = math.nan
    if x == 1.0:
        return True
    if x == 0.0:
        return False
    raise _Refused(f"{col} {text!r}: expected 1 or 0")


def convert(rows: list[list[str]], spec: ListSpec, source: str = "") -> SheetList:
    """One tab's rows (the header first, as the Sheet or a CSV export has
    them) -> request groups, refusals and warnings (module doc).

    Raises :class:`SheetError` when the tab as a whole cannot be read.
    """
    where = source or spec.name
    out = SheetList(spec.name, spec.kind)
    if not rows:
        raise SheetError(f"{where}: empty (no header row)")
    cols: dict[str, int] = {}
    unknown: list[str] = []
    for i, raw in enumerate(rows[0]):
        h = _header(str(raw))
        if not h:
            continue
        if h in cols:
            if h in REQUIRED or h in OPTIONAL:
                raise SheetError(
                    f"{where}: column {h!r} appears twice (columns {cols[h] + 1}, {i + 1})"
                )
            continue
        cols[h] = i
        if h not in REQUIRED and h not in OPTIONAL and h not in INFORMATIONAL:
            unknown.append(h)
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        raise SheetError(f"{where}: missing columns {missing} (legacy's mkdict reads them)")
    if unknown:
        out.warnings.append(f"{where}: columns not read by legacy or robofast, ignored: {unknown}")

    seen_names: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for n, row in enumerate(rows[1:], start=2):
        cells = [str(c).strip() for c in row]

        def cell(col: str, cells: list[str] = cells) -> str:
            i = cols.get(col)
            return cells[i] if i is not None and i < len(cells) else ""

        if not any(cells):
            out.blank += 1
            continue
        at = f"{spec.name} row {n}"
        try:
            active = _flag(cell("active"), "active")
            if not active:
                out.inactive += 1
                continue
            group = _row(cell, spec, n)
        except _Refused as exc:
            out.refused.append(f"{at}: {exc}")
            continue
        name, gid = group["name"], group["id"]
        if name in seen_names:
            out.refused.append(f"{at}: name {name!r} already on row {seen_names[name]}")
            continue
        if gid in seen_ids:
            out.refused.append(f"{at}: id {gid!r} (from the name) already on row {seen_ids[gid]}")
            continue
        try:
            RequestGroup.from_dict(group, at)
        except ValueError as exc:
            out.refused.append(f"{at}: {exc}")
            continue
        seen_names[name], seen_ids[gid] = n, n
        out.groups.append(group)
    return out


def _row(cell: Callable[[str], str], spec: ListSpec, n: int) -> dict[str, Any]:
    """One active row -> a request group (raises :class:`_Refused`)."""
    name = cell("name")
    if not name:
        raise _Refused("no name")
    ra_text, dec_text = cell("ra"), cell("dec")
    if not ra_text or not dec_text:
        raise _Refused("ra and dec are required")
    ra_h = _number(ra_text, "ra (J2000 hours)", 0.0, 24.0)
    if ra_h == 24.0:
        raise _Refused("ra (J2000 hours) 24: must be < 24")
    dec = _number(dec_text, "dec (J2000 deg)", -90.0, 90.0)
    ra = ra_h * 15.0
    radeg = cell("radeg")
    if radeg:
        rd = _number(radeg, "radeg")
        off = abs((rd - ra + 180.0) % 360.0 - 180.0) * 3600.0
        if off > RADEG_TOLERANCE:
            raise _Refused(
                f"radeg {radeg} disagrees with ra {ra_text} h x 15 by {off:.1f} arcsec; "
                "legacy used ra: fix whichever is wrong"
            )
    pm = []
    for col in ("pmra", "pmdec"):
        text = cell(col)
        if not text:
            raise _Refused(
                f"{col} is blank (legacy read NaN and could not point); write 0 if it is negligible"
            )
        pm.append(_number(text, col, -1e5, 1e5))
    target: dict[str, Any] = {
        "type": "ICRS",
        "name": name,
        "ra": ra % 360.0,
        "dec": dec,
        "proper_motion_ra": pm[0],
        "proper_motion_dec": pm[1],
        "epoch": 2000.0,
    }
    if cell("parallax"):
        target["parallax"] = _number(cell("parallax"), "parallax (mas)", 0.0, 1e4)
    if cell("rv"):
        target["extra_params"] = {"radial_velocity": _number(cell("rv"), "rv (km/s)", -3e5, 3e5)}
    vmag = _number(cell("vmag"), "vmag") if cell("vmag") else None
    if not cell("priority"):
        raise _Refused("priority is blank")
    priority = _number(cell("priority"), "priority")
    if priority <= 0:
        raise _Refused(f"priority {cell('priority')}: must be > 0 (set active 0 to drop a row)")
    if not cell("exptime"):
        raise _Refused("exptime is blank")
    exptime = _number(cell("exptime"), "exptime (s)")
    if exptime <= 0:
        raise _Refused(f"exptime (s) {cell('exptime')}: must be > 0")
    fau = None
    if cell("fauexptime"):
        fau = _number(cell("fauexptime"), "fauexptime (s)")
        if fau <= 0:
            raise _Refused(f"fauexptime (s) {cell('fauexptime')}: must be > 0 or blank")
    for col in ("acquisition_offset_north", "acquisition_offset_east"):
        if cell(col) and _number(cell(col), col) != 0.0:
            raise _Refused(
                f"{col} {cell(col)} arcsec: robofast cannot offset the acquisition yet, "
                "so it would guide on the wrong star"
            )
    iodine = _flag(cell("iodine"), "iodine")
    if iodine is False:
        raise _Refused(
            "iodine 0: legacy ignored this column and always observed through the cell; "
            "robofast will not guess which was meant (set 1, or request a template "
            "separately)"
        )
    seplimit = (
        _number(cell("seplimit"), "seplimit (s)", 0.0) if cell("seplimit") else DEFAULT_SEPLIMIT
    )
    if spec.kind == "bstars":
        cadence: dict[str, Any] = {
            "min_interval": seplimit,
            "max_per_night": spec.per_night,
            "pool": spec.pool,
        }
    else:
        maxobs = _whole(cell("maxobs"), "maxobs") if cell("maxobs") else DEFAULT_MAXOBS
        cadence = {"min_interval": seplimit, "max_per_night": maxobs}
    gid = f"{_ident(spec.id_prefix)}-{_ident(name)}"
    note = [f"MINERVA sheet {spec.name}, row {n}"]
    if vmag is not None:
        note.append(f"V = {vmag:g}")
    if cell("comment"):
        note.append(cell("comment"))
    config: dict[str, Any] = {
        "id": f"{gid}/c",
        "type": "SPECTRUM",
        "instrument_type": spec.instrument,
        "target": target,
        "guiding_config": {"mode": "ON"},
        "instrument_configs": [
            {"exposure_time": exptime, "exposure_count": 1, "extra_params": {"iodine": True}}
        ],
    }
    if fau is not None:
        config["acquisition_config"] = {"exposure_time": fau}
    return {
        "id": gid,
        "name": name,
        "proposal": spec.proposal,
        "ipp_value": priority,
        "operator": "SINGLE",
        "observation_type": "NORMAL",
        "requests": [
            {
                "id": f"{gid}/r",
                "observation_note": "; ".join(note),
                "extra_params": {"cadence": cadence},
                "configurations": [config],
            }
        ],
    }


# ------------------------------------------------------------------ fetching


def _cell_text(v: Any) -> str:
    """A cell (unformatted value) as text: full precision, no display rounding."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    return str(v)


def _client(key_file: Path) -> Any:
    """A gspread client authorised with the service-account key, read only."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise SheetUnavailable(
            f"a live fetch needs gspread and google-auth ({exc.name} is missing): "
            "pip install --group sheets"
        ) from None
    if not key_file.is_file():
        raise SheetUnavailable(f"service-account key {key_file} not found")
    if os.name == "posix" and key_file.stat().st_mode & 0o077:
        log.warning("service-account key %s is readable by others; chmod 600 it", key_file)
    creds = Credentials.from_service_account_file(str(key_file), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<sheet>")
    return text


def fetch(config: SheetConfig, *, client: Any = None) -> dict[str, list[list[str]]]:
    """Every enabled list's tab from the live Sheet: ``{list: rows}``.

    ``client`` is a gspread client (tests pass a fake); by default one is
    made from the key file. Errors never carry the sheet keys.
    """
    lists = config.enabled()
    for spec in lists.values():
        if _placeholder(spec.sheet):
            raise SheetError(
                f"list {spec.name!r}: 'sheet' is a placeholder; put the spreadsheet key in "
                "the site config"
            )
    secrets = [s.sheet for s in lists.values()]
    try:
        if client is None:
            key = config.key_path()
            if key is None:
                raise SheetUnavailable(f"no service-account key: set key_file or ${KEY_ENV}")
            client = _client(key)
        books: dict[str, Any] = {}
        tables = {}
        for name, spec in lists.items():
            if spec.sheet not in books:
                books[spec.sheet] = client.open_by_key(spec.sheet)
            ws = books[spec.sheet].worksheet(spec.tab)
            values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
            tables[name] = [[_cell_text(v) for v in row] for row in values]
        return tables
    except SheetError:
        raise
    except Exception as exc:
        raise SheetUnavailable(_scrub(f"{type(exc).__name__}: {exc}", secrets)) from None


# ------------------------------------------------------------------ snapshots


def _csv_text(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def write_snapshot(
    tables: Mapping[str, list[list[str]]],
    lists: Mapping[str, ListSpec],
    directory: Path,
    *,
    now: datetime | None = None,
    keep: int = DEFAULT_KEEP,
) -> Path:
    """Write ``<directory>/<UTC stamp>/<list>.csv`` and ``meta.json``
    atomically (a temporary directory renamed into place); then delete all
    but the newest ``keep`` snapshots. Returns the snapshot's path."""
    now = now or datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    tmp = directory / f".tmp-{stamp}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(mode=0o700)
    meta: dict[str, Any] = {"format": SNAPSHOT_FORMAT, "fetched": now.isoformat(), "lists": {}}
    for name, rows in tables.items():
        text = _csv_text(rows)
        (tmp / f"{name}.csv").write_text(text)
        spec = lists[name]
        meta["lists"][name] = {
            "kind": spec.kind,
            "tab": spec.tab,
            "rows": max(0, len(rows) - 1),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    (tmp / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    final, k = directory / stamp, 1
    while final.exists():
        final, k = directory / f"{stamp}-{k}", k + 1
    tmp.rename(final)
    for old in _snapshots(directory)[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return final


def _snapshots(directory: Path) -> list[Path]:
    """Complete snapshots, oldest first."""
    if not directory.is_dir():
        return []
    found = [
        p
        for p in directory.iterdir()
        if p.is_dir() and _STAMP.fullmatch(p.name) and (p / "meta.json").is_file()
    ]
    return sorted(found, key=lambda p: (p.name[:16], int(p.name[17:] or 0)))


def newest_snapshot(directory: Path) -> Path | None:
    snaps = _snapshots(directory)
    return snaps[-1] if snaps else None


def read_snapshot(path: Path) -> tuple[datetime, dict[str, list[list[str]]]]:
    """A snapshot's fetch time and tables (each CSV checked against its digest)."""
    meta = json.loads((path / "meta.json").read_text())
    if meta.get("format") != SNAPSHOT_FORMAT:
        raise SheetError(f"{path}: not a {SNAPSHOT_FORMAT} snapshot")
    tables = {}
    for name, info in meta["lists"].items():
        text = (path / f"{name}.csv").read_text()
        if hashlib.sha256(text.encode()).hexdigest() != info.get("sha256"):
            raise SheetError(f"{path / (name + '.csv')}: does not match its digest in meta.json")
        tables[name] = read_csv_text(text)
    return datetime.fromisoformat(meta["fetched"]), tables


def read_csv_text(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ------------------------------------------------------------------ loading


@dataclass
class Loaded:
    """What :func:`load` returns: the lists, and where they came from."""

    path: Path
    fetched: datetime
    live: bool
    lists: dict[str, SheetList]

    def groups(self) -> list[dict[str, Any]]:
        return [g for sl in self.lists.values() for g in sl.groups]

    def refused(self) -> list[str]:
        return [r for sl in self.lists.values() for r in sl.refused]


def _age(seconds: float) -> str:
    h = seconds / 3600.0
    return f"{h:.1f} h" if h < 48 else f"{h / 24.0:.1f} days"


def convert_tables(
    tables: Mapping[str, list[list[str]]], lists: Mapping[str, ListSpec], where: str = ""
) -> dict[str, SheetList]:
    out = {}
    for name, spec in lists.items():
        if name not in tables:
            raise SheetError(f"{where or 'input'}: no table for list {name!r}")
        src = f"{where}/{name}.csv" if where else name
        out[name] = convert(tables[name], spec, src)
    return out


def load(
    config: SheetConfig,
    *,
    offline: bool | None = None,
    client: Any = None,
    now: datetime | None = None,
) -> Loaded:
    """The enabled lists: fetched live (and snapshotted), or, if the Sheet
    cannot be reached (or ``offline``, or ``$ROBOFAST_OFFLINE``), from the
    newest snapshot with a warning giving its age. Logs the snapshot path
    used. Raises :class:`SheetUnavailable` when there is neither."""
    from robofast_core.zenodo import offline as offline_env

    now = now or datetime.now(UTC)
    offline = offline_env() if offline is None else offline
    lists = config.enabled()
    directory = config.snapshots()
    path: Path | None = None
    why = "offline"
    if not offline:
        try:
            tables = fetch(config, client=client)
        except SheetUnavailable as exc:
            why = f"the Sheet could not be read ({exc})"
        else:
            path = write_snapshot(tables, lists, directory, now=now, keep=config.keep)
            fetched, live = now, True
    if path is None:
        path = newest_snapshot(directory)
        if path is None:
            raise SheetUnavailable(f"{why}, and there is no snapshot under {directory}")
        fetched, tables = read_snapshot(path)
        live = False
        log.warning(
            "MINERVA target lists: %s; using the snapshot of %s (%s old)",
            why,
            fetched.isoformat(timespec="seconds"),
            _age((now - fetched).total_seconds()),
        )
    log.info("MINERVA target lists from %s", path)
    converted = convert_tables(tables, lists, str(path))
    for sl in converted.values():
        for w in sl.warnings:
            log.warning("%s", w)
        for r in sl.refused:
            log.warning("refused: %s", r)
        log.info("%s", sl.summary())
    return Loaded(path, fetched, live, converted)


# ------------------------------------------------------------------ CLI


def _sources(args: argparse.Namespace) -> tuple[dict[str, SheetList], str, bool]:
    """(converted lists, provenance, whether :func:`load` already logged
    the refusals) from the CLI's inputs."""
    config = SheetConfig.load(args.config) if args.config else None
    if args.csv:
        specs = config.lists if config else DEFAULT_LISTS
        out = {}
        for item in args.csv:
            name, sep, path = item.partition("=")
            if not sep or name not in specs:
                raise SheetError(f"--csv {item!r}: expected LIST=PATH with LIST in {sorted(specs)}")
            out[name] = convert(read_csv_text(Path(path).read_text()), specs[name], path)
        return out, "CSV exports " + ", ".join(args.csv), False
    if args.snapshot:
        specs = config.enabled() if config else DEFAULT_LISTS
        fetched, tables = read_snapshot(Path(args.snapshot))
        specs = {k: v for k, v in specs.items() if k in tables}
        out = convert_tables(tables, specs, args.snapshot)
        return out, f"snapshot {args.snapshot} (fetched {fetched.isoformat()})", False
    if config is None:
        raise SheetError("give --config, --snapshot or --csv")
    loaded = load(config, offline=args.offline or None)
    how = "live" if loaded.live else "fallback snapshot"
    prov = f"{how} {loaded.path} (fetched {loaded.fetched.isoformat(timespec='seconds')})"
    return loaded.lists, prov, True


def _report(lists: dict[str, SheetList], logged: bool) -> None:
    for sl in lists.values():
        if not logged:  # load() logs them itself
            for w in sl.warnings:
                print(f"warning: {w}", file=sys.stderr)
            for r in sl.refused:
                print(f"refused: {r}", file=sys.stderr)
        print(sl.summary(), file=sys.stderr)


def _fetch(args: argparse.Namespace) -> int:
    config = SheetConfig.load(args.config)
    tables = fetch(config)
    path = write_snapshot(tables, config.enabled(), config.snapshots(), keep=config.keep)
    print(path)
    for name, rows in tables.items():
        print(f"{name}: {max(0, len(rows) - 1)} rows")
    return 0


def _check(args: argparse.Namespace) -> int:
    lists, prov, logged = _sources(args)
    print(prov)
    _report(lists, logged)
    return 2 if any(sl.refused for sl in lists.values()) else 0


def _export(args: argparse.Namespace) -> int:
    lists, prov, logged = _sources(args)
    _report(lists, logged)
    refused = sum(len(sl.refused) for sl in lists.values())
    if refused and args.strict:
        print(f"error: {refused} rows refused (--strict)", file=sys.stderr)
        return 2
    groups = [g for sl in lists.values() for g in sl.groups]
    load_request_groups(groups)  # refuse anything the request model would
    doc = {
        "_note": [
            f"MINERVA target lists, from {prov}, by robofast_core.catalogs.minerva_sheet.",
            f"{len(groups)} request groups; {refused} rows refused (listed when exported).",
        ],
        "groups": groups,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.catalogs.minerva_sheet",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="read the live Sheet and write a snapshot")
    f.add_argument("--config", required=True)
    for name, helptext in (
        ("check", "validate the lists, printing refusals"),
        ("export", "the lists as request groups (JSON)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", help="site sheet config (fetch, or fall back to a snapshot)")
        p.add_argument("--offline", action="store_true", help="newest snapshot, no fetch")
        p.add_argument("--snapshot", help="this snapshot directory")
        p.add_argument("--csv", action="append", metavar="LIST=PATH", help="a CSV export of a tab")
        if name == "export":
            p.add_argument("-o", "--output")
            p.add_argument("--strict", action="store_true", help="fail if any row is refused")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return {"fetch": _fetch, "check": _check, "export": _export}[args.cmd](args)
    except (SheetError, SheetUnavailable, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
