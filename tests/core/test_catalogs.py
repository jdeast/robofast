"""TRES telshell catalogs (.mct, .off, .sec): parsing by telshell's rules,
round trips, the intermediate form and request groups.

Every catalog here is synthetic, written for these tests in the legacy
format; the program, P.I. and object names are made up.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from robofast_core.catalogs import (
    CatalogError,
    CatalogTarget,
    OffsetPattern,
    PrivateCatalog,
    Program,
    TargetList,
    from_dict,
    kind_of,
    load,
    parse_mct,
    parse_off,
    read_sec,
    resolve,
    to_requests,
    write_mct,
    write_off,
)
from robofast_core.catalogs.tres import parse_object_line
from robofast_core.request_model import load_request_groups
from robofast_core.sequencer_service import load_plan

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "sites" / "flwo60"

CANONICAL = """\
!P.I.: A. Nobody
!Program: Synthetic test, run 1
STAR-A 00:00:00.0 +00:00:00 2000.0
STAR-B 12:30:00.00 -00:30:00.0 2000.0 120.5 -33.25
STAR-C 23:59:59.999 +89:59:59.99

STAR-D 06:45:08.9 -16:42:58 1950.0 # a remark: kept
STAR-E 18:36:56.3 +38:47:01 2015.5 200.9 286.2 bright, check focus
"""


def test_canonical_catalog_fields():
    tl = parse_mct(CANONICAL, "synthetic")
    assert tl.program == Program("Synthetic test, run 1", "A. Nobody")
    assert [t.name for t in tl.targets] == ["STAR-A", "STAR-B", "STAR-C", "STAR-D", "STAR-E"]
    # #n counts the blank line, not the headers.
    assert [t.index for t in tl.targets] == [1, 2, 3, 5, 6]
    assert [t.line for t in tl.targets] == [3, 4, 5, 7, 8]
    a, b, c, d, e = tl.targets
    assert (a.ra, a.dec, a.equinox, a.epoch_given) == (0.0, 0.0, 2000.0, True)
    assert b.ra == pytest.approx(187.5) and b.dec == pytest.approx(-0.5)
    assert (b.pm_ra, b.pm_dec) == (120.5, -33.25)
    assert c.ra == pytest.approx(359.99999583) and c.dec == pytest.approx(89.99999722)
    assert (c.equinox, c.epoch_given, c.pm_ra) == (2000.0, False, None)
    assert (d.equinox, d.pm_ra, d.comment) == (1950.0, None, "# a remark: kept")
    assert (e.pm_ra, e.pm_dec, e.comment) == (200.9, 286.2, "bright, check focus")
    assert tl.warnings == []


def test_blanks_and_colons_are_both_separators():
    a = parse_object_line("X 12 30 00 -10:15:30.5 2000")[0]
    b = parse_object_line("X 12:30:00   -10 15 30.5   2000")[0]
    assert (a.ra, a.dec) == (b.ra, b.dec) == (187.5, pytest.approx(-10.258472222))


def _telshell(line: str) -> tuple[float, float, float, float, float]:
    """What telshell computes from a catalog line, transcribed from its C:
    the name ends at the first blank; strtok on blank and colon; atoi for
    hours/degrees and minutes, strtod for seconds; the sign is the first
    character of the Dec degrees; epoch if atof > 1800 (else the 2000.0 it
    starts with); proper motion strtod or 0."""

    def atof(s: str) -> float:
        m = re.match(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s)
        return float(m.group()) if m else 0.0

    def atoi(s: str) -> int:
        m = re.match(r"[+-]?\d+", s)
        return int(m.group()) if m else 0

    rest = line[line.index(" ") :]
    tok = [t for t in re.split("[ :]", rest) if t]
    h, m, s = atoi(tok[0]), atoi(tok[1]), atof(tok[2])
    ra = 15.0 * (h + m / 60 + s / 3600)
    d, dm, ds = atoi(tok[3]), atoi(tok[4]), atof(tok[5])
    dec = abs(d) + dm / 60 + ds / 3600
    if tok[3].startswith("-"):
        dec = -dec
    epoch = atof(tok[6]) if len(tok) > 6 and atof(tok[6]) > 1800 else 2000.0
    pmra = atof(tok[7]) if len(tok) > 7 else 0.0
    pmde = atof(tok[8]) if len(tok) > 8 else 0.0
    return ra % 360.0, dec, epoch, pmra, pmde


@pytest.mark.parametrize(
    "line",
    [
        "S1 00 00 00 +00 00 00",
        "S2 05:26:17.50 +28:36:28.3 2000",
        "S3 5:6:7 -0:0:1",
        "S4 23 59 59.9999 -89 59 59.9 1950.0 -5 7",
        "S5 12:00:00 -00:30:00 2000.0 1.5e2 -2.5 trailing words 12 34",
        "S6 01:02:03.4 +05:06:07.8 2010 # not 12, not proper motion",
        "S7 07:00:00 +45:59:60 2000",
        "S8 24:00:00 +10:00:00",
    ],
)
def test_reads_lines_as_telshell_does(line):
    t = parse_object_line(line)[0]
    ra, dec, epoch, pmra, pmde = _telshell(line)
    assert t.ra == pytest.approx(ra) and t.dec == pytest.approx(dec, abs=1e-12)
    assert t.equinox == epoch
    assert (t.pm_ra or 0.0, t.pm_dec or 0.0) == (pmra, pmde)


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("X 12.5 30 00 +10 00 00", "whole number"),
        ("X 12 30.5 00 +10 00 00", "whole number"),
        ("X 12 30 00 +10.5 00 00", "whole number"),
        ("X 12h 30 00 +10 00 00", "whole number"),
        ("X 12 30 00 +10 00", "three parts"),
        ("X", "no coordinates"),
        ("X 25 00 00 +10 00 00", "out of range"),
        ("X 24 00 01 +10 00 00", "out of range"),
        ("X -1 00 00 +10 00 00", "out of range"),
        ("X 12 60 00 +10 00 00", "out of range"),
        ("X 12 00 60 +10 00 00", "out of range"),
        ("X 12 00 00 +91 00 00", "out of range"),
        ("X 12 00 00 +90 00 01", "out of range"),
        ("X 12 00 00 +10 -5 00", "unsigned"),
        ("X 12 00 00 +10 05 -1", "seconds"),
        ("X 12 00 00 +10 00 00 J2000", "epoch"),
        ("X 12 00 00 +10 00 00 1700", "epoch"),
        ("X 12 00 00 +10 00 00 bright star", "epoch"),
        ("X 12 00 00 +10 00 00 2000 12", "proper motion"),
        ("X 12 00 00 +10 00 00 2000 12 stars", "proper motion"),
        ("X 12 00 00 +10 00 00 2000 12mas 5", "proper motion"),
        ("X 12 00 00 +10 00 00 2000 V= 8.5", "proper motion"),
        ("X 12 00 00 +10 00 00 2000 # 12", "proper motion"),
        ("X\t12 00 00 +10 00 00", "tab"),
        ("X 12 00 00\t+10 00 00", "tab"),
        (" X 12 00 00 +10 00 00", "leading blank"),
        ("X/1 12 00 00 +10 00 00", "only letters"),
        ("Ä 12 00 00 +10 00 00", "only letters"),
        ("A" * 30 + " 12 00 00 +10 00 00", "longer than 29"),
        ("X 12 00 00 +10 00 00 2000 0 0 " + "c" * 60, "after the name"),
        ("X 12 00 00 +10 00 00\x07", "control character"),
    ],
)
def test_refuses_what_telshell_refuses_or_misreads(line, why):
    with pytest.raises(CatalogError, match=why):
        parse_mct(f"!Program: P\nOK 01 00 00 +01 00 00\n{line}\n", "t", "t.mct")
    with pytest.raises(CatalogError, match=r"^t\.mct:3: "):
        parse_mct(f"!Program: P\nOK 01 00 00 +01 00 00\n{line}\n", "t", "t.mct")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("!Epoch: 2000\n", "validator refuses"),
        ("!Observer: Somebody\n", "only '!P.I.: '"),
        ("!Program:NoBlank\n", "only '!P.I.: '"),
        ("!P.I.:\n", "only '!P.I.: '"),
        ("!Program: semi;colon\n", "only letters"),
        ("!P.I.: tab\there\n", "only letters"),
        ("!Program: dos\r\n", "carriage return"),
        ("   \n", "only blanks"),
        ("\r\n", "carriage return"),
        ("X 01 00 00 +01 00 00 2000 0 0 " + "y" * 99 + "\n", "telshell splits"),
        (b"X 01 00 00 +01 00 00 2000 0 0 \xff\n", "not UTF-8"),
    ],
)
def test_refuses_bad_headers_and_lines(text, why):
    with pytest.raises(CatalogError, match=why):
        parse_mct(text, "t", "t.mct")


def test_entry_limit():
    ok = "".join(f"S{i} 01 00 00 +01 00 00\n" for i in range(10000))
    assert len(parse_mct(ok).targets) == 10000
    with pytest.raises(CatalogError, match=":10001: more than 10000"):
        parse_mct(ok + "\n")


def test_tolerated_oddities_warn():
    text = (
        "!Program: First\n"
        "HD100 01 00 00 +01 00 00\r\n"
        "HD10 02 00 00 +02 00 00\n"
        "HD100 03 00 00 +03 00 00\n"
        "Z 04 00 00 +04 59 60\n"
        "!Program: Second\n"
    )
    tl = parse_mct(text, "t", "t.mct")
    assert tl.program.name == "Second"
    joined = "\n".join(tl.warnings)
    assert "t.mct:2: DOS line end" in joined
    assert "t.mct:3: '!HD10' selects line 2" in joined
    assert "t.mct:4: '!HD100' selects line 2" in joined
    assert "t.mct:5: Dec minutes or seconds of 60" in joined
    assert "t.mct:6: second !Program:" in joined
    assert tl.targets[3].dec == pytest.approx(5.0)


def test_headers_are_optional_and_may_be_empty():
    tl = parse_mct("!Program: \nX 01 00 00 +01 00 00")  # no final newline either
    assert tl.program == Program() and len(tl.targets) == 1


# ------------------------------------------------------------ round trips


def test_canonical_round_trip_is_identity():
    assert write_mct(parse_mct(CANONICAL)) == CANONICAL


def test_round_trip_through_json_is_identity():
    tl = parse_mct(CANONICAL, "synthetic")
    again = from_dict(json.loads(json.dumps(tl.to_dict())))
    assert again == tl
    assert write_mct(again) == CANONICAL


def test_untidy_catalog_round_trips_to_the_same_targets():
    untidy = (
        "STAR-A 1 2 3   +4 5 6\n"
        "\n"
        "\n"
        "STAR-B 01:02:03.5 -00 00 30 1950 1 -2   comment  here  \n"
        "!P.I.: Late Header\n"
    )
    first = parse_mct(untidy, "u")
    text = write_mct(first)
    assert text.splitlines()[0] == "!P.I.: Late Header"
    second = parse_mct(text, "u")
    strip = [{k: v for k, v in t.to_dict().items() if k != "line"} for t in first.targets]
    assert [{k: v for k, v in t.to_dict().items() if k != "line"} for t in second.targets] == strip
    assert [t.index for t in second.targets] == [1, 4]
    assert write_mct(second) == text


def test_writes_targets_without_their_original_text():
    tl = TargetList(
        "made",
        Program("Made up", "Nobody"),
        [
            CatalogTarget("A", 359.9999999, -0.000001, index=1),
            CatalogTarget("B", 10.0, 89.999999, 1950.0, True, 3.0, -4.0, "note", index=3),
        ],
    )
    text = write_mct(tl)
    assert text == (
        "!P.I.: Nobody\n!Program: Made up\n"
        "A 00:00:00.000 -00:00:00.00\n\n"
        "B 00:40:00.000 +90:00:00.00 1950.0 3.0 -4.0 note\n"
    )
    back = parse_mct(text)
    assert back.targets[0].ra == 0.0 and back.targets[0].dec == 0.0
    assert back.targets[1].index == 3


def test_edited_coordinates_are_reformatted_not_the_stale_text():
    tl = parse_mct("X 01:00:00 +01:00:00 2000\n")
    tl.targets[0].ra = 30.0
    assert write_mct(tl) == "X 02:00:00.000 +01:00:00 2000\n"


@pytest.mark.parametrize(
    "target",
    [
        CatalogTarget("bad name", 0.0, 0.0),
        CatalogTarget("X", 0.0, 0.0, comment="12 looks like proper motion"),
        CatalogTarget("X", 0.0, 0.0, comment="tab\tinside"),
    ],
)
def test_never_writes_what_it_cannot_read_back(target):
    with pytest.raises(CatalogError):
        write_mct(TargetList("x", targets=[target]))


# ------------------------------------------------------------ offsets


OFFSETS = "0 0\n30 0 east\n-30 15\n0 -15\nLINK\n"


def test_offset_catalog():
    pat = parse_off(OFFSETS, "box")
    assert [(o.ra, o.dec) for o in pat.offsets] == [(0, 0), (30, 0), (-30, 15), (0, -15)]
    assert pat.repeat and pat.offsets[1].comment == "east"
    assert write_off(pat) == OFFSETS
    assert from_dict(json.loads(json.dumps(pat.to_dict()))) == pat


def test_offset_separators_telshell_accepts():
    pat = parse_off(" 5,-6\n7\t8\n9   10  x\n", "s")
    assert [(o.ra, o.dec) for o in pat.offsets] == [(5, -6), (7, 8), (9, 10)]
    assert not pat.repeat


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("10 20\n\n30 40\n", ":2: a blank line repeats"),
        ("10.5 3\n", ":1: expected two whole numbers"),
        ("10-20\n", ":1: expected two whole numbers"),
        ("10 20abc\n", ":1: expected two whole numbers"),
        ("10\n", ":1: expected two whole numbers"),
        ("LINK\n", ":1: LINK before any offset"),
        ("1 2\nLINK\n3 4\n", ":3: text after LINK"),
        ("", "no offsets"),
        ("1 2 " + "x" * 80 + "\n", ":1: .* telshell splits"),
    ],
)
def test_offset_errors(text, why):
    with pytest.raises(CatalogError, match=why):
        parse_off(text, "o", "o.off")


def test_offset_limit_and_tolerated_link_spelling():
    pat = parse_off("1 1\nLINKED\n\n", "o")
    assert pat.repeat and pat.warnings
    many = "1 1\n" * 1025
    assert len(parse_off(many).offsets) == 1025
    with pytest.raises(CatalogError, match="more than 1025"):
        parse_off(many + "1 1\n")


# ------------------------------------------------------------ .sec, files


def test_sealed_catalog_is_described_not_decrypted(tmp_path):
    payload = b"Salted__" + bytes(range(40))
    p = tmp_path / "secretprog.sec"
    p.write_bytes(payload)
    sec = load(p)
    assert isinstance(sec, PrivateCatalog)
    assert (sec.name, sec.size, sec.container) == ("secretprog", 48, "openssl-enc")
    d = sec.to_dict()
    assert d["private"] and d["targets"] == [] and d["kind"] == "private"
    assert read_sec(b"\x01\x02").container == "unknown"
    assert read_sec(b"!Program: x\n").container == "plaintext?"
    with pytest.raises(ValueError, match="no targets"):
        from_dict(d)


def test_kinds_and_names_by_telshell_rules(tmp_path):
    assert kind_of("a.mct") == "mct" and kind_of("a.off") == "off"
    assert kind_of("a.sec") == "sec" and kind_of("x.second.mct") == "sec"
    cats = tmp_path / "cats"
    assert resolve("prog", cats) == cats / "prog.mct"
    assert resolve("  prog", cats, ".off") == cats / "prog.off"
    assert resolve("prog.txt", cats) == cats / "prog.txt"
    assert resolve("/abs/where.mct", cats) == Path("/abs/where.mct")
    assert resolve("sub/prog", cats) == cats / "sub" / "prog.mct"
    with pytest.raises(CatalogError, match="leaves"):
        resolve("../elsewhere.mct", cats)


def test_load_by_suffix(tmp_path):
    (tmp_path / "p.mct").write_text(CANONICAL)
    (tmp_path / "p.off").write_text(OFFSETS)
    assert isinstance(load(tmp_path / "p.mct"), TargetList)
    assert isinstance(load(tmp_path / "p.off"), OffsetPattern)
    assert load(tmp_path / "p.mct").source == str(tmp_path / "p.mct")


# ------------------------------------------------------------ requests


def test_to_requests_valid_under_the_request_model():
    tl = parse_mct(CANONICAL, "synthetic")
    groups = to_requests(tl, exposure_time=600, exposure_count=2)
    loaded = load_request_groups(groups)
    assert [g.id for g in loaded] == [f"synthetic-{n}" for n in (1, 2, 3, 5, 6)]
    g = loaded[1]
    assert g.proposal == "Synthetic test, run 1" and g.name == "STAR-B"
    (r,) = g.requests
    assert r.telescopes == ["tillinghast"]
    (c,) = r.configurations
    assert (c.type, c.instrument, c.guide) == ("EXPOSE", "tres", True)
    assert (c.instrument_configs[0].exptime, c.instrument_configs[0].count) == (600.0, 2)
    assert (c.target.ra, c.target.dec) == (pytest.approx(187.5), pytest.approx(-0.5))
    assert (c.target.pm_ra, c.target.pm_dec, c.target.epoch) == (120.5, -33.25, 2000.0)
    note = groups[1]["requests"][0]["observation_note"]
    assert note == "catalog synthetic #2 (line 4); P.I.: A. Nobody; Program: Synthetic test, run 1"
    assert "bright, check focus" in groups[4]["requests"][0]["observation_note"]


def test_other_equinoxes_are_precessed_to_icrs():
    t = CatalogTarget("X", 0.0, 0.0, equinox=1950.0)
    ra, dec = t.icrs()
    # 50 years of general precession: about 0.64 deg in RA, 0.28 deg in Dec.
    assert ra == pytest.approx(0.6408, abs=2e-3) and dec == pytest.approx(0.2783, abs=2e-3)
    assert CatalogTarget("X", 10.0, 20.0).icrs() == (10.0, 20.0)


def test_private_lists_need_consent():
    tl = parse_mct(CANONICAL, "s")
    tl.private = True
    with pytest.raises(ValueError, match="private"):
        to_requests(tl)
    assert len(to_requests(tl, include_private=True)) == 5
    with pytest.raises(ValueError, match="exposure_time"):
        to_requests(parse_mct(CANONICAL), exposure_time=0)


def test_requests_run_on_the_flwo60_dispatch_plan(tmp_path):
    groups = to_requests(parse_mct(CANONICAL, "synthetic"))
    (tmp_path / "requests.example.json").write_text(json.dumps({"groups": groups}))
    shutil.copy(SITE / "dispatch.example.json", tmp_path / "dispatch.example.json")
    plan = load_plan(tmp_path / "dispatch.example.json", ["tillinghast"])
    assert len(plan.requests) == 5


# ------------------------------------------------------------ command line


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "robofast_core.catalogs", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )


def test_command_line(tmp_path):
    (tmp_path / "prog.mct").write_text(CANONICAL)
    (tmp_path / "bad.mct").write_text("X 12.5 00 00 +00 00 00\n")
    (tmp_path / "box.off").write_text(OFFSETS)
    (tmp_path / "hidden.sec").write_bytes(b"\x00\x01\x02")

    p = _cli("import", "prog.mct", "-o", "prog.json", cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    form = json.loads((tmp_path / "prog.json").read_text())
    assert form["kind"] == "targets" and len(form["targets"]) == 5

    p = _cli("export", "prog.json", cwd=tmp_path)
    assert p.returncode == 0 and p.stdout == CANONICAL

    p = _cli("import", "prog.mct", "--to-requests", cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert len(load_request_groups(json.loads(p.stdout)["groups"])) == 5

    p = _cli("import", "bad.mct", cwd=tmp_path)
    assert p.returncode == 2 and "bad.mct:1: RA hours '12.5'" in p.stderr

    p = _cli("import", "hidden.sec", "--to-requests", cwd=tmp_path)
    assert p.returncode == 2 and "does not decrypt" in p.stderr
    p = _cli("import", "hidden.sec", cwd=tmp_path)
    assert p.returncode == 0 and json.loads(p.stdout)["sealed"]["size"] == 3

    p = _cli("import", "prog.mct", "--private", "--to-requests", cwd=tmp_path)
    assert p.returncode == 2 and "private" in p.stderr

    p = _cli("check", "prog.mct", "box.off", "hidden.sec", "bad.mct", cwd=tmp_path)
    assert p.returncode == 2
    assert "prog.mct: 5 targets" in p.stdout and "box.off: 4 offsets, repeating" in p.stdout
    assert "sealed (3 bytes" in p.stdout and "bad.mct:1:" in p.stderr

    p = _cli("import", "box.off", "-o", "box.json", cwd=tmp_path)
    assert p.returncode == 0
    p = _cli("export", "box.json", cwd=tmp_path)
    assert p.stdout == OFFSETS
