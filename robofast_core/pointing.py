"""Mount pointing-error models in TPOINT's terms: apply, invert, fit.

A pointing model says where a mount *actually* points when its encoders
say it points somewhere else. Terms follow TPOINT (P. T. Wallace, "TPOINT
-- a telescope pointing analysis system", and the TPOINT manual), each a
coefficient in arcsec times a function of the position. The sign convention
here is TPOINT's: the model gives the **mount (encoder) coordinates minus
the true (sky) coordinates**,

    h_mount   = h_true   + dH(h_true, dec_true)
    dec_mount = dec_true + dD(h_true, dec_true)

(for an alt-az mount, azimuth and elevation in place of hour angle and
declination; azimuth from north through east). ``dH`` is a change of the
hour-angle *coordinate* (so on the sky it is ``dH cos dec``), likewise
``dA``.

Equatorial terms (``phi`` the latitude, ``h`` the hour angle)::

    IH   dH = -IH                         hour-angle index error
    ID   dD = -ID                         declination index error
    CH   dH = -CH sec(dec)                east-west collimation
    NP   dH = -NP tan(dec)                HA/Dec non-perpendicularity
    MA   dH = -MA cos(h) tan(dec);  dD = +MA sin(h)      polar axis left-right
    ME   dH = +ME sin(h) tan(dec);  dD = +ME cos(h)      polar axis vertical
    TF   dH = +TF cos(phi) sin(h) sec(dec)
         dD = +TF (cos(phi) cos(h) sin(dec) - sin(phi) cos(dec))   tube flexure
    FO   dD = +FO cos(h)                  fork flexure
    DAF  dH = -DAF (cos(phi) cos(h) + sin(phi) tan(dec))    declination-axis flexure
    HCES dH = +HCES sin(h);  HCEC dH = +HCEC cos(h)          HA centering
    DCES dD = +DCES sin(dec); DCEC dD = +DCEC cos(dec)       Dec centering

Alt-az terms (``A`` azimuth, ``E`` elevation)::

    IA   dA = -IA                         azimuth index error
    IE   dE = +IE                         elevation index error
    CA   dA = -CA sec(E)                  left-right collimation
    NPAE dA = -NPAE tan(E)                az/el non-perpendicularity
    AN   dA = -AN sin(A) tan(E);  dE = -AN cos(A)    azimuth axis tilt north-south
    AW   dA = -AW cos(A) tan(E);  dE = +AW sin(A)    azimuth axis tilt east-west
    TF   dE = -TF cos(E)                  tube flexure (alt-az form)
    TX   dE = -TX cot(E)                  tube flexure, tan form
    ACES dA = +ACES sin(A);  ACEC dA = +ACEC cos(A)  azimuth centering
    ECES dE = +ECES sin(E);  ECEC dE = +ECEC cos(E)  elevation centering

Every term is linear in its coefficient, so a model is fitted by linear
least squares (:func:`fit`) from (mount, true) pairs, e.g. plate-solved
images. :meth:`PointingModel.true_from_mount` inverts the model by fixed-
point iteration (the terms are small, so it converges in a few steps).

This module is pure numpy and the convention is the one the sky simulator
(:mod:`robofast_core.testing.skysim`) uses to make "commanded != actual" in
tests; a pointing-model builder fits the same terms.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "ALTAZ_TERMS",
    "EQUATORIAL_TERMS",
    "PointingModel",
    "altaz_to_hadec",
    "basis",
    "fit",
    "hadec_to_altaz",
]

_AS = math.pi / (180.0 * 3600.0)  # radians per arcsec

Term = Callable[[np.ndarray, np.ndarray, float], tuple[Any, Any]]


def _sec(x: np.ndarray) -> np.ndarray:
    return 1.0 / np.cos(x)


# (d first-coordinate, d second-coordinate) per unit coefficient, in the same
# units as the coefficient; arguments in radians (h or A, dec or E, phi).
EQUATORIAL_TERMS: dict[str, Term] = {
    "IH": lambda h, d, p: (-np.ones_like(h), 0.0),
    "ID": lambda h, d, p: (0.0, -np.ones_like(d)),
    "CH": lambda h, d, p: (-_sec(d), 0.0),
    "NP": lambda h, d, p: (-np.tan(d), 0.0),
    "MA": lambda h, d, p: (-np.cos(h) * np.tan(d), np.sin(h)),
    "ME": lambda h, d, p: (np.sin(h) * np.tan(d), np.cos(h)),
    "TF": lambda h, d, p: (
        math.cos(p) * np.sin(h) * _sec(d),
        math.cos(p) * np.cos(h) * np.sin(d) - math.sin(p) * np.cos(d),
    ),
    "FO": lambda h, d, p: (0.0, np.cos(h)),
    "DAF": lambda h, d, p: (-(math.cos(p) * np.cos(h) + math.sin(p) * np.tan(d)), 0.0),
    "HCES": lambda h, d, p: (np.sin(h), 0.0),
    "HCEC": lambda h, d, p: (np.cos(h), 0.0),
    "DCES": lambda h, d, p: (0.0, np.sin(d)),
    "DCEC": lambda h, d, p: (0.0, np.cos(d)),
}
ALTAZ_TERMS: dict[str, Term] = {
    "IA": lambda a, e, p: (-np.ones_like(a), 0.0),
    "IE": lambda a, e, p: (0.0, np.ones_like(e)),
    "CA": lambda a, e, p: (-_sec(e), 0.0),
    "NPAE": lambda a, e, p: (-np.tan(e), 0.0),
    "AN": lambda a, e, p: (-np.sin(a) * np.tan(e), -np.cos(a)),
    "AW": lambda a, e, p: (-np.cos(a) * np.tan(e), np.sin(a)),
    "TF": lambda a, e, p: (0.0, -np.cos(e)),
    "TX": lambda a, e, p: (0.0, -1.0 / np.tan(e)),
    "ACES": lambda a, e, p: (np.sin(a), 0.0),
    "ACEC": lambda a, e, p: (np.cos(a), 0.0),
    "ECES": lambda a, e, p: (0.0, np.sin(e)),
    "ECEC": lambda a, e, p: (0.0, np.cos(e)),
}


def _terms(mount: str) -> dict[str, Term]:
    if mount == "equatorial":
        return EQUATORIAL_TERMS
    if mount == "altaz":
        return ALTAZ_TERMS
    raise ValueError(f"mount {mount!r}: equatorial or altaz")


def basis(
    mount: str, name: str, c1: Any, c2: Any, latitude: float
) -> tuple[np.ndarray, np.ndarray]:
    """Term ``name``'s (d1, d2) per arcsec of coefficient at (c1, c2) deg
    (hour angle and dec, or azimuth and elevation), in arcsec."""
    f = _terms(mount)[name]
    a = np.radians(np.asarray(c1, dtype=float))
    b = np.radians(np.asarray(c2, dtype=float))
    d1, d2 = f(a, b, math.radians(latitude))
    return np.broadcast_to(d1, a.shape).astype(float), np.broadcast_to(d2, b.shape).astype(float)


@dataclass
class PointingModel:
    """Coefficients (arcsec) of TPOINT terms for one mount (module doc).

    ``mount`` is ``"equatorial"`` (coordinates hour angle and declination)
    or ``"altaz"`` (azimuth and elevation); ``latitude`` deg."""

    terms: dict[str, float] = field(default_factory=dict)
    mount: str = "equatorial"
    latitude: float = 0.0

    def __post_init__(self) -> None:
        known = _terms(self.mount)
        bad = set(self.terms) - set(known)
        if bad:
            raise ValueError(f"{self.mount} pointing model: unknown terms {sorted(bad)}")
        self.terms = {k: float(v) for k, v in self.terms.items()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> PointingModel:
        d = dict(d or {})
        d.pop("_note", None)
        return cls(dict(d.get("terms", {})), d.get("mount", "equatorial"), d.get("latitude", 0.0))

    def as_dict(self) -> dict[str, Any]:
        return {"mount": self.mount, "latitude": self.latitude, "terms": dict(self.terms)}

    def delta(self, c1: Any, c2: Any) -> tuple[np.ndarray, np.ndarray]:
        """(mount - true) in arcsec of each coordinate, at true (c1, c2) deg."""
        a = np.asarray(c1, dtype=float)
        d1 = np.zeros(a.shape)
        d2 = np.zeros(a.shape)
        for name, k in self.terms.items():
            if k:
                b1, b2 = basis(self.mount, name, c1, c2, self.latitude)
                d1 = d1 + k * b1
                d2 = d2 + k * b2
        return d1, d2

    def mount_from_true(self, c1: Any, c2: Any) -> tuple[np.ndarray, np.ndarray]:
        """Where the encoders read (deg) when the telescope points at true
        (c1, c2) deg: what to command to land on a target."""
        d1, d2 = self.delta(c1, c2)
        return np.asarray(c1, dtype=float) + d1 / 3600.0, np.asarray(c2, dtype=float) + d2 / 3600.0

    def true_from_mount(
        self, m1: Any, m2: Any, *, iterations: int = 8
    ) -> tuple[np.ndarray, np.ndarray]:
        """Where the telescope really points (deg) when the encoders read
        (m1, m2): the inverse of :meth:`mount_from_true`."""
        m1 = np.asarray(m1, dtype=float)
        m2 = np.asarray(m2, dtype=float)
        t1, t2 = m1.copy(), m2.copy()
        for _ in range(iterations):
            d1, d2 = self.delta(t1, t2)
            t1, t2 = m1 - d1 / 3600.0, m2 - d2 / 3600.0
        return t1, t2


def fit(
    names: list[str],
    true1: Any,
    true2: Any,
    mount1: Any,
    mount2: Any,
    *,
    mount: str = "equatorial",
    latitude: float = 0.0,
) -> tuple[PointingModel, dict[str, Any]]:
    """Fit the coefficients of ``names`` to (true, mount) pairs (deg).

    The first coordinate's residuals are weighted by cos of the second (so
    every point counts on the sky). Returns the model and a summary: the
    coefficients' formal errors (arcsec) and the sky rms (arcsec) before
    and after."""
    t1 = np.asarray(true1, dtype=float)
    t2 = np.asarray(true2, dtype=float)
    r1 = (np.asarray(mount1, dtype=float) - t1) * 3600.0
    r1 = (r1 + 180.0 * 3600.0) % (360.0 * 3600.0) - 180.0 * 3600.0
    r2 = (np.asarray(mount2, dtype=float) - t2) * 3600.0
    w = np.cos(np.radians(t2))
    cols = []
    for n in names:
        b1, b2 = basis(mount, n, t1, t2, latitude)
        cols.append(np.concatenate([b1 * w, b2]))
    a = np.array(cols).T
    y = np.concatenate([r1 * w, r2])
    coef, *_ = np.linalg.lstsq(a, y, rcond=None)
    resid = y - a @ coef
    dof = max(1, y.size - len(names))
    sigma2 = float(resid @ resid) / dof
    try:
        cov = np.linalg.inv(a.T @ a) * sigma2
        err = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        err = np.full(len(names), math.nan)
    n = t1.size
    before = math.sqrt(float(y @ y) / max(1, n))
    after = math.sqrt(float(resid @ resid) / max(1, n))
    model = PointingModel(dict(zip(names, coef.tolist(), strict=True)), mount, latitude)
    return model, {
        "errors": dict(zip(names, err.tolist(), strict=True)),
        "rms_before": before,
        "rms_after": after,
        "n": n,
    }


# -- coordinates ------------------------------------------------------------------


def hadec_to_altaz(ha: Any, dec: Any, latitude: float) -> tuple[np.ndarray, np.ndarray]:
    """Hour angle and declination (deg) -> azimuth (N through E) and
    elevation (deg), geometric."""
    h, d, p = np.radians(ha), np.radians(dec), math.radians(latitude)
    se = np.sin(p) * np.sin(d) + math.cos(p) * np.cos(d) * np.cos(h)
    e = np.arcsin(np.clip(se, -1, 1))
    y = -np.cos(d) * np.sin(h)
    x = np.sin(d) * math.cos(p) - np.cos(d) * np.cos(h) * math.sin(p)
    return np.degrees(np.arctan2(y, x)) % 360.0, np.degrees(e)


def altaz_to_hadec(az: Any, alt: Any, latitude: float) -> tuple[np.ndarray, np.ndarray]:
    """The inverse of :func:`hadec_to_altaz`."""
    a, e, p = np.radians(az), np.radians(alt), math.radians(latitude)
    sd = np.sin(p) * np.sin(e) + math.cos(p) * np.cos(e) * np.cos(a)
    d = np.arcsin(np.clip(sd, -1, 1))
    y = -np.cos(e) * np.sin(a)
    x = np.sin(e) * math.cos(p) - np.cos(e) * np.cos(a) * math.sin(p)
    h = np.degrees(np.arctan2(y, x))
    return (h + 180.0) % 360.0 - 180.0, np.degrees(d)
