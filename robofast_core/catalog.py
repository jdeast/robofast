"""Where the stars are: cone queries on a star catalog, offline-safe.

``docs/sky.md`` has the whole story (sources, licences, cache layout). In
short, :class:`Catalog` answers ``cone(ra, dec, radius, epoch=...)`` with a
:class:`Stars` table (RA, Dec at the requested epoch, proper motion, Gaia
G, BP-RP and an estimated V) from, in order:

1. **Gaia DR3 tiles** (:class:`TileCache`): Gaia DR3 down to ``G < 16``
   (configurable), cut into HEALPix tiles (nested scheme, ``level`` 7 by
   default: 196 608 tiles of 0.21 deg2), one ``.npy`` file each under a
   configurable directory. A missing tile is fetched from the Gaia archive
   (:class:`GaiaArchive`) when online and kept; once cached the query needs
   no network. Gaia ``source_id`` encodes the level-12 nested HEALPix
   index of the star (``source_id >> 35``), so a tile is a ``source_id``
   range query.
2. **The bright stars** (:func:`bright_stars`): every Gaia DR3 star
   brighter than ``G = 6.5`` (``bright_stars.npy``: in the cache directory,
   else downloaded once from Zenodo, :mod:`robofast_core.zenodo`; Gaia
   data are CC BY-NC 3.0 IGO, so not shipped in the BSD repository), for
   offline use and pointing models; and the brightest stars Gaia lacks
   (from Hipparcos via a separate list, when present).
3. Optionally a **live service** (:class:`VizierSource`, needs astroquery)
   for a field nothing cached covers.

Positions are ICRS. Gaia's are at epoch J2016.0; :func:`propagate` moves
them along their proper motion (on the sphere, so it is right near the
poles too; parallax and radial velocity are ignored, which is sub-mas over
decades for all but the nearest stars).

Everything here is numpy; astropy is only used by the optional services.
Nothing touches the network unless a tile is missing *and* a fetcher is
configured (``TileCache(fetch=None)`` is offline).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

__all__ = [
    "GAIA_EPOCH",
    "TILE_DTYPE",
    "WDS_DTYPE",
    "Catalog",
    "CatalogUnavailable",
    "DoubleStars",
    "GaiaArchive",
    "Stars",
    "TileCache",
    "VizierSource",
    "ang2pix",
    "bright_stars",
    "catalog_at",
    "catalog_from_config",
    "decimal_year",
    "g_to_v",
    "parse_wds",
    "pix2ang",
    "propagate",
    "separation",
    "tiles_in_cone",
]

#: Gaia DR3's reference epoch (Julian year).
GAIA_EPOCH = 2016.0
#: One star in a tile file.
TILE_DTYPE = np.dtype(
    [
        ("source_id", "<i8"),
        ("ra", "<f8"),
        ("dec", "<f8"),
        ("pmra", "<f4"),
        ("pmdec", "<f4"),
        ("g", "<f4"),
        ("bp_rp", "<f4"),
    ]
)
_MAS = math.pi / (180.0 * 3600.0 * 1000.0)


class CatalogUnavailable(RuntimeError):
    """The catalog cannot answer (tiles missing and no way to fetch them)."""


# -- time -------------------------------------------------------------------------


def decimal_year(when: datetime | float | None) -> float | None:
    """A Julian epoch (``2026.83``) from a datetime (naive: UTC) or a number."""
    if when is None or isinstance(when, int | float):
        return None if when is None else float(when)
    # J2000.0 = 2000-01-01T12:00 TT; the TT-UTC difference (69 s) is
    # irrelevant at proper-motion precision.
    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=when.tzinfo)
    return 2000.0 + (when - j2000).total_seconds() / (365.25 * 86400.0)


# -- geometry ---------------------------------------------------------------------


def _vec(ra: np.ndarray, dec: np.ndarray) -> np.ndarray:
    ra, dec = np.radians(ra), np.radians(dec)
    cd = np.cos(dec)
    return np.stack([cd * np.cos(ra), cd * np.sin(ra), np.sin(dec)], axis=-1)


def separation(ra1: Any, dec1: Any, ra2: Any, dec2: Any) -> np.ndarray:
    """Angular distance (deg), Vincenty's formula (exact at any distance)."""
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(v, dtype=float)) for v in (ra1, dec1, ra2, dec2))
    dra = ra2 - ra1
    s1, c1, s2, c2 = np.sin(dec1), np.cos(dec1), np.sin(dec2), np.cos(dec2)
    num = np.hypot(c2 * np.sin(dra), c1 * s2 - s1 * c2 * np.cos(dra))
    return np.degrees(np.arctan2(num, s1 * s2 + c1 * c2 * np.cos(dra)))


def propagate(
    ra: Any, dec: Any, pmra: Any, pmdec: Any, years: float
) -> tuple[np.ndarray, np.ndarray]:
    """Move stars ``years`` along their proper motions (``pmra`` includes the
    cos(dec) factor, as Gaia's; mas/yr). Linear on the sphere: the position
    vector plus ``years`` times the tangential velocity, renormalised."""
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    pmra = np.nan_to_num(np.asarray(pmra, dtype=float))
    pmdec = np.nan_to_num(np.asarray(pmdec, dtype=float))
    if years == 0:
        return ra.copy(), dec.copy()
    a, d = np.radians(ra), np.radians(dec)
    p = _vec(ra, dec)
    east = np.stack([-np.sin(a), np.cos(a), np.zeros_like(a)], axis=-1)
    north = np.stack([-np.sin(d) * np.cos(a), -np.sin(d) * np.sin(a), np.cos(d)], axis=-1)
    v = (pmra[..., None] * east + pmdec[..., None] * north) * (_MAS * years)
    q = p + v
    q /= np.linalg.norm(q, axis=-1, keepdims=True)
    return (
        np.degrees(np.arctan2(q[..., 1], q[..., 0])) % 360.0,
        np.degrees(np.arcsin(np.clip(q[..., 2], -1.0, 1.0))),
    )


def g_to_v(g: Any, bp_rp: Any) -> np.ndarray:
    """Johnson V from Gaia G and BP-RP (Riello et al. 2021, table C.2:
    ``G - V = -0.02704 + 0.01424 c - 0.2156 c^2 + 0.01426 c^3``, valid for
    -0.5 < BP-RP < 2.75; outside it the colour is clipped, and an unknown
    colour is taken as 0.8, a typical field star)."""
    c = np.clip(np.nan_to_num(np.asarray(bp_rp, dtype=float), nan=0.8), -0.5, 2.75)
    return np.asarray(g, dtype=float) - (-0.02704 + 0.01424 * c - 0.2156 * c**2 + 0.01426 * c**3)


# -- HEALPix (nested), enough for tiles -------------------------------------------

_JRLL = np.array([2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4])
_JPLL = np.array([1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7])


def _spread(v: np.ndarray) -> np.ndarray:
    """Interleave zeros between the bits of ``v`` (< 2**29)."""
    v = v.astype(np.int64)
    out = np.zeros_like(v)
    for b in range(30):
        out |= ((v >> b) & 1) << (2 * b)
    return out


def _compress(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.int64)
    out = np.zeros_like(v)
    for b in range(30):
        out |= ((v >> (2 * b)) & 1) << b
    return out


def ang2pix(level: int, ra: Any, dec: Any) -> np.ndarray:
    """Nested HEALPix index at ``level`` (nside = 2**level) of (ra, dec) deg."""
    nside = 1 << level
    ra = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    z = np.sin(np.radians(dec))
    za = np.abs(z)
    tt = (np.radians(ra) % (2 * math.pi)) / (math.pi / 2)  # [0, 4)
    tt = np.where(tt >= 4.0, 0.0, tt)
    face = np.empty(ra.shape, dtype=np.int64)
    ix = np.empty(ra.shape, dtype=np.int64)
    iy = np.empty(ra.shape, dtype=np.int64)
    eq = za <= 2.0 / 3.0
    if eq.any():
        t1 = nside * (0.5 + tt[eq])
        t2 = nside * z[eq] * 0.75
        jp = (t1 - t2).astype(np.int64)
        jm = (t1 + t2).astype(np.int64)
        ifp, ifm = jp >> level, jm >> level
        face[eq] = np.where(ifp == ifm, ifp | 4, np.where(ifp < ifm, ifp, ifm + 8))
        ix[eq] = jm & (nside - 1)
        iy[eq] = nside - (jp & (nside - 1)) - 1
    po = ~eq
    if po.any():
        ntt = np.minimum(3, tt[po].astype(np.int64))
        tp = tt[po] - ntt
        # sqrt(3 (1 - |z|)), computed from cos(dec) near the poles.
        cosd = np.cos(np.radians(dec[po]))
        tmp = nside * np.sqrt(3.0 * cosd**2 / (1.0 + za[po]))
        jp = np.minimum((tp * tmp).astype(np.int64), nside - 1)
        jm = np.minimum(((1.0 - tp) * tmp).astype(np.int64), nside - 1)
        north = z[po] >= 0
        face[po] = np.where(north, ntt, ntt + 8)
        ix[po] = np.where(north, nside - jm - 1, jp)
        iy[po] = np.where(north, nside - jp - 1, jm)
    return (face << (2 * level)) + _spread(ix) + (_spread(iy) << 1)


def pix2ang(level: int, pix: Any, dx: Any = 0.5, dy: Any = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """(ra, dec) deg of the point ``(dx, dy)`` (0..1 across the pixel; 0.5,
    0.5 is its centre) inside nested pixel ``pix`` at ``level``."""
    nside = 1 << level
    pix = np.atleast_1d(np.asarray(pix, dtype=np.int64))
    face = pix >> (2 * level)
    ipf = pix & ((1 << (2 * level)) - 1)
    x = (_compress(ipf) + np.asarray(dx, dtype=float)) / nside
    y = (_compress(ipf >> 1) + np.asarray(dy, dtype=float)) / nside
    jr = _JRLL[face] - x - y
    nr = np.where(jr < 1, jr, np.where(jr > 3, 4 - jr, 1.0))
    z = np.where(
        jr < 1, 1 - nr * nr / 3.0, np.where(jr > 3, nr * nr / 3.0 - 1, (2 - jr) * 2.0 / 3.0)
    )
    tmp = _JPLL[face] * nr + x - y
    tmp = np.where(tmp < 0, tmp + 8, np.where(tmp >= 8, tmp - 8, tmp))
    with np.errstate(divide="ignore", invalid="ignore"):
        phi = np.where(nr < 1e-15, 0.0, (math.pi / 4) * tmp / nr)
    return np.degrees(phi) % 360.0, np.degrees(np.arcsin(np.clip(z, -1, 1)))


def tiles_in_cone(level: int, ra: float, dec: float, radius: float) -> list[int]:
    """Every nested pixel at ``level`` that overlaps the cone (deg).

    Candidates are the pixels of points sampled over the cone grown by a
    pixel's size; a candidate is kept if the cone's centre is inside it or
    its boundary (sampled finely) comes within the radius, with a margin of
    the boundary sampling step (so a pixel is never missed; one that only
    nearly touches may be kept)."""
    nside = 1 << level
    pix_size = math.degrees(math.sqrt(math.pi / 3.0) / nside)  # mean side
    grow = 1.5 * pix_size
    step = pix_size / 3.0
    big = radius + grow
    n = max(2, math.ceil(2 * big / step) + 1)
    # A grid on the tangent plane at the centre, kept inside the grown cone.
    t = np.linspace(-big, big, n)
    xi, eta = np.meshgrid(np.radians(t), np.radians(t))
    keep = np.hypot(xi, eta) <= math.radians(big)
    ras, decs = _untan(ra, dec, xi[keep], eta[keep])
    cand = np.unique(np.concatenate([ang2pix(level, ras, decs), ang2pix(level, ra, dec)]))
    centre = int(ang2pix(level, ra, dec)[0])
    m = 16
    s = np.linspace(0.0, 1.0, m + 1)
    zero, one = np.zeros_like(s), np.ones_like(s)
    edge_dx = np.concatenate([s, one, s[::-1], zero])
    edge_dy = np.concatenate([zero, s, one, s[::-1]])
    out = []
    for p in cand.tolist():
        if p == centre:
            out.append(p)
            continue
        bra, bdec = pix2ang(level, np.full(edge_dx.shape, p), edge_dx, edge_dy)
        d = separation(ra, dec, bra, bdec)
        if d.min() <= radius + 1.2 * pix_size / m * 2:
            out.append(p)
    return sorted(out)


def _tan(ra0: float, dec0: float, ra: Any, dec: Any) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic projection about (ra0, dec0): standard coordinates (rad)."""
    a0, d0 = math.radians(ra0), math.radians(dec0)
    a, d = np.radians(np.asarray(ra, dtype=float)), np.radians(np.asarray(dec, dtype=float))
    cosc = math.sin(d0) * np.sin(d) + math.cos(d0) * np.cos(d) * np.cos(a - a0)
    xi = np.cos(d) * np.sin(a - a0) / cosc
    eta = (math.cos(d0) * np.sin(d) - math.sin(d0) * np.cos(d) * np.cos(a - a0)) / cosc
    return xi, eta


def _untan(ra0: float, dec0: float, xi: Any, eta: Any) -> tuple[np.ndarray, np.ndarray]:
    a0, d0 = math.radians(ra0), math.radians(dec0)
    xi, eta = np.asarray(xi, dtype=float), np.asarray(eta, dtype=float)
    rho = np.hypot(xi, eta)
    c = np.arctan(rho)
    with np.errstate(invalid="ignore", divide="ignore"):
        dec = np.arcsin(
            np.cos(c) * math.sin(d0) + np.where(rho > 0, eta * np.sin(c) * math.cos(d0) / rho, 0.0)
        )
    ra = a0 + np.arctan2(
        xi * np.sin(c), rho * math.cos(d0) * np.cos(c) - eta * math.sin(d0) * np.sin(c)
    )
    return np.degrees(ra) % 360.0, np.degrees(dec)


# -- the table --------------------------------------------------------------------


@dataclass
class Stars:
    """A table of stars (columns are numpy arrays of equal length).

    ``ra``/``dec`` deg ICRS at ``epoch``; ``pmra`` (with cos dec) and
    ``pmdec`` mas/yr; ``g`` Gaia G (or the source's own magnitude, see
    ``source``); ``bp_rp`` (NaN when unknown); ``source_id`` (Gaia's, or
    negative for stars from elsewhere); ``source`` names where each row came
    from (``"gaia"``, ``"bright"``, ...)."""

    ra: np.ndarray
    dec: np.ndarray
    pmra: np.ndarray
    pmdec: np.ndarray
    g: np.ndarray
    bp_rp: np.ndarray
    source_id: np.ndarray
    epoch: float = GAIA_EPOCH
    source: np.ndarray = field(default_factory=lambda: np.array([], dtype="<U8"))

    def __post_init__(self) -> None:
        n = len(self.ra)
        if self.source.shape != (n,):
            self.source = np.full(n, "gaia", dtype="<U8")

    @classmethod
    def empty(cls, epoch: float = GAIA_EPOCH) -> Stars:
        return cls.from_records(np.zeros(0, TILE_DTYPE), epoch=epoch)

    @classmethod
    def from_records(
        cls, rec: np.ndarray, epoch: float = GAIA_EPOCH, source: str = "gaia"
    ) -> Stars:
        return cls(
            np.asarray(rec["ra"], dtype=float),
            np.asarray(rec["dec"], dtype=float),
            np.nan_to_num(np.asarray(rec["pmra"], dtype=float)),
            np.nan_to_num(np.asarray(rec["pmdec"], dtype=float)),
            np.asarray(rec["g"], dtype=float),
            np.asarray(rec["bp_rp"], dtype=float),
            np.asarray(rec["source_id"], dtype=np.int64),
            epoch,
            np.full(len(rec), source, dtype="<U8"),
        )

    def __len__(self) -> int:
        return len(self.ra)

    @property
    def v(self) -> np.ndarray:
        """Estimated Johnson V (:func:`g_to_v`)."""
        return g_to_v(self.g, self.bp_rp)

    def take(self, idx: Any) -> Stars:
        return Stars(
            self.ra[idx],
            self.dec[idx],
            self.pmra[idx],
            self.pmdec[idx],
            self.g[idx],
            self.bp_rp[idx],
            self.source_id[idx],
            self.epoch,
            self.source[idx],
        )

    def at_epoch(self, epoch: float | datetime | None) -> Stars:
        """The same stars moved to ``epoch`` (None: unchanged)."""
        ep = decimal_year(epoch)
        if ep is None or ep == self.epoch:
            return self
        ra, dec = propagate(self.ra, self.dec, self.pmra, self.pmdec, ep - self.epoch)
        out = self.take(slice(None))
        out.ra, out.dec, out.epoch = ra, dec, ep
        return out

    def cone(self, ra: float, dec: float, radius: float) -> Stars:
        return self.take(separation(ra, dec, self.ra, self.dec) <= radius)

    def brighter(self, mag: float | None) -> Stars:
        return self if mag is None else self.take(self.g < mag)

    def sorted(self) -> Stars:
        """Brightest first."""
        return self.take(np.argsort(self.g, kind="stable"))

    @staticmethod
    def concat(parts: Sequence[Stars]) -> Stars:
        parts = [p for p in parts if len(p)]
        if not parts:
            return Stars.empty()
        ep = parts[0].epoch
        parts = [p.at_epoch(ep) for p in parts]
        cols = ("ra", "dec", "pmra", "pmdec", "g", "bp_rp", "source_id", "source")
        vals = {c: np.concatenate([getattr(p, c) for p in parts]) for c in cols}
        return Stars(epoch=ep, **vals)

    def to_records(self) -> np.ndarray:
        rec = np.zeros(len(self), TILE_DTYPE)
        for c in TILE_DTYPE.names:
            rec[c] = getattr(self, c)
        return rec


# -- Gaia tiles -------------------------------------------------------------------


def source_id_range(level: int, pix: int) -> tuple[int, int]:
    """The Gaia ``source_id`` range of nested pixel ``pix`` at ``level``
    (``source_id >> 35`` is the level-12 nested index)."""
    shift = 35 + 2 * (12 - level)
    return pix << shift, ((pix + 1) << shift) - 1


class GaiaArchive:
    """Fetches a tile from the ESA Gaia archive's TAP service (synchronous
    ADQL, a ``source_id`` range: the table is clustered on it)."""

    URL = "https://gea.esac.esa.int/tap-server/tap/sync"

    def __init__(self, url: str | None = None, timeout: float = 600.0) -> None:
        self.url = url or self.URL
        self.timeout = timeout

    def query(self, adql: str) -> np.ndarray:
        import httpx

        r = httpx.post(
            self.url,
            data={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return parse_csv(r.text)

    def __call__(self, level: int, pix: int, mag_limit: float) -> np.ndarray:
        lo, hi = source_id_range(level, pix)
        return self.query(
            "SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag AS g, bp_rp "
            f"FROM gaiadr3.gaia_source WHERE source_id BETWEEN {lo} AND {hi} "
            f"AND phot_g_mean_mag < {mag_limit:g}"
        )


def parse_csv(text: str) -> np.ndarray:
    """A Gaia-archive CSV (header ``source_id,ra,dec,pmra,pmdec,g,bp_rp``,
    empty fields for nulls) as :data:`TILE_DTYPE` records."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return np.zeros(0, TILE_DTYPE)
    head = [h.strip().strip('"').lower() for h in lines[0].split(",")]
    alias = {"phot_g_mean_mag": "g"}
    head = [alias.get(h, h) for h in head]
    missing = set(TILE_DTYPE.names) - set(head)
    if missing:
        raise ValueError(f"catalog CSV: missing columns {sorted(missing)}")
    rec = np.zeros(len(lines) - 1, TILE_DTYPE)
    cols = {name: head.index(name) for name in TILE_DTYPE.names}
    for i, ln in enumerate(lines[1:]):
        f = ln.split(",")
        for name, j in cols.items():
            v = f[j].strip()
            if name == "source_id":
                rec[name][i] = int(v)
            else:
                rec[name][i] = float(v) if v else np.nan
    return rec


class TileCache:
    """Gaia DR3 HEALPix tiles on disk (module doc; layout in docs/sky.md)::

        <root>/gaia-dr3-g<mag>-hpx<level>/manifest.json
        <root>/gaia-dr3-g<mag>-hpx<level>/<pix // 1024:03d>/<pix:06d>.npy

    ``fetch(level, pix, mag_limit)`` returns a missing tile's records
    (:class:`GaiaArchive`); None: offline, a missing tile raises
    :class:`CatalogUnavailable`. Tiles are written atomically, so
    concurrent readers and writers are safe."""

    def __init__(
        self,
        root: str | Path,
        *,
        level: int = 7,
        mag_limit: float = 16.0,
        fetch: Callable[[int, int, float], np.ndarray] | None = None,
    ) -> None:
        self.root = Path(root)
        self.level = level
        self.mag_limit = mag_limit
        self.fetch = fetch
        self._lock = threading.Lock()
        self._mem: dict[int, np.ndarray] = {}

    @property
    def directory(self) -> Path:
        return self.root / f"gaia-dr3-g{self.mag_limit:g}-hpx{self.level}"

    def path(self, pix: int) -> Path:
        return self.directory / f"{pix // 1024:03d}" / f"{pix:06d}.npy"

    def have(self, pix: int) -> bool:
        return pix in self._mem or self.path(pix).is_file()

    def tile(self, pix: int) -> np.ndarray:
        rec = self._mem.get(pix)
        if rec is not None:
            return rec
        p = self.path(pix)
        if p.is_file():
            rec = np.load(p, allow_pickle=False)
        elif self.fetch is None:
            raise CatalogUnavailable(f"tile {pix} (level {self.level}) not cached at {p}")
        else:
            log.info(
                "catalog: fetching tile %d (level %d, G < %g)", pix, self.level, self.mag_limit
            )
            rec = np.asarray(self.fetch(self.level, pix, self.mag_limit)).astype(TILE_DTYPE)
            self.save(pix, rec)
        with self._lock:
            if len(self._mem) > 512:
                self._mem.clear()
            self._mem[pix] = rec
        return rec

    def save(self, pix: int, rec: np.ndarray) -> Path:
        p = self.path(pix)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._write_manifest()
        tmp = p.with_name(f".{uuid.uuid4().hex}.tmp.npy")
        np.save(tmp, np.asarray(rec, dtype=TILE_DTYPE), allow_pickle=False)
        os.replace(tmp, p)
        return p

    def _write_manifest(self) -> None:
        m = self.directory / "manifest.json"
        if m.exists():
            return
        m.parent.mkdir(parents=True, exist_ok=True)
        tmp = m.with_name(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "catalog": "Gaia DR3 (gaiadr3.gaia_source)",
                    "licence": "CC BY-NC 3.0 IGO; credit ESA/Gaia/DPAC (docs/sky.md)",
                    "epoch": GAIA_EPOCH,
                    "healpix": {"scheme": "nested", "level": self.level},
                    "mag_limit": {"band": "G", "value": self.mag_limit},
                    "columns": {n: str(TILE_DTYPE[n]) for n in TILE_DTYPE.names},
                },
                indent=1,
            )
        )
        os.replace(tmp, m)

    def cone(self, ra: float, dec: float, radius: float) -> Stars:
        """Stars within ``radius`` deg (at epoch J2016; :class:`Catalog`
        widens the cone for proper motion)."""
        tiles = tiles_in_cone(self.level, ra, dec, radius)
        recs = [self.tile(p) for p in tiles]
        rec = np.concatenate(recs) if recs else np.zeros(0, TILE_DTYPE)
        return Stars.from_records(rec).cone(ra, dec, radius)

    def missing(self, ra: float, dec: float, radius: float) -> list[int]:
        return [p for p in tiles_in_cone(self.level, ra, dec, radius) if not self.have(p)]


# -- the bright stars ---------------------------------------------------------------

_BRIGHT: Stars | None = None
#: The bright-star file's name, in a cache directory and in the Zenodo record.
BRIGHT_FILE = "bright_stars.npy"


def bright_stars(path: str | Path | None = None) -> Stars:
    """The bright-star catalog (module doc), brightest first.

    ``path``: a file of :data:`TILE_DTYPE` records. Default: the Gaia DR3
    extracts' ``bright_stars.npy``, downloaded once from Zenodo into
    :func:`robofast_core.zenodo.data_dir` (docs/sky.md). Raises
    :class:`CatalogUnavailable` if it is not there and cannot be fetched."""
    global _BRIGHT
    if path is None and _BRIGHT is not None:
        return _BRIGHT
    if path is None:
        from robofast_core import zenodo, zenodo_assets

        try:
            p = zenodo.fetch_file(zenodo_assets.GAIA_EXTRACTS, BRIGHT_FILE)
        except zenodo.AssetUnavailable as exc:
            raise CatalogUnavailable(f"bright stars: {exc}") from exc
    else:
        p = Path(path)
    rec = np.load(p, allow_pickle=False)
    stars = Stars.from_records(rec, source="bright").sorted()
    if path is None:
        _BRIGHT = stars
    return stars


_WARNED: set[str] = set()


def _default_bright(root: Path | None = None) -> Stars | None:
    """``root/bright_stars.npy`` if present, else :func:`bright_stars`; None
    (with one warning per reason) when neither is available: Gaia's own
    tiles still answer, only ``bright_only`` queries and the few stars Gaia
    lacks are lost."""
    if root is not None and (root / BRIGHT_FILE).is_file():
        return bright_stars(root / BRIGHT_FILE)
    try:
        return bright_stars()
    except CatalogUnavailable as exc:
        if str(exc) not in _WARNED:
            _WARNED.add(str(exc))
            log.warning("catalog: no bright-star list (%s)", exc)
        return None


# -- a live service (optional) ------------------------------------------------------


class VizierSource:
    """Gaia DR3 from VizieR (``I/355/gaiadr3``) through astroquery, for a
    field no cache covers. Optional: astroquery is not a robofast
    dependency, and the query needs the network."""

    def __init__(self, catalog: str = "I/355/gaiadr3", row_limit: int = 20000) -> None:
        self.catalog = catalog
        self.row_limit = row_limit

    def cone(self, ra: float, dec: float, radius: float, mag_limit: float) -> Stars:
        try:
            from astropy import units as u
            from astropy.coordinates import SkyCoord
            from astroquery.vizier import Vizier
        except ImportError as exc:  # pragma: no cover - optional
            raise CatalogUnavailable("astroquery is not installed") from exc
        v = Vizier(
            columns=["Source", "RA_ICRS", "DE_ICRS", "pmRA", "pmDE", "Gmag", "BP-RP"],
            column_filters={"Gmag": f"<{mag_limit:g}"},
            row_limit=self.row_limit,
        )
        res = v.query_region(
            SkyCoord(ra * u.deg, dec * u.deg), radius=radius * u.deg, catalog=self.catalog
        )
        if not res:
            return Stars.empty()
        t = res[0]
        rec = np.zeros(len(t), TILE_DTYPE)
        for ours, theirs in (
            ("source_id", "Source"),
            ("ra", "RA_ICRS"),
            ("dec", "DE_ICRS"),
            ("pmra", "pmRA"),
            ("pmdec", "pmDE"),
            ("g", "Gmag"),
            ("bp_rp", "BP-RP"),
        ):
            rec[ours] = np.ma.filled(np.ma.asarray(t[theirs], dtype=float), np.nan)
        return Stars.from_records(rec, source="vizier")


# -- the facade ---------------------------------------------------------------------


class Catalog:
    """Cone queries over the sources in order (module doc).

    ``tiles``: a :class:`TileCache` (or None); ``bright``: the bright-star
    table (default: :func:`bright_stars`, or none, with a warning, if it is
    not available; None: none); ``live``: an optional
    :class:`VizierSource`. ``bright_limit``: bright-catalog stars brighter
    than this (G) are added where Gaia has no star within
    ``bright_match`` arcsec (Gaia is incomplete for the brightest stars)."""

    def __init__(
        self,
        tiles: TileCache | None = None,
        *,
        bright: Stars | str | None = "default",
        live: VizierSource | None = None,
        bright_limit: float = 6.5,
        bright_match: float = 3.0,
        max_pm: float = 3000.0,
        doubles: DoubleStars | None = None,
    ) -> None:
        self.tiles = tiles
        self.bright = _default_bright() if isinstance(bright, str) else bright
        self.live = live
        #: The WDS (for target vetting), when cached.
        self.doubles = doubles
        self.bright_limit = bright_limit
        self.bright_match = bright_match
        #: mas/yr: the cone is widened by this times the epoch difference,
        #: so a fast star that moved into the field is found.
        self.max_pm = max_pm

    @property
    def mag_limit(self) -> float:
        return self.tiles.mag_limit if self.tiles is not None else self.bright_limit

    def cone(
        self,
        ra: float,
        dec: float,
        radius: float,
        *,
        epoch: float | datetime | None = None,
        mag_limit: float | None = None,
        bright_only: bool = False,
    ) -> Stars:
        """Stars within ``radius`` deg of (ra, dec) at ``epoch`` (default:
        the catalog's, J2016.0), brighter than ``mag_limit`` (G), brightest
        first. Raises :class:`CatalogUnavailable` if no source covers the
        field (``bright_only``: the bright stars alone, which need no tile
        and no network once ``bright_stars.npy`` is in place)."""
        ep = decimal_year(epoch)
        widen = 0.0 if ep is None else abs(ep - GAIA_EPOCH) * self.max_pm / 3.6e6
        r0 = radius + widen
        parts: list[Stars] = []
        gaia: Stars | None = None
        if not bright_only:
            errors: list[str] = []
            if self.tiles is not None:
                try:
                    gaia = self.tiles.cone(ra, dec, r0)
                except (CatalogUnavailable, OSError) as exc:
                    errors.append(str(exc))
            if gaia is None and self.live is not None:
                try:
                    gaia = self.live.cone(ra, dec, r0, self.mag_limit)
                except Exception as exc:
                    errors.append(f"live: {exc!r}")
            if gaia is None:
                raise CatalogUnavailable("; ".join(errors) or "no catalog source configured")
            parts.append(gaia)
        if bright_only and self.bright is None:
            raise CatalogUnavailable("bright_only: no bright-star list (docs/sky.md)")
        if self.bright is not None:
            b = self.bright.cone(ra, dec, r0)
            if gaia is not None and len(b):
                b = b.take(b.g < self.bright_limit)
                if len(gaia) and len(b):
                    # Only the bright stars Gaia lacks.
                    near = np.array(
                        [
                            separation(r, d, gaia.ra, gaia.dec).min() * 3600.0 > self.bright_match
                            for r, d in zip(b.ra, b.dec, strict=True)
                        ]
                    )
                    b = b.take(near)
            parts.append(b)
        stars = Stars.concat(parts) if parts else Stars.empty()
        stars = stars.at_epoch(ep).cone(ra, dec, radius).brighter(mag_limit)
        return stars.sorted()


def catalog_from_config(cfg: dict[str, Any] | None, *, base: Path | None = None) -> Catalog:
    """A :class:`Catalog` from a ``catalog`` config entry (docs/sky.md)::

        {"cache_dir": "/var/lib/robofast/catalog", "level": 7, "mag_limit": 16,
         "fetch": true, "live": false}

    ``cache_dir`` defaults to ``$ROBOFAST_CATALOG_DIR``, else
    ``~/.cache/robofast/catalog``; ``fetch`` (default true) lets missing
    tiles be downloaded; ``live`` adds the VizieR service."""
    cfg = dict(cfg or {})
    cfg.pop("_note", None)
    known = {"cache_dir", "level", "mag_limit", "fetch", "live", "bright", "archive_url"}
    if set(cfg) - known:
        raise ValueError(f"catalog: unknown keys {sorted(set(cfg) - known)}")
    root = cfg.get("cache_dir") or os.environ.get("ROBOFAST_CATALOG_DIR")
    root_path = Path(root) if root else Path.home() / ".cache" / "robofast" / "catalog"
    if base is not None and not root_path.is_absolute():
        root_path = base / root_path
    return catalog_at(
        root_path,
        level=int(cfg.get("level", 7)),
        mag_limit=float(cfg.get("mag_limit", 16.0)),
        fetch=GaiaArchive(cfg.get("archive_url")) if cfg.get("fetch", True) else None,
        bright=bool(cfg.get("bright", True)),
        live=bool(cfg.get("live", False)),
    )


def catalog_at(
    root: str | Path,
    *,
    level: int = 7,
    mag_limit: float = 16.0,
    fetch: Callable[[int, int, float], np.ndarray] | None = None,
    bright: bool = True,
    live: bool = False,
) -> Catalog:
    """A :class:`Catalog` on the cache directory ``root``: its Gaia tiles,
    the bright stars (``root/bright_stars.npy`` if present, else
    :func:`bright_stars`'s download) plus ``root/hipparcos-not-in-gaia.npy``
    (the bright stars Gaia lacks) and ``root/wds.npy`` (double stars) when
    present (``scripts/build_catalog.py`` writes them)."""
    root = Path(root)
    tiles = TileCache(root, level=level, mag_limit=mag_limit, fetch=fetch)
    b: Stars | None = None
    if bright:
        b = _default_bright(root)
        hip = root / "hipparcos-not-in-gaia.npy"
        if hip.is_file() and b is None:
            b = bright_stars(hip)
        elif hip.is_file() and b is not None:
            h = bright_stars(hip)
            # Only those with no Gaia bright star within 3 arcsec.
            va, vb = _vec(h.ra, h.dec), _vec(b.ra, b.dec)
            near = np.max(va @ vb.T, axis=1) > math.cos(math.radians(3.0 / 3600.0))
            b = Stars.concat([b, h.take(~near)]).sorted()
    wds = root / "wds.npy"
    return Catalog(
        tiles,
        bright=b,
        live=VizierSource() if live else None,
        doubles=DoubleStars.load(wds) if wds.is_file() else None,
    )


def fill_cache(
    tiles: TileCache, fields: Iterable[tuple[float, float, float]], *, dry_run: bool = False
) -> list[int]:
    """Fetch every tile the (ra, dec, radius) ``fields`` need; returns the
    tiles that were missing."""
    todo: list[int] = []
    for ra, dec, r in fields:
        todo += [p for p in tiles.missing(ra, dec, r) if p not in todo]
    if not dry_run:
        for p in todo:
            tiles.tile(p)
    return todo


# -- double stars (WDS) ---------------------------------------------------------------

#: One pair from the Washington Double Star catalog (a cached, parsed copy).
WDS_DTYPE = np.dtype(
    [
        ("wds", "U10"),
        ("disc", "U7"),
        ("comp", "U5"),
        ("ra", "<f8"),
        ("dec", "<f8"),
        ("last", "<i2"),
        ("theta", "<f4"),
        ("rho", "<f4"),
        ("mag1", "<f4"),
        ("mag2", "<f4"),
    ]
)
WDS_URL = "https://www.astro.gsu.edu/wds/Webtextfiles/wdsweb_summ2.txt"


def _f(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return math.nan


def parse_wds(text: str) -> np.ndarray:
    """The WDS summary file (``wdsweb_summ2.txt``: fixed columns; the
    primary's "precise coordinates", J2000, ``hhmmss.ss+ddmmss.s``) as
    :data:`WDS_DTYPE` records. Pairs without precise coordinates are
    placed at the 10-character WDS identifier's (arcmin) position."""
    out = []
    for ln in text.splitlines():
        if len(ln) < 70 or not ln[:5].isdigit():
            continue
        coord = ln[112:130].strip()
        try:
            if len(coord) >= 17:
                h, m, s = float(coord[0:2]), float(coord[2:4]), float(coord[4:9])
                sign = -1.0 if coord[9] == "-" else 1.0
                dd, dm, ds = float(coord[10:12]), float(coord[12:14]), float(coord[14:18])
            else:
                h, m, s = float(ln[0:2]), float(ln[2:5]) / 10.0, 0.0
                sign = -1.0 if ln[5] == "-" else 1.0
                dd, dm, ds = float(ln[6:8]), float(ln[8:10]), 0.0
        except ValueError:
            continue
        ra = 15.0 * (h + m / 60.0 + s / 3600.0)
        dec = sign * (dd + dm / 60.0 + ds / 3600.0)
        last = ln[28:32].strip()
        out.append(
            (
                ln[0:10],
                ln[10:17].strip(),
                ln[17:22].strip(),
                ra,
                dec,
                int(last) if last.isdigit() else 0,
                _f(ln[42:45]),
                _f(ln[52:57]),
                _f(ln[58:63]),
                _f(ln[64:69]),
            )
        )
    return np.array(out, dtype=WDS_DTYPE)


class DoubleStars:
    """WDS pairs (module doc of :mod:`robofast_core.vetting`), from a parsed
    cache file ``<dir>/wds.npy`` (``scripts/build_catalog.py wds`` writes
    it). Gaia misses or mismeasures pairs closer than ~1-2 arcsec and the
    very bright, which is what the WDS adds."""

    def __init__(self, records: np.ndarray) -> None:
        self.rec = records

    @classmethod
    def load(cls, path: str | Path) -> DoubleStars:
        p = Path(path)
        if p.is_dir():
            p = p / "wds.npy"
        return cls(np.load(p, allow_pickle=False))

    def cone(self, ra: float, dec: float, radius: float) -> np.ndarray:
        r = self.rec
        if not len(r):
            return r
        near = np.abs(r["dec"] - dec) <= radius
        sub = r[near]
        return sub[separation(ra, dec, sub["ra"], sub["dec"]) <= radius]
