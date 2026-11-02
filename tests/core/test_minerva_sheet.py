"""MINERVA's target-list Sheet: rows to request groups, refusals, snapshots
and the fallback, the CLI.

Every list here is synthetic: the column headers are legacy's (the
``targets`` and ``B stars`` tabs as minerva-control reads them), the stars
and values are made up. gspread is never imported: a fake client stands in
for it, so nothing touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast_core.catalogs import minerva_sheet as ms
from robofast_core.dispatch import DispatchScheduler, TelescopeLimits
from robofast_core.request_model import load_request_groups

DATA = Path(__file__).parent / "data" / "minerva_sheet"
SITE = Path(__file__).resolve().parents[2] / "sites" / "minerva"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def table(name: str) -> list[list[str]]:
    return ms.read_csv_text((DATA / f"{name}.csv").read_text())


def spec(name: str) -> ms.ListSpec:
    return ms.DEFAULT_LISTS[name]


# -- rows to requests --------------------------------------------------------------


def test_targets_tab_fields():
    sl = ms.convert(table("targets"), spec("targets"), "targets.csv")
    assert [g["name"] for g in sl.groups] == ["SYN-ALPHA", "SYN BETA"]
    assert (sl.inactive, sl.blank) == (1, 1)  # the inactive row is not checked
    a, b = sl.groups
    assert a["id"] == "RV-SYN-ALPHA" and b["id"] == "RV-SYN_BETA"
    assert a["proposal"] == "minerva-rv" and a["ipp_value"] == 5.0
    r = a["requests"][0]
    assert r["extra_params"]["cadence"] == {"min_interval": 7200.0, "max_per_night": 3}
    assert r["observation_note"] == "MINERVA sheet targets, row 2; V = 6.5; synthetic"
    c = r["configurations"][0]
    assert c["type"] == "SPECTRUM" and c["instrument_type"] == "spectrograph"
    assert c["instrument_configs"] == [
        {"exposure_time": 600.0, "exposure_count": 1, "extra_params": {"iodine": True}}
    ]
    assert c["acquisition_config"] == {"exposure_time": 1.5}
    assert c["target"] == {
        "type": "ICRS",
        "name": "SYN-ALPHA",
        "ra": 22.5,  # 1.5 h
        "dec": 20.0,
        "proper_motion_ra": 10.5,
        "proper_motion_dec": -3.25,
        "epoch": 2000.0,
        "parallax": 20.0,
        "extra_params": {"radial_velocity": -12.3},
    }
    # Blank seplimit / maxobs: legacy's defaults; blank parallax, rv, fauexptime: absent.
    rb = b["requests"][0]
    assert rb["extra_params"]["cadence"] == {"min_interval": 10800.0, "max_per_night": 2}
    tb = rb["configurations"][0]["target"]
    assert "parallax" not in tb and "extra_params" not in tb
    assert "acquisition_config" not in rb["configurations"][0]
    assert rb["observation_note"] == "MINERVA sheet targets, row 3; V = 6.5"
    load_request_groups(sl.groups)  # valid under the request model


def test_bad_rows_are_refused_with_row_and_reason():
    sl = ms.convert(table("targets"), spec("targets"), "targets.csv")
    refused = "\n".join(sl.refused)
    assert len(sl.refused) == 8, refused
    for row, why in [
        (6, "pmra is blank"),
        (7, "iodine 0: legacy ignored this column"),
        (8, "acquisition_offset_north 5 arcsec: robofast cannot offset"),
        (9, "name 'SYN-ALPHA' already on row 2"),
        (10, "radeg 136.0 disagrees with ra 9.0 h x 15 by 3600.0 arcsec"),
        (11, "active 'yes': expected 1 or 0"),
        (12, "priority 0: must be > 0"),
        (13, "dec (J2000 deg) '95.0' is outside [-90, 90]"),
    ]:
        assert f"targets row {row}: {why}" in refused


def test_bstars_share_one_visit_a_night():
    sl = ms.convert(table("bstars"), spec("bstars"))
    assert [g["id"] for g in sl.groups] == ["BSTAR-SYN-B1", "BSTAR-SYN-B2"]
    assert sl.inactive == 1 and not sl.refused
    for g in sl.groups:
        r = g["requests"][0]
        assert g["proposal"] == "iodine-calibration"
        # B stars: the maxobs column is not used; one of the pool a night.
        assert r["extra_params"]["cadence"] == {
            "min_interval": 10800.0,
            "max_per_night": 1,
            "pool": "bstars",
        }
        ic = r["configurations"][0]["instrument_configs"][0]
        assert ic["extra_params"] == {"iodine": True}
    groups = load_request_groups(sl.groups)
    d = DispatchScheduler(groups, ms_site(), {"t1": TelescopeLimits(instruments=["spectrograph"])})
    assert all(r.cadence.pool == "bstars" for r in d.requests.values())


def ms_site():
    from robofast_core import sky

    return sky.Site(31.680407, -110.878977, 2316.0)


def test_header_problems_refuse_the_tab():
    rows = table("targets")
    no_pm = [[c for i, c in enumerate(r) if i != 4] for r in rows]  # drop pmra
    with pytest.raises(ms.SheetError, match=r"missing columns \['pmra'\]"):
        ms.convert(no_pm, spec("targets"))
    twice = [[*r, r[13]] for r in rows]  # a second 'priority'
    with pytest.raises(ms.SheetError, match="column 'priority' appears twice"):
        ms.convert(twice, spec("targets"))
    extra = [[*rows[0], "new column (units)"], *[[*r, "x"] for r in rows[1:]]]
    sl = ms.convert(extra, spec("targets"))
    assert any("ignored: ['new column']" in w for w in sl.warnings)
    with pytest.raises(ms.SheetError, match="empty"):
        ms.convert([], spec("targets"))


def test_cell_values_keep_full_precision():
    assert ms._cell_text(1.0) == "1"
    assert ms._cell_text(1.2345678901234) == "1.2345678901234"
    assert ms._cell_text(None) == "" and ms._cell_text(True) == "TRUE"
    assert ms._cell_text(7) == "7" and ms._cell_text(" a ") == " a "


# -- config ------------------------------------------------------------------------


def test_site_example_config_loads():
    cfg = ms.SheetConfig.load(SITE / "target_sheet.example.json")
    assert set(cfg.enabled()) == {"targets", "bstars"}  # the red list is off
    assert cfg.lists["bstars"].kind == "bstars" and cfg.lists["bstars"].tab == "B stars"
    assert cfg.key_file == Path("/etc/robofast/secrets/minerva_sheets.json")
    # The public example carries placeholders only, and fetch refuses them.
    assert all(ms._placeholder(s.sheet) for s in cfg.lists.values())
    with pytest.raises(ms.SheetError, match="placeholder"):
        ms.fetch(cfg, client=FakeClient({}))


def test_config_errors(tmp_path):
    with pytest.raises(ms.SheetError, match="unknown keys"):
        ms.SheetConfig.from_dict({"lists": {"t": {"kind": "targets", "tab": "x"}}, "bogus": 1})
    with pytest.raises(ms.SheetError, match="kind 'red'"):
        ms.SheetConfig.from_dict({"lists": {"t": {"kind": "red", "tab": "x"}}})
    with pytest.raises(ms.SheetError, match="'kind' and 'tab'"):
        ms.SheetConfig.from_dict({"lists": {"t": {"tab": "x"}}})


def test_key_path_from_the_environment(monkeypatch):
    cfg = config(Path("/nonexistent"))
    monkeypatch.delenv(ms.KEY_ENV, raising=False)
    assert cfg.key_path() == Path("/etc/robofast/secrets/k.json")
    monkeypatch.setenv(ms.KEY_ENV, "/elsewhere/k.json")
    assert cfg.key_path() == Path("/elsewhere/k.json")


# -- fetching (a fake gspread) -----------------------------------------------------


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_all_values(self, **kw):
        self.calls.append(kw)
        return self.rows


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, title):
        if title not in self.tabs:
            raise LookupError(f"no worksheet {title!r}")
        return self.tabs[title]


class FakeClient:
    def __init__(self, books, fail: Exception | None = None):
        self.books, self.fail, self.opened = books, fail, []

    def open_by_key(self, key):
        self.opened.append(key)
        if self.fail is not None:
            raise self.fail
        return self.books[key]


SHEET = "synthetic-sheet-key-0123456789"


def config(tmp: Path, **kw) -> ms.SheetConfig:
    d = {
        "key_file": "/etc/robofast/secrets/k.json",
        "snapshot_dir": str(tmp / "snaps"),
        "lists": {
            "targets": {"sheet": SHEET, "tab": "targets", "kind": "targets", "id_prefix": "RV"},
            "bstars": {"sheet": SHEET, "tab": "B stars", "kind": "bstars", "id_prefix": "BSTAR"},
        },
        **kw,
    }
    return ms.SheetConfig.from_dict(d)


def live_client() -> FakeClient:
    # Unformatted values, as the Sheets API returns them: numbers as numbers.
    def typed(rows):
        out = [rows[0]]
        for r in rows[1:]:
            new = []
            for c in r:
                try:
                    new.append(float(c) if "." in c else int(c))
                except ValueError:
                    new.append(c)
            out.append(new)
        return out

    book = FakeBook(
        {
            "targets": FakeWorksheet(typed(table("targets"))),
            "B stars": FakeWorksheet(typed(table("bstars"))),
        }
    )
    return FakeClient({SHEET: book})


def test_fetch_reads_unformatted_values_once_per_sheet(tmp_path):
    client = live_client()
    tables = ms.fetch(config(tmp_path), client=client)
    assert client.opened == [SHEET]  # both tabs from one open
    ws = client.books[SHEET].tabs["targets"]
    assert ws.calls == [{"value_render_option": "UNFORMATTED_VALUE"}]
    assert tables["targets"][1][:4] == ["SYN-ALPHA", "1.5", "22.5", "20"]


def test_fetch_errors_never_carry_the_sheet_key(tmp_path):
    client = FakeClient({}, fail=RuntimeError(f"403 for spreadsheets/{SHEET}/values"))
    with pytest.raises(ms.SheetUnavailable) as exc:
        ms.fetch(config(tmp_path), client=client)
    assert SHEET not in str(exc.value) and "<sheet>" in str(exc.value)


def test_client_uses_the_read_only_scope(tmp_path, monkeypatch):
    seen = {}
    gspread = types.ModuleType("gspread")
    gspread.authorize = lambda creds: seen.setdefault("client", ("client", creds))
    sa = types.ModuleType("google.oauth2.service_account")

    class Credentials:
        @staticmethod
        def from_service_account_file(path, scopes):
            seen["path"], seen["scopes"] = path, scopes
            return "creds"

    sa.Credentials = Credentials
    monkeypatch.setitem(sys.modules, "gspread", gspread)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", sa)
    key = tmp_path / "key.json"
    key.write_text("{}")
    key.chmod(0o600)
    assert ms._client(key) == ("client", "creds")
    assert seen["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    with pytest.raises(ms.SheetUnavailable, match="not found"):
        ms._client(tmp_path / "missing.json")


def test_client_without_gspread(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "gspread", None)
    with pytest.raises(ms.SheetUnavailable, match="pip install --group sheets"):
        ms._client(tmp_path / "key.json")


# -- snapshots and the fallback ----------------------------------------------------


def test_live_load_writes_a_snapshot(tmp_path, caplog):
    cfg = config(tmp_path)
    with caplog.at_level(logging.INFO, logger=ms.__name__):
        loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW)
    assert loaded.live and loaded.path == tmp_path / "snaps" / "20261101T120000Z"
    assert f"MINERVA target lists from {loaded.path}" in caplog.text
    assert sorted(p.name for p in loaded.path.iterdir()) == [
        "bstars.csv",
        "meta.json",
        "targets.csv",
    ]
    meta = json.loads((loaded.path / "meta.json").read_text())
    assert meta["lists"]["targets"]["rows"] == 12 and SHEET not in json.dumps(meta)
    assert len(loaded.groups()) == 4 and len(loaded.refused()) == 8
    if os.name == "posix":
        assert loaded.path.stat().st_mode & 0o077 == 0
    # The snapshot reads back to the same requests.
    fetched, tables = ms.read_snapshot(loaded.path)
    assert fetched == NOW
    again = ms.convert_tables(tables, cfg.enabled())
    assert [g for sl in again.values() for g in sl.groups] == loaded.groups()


def test_unreachable_sheet_falls_back_to_the_newest_snapshot(tmp_path, caplog):
    cfg = config(tmp_path)
    ms.load(cfg, offline=False, client=live_client(), now=NOW - timedelta(days=3))
    newest = ms.load(cfg, offline=False, client=live_client(), now=NOW - timedelta(hours=5))
    down = FakeClient({}, fail=ConnectionError("network unreachable"))
    with caplog.at_level(logging.INFO, logger=ms.__name__):
        loaded = ms.load(cfg, offline=False, client=down, now=NOW)
    assert not loaded.live and loaded.path == newest.path
    assert "could not be read (ConnectionError: network unreachable)" in caplog.text
    assert "(5.0 h old)" in caplog.text
    assert f"MINERVA target lists from {newest.path}" in caplog.text
    assert loaded.groups() == newest.groups()
    # Offline: no fetch is even tried.
    loaded = ms.load(cfg, offline=True, client=down, now=NOW + timedelta(days=4))
    assert loaded.path == newest.path and down.opened == ["synthetic-sheet-key-0123456789"]


def test_no_sheet_and_no_snapshot(tmp_path):
    down = FakeClient({}, fail=ConnectionError("down"))
    with pytest.raises(ms.SheetUnavailable, match="no snapshot under"):
        ms.load(config(tmp_path), offline=False, client=down, now=NOW)


def test_snapshots_are_pruned_and_checked(tmp_path):
    cfg = config(tmp_path, keep=2)
    for h in range(4):
        ms.load(cfg, offline=False, client=live_client(), now=NOW + timedelta(hours=h))
    snaps = ms._snapshots(tmp_path / "snaps")
    assert [p.name for p in snaps] == ["20261101T140000Z", "20261101T150000Z"]
    # Same second twice: a suffix, still newest.
    p = ms.write_snapshot(
        {"targets": [["name"]]}, cfg.lists, tmp_path / "snaps", now=NOW + timedelta(hours=3), keep=5
    )
    assert p.name == "20261101T150000Z-1" and ms.newest_snapshot(tmp_path / "snaps") == p
    (p / "targets.csv").write_text("tampered\n")
    with pytest.raises(ms.SheetError, match="does not match its digest"):
        ms.read_snapshot(p)
    # A half-written snapshot (no meta.json) is never picked.
    (tmp_path / "snaps" / "20991231T000000Z").mkdir()
    assert ms.newest_snapshot(tmp_path / "snaps") == p


# -- command line ------------------------------------------------------------------


def test_cli_check_and_export_from_csv(tmp_path, capsys):
    t, b = f"targets={DATA / 'targets.csv'}", f"bstars={DATA / 'bstars.csv'}"
    assert ms.main(["check", "--csv", t, "--csv", b]) == 2  # refused rows
    err = capsys.readouterr().err
    assert "refused: targets row 6: pmra is blank" in err
    assert "targets: 2 requests, 1 inactive, 8 refused" in err
    assert "bstars: 2 requests, 1 inactive, 0 refused" in err
    out = tmp_path / "requests.json"
    assert ms.main(["export", "--csv", t, "--csv", b, "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert [g["id"] for g in doc["groups"]] == [
        "RV-SYN-ALPHA",
        "RV-SYN_BETA",
        "BSTAR-SYN-B1",
        "BSTAR-SYN-B2",
    ]
    assert len(load_request_groups(str(out))) == 4
    assert ms.main(["export", "--csv", t, "--strict"]) == 2
    assert ms.main(["check", "--csv", b]) == 0
    assert ms.main(["check", "--csv", f"nope={DATA / 'bstars.csv'}"]) == 2
    assert ms.main(["check"]) == 2


def test_cli_offline_and_snapshot(tmp_path, capsys, monkeypatch):
    cfg = config(tmp_path)
    loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW)
    cfile = tmp_path / "sheet.json"
    raw = {
        "snapshot_dir": str(tmp_path / "snaps"),
        "lists": {
            k: {"kind": v.kind, "tab": v.tab, "sheet": SHEET, "id_prefix": v.id_prefix}
            for k, v in cfg.lists.items()
        },
    }
    cfile.write_text(json.dumps(raw))
    assert ms.main(["check", "--config", str(cfile), "--offline"]) == 2
    out = capsys.readouterr().out
    assert f"fallback snapshot {loaded.path}" in out
    assert ms.main(["check", "--snapshot", str(loaded.path)]) == 2
    assert f"snapshot {loaded.path}" in capsys.readouterr().out
    # fetch with no key file: an error, no fallback, exit 2.
    monkeypatch.delenv(ms.KEY_ENV, raising=False)
    assert ms.main(["fetch", "--config", str(cfile)]) == 2
    assert "no service-account key" in capsys.readouterr().err
