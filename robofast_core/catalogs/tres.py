"""TRES / FLWO 60-inch telshell catalogs: ``.mct``, ``.off`` and ``.sec``.

The rules are ported from the FLWO 60-inch TCS ("telshell", T. Groner,
FLWO; with J. Irwin's 2022 proper-motion change): its catalog loader, its
catalog validator and the coordinate routines that turn a catalog line into
the command sent to the telescope. docs/catalogs.md describes the formats.

Principle: accept what telshell accepts **and** reads the way the line
means; refuse, with ``file:line: reason``, what telshell's validator refuses
and what telshell would silently misread (a fractional RA hour it
truncates, a word where the epoch goes that makes it keep the previous
target's epoch, a line it splits in two). Tolerated oddities that are read
correctly become warnings.

``.sec`` catalogs are encrypted by an external helper that is not part of
telshell's source. They are read only from outside (:func:`read_sec`):
robofast does not decrypt them.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from robofast_core.catalogs.model import (
    CatalogTarget,
    Offset,
    OffsetPattern,
    PrivateCatalog,
    TargetList,
)

#: telshell reads a catalog line into a 130-byte buffer: longer lines are
#: split into two entries.
MAX_LINE = 128
#: ... and copies what follows the name into an 80-byte buffer.
MAX_AFTER_NAME = 79
#: The object name lands in a 30-byte buffer (OBJECT header).
MAX_NAME = 29
#: List entries (object and blank lines) telshell loads.
MAX_ENTRIES = 10000
#: Offset catalogs: 80-byte line buffer and at most 1025 entries.
MAX_OFF_LINE = 78
MAX_OFFSETS = 1025

#: Header keys the validator accepts, exactly as written (the blank after
#: the colon is part of the key).
HEADERS = {"!P.I.: ": "pi", "!Program: ": "name"}
_VALUE_OK = re.compile(r"[A-Za-z0-9 #\-_.,/]*")
_NAME_OK = re.compile(r"[A-Za-z0-9+\-_.]+")
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
#: What C's strtod / atof would start reading a number from.
_NUMERIC_PREFIX = re.compile(r"[+-]?(\d|\.\d)")
#: A token as telshell's tokenizer sees it (blanks and colons separate).
_TOKEN = re.compile(r"[^ :]+")

#: Leading bytes of common encrypted / compressed containers.
_MAGIC = [
    (b"Salted__", "openssl-enc"),
    (b"-----BEGIN PGP", "pgp-armored"),
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"\x00m\x03", "mcrypt"),
    (b"\x85\x01", "pgp"),
    (b"\x8c\x0d", "pgp"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ", "xz"),
]


class CatalogError(ValueError):
    """A catalog telshell would refuse or misread; the message names the
    file and line."""


def _decode(data: bytes, where: str) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = data[: exc.start].count(b"\n") + 1
        raise CatalogError(f"{where}:{line}: not UTF-8 text") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_float(s: str) -> bool:
    return bool(_FLOAT.fullmatch(s))


def _sexagesimal(tokens: list[str], what: str, *, ra: bool) -> float:
    """telshell's reading of three tokens: whole hours (degrees) and minutes
    (C ``atoi``), seconds with a fraction; the sign is the first character
    of the first token. Hours, degrees or minutes with a fraction are
    refused, since telshell drops the fraction."""
    a, m, s = tokens
    for t, part in ((a, "hours" if ra else "degrees"), (m, "minutes")):
        if not _INT.fullmatch(t):
            hint = " (telshell reads only its whole part)" if _is_float(t) else ""
            raise ValueError(f"{what} {part} {t!r} must be a whole number{hint}")
    if not _is_float(s) or s[0] in "+-":
        raise ValueError(f"{what} seconds {s!r} must be a number >= 0")
    ai, mi, sf = int(a), int(m), float(s)
    neg = a.startswith("-")
    if ra:
        if neg or ai > 24 or mi >= 60 or sf >= 60.0 or (ai == 24 and (mi or sf)):
            raise ValueError(f"{what} {a}:{m}:{s} out of range (0 <= RA < 24h)")
        if m.startswith(("+", "-")):
            raise ValueError(f"{what} minutes {m!r} must be unsigned")
        return ((ai + mi / 60.0 + sf / 3600.0) * 15.0) % 360.0
    if m.startswith(("+", "-")):
        raise ValueError(f"{what} minutes {m!r} must be unsigned")
    if mi > 60 or sf > 60.0:
        raise ValueError(f"{what} {a}:{m}:{s} out of range (minutes, seconds <= 60)")
    value = abs(ai) + mi / 60.0 + sf / 3600.0
    if value > 90.0:
        raise ValueError(f"{what} {a}:{m}:{s} out of range (|Dec| <= 90)")
    return -value if neg else value


def parse_object_line(line: str, where: str = "line") -> tuple[CatalogTarget, list[str]]:
    """One object line (no header, not blank) -> a target and warnings.

    ``NAME RA DEC [EPOCH [PMRA PMDEC]] [comment]``: the name ends at the
    first blank; RA and Dec are three parts each, separated by blanks or
    colons; EPOCH (> 1800) is both the equinox and the proper-motion epoch;
    PMRA (mu_alpha cos dec) and PMDEC are mas/yr. What follows is ignored by
    telshell and kept here as a comment.
    """
    warnings: list[str] = []
    if "\t" in line:
        raise CatalogError(f"{where}: a tab; telshell separates fields with blanks only")
    if any(ord(c) < 32 or ord(c) == 127 for c in line):
        raise CatalogError(f"{where}: a control character")
    if line.startswith(" "):
        raise CatalogError(
            f"{where}: leading blank; telshell would take an empty name and misread the line"
        )
    if " " not in line:
        raise CatalogError(f"{where}: a name and no coordinates")
    name, rest = line.split(" ", 1)
    if not _NAME_OK.fullmatch(name) or not name.isascii():
        raise CatalogError(f"{where}: name {name!r}: only letters, digits and + - _ . are allowed")
    if len(name) > MAX_NAME:
        raise CatalogError(f"{where}: name {name!r} longer than {MAX_NAME} characters")
    rest = rest.lstrip(" ")
    if len(rest) > MAX_AFTER_NAME:
        raise CatalogError(
            f"{where}: {len(rest)} characters after the name; telshell copies at most "
            f"{MAX_AFTER_NAME} (shorten the comment)"
        )
    toks = list(_TOKEN.finditer(rest))
    words = [t.group() for t in toks]
    if len(words) < 6:
        raise CatalogError(
            f"{where}: RA and Dec need three parts each; telshell would keep the previous "
            "target's coordinates"
        )
    try:
        ra = _sexagesimal(words[0:3], "RA", ra=True)
        dec = _sexagesimal(words[3:6], "Dec", ra=False)
    except ValueError as exc:
        raise CatalogError(f"{where}: {exc}") from None
    if int(words[4]) == 60 or float(words[5]) == 60.0:
        warnings.append(f"{where}: Dec minutes or seconds of 60 (read as written)")
    if int(words[0]) == 24:
        warnings.append(f"{where}: RA 24:00:00 read as 0h")
    text = {"ra": ":".join(words[0:3]), "dec": ":".join(words[3:6])}
    t = CatalogTarget(name=name, ra=ra, dec=dec)
    comment_from = None
    if len(words) > 6:
        ep = words[6]
        if not _is_float(ep) or not 1800.0 < float(ep) <= 2200.0:
            raise CatalogError(
                f"{where}: epoch {ep!r} must be a year (1800 < epoch <= 2200); telshell "
                "ignores anything else and keeps the previous target's epoch"
            )
        t.equinox, t.epoch_given, text["epoch"] = float(ep), True, ep
        if len(words) > 7:
            p1, p2 = words[7], words[8] if len(words) > 8 else None
            if _is_float(p1) and p2 is not None and _is_float(p2):
                t.pm_ra, t.pm_dec = float(p1), float(p2)
                text["pm_ra"], text["pm_dec"] = p1, p2
                if len(words) > 9:
                    comment_from = toks[9].start()
            elif _NUMERIC_PREFIX.match(p1) or (p2 is not None and _NUMERIC_PREFIX.match(p2)):
                raise CatalogError(
                    f"{where}: after the epoch, {p1!r} {p2 or ''!r}: proper motion needs two "
                    "numbers (mas/yr); telshell would read a number here as proper motion"
                )
            else:
                comment_from = toks[7].start()
    if comment_from is not None:
        t.comment = rest[comment_from:].rstrip(" ")
    t.text = text
    return t, warnings


def parse_mct(data: bytes | str, name: str = "catalog", source: str = "") -> TargetList:
    """Parse a ``.mct`` catalog (bytes or text) into a :class:`TargetList`.

    Raises :class:`CatalogError` naming ``source`` (or ``name``) and the line.
    """
    where = source or name
    if isinstance(data, str):
        data = data.encode("utf-8")
    lines = _decode(data, where)
    tl = TargetList(name=name, source=source)
    entries = 0
    seen: dict[str, int] = {}  # every prefix of an earlier name -> its line
    for n, raw in enumerate(lines, start=1):
        at = f"{where}:{n}"
        line = raw
        if line.endswith("\r"):
            line = line[:-1]
            if line.startswith("!") or not line.strip(" "):
                raise CatalogError(f"{at}: a carriage return (DOS line end) on this line")
            tl.warnings.append(f"{at}: DOS line end (tolerated)")
        if len(line) > MAX_LINE:
            raise CatalogError(
                f"{at}: {len(line)} characters; telshell splits lines longer than {MAX_LINE}"
            )
        if line.startswith("!"):
            for key, attr in HEADERS.items():
                if line.startswith(key):
                    value = line[len(key) :]
                    if not _VALUE_OK.fullmatch(value):
                        raise CatalogError(
                            f"{at}: {key.strip()} value {value!r}: only letters, digits, "
                            "blanks and # - _ . , / are allowed"
                        )
                    if getattr(tl.program, attr) is not None:
                        tl.warnings.append(f"{at}: second {key.strip()} header; this one wins")
                    setattr(tl.program, attr, value.strip(" ") or None)
                    break
            else:
                key = line.split(" ", 1)[0]
                why = (
                    "telshell's loader reads it but its validator refuses the catalog"
                    if line.startswith("!Epoch:")
                    else "only '!P.I.: ' and '!Program: ' (with the blank) are allowed"
                )
                raise CatalogError(f"{at}: header {key!r}: {why}")
            continue
        entries += 1
        if entries > MAX_ENTRIES:
            raise CatalogError(f"{at}: more than {MAX_ENTRIES} entries")
        if line == "":
            continue  # a blank entry: it counts for #n, nothing else
        if not line.strip(" "):
            raise CatalogError(f"{at}: only blanks; telshell's validator misreads such a line")
        t, warns = parse_object_line(line, at)
        t.index, t.line = entries, n
        tl.warnings.extend(warns)
        if t.name in seen:
            tl.warnings.append(
                f"{at}: '!{t.name}' selects line {seen[t.name]} instead "
                "(telshell matches the first name starting with what is typed)"
            )
        for k in range(1, len(t.name) + 1):
            seen.setdefault(t.name[:k], n)
        tl.targets.append(t)
    return tl


def parse_off(data: bytes | str, name: str = "offsets", source: str = "") -> OffsetPattern:
    """Parse a ``.off`` offset catalog into an :class:`OffsetPattern`.

    Each line: whole arcsec in RA and in Dec, separated by one blank, tab or
    comma (then any blanks); text after the Dec offset is a comment. A line
    starting ``LINK`` ends the list and makes it repeat.
    """
    where = source or name
    if isinstance(data, str):
        data = data.encode("utf-8")
    pat = OffsetPattern(name=name, source=source)
    linked_at = 0
    for n, raw in enumerate(_decode(data, where), start=1):
        at = f"{where}:{n}"
        line = raw
        if line.endswith("\r"):
            line = line[:-1]
            pat.warnings.append(f"{at}: DOS line end (tolerated)")
        if linked_at:
            if line.strip():
                raise CatalogError(f"{at}: text after LINK (line {linked_at}); telshell ignores it")
            continue
        if len(line) > MAX_OFF_LINE:
            raise CatalogError(
                f"{at}: {len(line)} characters; telshell splits lines longer than {MAX_OFF_LINE}"
            )
        if line.startswith("LINK"):
            if not pat.offsets:
                raise CatalogError(f"{at}: LINK before any offset (telshell crashes on this)")
            if line != "LINK":
                pat.warnings.append(f"{at}: {line!r} read as LINK")
            pat.repeat, linked_at = True, n
            continue
        m = re.fullmatch(r"[ \t]*([+-]?\d+)[ \t,][ \t]*([+-]?\d+)(?:([ \t].*))?", line)
        if not m:
            why = (
                "a blank line repeats the previous offset in telshell"
                if not line.strip()
                else "expected two whole numbers of arcsec (RA Dec)"
            )
            raise CatalogError(f"{at}: {why}")
        if len(pat.offsets) >= MAX_OFFSETS:
            raise CatalogError(f"{at}: more than {MAX_OFFSETS} offsets")
        comment = (m.group(3) or "").strip()
        pat.offsets.append(Offset(int(m.group(1)), int(m.group(2)), comment, n))
    if not pat.offsets:
        raise CatalogError(f"{where}: no offsets (telshell crashes on an empty offset catalog)")
    return pat


def read_sec(data: bytes, name: str = "private", source: str = "") -> PrivateCatalog:
    """A ``.sec`` catalog from outside: size, SHA-256 and the container type
    its leading bytes name (``unknown`` if none). Nothing is decrypted."""
    container = next((kind for magic, kind in _MAGIC if data.startswith(magic)), "unknown")
    if container == "unknown" and data[:1] == b"!":
        container = "plaintext?"  # looks like an unencrypted .mct
    return PrivateCatalog(
        name=name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        container=container,
        source=source,
    )


def kind_of(path: str | Path) -> str:
    """``sec``, ``off`` or ``mct``, by telshell's rules: any path containing
    ``.sec`` goes to the decryption helper (even ``x.second.mct``); offset
    catalogs are loaded by their own command, so here by suffix."""
    p = str(path)
    if ".sec" in p:
        return "sec"
    return "off" if p.endswith(".off") else "mct"


def resolve(name: str, directory: str | Path, suffix: str = ".mct") -> Path:
    """The file telshell opens for a catalog name typed by the observer: an
    absolute path as is; a name containing a dot relative to the catalog
    directory, unchanged; otherwise the directory, the name and ``suffix``
    (``.mct``, or ``.off`` for offset catalogs). Leading blanks are dropped.
    Unlike telshell, a name that leaves the directory is refused."""
    name = name.lstrip()
    if name.startswith("/"):
        return Path(name)
    base = Path(directory)
    p = base / (name if "." in name else name + suffix)
    if ".." in Path(name).parts:
        raise CatalogError(f"catalog name {name!r} leaves the catalog directory")
    return p


def load(path: str | Path) -> TargetList | OffsetPattern | PrivateCatalog:
    """Read a catalog file of any kind (by :func:`kind_of`)."""
    p = Path(path)
    data = p.read_bytes()
    kind = kind_of(p)
    stem = p.name
    for suffix in (".sec", ".mct", ".off"):
        stem = stem.removesuffix(suffix)
    if kind == "sec":
        return read_sec(data, stem, str(p))
    if kind == "off":
        return parse_off(data, stem, str(p))
    return parse_mct(data, stem, str(p))


# ---------------------------------------------------------------- writing


def _fmt_ra(deg: float) -> str:
    f = round((deg % 360.0) / 15.0 * 3600.0 * 1000.0)  # ms of time
    f %= 24 * 3600 * 1000
    h, f = divmod(f, 3600 * 1000)
    m, f = divmod(f, 60 * 1000)
    return f"{h:02d}:{m:02d}:{f / 1000.0:06.3f}"


def _fmt_dec(deg: float) -> str:
    sign = "-" if deg < 0 else "+"
    f = round(abs(deg) * 3600.0 * 100.0)  # 10 mas
    d, f = divmod(f, 3600 * 100)
    m, f = divmod(f, 60 * 100)
    return f"{sign}{d:02d}:{m:02d}:{f / 100.0:05.2f}"


def _same(text: str | None, value: float, *, ra: bool) -> bool:
    """Whether a kept token still says ``value`` (else it is re-formatted)."""
    if not text:
        return False
    parts = text.split(":")
    if len(parts) != 3:
        return False
    try:
        got = _sexagesimal(parts, "x", ra=ra)
    except ValueError:
        return False
    return math.isclose(got, value, abs_tol=1e-9)


def format_target(t: CatalogTarget) -> str:
    """The catalog line for one target (the kept tokens where they still
    hold, else freshly formatted: RA to 1 ms, Dec to 10 mas)."""
    if not _NAME_OK.fullmatch(t.name) or len(t.name) > MAX_NAME:
        raise CatalogError(f"name {t.name!r} cannot be written to a telshell catalog")
    text = t.text
    parts = [
        t.name,
        text["ra"] if _same(text.get("ra"), t.ra, ra=True) else _fmt_ra(t.ra),
        text["dec"] if _same(text.get("dec"), t.dec, ra=False) else _fmt_dec(t.dec),
    ]
    need_epoch = t.epoch_given or t.equinox != 2000.0 or t.pm_ra is not None or t.comment
    if need_epoch:
        kept = text.get("epoch")
        parts.append(kept if kept and float(kept) == t.equinox else f"{t.equinox:.1f}")
    if t.pm_ra is not None:
        for key, v in (("pm_ra", t.pm_ra), ("pm_dec", t.pm_dec or 0.0)):
            kept = text.get(key)
            parts.append(kept if kept and float(kept) == v else repr(float(v)))
    if t.comment:
        parts.append(t.comment)
    line = " ".join(parts)
    parse_object_line(line, f"target {t.name!r}")  # never write what we cannot read
    return line


def write_mct(tl: TargetList) -> str:
    """A ``.mct`` text: the headers (P.I., then Program), then the targets at
    their ``index`` positions, blank lines filling any gaps so ``#n`` still
    selects the same target."""
    out = []
    if tl.program.pi:
        out.append(f"!P.I.: {tl.program.pi}")
    if tl.program.name:
        out.append(f"!Program: {tl.program.name}")
    for line in out:
        if not _VALUE_OK.fullmatch(line.split(": ", 1)[1]):
            raise CatalogError(f"header {line!r} has characters telshell refuses")
    pos = 0
    for t in sorted(tl.targets, key=lambda t: t.index):
        if t.index > pos + 1:
            out.extend([""] * (t.index - pos - 1))
        out.append(format_target(t))
        pos = max(pos + 1, t.index)
    for n, line in enumerate(out, start=1):
        if len(line) > MAX_LINE:
            raise CatalogError(f"line {n} would be {len(line)} characters (> {MAX_LINE})")
    return "".join(line + "\n" for line in out)


def write_off(pat: OffsetPattern) -> str:
    """A ``.off`` text: ``RA DEC [comment]`` per offset, ``LINK`` if it repeats."""
    if not pat.offsets:
        raise CatalogError("an offset pattern needs at least one offset")
    lines = [f"{o.ra} {o.dec}" + (f" {o.comment}" if o.comment else "") for o in pat.offsets]
    if pat.repeat:
        lines.append("LINK")
    for n, line in enumerate(lines, start=1):
        if len(line) > MAX_OFF_LINE:
            raise CatalogError(f"offset line {n} would be {len(line)} characters")
    return "".join(line + "\n" for line in lines)
