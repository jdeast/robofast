"""Which detected star is the target: the acquisition cascade (docs/acquisition.md).

The FAU sees ~2 % of the light (the fiber takes the rest), so a short
acquisition frame usually shows only the target, perhaps with a bright
companion. Plate solving needs more stars than that, so how the target is
told apart depends on how many stars the frame shows. :class:`Acquirer`
decides per frame and records why (the *trace*):

``AUTO`` (the default), by the number of stars detected ``n``:

* ``n >= solve_min`` (5): **solve** -- plate-solve near the target
  (:func:`robofast_core.astrometry.solve_near`) and take the star at the
  target's catalog position;
* ``3 <= n < solve_min``: **triangulate** -- match the detected stars against
  the catalog pattern around the target, projected with the port's
  calibrated PA, parity and plate scale (``robofast_core/rotcal.py``): a
  small translation-only matcher (with the PA and scale known, the pattern
  is rigid), at least 3 stars matched;
* ``n == 2``: **pair** -- the same matcher, the pair's separation vector
  against the catalog companion's: the target end of the pair;
* ``n == 1``: **single** -- the only star is taken; its offset from where
  the pointing put the target is logged and flagged if beyond
  ``single_flag_sigma`` times the pointing uncertainty (the target may be
  missing and that another star);
* ``n == 0``: nothing (optionally a longer exposure, ``retry_exposure``).

A geometric match is accepted only if it is unique (two translations
matching as many stars is ambiguous), the matched stars' brightness order
agrees with the catalog's where they differ by more than ``dmag_order``,
and, on the first frame with a known pointing uncertainty, the match is
where the pointing put it (within ``prior_sigma`` sigmas). A port without a
PA calibration cannot use the geometric branches. Never a silent guess: a
branch that fails says why and the target is "not identified".

Explicit modes skip the cascade: ``BRIGHTEST``, ``GEOMETRY`` (the pattern
matcher whatever ``n``), ``SOLVE``, ``COORDINATES`` (the star nearest where
the pointing put the target, which must be the only one within the
tolerance). They fail if their preconditions are not met, unless the
target also says ``fallback``: then the AUTO cascade runs.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

__all__ = ["MODES", "AcqConfig", "Acquirer", "Detected", "Neighbour", "pattern_pixels"]

MODES = ("AUTO", "BRIGHTEST", "GEOMETRY", "SOLVE", "COORDINATES")
#: OCS acquisition_config.mode values and what they mean here.
OCS_MODES = {"WCS": "SOLVE", "OFF": "COORDINATES", "": "AUTO"}


@dataclass
class AcqConfig:
    """Thresholds of the cascade (per site; docs/acquisition.md)."""

    solve_min: int = 5
    triangulate_min: int = 3
    #: Match tolerance (pixels) for the geometric branches, and the floor of
    #: the COORDINATES radius.
    tolerance: float = 6.0
    #: Brightness order must agree for matched stars further apart than this (mag).
    dmag_order: float = 0.5
    #: First frame: the match must lie within this many pointing sigmas.
    prior_sigma: float = 4.0
    #: Single star: flag when its offset exceeds this many pointing sigmas.
    single_flag_sigma: float = 4.0
    #: Catalog stars fainter than the target by more than this are left out
    #: of the predicted pattern.
    pattern_dmag: float = 4.0
    #: Detection: stars above ``nsigma`` and at most ``max_stars`` brightest.
    nsigma: float = 5.0
    max_stars: int = 20
    #: Optional longer exposure (s) for an empty or unmatched frame (None: off).
    retry_exposure: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> AcqConfig:
        d = {k: v for k, v in dict(d or {}).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"acquisition: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


@dataclass
class Neighbour:
    """A catalog star near the target: offset east and north (arcsec) and G."""

    east: float
    north: float
    g: float


@dataclass
class Detected:
    x: float
    y: float
    flux: float


def pattern_pixels(
    neighbours: Sequence[Neighbour], scale: float, pa: float, flip: bool
) -> np.ndarray:
    """Pixel offsets from the target of each neighbour, with the port's
    plate scale (arcsec/px), PA (deg) and parity in the FITS/plate-solve
    convention of the PA calibration (``robofast_core/rotcal.py``)."""
    from robofast_core.guider.calibration import fits_sky_matrix

    inv = np.linalg.inv(fits_sky_matrix(scale, pa, flip))
    sky = np.array([[n.east, n.north] for n in neighbours], dtype=float).reshape(-1, 2)
    return (inv @ sky.T).T


def neighbours_from_catalog(stars: Any, ra: float, dec: float, g_target: float | None) -> list:
    """Offsets (arcsec east, north) of catalog ``stars`` (a
    :class:`~robofast_core.catalog.Stars`, at the epoch) from (ra, dec)."""
    from robofast_core.catalog import _tan

    xi, eta = _tan(ra, dec, stars.ra, stars.dec)
    e = np.degrees(xi) * 3600.0
    n = np.degrees(eta) * 3600.0
    return [Neighbour(float(a), float(b), float(g)) for a, b, g in zip(e, n, stars.g, strict=True)]


@dataclass
class Decision:
    pixel: tuple[float, float] | None
    method: str
    n_stars: int
    reason: str
    flagged: str | None = None


@dataclass
class Acquirer:
    """The target's pixel in an acquisition frame (module doc).

    ``predicted``: where the pointing puts the target (the guide target
    pixel, normally the fiber). ``target_g`` and ``neighbours`` (the
    catalog around the target, offsets in arcsec, the target excluded)
    give the pattern; ``scale``/``pa``/``flip`` the port's calibration in
    the PA calibration's (FITS) convention (``pa`` None: uncalibrated);
    ``pointing_sigma`` (arcsec) the pointing model's uncertainty (None:
    unknown); ``solve`` an optional plate-solve
    identifier (frame -> pixel or None, :mod:`robofast_core.guider.identify`).
    """

    predicted: tuple[float, float]
    target_g: float | None = None
    neighbours: list[Neighbour] = field(default_factory=list)
    scale: float | None = None
    pa: float | None = None
    flip: bool = False
    pointing_sigma: float | None = None
    solve: Any = None
    mode: str = "AUTO"
    fallback: bool = False
    config: AcqConfig = field(default_factory=AcqConfig)
    #: Every decision (one per frame): method, star count, reason.
    trace: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0

    def __post_init__(self) -> None:
        self.mode = OCS_MODES.get(self.mode.upper(), self.mode.upper())
        if self.mode not in MODES:
            raise ValueError(f"acquisition mode {self.mode!r}: one of {MODES}")

    # -- the public call --

    def __call__(self, frame: Any) -> tuple[float, float] | None:
        stars = self.detect(frame)
        d = self.decide(frame, stars)
        self.calls += 1
        entry = {
            "frame": self.calls,
            "mode": self.mode,
            "method": d.method,
            "stars": d.n_stars,
            "ok": d.pixel is not None,
            "reason": d.reason,
        }
        if d.flagged:
            entry["flag"] = d.flagged
        self.trace.append(entry)
        log.info("acquisition frame %d: %s", self.calls, entry)
        return d.pixel

    @property
    def last(self) -> dict[str, Any] | None:
        return self.trace[-1] if self.trace else None

    def summary(self) -> dict[str, Any]:
        ok = [t for t in self.trace if t["ok"]]
        first = ok[0] if ok else (self.trace[-1] if self.trace else {})
        return {
            "mode": self.mode,
            "method": first.get("method"),
            "stars": first.get("stars"),
            "identified": bool(ok),
            "reason": first.get("reason"),
            "trace": self.trace[-10:],
        }

    def detect(self, frame: Any) -> list[Detected]:
        from robofast_core.guider.centroid import WindowedCOM, find_stars

        out = []
        for col, row, _ in find_stars(
            frame.data, nsigma=self.config.nsigma, max_stars=self.config.max_stars
        ):
            m = WindowedCOM().measure(frame, (col + frame.roi.x, row + frame.roi.y))
            if m.valid:
                out.append(Detected(m.x, m.y, m.flux))
        # Merge duplicates (one star found twice).
        merged: list[Detected] = []
        for s in sorted(out, key=lambda s: -s.flux):
            if all(math.hypot(s.x - o.x, s.y - o.y) > 3.0 for o in merged):
                merged.append(s)
        return merged

    # -- the cascade --

    def decide(self, frame: Any, stars: list[Detected]) -> Decision:
        n = len(stars)
        if self.mode != "AUTO":
            d = self._explicit(self.mode, frame, stars)
            if d.pixel is not None or not self.fallback:
                return d
            auto = self._auto(frame, stars)
            auto.reason = f"{self.mode} failed ({d.reason}); fallback: {auto.reason}"
            return auto
        return self._auto(frame, stars) if n else Decision(None, "none", 0, "no star detected")

    def _auto(self, frame: Any, stars: list[Detected]) -> Decision:
        c = self.config
        n = len(stars)
        if n == 0:
            return Decision(None, "none", 0, "no star detected")
        if n >= c.solve_min:
            d = self._solve(frame, stars)
            if d.pixel is not None:
                return d
            g = self._geometry(stars, c.triangulate_min, "triangulate")
            g.reason = f"solve failed ({d.reason}); {g.reason}"
            return g
        if n >= c.triangulate_min:
            return self._geometry(stars, c.triangulate_min, "triangulate")
        if n == 2:
            return self._geometry(stars, 2, "pair")
        return self._single(stars)

    def _explicit(self, mode: str, frame: Any, stars: list[Detected]) -> Decision:
        n = len(stars)
        if n == 0:
            return Decision(None, mode.lower(), 0, "no star detected")
        if mode == "BRIGHTEST":
            s = max(stars, key=lambda s: s.flux)
            return Decision((s.x, s.y), "brightest", n, "brightest star (explicit)")
        if mode == "SOLVE":
            return self._solve(frame, stars)
        if mode == "GEOMETRY":
            return self._geometry(stars, min(2, n) if n > 1 else 2, "geometry")
        return self._coordinates(stars)

    def _solve(self, frame: Any, stars: list[Detected]) -> Decision:
        n = len(stars)
        if self.solve is None:
            return Decision(None, "solve", n, "no plate solver configured")
        hint = self.solve(frame)
        if hint is None:
            return Decision(None, "solve", n, "no plate solution")
        best = min(stars, key=lambda s: math.hypot(s.x - hint[0], s.y - hint[1]))
        dist = math.hypot(best.x - hint[0], best.y - hint[1])
        if dist > self.config.tolerance:
            return Decision(None, "solve", n, f"no star at the solved position ({dist:.1f} px)")
        return Decision((best.x, best.y), "solve", n, "plate solution")

    def _sigma_px(self) -> float | None:
        if self.pointing_sigma is None or not self.scale:
            return None
        return self.pointing_sigma / self.scale

    def _coordinates(self, stars: list[Detected]) -> Decision:
        n = len(stars)
        sig = self._sigma_px()
        radius = max(self.config.tolerance, (sig or 0.0) * self.config.prior_sigma)
        px, py = self.predicted
        near = [s for s in stars if math.hypot(s.x - px, s.y - py) <= radius]
        if not near:
            return Decision(
                None, "coordinates", n, f"no star within {radius:.0f} px of the pointing"
            )
        if len(near) > 1:
            return Decision(
                None, "coordinates", n, f"{len(near)} stars within {radius:.0f} px: ambiguous"
            )
        s = near[0]
        return Decision((s.x, s.y), "coordinates", n, "nearest to the pointing")

    def _single(self, stars: list[Detected]) -> Decision:
        s = stars[0]
        off = math.hypot(s.x - self.predicted[0], s.y - self.predicted[1])
        sig = self._sigma_px()
        flag = None
        if sig is not None and off > self.config.single_flag_sigma * max(sig, 1.0):
            flag = (
                f"the only star is {off:.0f} px from the pointing ({off / sig:.1f} sigma):"
                " the target may be missing"
            )
        return Decision((s.x, s.y), "single", 1, f"only star, {off:.1f} px from the pointing", flag)

    def _geometry(self, stars: list[Detected], need: int, method: str) -> Decision:
        n = len(stars)
        c = self.config
        if self.scale is None or self.pa is None:
            return Decision(None, method, n, "no PA calibration for this port")
        pat = [Neighbour(0.0, 0.0, self.target_g if self.target_g is not None else 0.0)]
        lim = None if self.target_g is None else self.target_g + c.pattern_dmag
        pat += [nb for nb in self.neighbours if lim is None or nb.g <= lim]
        pp = pattern_pixels(pat, self.scale, self.pa, self.flip)
        if len(pat) < need:
            return Decision(
                None, method, n, f"the catalog predicts {len(pat)} star(s); {need} needed"
            )
        det = np.array([[s.x, s.y] for s in stars])
        flux = np.array([s.flux for s in stars])
        mags = np.array([p.g for p in pat])
        tol = c.tolerance
        found: list[tuple[int, float, np.ndarray, list[tuple[int, int]]]] = []
        for i in range(len(det)):
            for j in range(len(pp)):
                t = det[i] - pp[j]
                if any(np.hypot(*(t - f[2])) < tol for f in found):
                    continue
                pairs = []
                used: set[int] = set()
                for k in range(len(pp)):
                    dd = np.hypot(*(det - (pp[k] + t)).T)
                    for q in np.argsort(dd):
                        if dd[q] > tol:
                            break
                        if q not in used:
                            used.add(int(q))
                            pairs.append((k, int(q)))
                            break
                if len(pairs) < need:
                    continue
                if not self._order_ok(pairs, mags, flux):
                    continue
                resid = float(np.mean([np.hypot(*(det[q] - pp[k] - t)) for k, q in pairs]))
                found.append((len(pairs), resid, t, pairs))
        if not found:
            return Decision(
                None,
                method,
                n,
                f"the detected stars do not match the catalog pattern ({need} needed):"
                " check the PA calibration",
            )
        best_n = max(f[0] for f in found)
        top = [f for f in found if f[0] == best_n]
        sig = self._sigma_px()
        # Until the target has been identified once, the pointing says where
        # it should be; afterwards the mount has moved it on purpose.
        prior_ok = sig is not None and not any(t["ok"] for t in self.trace)
        if prior_ok:
            lim_px = self.config.prior_sigma * max(sig, 1.0)
            p0 = np.asarray(self.predicted, dtype=float)
            near = [f for f in top if np.hypot(*(f[2] - p0)) <= lim_px + tol]
            if not near:
                off = min(float(np.hypot(*(f[2] - p0))) for f in top)
                return Decision(
                    None,
                    method,
                    n,
                    f"the pattern matches {off:.0f} px from where the pointing put it"
                    f" (> {lim_px:.0f} px): check the PA calibration",
                )
            top = near
        if len(top) > 1:
            return Decision(
                None, method, n, f"{len(top)} positions match {best_n} stars equally: ambiguous"
            )
        _, resid, t, pairs = top[0]
        tx, ty = (float(v) for v in (pp[0] + t))
        on = [q for k, q in pairs if k == 0]
        if on:
            tx, ty = float(det[on[0]][0]), float(det[on[0]][1])
        return Decision(
            (tx, ty),
            method,
            n,
            f"{best_n} stars match the catalog pattern (residual {resid:.1f} px)",
            None if on else "the target itself was not detected (position predicted)",
        )

    def _order_ok(self, pairs: list[tuple[int, int]], mags: np.ndarray, flux: np.ndarray) -> bool:
        lim = self.config.dmag_order
        for a in range(len(pairs)):
            for b in range(a + 1, len(pairs)):
                (k1, q1), (k2, q2) = pairs[a], pairs[b]
                dm = mags[k1] - mags[k2]
                if abs(dm) < lim:
                    continue
                if (dm < 0) != (flux[q1] > flux[q2]):
                    return False
        return True
