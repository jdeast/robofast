"""The intermediate form of an imported catalog, and its mapping onto
robofast's request model.

This is not the target database (whose design waits for a look at a live
TRES catalog directory; docs/catalogs.md, "Deferred"). It is a small,
JSON-serialisable form that maps one to one onto the proposed target model:

========================  ==========================================
Here                      Proposed target model
========================  ==========================================
:class:`Program`          Program (name, PI)
:class:`TargetList`       TargetList owned by the Program (one file)
:class:`CatalogTarget`    Target (sidereal), with its list position
:class:`OffsetPattern`    an offset / dither pattern (repeating or not)
:class:`PrivateCatalog`   a private TargetList whose targets are sealed
========================  ==========================================

and turns a :class:`TargetList` into request groups today
(:func:`to_requests`), one per target, valid under
:mod:`robofast_core.request_model`.

Coordinates are kept as the catalog states them: right ascension and
declination in degrees for the mean equinox ``equinox`` (a Julian year), at
epoch ``equinox`` (TRES catalogs have one number for both: telshell sends it
to the TCS as the equinox and uses it as the proper-motion epoch).
:meth:`CatalogTarget.icrs` converts to ICRS.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Version tag of the JSON form written by :meth:`TargetList.to_dict` & co.
FORMAT = "robofast-catalog/1"

#: Defaults for :func:`to_requests` (the FLWO 60-inch with TRES; the names in
#: sites/flwo60/). TRES catalogs carry no exposure times: the default is a
#: placeholder to be replaced per program.
DEFAULT_TELESCOPE = "tillinghast"
DEFAULT_INSTRUMENT = "tres"
DEFAULT_EXPOSURE_TIME = 300.0


@dataclass
class Program:
    """Who a list belongs to: the ``!Program:`` and ``!P.I.:`` headers."""

    name: str | None = None
    pi: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pi": self.pi}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Program:
        d = d or {}
        return cls(d.get("name") or None, d.get("pi") or None)


@dataclass
class CatalogTarget:
    """One object line of a catalog.

    ``ra``/``dec`` are degrees for the mean equinox ``equinox`` (Julian
    year); ``epoch_given`` says whether the line stated it (else the default,
    2000.0). ``pm_ra`` is mu_alpha cos(dec) and ``pm_dec`` mu_delta, both in
    mas/yr, at epoch ``equinox``. ``index`` is the legacy list position (what
    telshell's ``#n`` selects; blank lines count, headers do not), ``line``
    the line number in the file. ``text`` keeps the tokens as written (RA,
    Dec, epoch, proper motions) so an export reproduces them.
    """

    name: str
    ra: float
    dec: float
    equinox: float = 2000.0
    epoch_given: bool = False
    pm_ra: float | None = None
    pm_dec: float | None = None
    comment: str = ""
    index: int = 0
    line: int = 0
    text: dict[str, str] = field(default_factory=dict)

    def icrs(self) -> tuple[float, float]:
        """(RA, Dec) in degrees, ICRS, at epoch :attr:`equinox`.

        Equinox 2000.0 is taken as ICRS (FK5 J2000 and ICRS differ by about
        20 mas, far below the TRES fiber). Any other equinox is precessed as
        FK5 with a Julian equinox (astropy); a catalog meant as FK4 / B1950
        differs from that by up to about 1 arcsec (docs/catalogs.md).
        """
        if self.equinox == 2000.0:
            return self.ra, self.dec
        import astropy.units as u
        from astropy.coordinates import FK5, SkyCoord
        from astropy.time import Time

        eq = Time(self.equinox, format="jyear", scale="tt")
        c = SkyCoord(self.ra * u.deg, self.dec * u.deg, frame=FK5(equinox=eq)).icrs
        return float(c.ra.deg) % 360.0, float(c.dec.deg)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "ra": self.ra,
            "dec": self.dec,
            "equinox": self.equinox,
            "epoch_given": self.epoch_given,
        }
        if self.pm_ra is not None:
            d["pm_ra"] = self.pm_ra
            d["pm_dec"] = self.pm_dec
        if self.comment:
            d["comment"] = self.comment
        d["index"] = self.index
        if self.line:
            d["line"] = self.line
        if self.text:
            d["text"] = dict(self.text)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CatalogTarget:
        return cls(
            name=str(d["name"]),
            ra=float(d["ra"]),
            dec=float(d["dec"]),
            equinox=float(d.get("equinox", 2000.0)),
            epoch_given=bool(d.get("epoch_given", False)),
            pm_ra=None if d.get("pm_ra") is None else float(d["pm_ra"]),
            pm_dec=None if d.get("pm_dec") is None else float(d["pm_dec"]),
            comment=str(d.get("comment") or ""),
            index=int(d.get("index", 0)),
            line=int(d.get("line", 0)),
            text={str(k): str(v) for k, v in (d.get("text") or {}).items()},
        )


@dataclass
class TargetList:
    """One ``.mct`` file: a named list of targets owned by a program.

    ``private`` marks a list from a confidential catalog (a decrypted
    ``.sec``): its coordinates must only be shown to the program.
    ``warnings`` are the parser's remarks (tolerated oddities), with line
    numbers.
    """

    name: str
    program: Program = field(default_factory=Program)
    targets: list[CatalogTarget] = field(default_factory=list)
    private: bool = False
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format": FORMAT,
            "kind": "targets",
            "name": self.name,
            "program": self.program.to_dict(),
            "private": self.private,
        }
        if self.source:
            d["source"] = self.source
        d["targets"] = [t.to_dict() for t in self.targets]
        if self.warnings:
            d["warnings"] = list(self.warnings)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TargetList:
        _expect(d, "targets")
        return cls(
            name=str(d["name"]),
            program=Program.from_dict(d.get("program")),
            targets=[CatalogTarget.from_dict(t) for t in d.get("targets") or []],
            private=bool(d.get("private", False)),
            source=str(d.get("source") or ""),
            warnings=[str(w) for w in d.get("warnings") or []],
        )


@dataclass
class Offset:
    """One step of an offset pattern: arcsec in RA and Dec from the target."""

    ra: int
    dec: int
    comment: str = ""
    line: int = 0


@dataclass
class OffsetPattern:
    """One ``.off`` file: offsets in arcsec from the target, in order;
    ``repeat`` (the file's ``LINK``) makes the pattern cycle."""

    name: str
    offsets: list[Offset] = field(default_factory=list)
    repeat: bool = False
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"format": FORMAT, "kind": "offsets", "name": self.name}
        if self.source:
            d["source"] = self.source
        d["repeat"] = self.repeat
        d["offsets"] = [
            {"ra": o.ra, "dec": o.dec, **({"comment": o.comment} if o.comment else {})}
            | ({"line": o.line} if o.line else {})
            for o in self.offsets
        ]
        if self.warnings:
            d["warnings"] = list(self.warnings)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OffsetPattern:
        _expect(d, "offsets")
        return cls(
            name=str(d["name"]),
            offsets=[
                Offset(
                    int(o["ra"]), int(o["dec"]), str(o.get("comment") or ""), int(o.get("line", 0))
                )
                for o in d.get("offsets") or []
            ],
            repeat=bool(d.get("repeat", False)),
            source=str(d.get("source") or ""),
            warnings=[str(w) for w in d.get("warnings") or []],
        )


@dataclass
class PrivateCatalog:
    """A confidential (``.sec``) catalog: the private targets of a program,
    sealed. robofast reads only what is outside the encrypted payload
    (its size, a digest, and the container type if its leading bytes name
    one); it never decrypts it. ``name`` is the file name without
    ``.sec``, which is all that identifies the program from outside."""

    name: str
    size: int
    sha256: str
    container: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format": FORMAT,
            "kind": "private",
            "name": self.name,
            "private": True,
            "sealed": {"size": self.size, "sha256": self.sha256, "container": self.container},
            "targets": [],
        }
        if self.source:
            d["source"] = self.source
        return d


def _expect(d: Any, kind: str) -> None:
    if not isinstance(d, dict) or d.get("format") != FORMAT or d.get("kind") != kind:
        raise ValueError(f"not a {FORMAT} {kind!r} document")


def from_dict(d: dict[str, Any]) -> TargetList | OffsetPattern:
    """A :class:`TargetList` or :class:`OffsetPattern` from its JSON form."""
    if isinstance(d, dict) and d.get("kind") == "offsets":
        return OffsetPattern.from_dict(d)
    if isinstance(d, dict) and d.get("kind") == "private":
        raise ValueError("a sealed private catalog has no targets to export")
    return TargetList.from_dict(d)


def _ident(text: str) -> str:
    """A request id fragment: anything outside [A-Za-z0-9._+-] becomes '_'."""
    return re.sub(r"[^A-Za-z0-9._+-]", "_", text) or "_"


def to_requests(
    tl: TargetList,
    *,
    telescope: str = DEFAULT_TELESCOPE,
    instrument: str = DEFAULT_INSTRUMENT,
    exposure_time: float = DEFAULT_EXPOSURE_TIME,
    exposure_count: int = 1,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    """One request group (JSON, the request language) per target of ``tl``.

    Each is a one-shot ``EXPOSE`` on ``telescope`` with ``instrument``,
    guided, with the target in ICRS (precessed if its equinox is not 2000)
    and its proper motion and epoch. ``proposal`` is the program's name (or
    the list's, without a ``!Program:`` header); the P.I., the catalog and
    line and the line's comment go in the request's ``observation_note``.
    Ids are ``<list>-<n>`` with ``n`` the legacy ``#n`` position, so they
    are unique within a list. A private list is refused unless
    ``include_private`` (robofast has no per-program visibility yet).
    """
    if tl.private and not include_private:
        raise ValueError(
            f"{tl.name}: a private list; requests would expose its coordinates "
            "(pass include_private to do it anyway)"
        )
    if not (exposure_time > 0 and math.isfinite(exposure_time)) or exposure_count < 1:
        raise ValueError("exposure_time must be > 0 and exposure_count >= 1")
    proposal = tl.program.name or tl.name
    groups = []
    for t in tl.targets:
        gid = f"{_ident(tl.name)}-{t.index}"
        ra, dec = t.icrs()
        target: dict[str, Any] = {"type": "ICRS", "name": t.name, "ra": ra, "dec": dec}
        if t.pm_ra is not None:
            target["proper_motion_ra"] = t.pm_ra
            target["proper_motion_dec"] = t.pm_dec
        target["epoch"] = t.equinox
        note = [f"catalog {tl.name} #{t.index}" + (f" (line {t.line})" if t.line else "")]
        if tl.program.pi:
            note.append(f"P.I.: {tl.program.pi}")
        if tl.program.name:
            note.append(f"Program: {tl.program.name}")
        if t.comment:
            note.append(t.comment)
        groups.append(
            {
                "id": gid,
                "name": t.name,
                "proposal": proposal,
                "ipp_value": 1.0,
                "operator": "SINGLE",
                "observation_type": "NORMAL",
                "requests": [
                    {
                        "id": f"{gid}/r",
                        "location": {"telescope": telescope},
                        "observation_note": "; ".join(note),
                        "configurations": [
                            {
                                "id": f"{gid}/c",
                                "type": "EXPOSE",
                                "instrument_type": instrument,
                                "target": target,
                                "guiding_config": {"mode": "ON"},
                                "instrument_configs": [
                                    {
                                        "exposure_time": float(exposure_time),
                                        "exposure_count": int(exposure_count),
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    return groups
