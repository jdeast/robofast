"""robofast's own pointing model: built from plate solves, applied before
the mount, watched for drift (docs/pointing_model.md).

:mod:`robofast_core.pointing` has the TPOINT terms and a least-squares fit.
This module turns that into a working pointing model for one telescope and
M3 port:

* **Samples** (:class:`Sample`): a mount position (what the mount was told
  and reports, in the coordinates the sequencer commands) and where the
  telescope really pointed (the plate-solved optical axis), with the local
  sidereal time. They come from a dedicated pointing run
  (``kind="pointing_model"``, a ``POINTING_MODEL`` request configuration:
  a grid over the sky, slew -> expose -> solve -> record) and,
  opportunistically, from every solved light frame. :class:`SampleLog`
  keeps them, one JSON-lines file per telescope and port.
* **Building** (:func:`build`): outliers clipped, then TPOINT terms chosen
  by significance (:func:`select_terms`: from the index and basic
  geometric terms, backward elimination of any whose coefficient is not
  ``significance`` sigma from zero, then forward selection of the other
  terms that are and lower the Bayesian information criterion), and the
  result **cross-validated** (k-fold rms of the held-out points).
  :class:`ModelStore` saves every model with its provenance (when, from
  how many samples of which kind, the term errors, rms before / after /
  cross-validated) and keeps the history.
* **Applying** (:class:`PointingCorrector`, per telescope): the commanded
  position is the model applied forwards to the target (so the telescope
  lands on it); the mount's reported position is inverted through the
  model for the FITS headers (``RA``/``DEC`` are where it really points,
  ``MNTRA``/``MNTDEC`` what the mount said). Optionally the vendor's own
  model is switched off first (an Alpaca ``Action``).
* **Rolling local correction** (:class:`LocalCorrection`, after LCO): the
  last acquisition's residual offset, decayed with sky distance and time,
  is applied to the next slew.
* **Drift** (:class:`DriftMonitor`): tonight's residuals against the
  current model; a median offset beyond the threshold raises an alarm
  once a night.

Everything here is pure numpy (plus the solver for a run); the sequencer
owns the timing (``robofast_core.sequencer``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from robofast_core.pointing import PointingModel, altaz_to_hadec, fit, hadec_to_altaz

log = logging.getLogger(__name__)

__all__ = [
    "BuiltModel",
    "DriftMonitor",
    "LocalCorrection",
    "ModelStore",
    "PointingCorrector",
    "Sample",
    "SampleLog",
    "build",
    "cross_validate",
    "select_terms",
    "sky_grid",
]

#: Always fitted: the index errors.
BASE_TERMS = {"equatorial": ["IH", "ID"], "altaz": ["IA", "IE"]}
#: The basic geometric terms a model starts from (TPOINT's usual set).
BASIC_TERMS = {
    "equatorial": ["CH", "NP", "MA", "ME", "TF"],
    "altaz": ["CA", "NPAE", "AN", "AW", "TF"],
}
#: Offered to the term selection.
CANDIDATE_TERMS = {
    "equatorial": ["CH", "NP", "MA", "ME", "TF", "FO", "DAF", "HCES", "HCEC", "DCES", "DCEC"],
    "altaz": ["CA", "NPAE", "AN", "AW", "TF", "TX", "ACES", "ACEC", "ECES", "ECEC"],
}
#: Sidereal degrees per solar second.
SIDEREAL_DEG_PER_S = 360.98564736629 / 86400.0


def _wrap180(x: Any) -> Any:
    return (np.asarray(x, dtype=float) + 180.0) % 360.0 - 180.0


def _iso(t: datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat()


def _parse_time(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# -- samples ------------------------------------------------------------------------


@dataclass
class Sample:
    """One pointing measurement. ``mount_*``: the mount's position (deg) in
    the coordinates the sequencer commands (with robofast's model on, the
    *commanded* position, not the target); ``true_*``: the plate-solved
    optical axis (ICRS deg); ``lst`` deg at mid-exposure."""

    when: str
    telescope: str
    port: str | None
    lst: float
    mount_ra: float
    mount_dec: float
    true_ra: float
    true_dec: float
    #: ``"run"`` (a pointing run), ``"frame"`` (a solved light frame).
    source: str = "frame"
    rms: float | None = None
    n_matched: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Sample:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def time(self) -> datetime:
        return _parse_time(self.when)

    @property
    def error_arcsec(self) -> float:
        """How far the telescope was from where the mount said (arcsec)."""
        return _sep(self.mount_ra, self.mount_dec, self.true_ra, self.true_dec) * 3600.0


def solve_sample(
    image: np.ndarray,
    corrector: PointingCorrector,
    mount_ra: float,
    mount_dec: float,
    lst: float,
    when: datetime,
    port: str | None,
    binning: int = 1,
) -> Sample:
    """Plate-solve a pointing-run image (FITS order) near the mount's
    position with the corrector's solver and catalog: a ``run`` sample
    (the optical axis is the image centre)."""
    from robofast_core.astrometry import solve_near

    sol = solve_near(
        image, corrector.catalog, mount_ra, mount_dec, corrector.solver, binning=binning, epoch=when
    )
    return Sample(
        _iso(when),
        corrector.telescope,
        port,
        lst % 360.0,
        mount_ra % 360.0,
        mount_dec,
        sol.ra % 360.0,
        sol.dec,
        "run",
        round(sol.rms, 3),
        sol.n_matched,
    )


def model_coords(
    samples: Sequence[Sample], mount: str, latitude: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(true1, true2, mount1, mount2) in the model's coordinates (deg): hour
    angle and dec, or azimuth and elevation."""
    lst = np.array([s.lst for s in samples], dtype=float)
    tha = _wrap180(lst - np.array([s.true_ra for s in samples], dtype=float))
    mha = _wrap180(lst - np.array([s.mount_ra for s in samples], dtype=float))
    tdec = np.array([s.true_dec for s in samples], dtype=float)
    mdec = np.array([s.mount_dec for s in samples], dtype=float)
    if mount == "altaz":
        ta, te = hadec_to_altaz(tha, tdec, latitude)
        ma, me = hadec_to_altaz(mha, mdec, latitude)
        return ta, te, ma, me
    return tha, tdec, mha, mdec


def sky_residuals(
    model: PointingModel, t1: np.ndarray, t2: np.ndarray, m1: np.ndarray, m2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Measured minus modelled (mount - true), on the sky (arcsec): the
    first coordinate times cos of the second."""
    d1, d2 = model.delta(t1, t2)
    r1 = _wrap180(np.asarray(m1) - np.asarray(t1)) * 3600.0 - d1
    r2 = (np.asarray(m2) - np.asarray(t2)) * 3600.0 - d2
    return r1 * np.cos(np.radians(t2)), r2


def _rms(r1: np.ndarray, r2: np.ndarray) -> float:
    n = max(1, len(r1))
    return math.sqrt(float(np.sum(r1 * r1 + r2 * r2)) / n)


# -- fitting --------------------------------------------------------------------------


def _fit(
    names: list[str], arrays: tuple, mount: str, latitude: float
) -> tuple[PointingModel, dict[str, Any]]:
    t1, t2, m1, m2 = arrays
    return fit(names, t1, t2, m1, m2, mount=mount, latitude=latitude)


def _bic(rms: float, n_points: int, k: int) -> float:
    n_obs = 2 * n_points
    return n_obs * math.log(max(rms * rms, 1e-12)) + k * math.log(n_obs)


def select_terms(
    arrays: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    *,
    mount: str = "equatorial",
    latitude: float = 0.0,
    base: Sequence[str] | None = None,
    candidates: Sequence[str] | None = None,
    significance: float = 4.0,
) -> list[str]:
    """Which TPOINT terms the data support (module doc). Like a TPOINT user:
    start from the ``base`` terms plus the basic geometric ones
    (:data:`BASIC_TERMS`), drop the least significant until every one left
    is ``significance`` sigma from zero (backward; a pair of terms that the
    sky coverage cannot tell apart loses one, then the other stands), then
    add any other candidate that is that significant and lowers the Bayesian
    information criterion (forward), then prune again. The base terms always
    stay. ``arrays``: :func:`model_coords`."""
    base = list(BASE_TERMS[mount] if base is None else base)
    pool = list(CANDIDATE_TERMS[mount] if candidates is None else candidates)
    n = len(arrays[0])

    def prune(names: list[str]) -> list[str]:
        names = list(names)
        while True:
            m, inf = _fit(names, arrays, mount, latitude)
            weak = [
                (abs(m.terms[c]) / inf["errors"][c] if inf["errors"][c] > 0 else 0.0, c)
                for c in names
                if c not in base
            ]
            weak = [w for w in weak if w[0] < significance]
            if not weak:
                return names
            names.remove(min(weak)[1])

    start = base + [c for c in BASIC_TERMS[mount] if c in pool and c not in base]
    if 2 * n < len(start) + 4:
        start = list(base)
    chosen = prune(start)
    rest = [c for c in pool if c not in chosen and c not in base]
    _, info = _fit(chosen, arrays, mount, latitude)
    bic = _bic(info["rms_after"], n, len(chosen))
    while rest and 2 * n > len(chosen) + 2:
        best: tuple[float, str] | None = None
        for c in rest:
            m, inf = _fit([*chosen, c], arrays, mount, latitude)
            err = inf["errors"][c]
            if not (err > 0 and abs(m.terms[c]) >= significance * err):
                continue
            if best is None or inf["rms_after"] < best[0]:
                best = (inf["rms_after"], c)
        if best is None:
            break
        new_bic = _bic(best[0], n, len(chosen) + 1)
        if new_bic >= bic:
            break
        bic = new_bic
        chosen.append(best[1])
        rest.remove(best[1])
    return prune(chosen)


def cross_validate(
    names: list[str],
    arrays: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    *,
    mount: str = "equatorial",
    latitude: float = 0.0,
    folds: int = 5,
    seed: int = 0,
) -> float:
    """k-fold cross-validated sky rms (arcsec): each point's residual from a
    model fitted without it."""
    t1, t2, m1, m2 = arrays
    n = len(t1)
    folds = max(2, min(folds, n))
    order = np.random.default_rng(seed).permutation(n)
    r1 = np.zeros(n)
    r2 = np.zeros(n)
    for k in range(folds):
        test = order[k::folds]
        train = np.setdiff1d(order, test)
        if len(train) <= len(names) // 2 + 1:
            return math.nan
        sub = tuple(a[train] for a in arrays)
        m, _ = _fit(names, sub, mount, latitude)
        a, b = sky_residuals(m, t1[test], t2[test], m1[test], m2[test])
        r1[test], r2[test] = a, b
    return _rms(r1, r2)


@dataclass
class BuiltModel:
    """A fitted model and its provenance (module doc)."""

    model: PointingModel
    telescope: str
    port: str | None
    #: Formal 1-sigma errors of the terms (arcsec).
    errors: dict[str, float]
    rms_before: float
    rms_after: float
    #: k-fold cross-validated rms (arcsec): the model's honest accuracy.
    cv_rms: float
    n: int
    n_rejected: int = 0
    built: str = ""
    #: Samples by source (``run``/``frame``) and their time span.
    sources: dict[str, int] = field(default_factory=dict)
    first: str | None = None
    last: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "model"}
        d["model"] = self.model.as_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BuiltModel:
        d = dict(d)
        model = PointingModel.from_dict(d.pop("model"))
        known = set(cls.__dataclass_fields__)
        return cls(model=model, **{k: v for k, v in d.items() if k in known})

    def summary(self) -> str:
        terms = ", ".join(
            f"{k} {v:+.1f}+-{self.errors.get(k, 0):.1f}" for k, v in self.model.terms.items()
        )
        return (
            f"{self.telescope} port {self.port}: {self.n} points ({self.n_rejected} rejected), "
            f"rms {self.rms_before:.1f} -> {self.rms_after:.2f} arcsec "
            f"(cross-validated {self.cv_rms:.2f}); {terms}"
        )


def build(
    samples: Sequence[Sample],
    *,
    mount: str = "equatorial",
    latitude: float,
    telescope: str = "",
    port: str | None = None,
    terms: Sequence[str] | None = None,
    candidates: Sequence[str] | None = None,
    significance: float = 4.0,
    clip: float = 5.0,
    folds: int = 5,
    min_points: int = 8,
    when: datetime | None = None,
) -> BuiltModel:
    """A model from ``samples`` (module doc). ``terms`` fixes the terms
    (no selection). Points more than ``clip`` robust sigma (and 2 arcsec)
    from a fit with every candidate term are rejected first."""
    samples = list(samples)
    if len(samples) < min_points:
        raise ValueError(f"{len(samples)} pointing samples; at least {min_points} needed")
    arrays = model_coords(samples, mount, latitude)
    keep = np.ones(len(samples), dtype=bool)
    full = list(BASE_TERMS[mount]) + [
        c
        for c in (CANDIDATE_TERMS[mount] if candidates is None else candidates)
        if c not in BASE_TERMS[mount]
    ]
    if terms is not None:
        full = list(terms)
    for _ in range(3):
        sub = tuple(a[keep] for a in arrays)
        names = full if 2 * int(keep.sum()) > 2 * len(full) else list(BASE_TERMS[mount])
        m, _ = _fit(names, sub, mount, latitude)
        r1, r2 = sky_residuals(m, *arrays)
        r = np.hypot(r1, r2)
        sigma = 1.4826 * float(np.median(r[keep])) / 1.1774  # Rayleigh median -> sigma
        new = r <= max(clip * sigma, 2.0)
        if np.array_equal(new, keep) or new.sum() < min_points:
            break
        keep = new
    sub = tuple(a[keep] for a in arrays)
    if terms is None:
        names = select_terms(
            sub,
            mount=mount,
            latitude=latitude,
            candidates=candidates,
            significance=significance,
        )
    else:
        names = list(terms)
    model, info = _fit(names, sub, mount, latitude)
    cv = cross_validate(names, sub, mount=mount, latitude=latitude, folds=folds)
    kept = [s for s, k in zip(samples, keep, strict=True) if k]
    sources: dict[str, int] = {}
    for s in kept:
        sources[s.source] = sources.get(s.source, 0) + 1
    times = sorted(s.when for s in kept)
    return BuiltModel(
        model=model,
        telescope=telescope,
        port=port,
        errors={k: round(v, 3) for k, v in info["errors"].items()},
        rms_before=round(info["rms_before"], 3),
        rms_after=round(info["rms_after"], 3),
        cv_rms=round(cv, 3) if math.isfinite(cv) else cv,
        n=int(keep.sum()),
        n_rejected=int((~keep).sum()),
        built=_iso(when or datetime.now(UTC)),
        sources=sources,
        first=times[0] if times else None,
        last=times[-1] if times else None,
    )


# -- persistence ------------------------------------------------------------------------


def _port_dir(port: str | None) -> str:
    return "default" if port in (None, "") else "".join(c if c.isalnum() else "_" for c in port)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ModelStore:
    """Built models on disk: ``<root>/<telescope>/<port>/current.json`` and
    every model ever saved as ``model-<UTC time>.json`` beside it."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, telescope: str, port: str | None) -> Path:
        return self.root / telescope / _port_dir(port)

    def save(self, built: BuiltModel) -> Path:
        d = self._dir(built.telescope, built.port)
        stamp = _parse_time(built.built or _iso(datetime.now(UTC))).strftime("%Y%m%dT%H%M%S")
        text = json.dumps(built.as_dict(), indent=1)
        path = d / f"model-{stamp}.json"
        _atomic_write(path, text)
        _atomic_write(d / "current.json", text)
        return path

    def load(self, telescope: str, port: str | None) -> BuiltModel | None:
        path = self._dir(telescope, port) / "current.json"
        if not path.exists():
            return None
        return BuiltModel.from_dict(json.loads(path.read_text()))

    def history(self, telescope: str, port: str | None) -> list[BuiltModel]:
        d = self._dir(telescope, port)
        return [BuiltModel.from_dict(json.loads(p.read_text())) for p in sorted(d.glob("model-*"))]


class SampleLog:
    """Pointing samples, appended to ``<root>/<telescope>/<port>/samples.jsonl``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, telescope: str, port: str | None) -> Path:
        return self.root / telescope / _port_dir(port) / "samples.jsonl"

    def append(self, sample: Sample) -> None:
        p = self.path(sample.telescope, sample.port)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(sample.as_dict()) + "\n")

    def read(
        self,
        telescope: str,
        port: str | None,
        *,
        since: datetime | None = None,
        sources: Iterable[str] | None = None,
    ) -> list[Sample]:
        p = self.path(telescope, port)
        if not p.exists():
            return []
        want = set(sources) if sources is not None else None
        out = []
        for line in p.read_text().splitlines():
            try:
                s = Sample.from_dict(json.loads(line))
            except (ValueError, TypeError):
                continue  # a torn last line
            if want is not None and s.source not in want:
                continue
            if since is not None and s.time < since:
                continue
            out.append(s)
        return out


# -- the sky grid of a pointing run ---------------------------------------------------------


def sky_grid(
    n: int,
    *,
    min_alt: float = 25.0,
    max_alt: float = 85.0,
    latitude: float | None = None,
    avoid: Sequence[tuple[float, float, float]] = (),
    lst: float | None = None,
    max_hour_angle: float | None = None,
    min_dec: float | None = None,
    max_dec: float | None = None,
) -> list[tuple[float, float]]:
    """``n`` (azimuth, altitude) deg spread evenly over the sky between
    ``min_alt`` and ``max_alt`` (a Fibonacci lattice: equal area), in a
    slew-friendly order (azimuth bands, alternate directions). With
    ``latitude`` and ``lst`` (deg), points are dropped that fall within
    ``radius`` of an ``avoid`` (ra, dec, radius) position (the Moon), or
    outside an equatorial mount's hour-angle (hours) or dec limits; more
    are generated so ``n`` remain where possible."""
    out: list[tuple[float, float]] = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    z0, z1 = math.sin(math.radians(min_alt)), math.sin(math.radians(max_alt))
    for total in (n, 2 * n, 4 * n):
        pts = []
        for i in range(total):
            z = z0 + (z1 - z0) * (i + 0.5) / total
            az = math.degrees(i * golden) % 360.0
            alt = math.degrees(math.asin(z))
            if latitude is not None and lst is not None:
                ha, dec = altaz_to_hadec(az, alt, latitude)
                ha, dec = float(ha), float(dec)
                if max_hour_angle is not None and abs(ha) > max_hour_angle * 15.0:
                    continue
                if min_dec is not None and dec < min_dec:
                    continue
                if max_dec is not None and dec > max_dec:
                    continue
                ra = (lst - ha) % 360.0
                if any(_sep(ra, dec, a, d) < r for a, d, r in avoid):
                    continue
            pts.append((az, alt))
        out = pts
        if len(out) >= n:
            break
    if len(out) > n:
        idx = np.round(np.linspace(0, len(out) - 1, n)).astype(int)
        out = [out[i] for i in idx]
    # Order: azimuth bands of about sqrt(n) points, alternate altitude direction.
    bands = max(1, round(math.sqrt(len(out))))
    out.sort(key=lambda p: p[0])
    ordered: list[tuple[float, float]] = []
    size = math.ceil(len(out) / bands)
    for b in range(bands):
        chunk = sorted(out[b * size : (b + 1) * size], key=lambda p: p[1], reverse=bool(b % 2))
        ordered.extend(chunk)
    return ordered


def grid_radec(az: float, alt: float, latitude: float, lst: float) -> tuple[float, float]:
    """RA, Dec (deg) of an (azimuth, altitude) point at ``lst`` deg."""
    ha, dec = altaz_to_hadec(az, alt, latitude)
    return float((lst - float(ha)) % 360.0), float(dec)


def _sep(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# -- the rolling local correction ----------------------------------------------------------


@dataclass
class LocalCorrection:
    """The last acquisition's residual (module doc), decayed by sky distance
    (``exp(-sep / scale)``) and age (``exp(-dt / tau)``), and never more
    than ``max_offset`` arcsec. Offsets are true minus target on the sky:
    ``dx`` = dRA cos(dec), ``dy`` = dDec (arcsec)."""

    scale: float = 15.0
    tau: float = 3600.0
    max_offset: float = 300.0
    ra: float | None = None
    dec: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    when: datetime | None = None

    def weight(self, ra: float, dec: float, when: datetime) -> float:
        if self.ra is None or self.dec is None or self.when is None:
            return 0.0
        dt = max(0.0, (when - self.when).total_seconds())
        return math.exp(-_sep(ra, dec, self.ra, self.dec) / self.scale - dt / self.tau)

    def offset(self, ra: float, dec: float, when: datetime) -> tuple[float, float]:
        w = self.weight(ra, dec, when)
        return self.dx * w, self.dy * w

    def update(self, ra: float, dec: float, dx: float, dy: float, when: datetime) -> None:
        if math.hypot(dx, dy) > self.max_offset:
            log.warning("local pointing correction %.0f arcsec ignored", math.hypot(dx, dy))
            return
        self.ra, self.dec, self.dx, self.dy, self.when = ra, dec, dx, dy, when


# -- drift -----------------------------------------------------------------------------------


@dataclass
class DriftMonitor:
    """Tonight's residuals (true minus model-predicted, on the sky, arcsec)
    against the current model; :meth:`check` alarms once a night when the
    median offset of at least ``min_samples`` exceeds ``threshold`` arcsec
    (default: the larger of ``factor`` times the model's cross-validated
    rms and ``floor``)."""

    min_samples: int = 8
    threshold: float | None = None
    factor: float = 5.0
    floor: float = 5.0
    residuals: list[tuple[float, float]] = field(default_factory=list)
    alarmed: bool = False

    def add(self, dx: float, dy: float) -> None:
        self.residuals.append((dx, dy))

    def limit(self, model_rms: float | None) -> float:
        if self.threshold is not None:
            return self.threshold
        return max(self.floor, self.factor * (model_rms or 0.0))

    def stats(self) -> dict[str, Any]:
        if not self.residuals:
            return {"n": 0}
        r = np.array(self.residuals)
        mx, my = float(np.median(r[:, 0])), float(np.median(r[:, 1]))
        return {
            "n": len(r),
            "median_dx": round(mx, 2),
            "median_dy": round(my, 2),
            "shift": round(math.hypot(mx, my), 2),
            "rms": round(math.sqrt(float(np.mean(np.sum(r * r, axis=1)))), 2),
        }

    def check(self, model_rms: float | None) -> str | None:
        """An alarm text the first time the drift exceeds the limit; else None."""
        st = self.stats()
        if self.alarmed or st["n"] < self.min_samples:
            return None
        limit = self.limit(model_rms)
        if st["shift"] > limit:
            self.alarmed = True
            return (
                f"pointing drift {st['shift']:.1f} arcsec (median of {st['n']} solved frames;"
                f" limit {limit:.1f}): rebuild the pointing model"
            )
        return None

    def reset(self) -> None:
        self.residuals.clear()
        self.alarmed = False


# -- applying it ---------------------------------------------------------------------------------


@dataclass
class _Slew:
    target: tuple[float, float]  # deg
    commanded: tuple[float, float]  # deg
    local: tuple[float, float]  # arcsec applied
    port: str | None
    when: datetime
    guided: bool
    used: bool = False


class PointingCorrector:
    """One telescope's pointing model in use (module doc).

    ``models`` maps M3 port (None: the only one) to a :class:`BuiltModel`;
    ``store`` (a :class:`ModelStore`) is where they are loaded from and new
    ones saved; ``samples`` (a :class:`SampleLog`) where samples go.
    ``enabled`` False measures (samples, drift) but never changes a
    command. ``vendor_off``: an Alpaca ``Action`` (``{"action", "parameters"}``)
    that switches the mount software's own model off, sent before the first
    slew of each generation."""

    def __init__(
        self,
        telescope: str,
        *,
        latitude: float,
        longitude: float = 0.0,
        mount: str = "equatorial",
        models: dict[str | None, BuiltModel] | None = None,
        store: ModelStore | None = None,
        samples: SampleLog | None = None,
        enabled: bool = True,
        local: LocalCorrection | None = None,
        drift: dict[str, Any] | None = None,
        vendor_off: dict[str, str] | None = None,
        solver: Any = None,
        catalog: Any = None,
        run: dict[str, Any] | None = None,
        frame_samples: bool = True,
        min_frame_matches: int = 6,
        max_frame_rms: float = 2.0,
    ) -> None:
        if mount not in BASE_TERMS:
            raise ValueError(f"pointing: mount {mount!r}: equatorial or altaz")
        self.telescope = telescope
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.mount = mount
        self.models: dict[str | None, BuiltModel] = dict(models or {})
        self.store = store
        self.samples = samples
        self.enabled = enabled
        self.local = local
        self._drift_cfg = dict(drift or {})
        self.drift: dict[str | None, DriftMonitor] = {}
        self.vendor_off = vendor_off
        #: For a pointing run: a SolverConfig and a Catalog.
        self.solver = solver
        self.catalog = catalog
        self.run = dict(run or {})
        self.frame_samples = frame_samples
        self.min_frame_matches = min_frame_matches
        self.max_frame_rms = max_frame_rms
        #: Called with an alarm text (the sequencer sets it).
        self.on_alarm: Callable[[str], None] | None = None
        self._last: _Slew | None = None
        self.port: str | None = None
        #: A pointing run is in progress: its frames are not frame samples.
        self.running = False

    # -- configuration --

    @classmethod
    def from_config(
        cls, telescope: str, d: dict[str, Any], *, base: Path | None = None
    ) -> PointingCorrector:
        """A ``pointing`` entry of a telescope in the enclosure config
        (docs/pointing_model.md)."""
        d = dict(d)
        d.pop("_note", None)
        known = {
            "latitude",
            "longitude",
            "mount",
            "enabled",
            "store",
            "local_correction",
            "drift",
            "vendor_off",
            "solver",
            "catalog",
            "run",
            "frame_samples",
            "min_frame_matches",
            "max_frame_rms",
        }
        if set(d) - known:
            raise ValueError(f"pointing: unknown keys {sorted(set(d) - known)}")
        if "latitude" not in d:
            raise ValueError("pointing: latitude is required")
        store = samples = None
        if d.get("store"):
            root = Path(d["store"])
            if base is not None and not root.is_absolute():
                root = base / root
            store, samples = ModelStore(root / "models"), SampleLog(root / "samples")
        local = None
        if d.get("local_correction"):
            lc = d["local_correction"]
            local = LocalCorrection(**({} if lc is True else dict(lc)))
        solver = catalog = None
        if d.get("solver") is not None:
            from robofast_core.astrometry import SolverConfig

            solver = SolverConfig.from_dict(d["solver"])
        if d.get("catalog") is not None:
            from robofast_core.catalog import catalog_from_config

            catalog = catalog_from_config(d["catalog"], base=base)
        drift = dict(d.get("drift") or {})
        unknown = set(drift) - {"min_samples", "threshold", "factor", "floor"}
        if unknown:
            raise ValueError(f"pointing.drift: unknown keys {sorted(unknown)}")
        return cls(
            telescope,
            latitude=float(d["latitude"]),
            longitude=float(d.get("longitude", 0.0)),
            mount=d.get("mount", "equatorial"),
            store=store,
            samples=samples,
            enabled=bool(d.get("enabled", True)),
            local=local,
            drift=drift,
            vendor_off=d.get("vendor_off"),
            solver=solver,
            catalog=catalog,
            run=d.get("run"),
            frame_samples=bool(d.get("frame_samples", True)),
            min_frame_matches=int(d.get("min_frame_matches", 6)),
            max_frame_rms=float(d.get("max_frame_rms", 2.0)),
        )

    def model_for(self, port: str | None) -> BuiltModel | None:
        if port not in self.models and self.store is not None:
            try:
                m = self.store.load(self.telescope, port)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.error("%s: pointing model (port %s) unreadable: %s", self.telescope, port, exc)
                m = None
            self.models[port] = m  # type: ignore[assignment]
        return self.models.get(port)

    def set_model(self, built: BuiltModel, *, save: bool = True) -> None:
        self.models[built.port] = built
        self.drift.pop(built.port, None)
        if save and self.store is not None:
            self.store.save(built)

    def _monitor(self, port: str | None) -> DriftMonitor:
        if port not in self.drift:
            self.drift[port] = DriftMonitor(**self._drift_cfg)
        return self.drift[port]

    # -- coordinates --

    def _to_model(self, ra: float, dec: float, lst: float) -> tuple[float, float]:
        ha = float(_wrap180(lst - ra))
        if self.mount == "altaz":
            a, e = hadec_to_altaz(ha, dec, self.latitude)
            return float(a), float(e)
        return ha, dec

    def _from_model(self, c1: float, c2: float, lst: float) -> tuple[float, float]:
        if self.mount == "altaz":
            ha, dec = altaz_to_hadec(c1, c2, self.latitude)
            return float((lst - float(ha)) % 360.0), float(dec)
        return float((lst - c1) % 360.0), float(c2)

    def mount_for(self, ra: float, dec: float, lst: float, port: str | None) -> tuple[float, float]:
        """Mount position (deg) that points at true (ra, dec) deg (model only)."""
        built = self.model_for(port)
        if built is None or not self.enabled:
            return ra % 360.0, dec
        c1, c2 = self._to_model(ra, dec, lst)
        m1, m2 = built.model.mount_from_true(c1, c2)
        return self._from_model(float(m1), float(m2), lst)

    def true_for(self, ra: float, dec: float, lst: float, port: str | None) -> tuple[float, float]:
        """Where the telescope really points (deg) with the mount at (ra, dec)."""
        built = self.model_for(port)
        if built is None:
            return ra % 360.0, dec
        c1, c2 = self._to_model(ra, dec, lst)
        t1, t2 = built.model.true_from_mount(c1, c2)
        return self._from_model(float(t1), float(t2), lst)

    def command(
        self,
        ra_hours: float,
        dec: float,
        lst_hours: float,
        port: str | None,
        when: datetime,
        *,
        guided: bool = False,
    ) -> tuple[float, float, dict[str, Any]]:
        """What to command (RA hours, Dec deg) to land on the target, and
        what was applied (for the timeline)."""
        ra, lst = ra_hours * 15.0, lst_hours * 15.0
        self.port = port
        dx = dy = 0.0
        if self.local is not None and self.enabled:
            dx, dy = self.local.offset(ra, dec, when)
        aim_dec = dec - dy / 3600.0
        aim_ra = ra - dx / 3600.0 / max(math.cos(math.radians(dec)), 1e-6)
        mra, mdec = self.mount_for(aim_ra, aim_dec, lst, port)
        self._last = _Slew((ra, dec), (mra, mdec), (dx, dy), port, when, guided)
        detail = {
            "model": self.enabled and self.model_for(port) is not None,
            "offset_arcsec": round(_sep(ra, dec, mra, mdec) * 3600.0, 2),
            "local_arcsec": round(math.hypot(dx, dy), 2),
        }
        return mra / 15.0, mdec, detail

    def reported(self, ra_hours: float, dec: float, lst_hours: float) -> tuple[float, float]:
        """The mount's reported position (hours, deg) through the model
        inverse: where the telescope really points (for headers)."""
        tra, tdec = self.true_for(ra_hours * 15.0, dec, lst_hours * 15.0, self.port)
        return tra / 15.0, tdec

    # -- measurements --

    def add_sample(self, sample: Sample) -> dict[str, Any]:
        """Record a sample: log it, add its residual against the current
        model to tonight's drift monitor, raise the drift alarm if due."""
        if self.samples is not None:
            try:
                self.samples.append(sample)
            except OSError as exc:
                log.error("%s: could not log a pointing sample: %s", self.telescope, exc)
        out: dict[str, Any] = {}
        built = self.model_for(sample.port)
        if built is None:
            return out
        arrays = model_coords([sample], self.mount, self.latitude)
        r1, r2 = sky_residuals(built.model, *arrays)
        # Residual as true minus predicted true, on the sky: minus (mount - true - delta).
        dx, dy = -float(r1[0]), -float(r2[0])
        mon = self._monitor(sample.port)
        mon.add(dx, dy)
        out["residual_arcsec"] = round(math.hypot(dx, dy), 2)
        text = mon.check(built.cv_rms if math.isfinite(built.cv_rms) else built.rms_after)
        if text is not None:
            text = f"{self.telescope} port {sample.port}: {text}"
            out["alarm"] = text
            log.error("%s", text)
            if self.on_alarm is not None:
                self.on_alarm(text)
        return out

    def frame_solved(self, solve: dict[str, Any], when: datetime | None = None) -> dict | None:
        """A solved light frame's summary (:mod:`robofast_core.framesolve`'s
        index entry, with ``axis_ra``/``axis_dec``, ``mount_ra``/``mount_dec``
        deg, ``lst`` deg, ``port``, ``objra``/``objdec``, ``frame``): a
        sample, and the first unguided frame after a slew updates the local
        correction."""
        if not solve.get("solved") or not self.frame_samples:
            return None
        need = ("axis_ra", "axis_dec", "mount_ra", "mount_dec", "lst")
        if any(solve.get(k) is None for k in need):
            return None
        if solve.get("degenerate") or (solve.get("n_matched") or 0) < self.min_frame_matches:
            return None
        if (solve.get("rms") or 0.0) > self.max_frame_rms:
            return None
        t = _parse_time(solve["date_obs"]) if solve.get("date_obs") else when or datetime.now(UTC)
        sample = Sample(
            _iso(t),
            self.telescope,
            solve.get("port"),
            float(solve["lst"]),
            float(solve["mount_ra"]),
            float(solve["mount_dec"]),
            float(solve["axis_ra"]),
            float(solve["axis_dec"]),
            "frame",
            solve.get("rms"),
            solve.get("n_matched"),
        )
        out = self.add_sample(sample)
        last = self._last
        if (
            self.local is not None
            and last is not None
            and not last.used
            and not last.guided
            and solve.get("objra") is not None
            and _sep(float(solve["objra"]), float(solve["objdec"]), *last.target) < 1.0 / 3600.0
        ):
            last.used = True
            tra, tdec = last.target
            cosd = math.cos(math.radians(tdec))
            mx = float(_wrap180(sample.true_ra - tra)) * 3600.0 * cosd
            my = (sample.true_dec - tdec) * 3600.0
            # The measured miss plus what was already applied.
            self.local.update(tra, tdec, last.local[0] + mx, last.local[1] + my, t)
            out["local_update"] = [round(last.local[0] + mx, 2), round(last.local[1] + my, 2)]
        return out

    def night_summary(self) -> dict[str, Any]:
        """Tonight's residuals per port (for the morning report)."""
        return {
            str(p): {**m.stats(), "alarmed": m.alarmed}
            for p, m in self.drift.items()
            if m.residuals
        }

    def new_night(self) -> None:
        for m in self.drift.values():
            m.reset()
        if self.local is not None:
            self.local.when = None


# -- the command line --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m robofast_core.pointmodel build|show`` (docs/pointing_model.md)."""
    p = argparse.ArgumentParser(prog="python -m robofast_core.pointmodel")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="fit a model from logged samples and save it")
    b.add_argument("--store", required=True, help="the pointing store (with models/, samples/)")
    b.add_argument("--telescope", required=True)
    b.add_argument("--port", default=None)
    b.add_argument("--latitude", type=float, required=True)
    b.add_argument("--mount", default="equatorial")
    b.add_argument("--since", default=None, help="ISO time: only samples from then on")
    b.add_argument("--source", action="append", help="run and/or frame (default both)")
    b.add_argument("--terms", default=None, help="comma-separated: fix the terms")
    b.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("show", help="print the current model and its history")
    s.add_argument("--store", required=True)
    s.add_argument("--telescope", required=True)
    s.add_argument("--port", default=None)
    a = p.parse_args(argv)
    root = Path(a.store)
    if a.cmd == "show":
        hist = ModelStore(root / "models").history(a.telescope, a.port)
        for m in hist:
            print(m.built, m.summary())
        return 0 if hist else 1
    log_ = SampleLog(root / "samples")
    since = _parse_time(a.since) if a.since else None
    samples = log_.read(a.telescope, a.port, since=since, sources=a.source)
    built = build(
        samples,
        mount=a.mount,
        latitude=a.latitude,
        telescope=a.telescope,
        port=a.port,
        terms=a.terms.split(",") if a.terms else None,
    )
    print(built.summary())
    if not a.dry_run:
        print(ModelStore(root / "models").save(built))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
