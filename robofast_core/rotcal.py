"""Rotator and position-angle calibration: what the sky looks like on each
port's detector, measured from plate solves and saved (docs/rotcal.md).

For each telescope and port, the image's position angle (``pa``: of the
detector's +y axis, degrees east of north) and parity (``flip``: east to
the right with north up, a mirror image) follow the rotator's mechanical
angle as

    pa = offset + sense * mechanical        (mod 360)

``sense`` (+1 or -1) is the direction the rotator turns the sky; a port
without a rotator is ``mechanical = 0``, so ``offset`` *is* its PA. The
legacy ``calibrateRotator`` measured this from plate solves at several
rotator angles (158 results over the years) but never saved the answer;
this module measures it the same way and keeps it, with its provenance, in
a :class:`PAStore` that the frame pipeline (the approximate WCS and the
plate solver's hint), the FITS writer (``PACALIB``/``PAFLIP``) and the
guider (its mount calibration matrix, seeded from the sky) read.

* :func:`fit` -- the offset, sense and parity from (mechanical angle,
  solved PA, solved parity) measurements: two or more angles decide the
  sense (else it is assumed); a mix of parities is an error (two ports, or
  a bad solve).
* :func:`calibrate` -- the routine: for each angle, move the rotator,
  expose, solve (any PA, either parity), record; then fit. The sequencer
  runs it as ``kind="rotator_calibration"``; it is independent of the
  devices (callables), so frames from anywhere will do.
* ``python -m robofast_core.rotcal solve`` -- the same from FITS files
  already taken (legacy frames included: ``--jnow`` precesses their
  header RA/Dec, which are JNow, to ICRS first).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

__all__ = [
    "PACalibration",
    "PAMeasurement",
    "PAStore",
    "RotatorSetup",
    "calibrate",
    "fit",
    "seed_mount_matrix",
]


def _wrap180(x: Any) -> Any:
    return (np.asarray(x, dtype=float) + 180.0) % 360.0 - 180.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PAMeasurement:
    """One solved frame: the rotator's mechanical angle (deg; 0 without a
    rotator), the solved PA (deg) and parity, plate scale (arcsec/px)."""

    mechanical: float
    pa: float
    flip: bool
    scale: float | None = None
    rms: float | None = None
    n_matched: int | None = None
    when: str = ""
    frame: str = ""


@dataclass
class PACalibration:
    """A port's PA calibration (module doc) and its provenance."""

    telescope: str
    port: str | None
    #: PA (deg) at mechanical angle 0, and the rotation sense (+1/-1).
    offset: float
    sense: int
    flip: bool
    #: Measured plate scale (arcsec per unbinned pixel), if solved.
    plate_scale: float | None = None
    #: Rms of the PA residuals (deg) and the number of measurements.
    rms: float = 0.0
    n: int = 0
    #: The mechanical angles measured; a single one leaves ``sense`` assumed.
    angles: list[float] = field(default_factory=list)
    sense_assumed: bool = False
    measured: str = ""
    source: str = ""
    notes: str = ""
    measurements: list[dict[str, Any]] = field(default_factory=list)

    def pa(self, mechanical: float | None = 0.0) -> float:
        """The image PA (deg, [0, 360)) at a mechanical angle."""
        return float((self.offset + self.sense * float(mechanical or 0.0)) % 360.0)

    def guider_pa(self, mechanical: float | None = 0.0) -> float:
        """The PA in the guider's :func:`~robofast_core.guider.calibration.sky_matrix`
        convention (with :attr:`parity`): a flipped field's PA is measured
        the other way there."""
        pa = self.pa(mechanical)
        return (-pa) % 360.0 if self.flip else pa

    @property
    def parity(self) -> int:
        """The guider's convention (:func:`robofast_core.guider.calibration.sky_matrix`):
        -1 for a mirror-imaged field."""
        return -1 if self.flip else 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PACalibration:
        known = set(cls.__dataclass_fields__)
        c = cls(**{k: v for k, v in d.items() if k in known})
        if c.sense not in (1, -1):
            raise ValueError(f"PA calibration {c.telescope}/{c.port}: sense must be +1 or -1")
        return c

    def summary(self) -> str:
        return (
            f"{self.telescope} port {self.port}: PA = {self.offset:.3f}"
            f" {'+' if self.sense > 0 else '-'} mechanical"
            f"{' (sense assumed)' if self.sense_assumed else ''},"
            f" {'flipped' if self.flip else 'not flipped'}, rms {self.rms:.3f} deg, n {self.n}"
            + (f", scale {self.plate_scale:.4f} arcsec/px" if self.plate_scale else "")
        )


def _circmean(deg: np.ndarray) -> float:
    r = np.radians(deg)
    return float(np.degrees(math.atan2(float(np.mean(np.sin(r))), float(np.mean(np.cos(r))))))


def fit(
    measurements: Sequence[PAMeasurement],
    *,
    telescope: str = "",
    port: str | None = None,
    sense: int | None = None,
    min_spread: float = 20.0,
    source: str = "",
) -> PACalibration:
    """The offset, sense and parity from ``measurements`` (module doc).

    With mechanical angles spanning at least ``min_spread`` deg the sense is
    the one that fits better (unless ``sense`` is given); otherwise
    ``sense`` (default +1) is assumed and flagged."""
    ms = list(measurements)
    if not ms:
        raise ValueError("no PA measurements")
    flips = {m.flip for m in ms}
    if len(flips) > 1:
        raise ValueError(
            "PA measurements disagree about the parity (two ports mixed, or a bad solve)"
        )
    mech = np.array([m.mechanical for m in ms], dtype=float)
    pa = np.array([m.pa for m in ms], dtype=float)
    spread = float(np.max(np.abs(_wrap180(mech - mech[0])))) if len(ms) > 1 else 0.0
    senses = [sense] if sense is not None else ([1, -1] if spread >= min_spread else [1])
    best: tuple[float, int, float] | None = None
    for s in senses:
        off = _circmean(pa - s * mech)
        rms = float(np.sqrt(np.mean(_wrap180(pa - s * mech - off) ** 2)))
        if best is None or rms < best[0]:
            best = (rms, s, off)
    assert best is not None
    rms, s, off = best
    scales = [m.scale for m in ms if m.scale]
    return PACalibration(
        telescope=telescope,
        port=port,
        offset=round(off % 360.0, 4),
        sense=int(s),
        flip=flips.pop(),
        plate_scale=round(float(np.median(scales)), 5) if scales else None,
        rms=round(rms, 4),
        n=len(ms),
        angles=sorted({round(float(a), 3) for a in mech}),
        sense_assumed=sense is None and spread < min_spread,
        measured=_now(),
        source=source,
        measurements=[asdict(m) for m in ms],
    )


async def calibrate(
    angles: Sequence[float],
    *,
    move: Callable[[float], Awaitable[None]] | None,
    expose: Callable[[], Awaitable[Any]],
    solve: Callable[[Any], Any],
    telescope: str = "",
    port: str | None = None,
    sense: int | None = None,
    min_measurements: int = 1,
    event: Callable[..., None] | None = None,
    source: str = "rotator calibration",
) -> PACalibration:
    """The routine (module doc): at each mechanical angle (``move`` None:
    no rotator, one measurement per angle as it stands), ``expose`` an
    image and ``solve`` it (returning a
    :class:`~robofast_core.astrometry.Solution`, run in a thread; a failure
    skips the angle). Raises ValueError with fewer than ``min_measurements``."""
    import asyncio

    out: list[PAMeasurement] = []
    for angle in angles:
        if move is not None:
            await move(float(angle))
        image = await expose()
        try:
            sol = await asyncio.to_thread(solve, image)
        except Exception as exc:
            log.warning("PA calibration at %.1f deg: %s", angle, exc)
            if event is not None:
                event("pa_point_unsolved", angle=angle, error=str(exc)[:200])
            continue
        m = PAMeasurement(
            float(angle),
            float(sol.pa),
            bool(sol.flip),
            float(sol.scale),
            float(sol.rms),
            int(sol.n_matched),
            _now(),
        )
        out.append(m)
        if event is not None:
            event("pa_point", angle=angle, pa=round(m.pa, 3), flip=m.flip)
    if len(out) < max(1, min_measurements):
        raise ValueError(f"PA calibration: {len(out)} of {len(angles)} angles solved")
    return fit(out, telescope=telescope, port=port, sense=sense, source=source)


def seed_mount_matrix(cal: PACalibration, plate_scale: float | None = None) -> np.ndarray:
    """Pixels per arcsecond of mount offset (east, north) for the guider,
    from the PA calibration: moving the telescope east moves the stars
    west, so minus the inverse of the sky matrix
    (:func:`robofast_core.guider.calibration.sky_matrix`)."""
    from robofast_core.guider.calibration import fits_sky_matrix

    ps = plate_scale or cal.plate_scale
    if not ps:
        raise ValueError("seeding a mount matrix needs a plate scale")
    return -np.linalg.inv(fits_sky_matrix(ps, cal.pa(0.0), cal.flip))


@dataclass
class RotatorSetup:
    """A telescope's ``pa_calibration`` entry (docs/rotcal.md): the store,
    the solver and catalog for the routine, and its defaults."""

    store: PAStore
    solver: Any = None
    catalog: Any = None
    #: Mechanical angles to measure (a telescope without a rotator: one
    #: measurement as it stands).
    angles: list[float] = field(default_factory=lambda: [0.0, 90.0, 180.0, 270.0])
    #: Rotation sense if known (else measured from the angles).
    sense: int | None = None
    #: Bound on one rotator move (s) and the wait after it.
    move_timeout: float = 120.0
    settle: float = 1.0
    min_measurements: int = 2

    @classmethod
    def from_config(cls, d: dict[str, Any], *, base: Path | None = None) -> RotatorSetup:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"pa_calibration: unknown keys {sorted(set(d) - known)}")
        if "store" not in d:
            raise ValueError("pa_calibration: store is required")
        path = Path(d.pop("store"))
        if base is not None and not path.is_absolute():
            path = base / path
        if d.get("solver") is not None:
            from robofast_core.astrometry import SolverConfig

            d["solver"] = SolverConfig.from_dict(d["solver"])
        if d.get("catalog") is not None:
            from robofast_core.catalog import catalog_from_config

            d["catalog"] = catalog_from_config(d["catalog"], base=base)
        return cls(PAStore(path), **d)


# -- the store -------------------------------------------------------------------------


def _key(port: str | None) -> str:
    return "default" if port in (None, "") else str(port)


class PAStore:
    """Every port's PA calibration in one JSON file (``{"telescopes": {tel:
    {port: {"current": {...}, "history": [...]}}}}``), written atomically.
    A missing file is an empty store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] | None = None
        self._mtime: float | None = None

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return {"telescopes": {}}
        if self._data is None or mtime != self._mtime:
            self._data = json.loads(self.path.read_text())
            self._mtime = mtime
        return self._data

    def get(self, telescope: str, port: str | None) -> PACalibration | None:
        entry = self._load().get("telescopes", {}).get(telescope, {}).get(_key(port))
        if not entry or not entry.get("current"):
            return None
        return PACalibration.from_dict(entry["current"])

    def history(self, telescope: str, port: str | None) -> list[PACalibration]:
        entry = self._load().get("telescopes", {}).get(telescope, {}).get(_key(port)) or {}
        return [PACalibration.from_dict(d) for d in entry.get("history", [])]

    def save(self, cal: PACalibration) -> None:
        data = json.loads(json.dumps(self._load()))
        entry = data.setdefault("telescopes", {}).setdefault(cal.telescope, {})
        e = entry.setdefault(_key(cal.port), {"history": []})
        d = cal.as_dict()
        e["current"] = d
        e.setdefault("history", []).append({k: v for k, v in d.items() if k != "measurements"})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._data, self._mtime = None, None

    def wcs_for(
        self, telescope: str, port: str | None, mechanical: float | None
    ) -> tuple[float, bool] | None:
        """(PA, flip) for the approximate WCS, if the port is calibrated."""
        cal = self.get(telescope, port)
        if cal is None:
            return None
        return cal.pa(mechanical), cal.flip


# -- from FITS files ---------------------------------------------------------------------


def measure_fits(
    paths: Sequence[str | Path],
    catalog: Any,
    solver: Any,
    *,
    jnow: bool = False,
    mechanical_key: str = "ROTMECH",
    search_radius: float | None = None,
) -> list[PAMeasurement]:
    """Solve FITS frames near their header RA/Dec (``RA``/``DEC`` or
    ``CRVAL1``/``CRVAL2``, degrees, or sexagesimal ``RA``/``DEC`` strings):
    one measurement each (unsolved frames are logged and skipped)."""
    from astropy.io import fits

    from robofast_core.astrometry import solve_near

    cfg = replace(solver, pa=None, flip=None)
    if search_radius is not None:
        cfg = replace(cfg, search_radius=search_radius)
    out = []
    for p in paths:
        with fits.open(p) as hdul:
            h, data = hdul[0].header, hdul[0].data
        try:
            ra, dec, when = _pointing(h, jnow)
            sol = solve_near(
                np.asarray(data, dtype=float),
                catalog,
                ra,
                dec,
                cfg,
                binning=int(h.get("XBINNING", 1) or 1),
                epoch=when,
            )
        except Exception as exc:
            log.warning("%s: %s", p, exc)
            continue
        out.append(
            PAMeasurement(
                float(h.get(mechanical_key, 0.0) or 0.0),
                float(sol.pa),
                bool(sol.flip),
                float(sol.scale),
                float(sol.rms),
                int(sol.n_matched),
                when.isoformat() if when else "",
                str(Path(p).name),
            )
        )
    return out


def _pointing(h: Any, jnow: bool) -> tuple[float, float, datetime | None]:
    from astropy import units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    when = None
    if h.get("DATE-OBS"):
        when = datetime.fromisoformat(str(h["DATE-OBS"]).replace("Z", "+00:00"))
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
    ra, dec = h.get("RA"), h.get("DEC")
    if ra is None or dec is None:
        ra, dec = h.get("CRVAL1"), h.get("CRVAL2")
    if isinstance(ra, str):
        c = SkyCoord(ra, dec, unit=(u.hourangle, u.deg))
        ra, dec = c.ra.deg, c.dec.deg
    ra, dec = float(ra), float(dec)
    if jnow:
        from robofast_core.sky import offline_iers

        offline_iers()
        eq = Time(when) if when else Time.now()
        c = SkyCoord(ra * u.deg, dec * u.deg, frame=FK5(equinox=eq)).icrs
        ra, dec = float(c.ra.deg), float(c.dec.deg)
    return ra, dec, when


def main(argv: list[str] | None = None) -> int:
    """``python -m robofast_core.rotcal solve|show`` (docs/rotcal.md)."""
    p = argparse.ArgumentParser(prog="python -m robofast_core.rotcal")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve", help="measure a port's PA calibration from FITS frames")
    s.add_argument("--store", required=True, help="the PA calibration JSON file")
    s.add_argument("--telescope", required=True)
    s.add_argument("--port", default=None)
    s.add_argument("--plate-scale", type=float, required=True, help="arcsec per unbinned pixel")
    s.add_argument("--catalog", default=None, help="catalog cache directory")
    s.add_argument("--jnow", action="store_true", help="header RA/Dec are JNow (legacy frames)")
    s.add_argument("--search-radius", type=float, default=0.3)
    s.add_argument("--sense", type=int, choices=(1, -1), default=None)
    s.add_argument("--mechanical-key", default="ROTMECH")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("frames", nargs="+")
    sh = sub.add_parser("show", help="print a port's calibration and history")
    sh.add_argument("--store", required=True)
    sh.add_argument("--telescope", required=True)
    sh.add_argument("--port", default=None)
    a = p.parse_args(argv)
    store = PAStore(a.store)
    if a.cmd == "show":
        hist = store.history(a.telescope, a.port)
        for c in hist:
            print(c.measured, c.summary())
        return 0 if hist else 1
    from robofast_core.astrometry import SolverConfig
    from robofast_core.catalog import catalog_from_config

    catalog = catalog_from_config({"cache_dir": a.catalog} if a.catalog else None)
    ms = measure_fits(
        a.frames,
        catalog,
        SolverConfig(plate_scale=a.plate_scale),
        jnow=a.jnow,
        mechanical_key=a.mechanical_key,
        search_radius=a.search_radius,
    )
    if not ms:
        print("no frame solved", file=sys.stderr)
        return 1
    cal = fit(ms, telescope=a.telescope, port=a.port, sense=a.sense, source="FITS frames")
    print(cal.summary())
    if not a.dry_run:
        store.save(cal)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
