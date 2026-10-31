"""Fits for autofocus: V-curve hyperbola, parabola, arm lines, two-point solve.

All positions are focuser positions (steps); sizes are half-flux diameters
(HFD) in any consistent unit (the autofocus uses unbinned pixels). Every
function is deterministic and uses only numpy.

**Hyperbola** (the default sweep fit; a V-curve *is* a hyperbola: the
geometric blur grows linearly with defocus and adds to the seeing roughly
in quadrature)::

    h(x) = sqrt(a^2 + m(x)^2 (x - x0)^2),   m = m_plus for x > x0, else m_minus

``a`` is the HFD at best focus (the seeing), ``m_plus``/``m_minus`` the
slopes of the two arms (magnitudes). For a trial ``x0`` the model is linear
in ``(a^2, m_plus^2, m_minus^2)`` (in ``h^2``), so ``x0`` is found by a grid
search refined by golden section, each trial a weighted linear solve; then
outliers beyond ``clip`` robust sigma are dropped and the fit repeated.

**Parabola** (the legacy fit, fixed): a weighted quadratic, with the
*weights kept* through the sigma clipping (the legacy code dropped them on
the first refit), and the vertex returned as the fit's result.

**Two-point V-curve solve** (:func:`two_point`): with the calibrated slope
``m`` of the arm, two HFDs on the same arm determine both the arm's zero
``z`` and the seeing term ``a``::

    h_i^2 = a^2 + m^2 (x_i - z)^2
    z = (x1 + x2)/2 - (h1^2 - h2^2) / (2 m^2 (x1 - x2))

so the seeing drops out instead of biasing the result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "ArmFit",
    "FitError",
    "HyperbolaFit",
    "LineFit",
    "ParabolaFit",
    "TwoPoint",
    "fit_arm",
    "fit_hyperbola",
    "fit_line",
    "fit_parabola",
    "two_point",
]

_GOLD = (math.sqrt(5) - 1) / 2


class FitError(ValueError):
    """The data do not support a fit (too few points, wrong shape, ...)."""


def _arrays(x, h, err) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    h = np.asarray(h, dtype=float)
    if err is None:
        e = np.full_like(h, max(float(np.nanmedian(h)) * 0.02, 1e-6))
    else:
        e = np.asarray(err, dtype=float).copy()
        floor = max(float(np.nanmedian(h)) * 0.005, 1e-6)
        e[~np.isfinite(e) | (e < floor)] = floor
    ok = np.isfinite(x) & np.isfinite(h) & (h > 0)
    return x[ok], h[ok], e[ok]


@dataclass
class HyperbolaFit:
    x0: float
    x0_err: float
    a: float
    m_plus: float
    m_minus: float
    chi2: float
    #: Indices (into the inputs that were finite) of the points used.
    used: list[int] = field(default_factory=list)

    def __call__(self, x: np.ndarray | float) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        m = np.where(x > self.x0, self.m_plus, self.m_minus)
        return np.sqrt(self.a**2 + (m * (x - self.x0)) ** 2)


def _solve_at(
    x0: float, x: np.ndarray, h: np.ndarray, e: np.ndarray, symmetric: bool
) -> tuple[float, tuple[float, float, float]]:
    u2 = (x - x0) ** 2
    plus = x > x0
    if symmetric:
        A = np.column_stack([np.ones_like(x), u2])
    else:
        A = np.column_stack([np.ones_like(x), u2 * plus, u2 * ~plus])
    w = 1.0 / (2.0 * h * e)  # sigma of h^2 is 2 h sigma_h
    sol, *_ = np.linalg.lstsq(A * w[:, None], h**2 * w, rcond=None)
    a2 = max(float(sol[0]), 0.0)
    mp2 = max(float(sol[1]), 1e-30)
    mm2 = mp2 if symmetric else max(float(sol[2]), 1e-30)
    m = np.where(plus, math.sqrt(mp2), math.sqrt(mm2))
    model = np.sqrt(a2 + m**2 * u2)
    chi2 = float((((h - model) / e) ** 2).sum())
    return chi2, (math.sqrt(a2), math.sqrt(mp2), math.sqrt(mm2))


def _fit_x0(
    x: np.ndarray, h: np.ndarray, e: np.ndarray, symmetric: bool, grid: int = 201
) -> tuple[float, float, tuple[float, float, float]]:
    lo, hi = float(x.min()), float(x.max())
    xs = np.linspace(lo, hi, grid)
    chis = np.array([_solve_at(x0, x, h, e, symmetric)[0] for x0 in xs])
    i = int(np.argmin(chis))
    a, b = xs[max(0, i - 1)], xs[min(grid - 1, i + 1)]
    # Golden-section refinement.
    c, d = b - _GOLD * (b - a), a + _GOLD * (b - a)
    fc = _solve_at(c, x, h, e, symmetric)[0]
    fd = _solve_at(d, x, h, e, symmetric)[0]
    for _ in range(60):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - _GOLD * (b - a)
            fc = _solve_at(c, x, h, e, symmetric)[0]
        else:
            a, c, fc = c, d, fd
            d = a + _GOLD * (b - a)
            fd = _solve_at(d, x, h, e, symmetric)[0]
        if b - a < 1e-6 * max(1.0, hi - lo):
            break
    x0 = (a + b) / 2
    chi2, params = _solve_at(x0, x, h, e, symmetric)
    return x0, chi2, params


def _x0_error(x0: float, chi2: float, x, h, e, symmetric: bool, span: float) -> float:
    """1-sigma error on x0 from the chi^2 curvature (scaled by the reduced
    chi^2 when it exceeds 1)."""
    dof = max(1, x.size - (3 if symmetric else 4))
    scale = max(1.0, chi2 / dof)
    step = max(span * 1e-3, 1e-6)
    for _ in range(30):
        cp = _solve_at(x0 + step, x, h, e, symmetric)[0]
        cm = _solve_at(x0 - step, x, h, e, symmetric)[0]
        curv = (cp + cm - 2 * chi2) / step**2
        if curv > 0 and cp - chi2 > 0.05 and cm - chi2 > 0.05:
            break
        step *= 2
        if step > span:
            break
    if curv <= 0:
        return math.inf
    return math.sqrt(2.0 / curv * scale)


def fit_hyperbola(
    x, h, err=None, *, symmetric: bool | None = None, clip: float = 4.0, min_points: int = 5
) -> HyperbolaFit:
    """Fit the V-curve hyperbola (module doc). ``symmetric`` forces equal arm
    slopes (default: only when one side of the minimum has fewer than 3
    points). Raises :class:`FitError` if the best focus is not bracketed."""
    x, h, e = _arrays(x, h, err)
    if x.size < min_points:
        raise FitError(f"hyperbola: {x.size} points (need {min_points})")
    idx = np.arange(x.size)
    keep = np.ones(x.size, dtype=bool)
    for _ in range(4):
        xs, hs, es = x[keep], h[keep], e[keep]
        imin = int(np.argmin(hs))
        sym = symmetric
        if sym is None:
            sym = min(int((xs < xs[imin]).sum()), int((xs > xs[imin]).sum())) < 2
        x0, chi2, (a, mp, mm) = _fit_x0(xs, hs, es, sym)
        m = np.where(x > x0, mp, mm)
        model = np.sqrt(a**2 + (m * (x - x0)) ** 2)
        z = (h - model) / e
        zk = z[keep]
        sig = 1.4826 * float(np.median(np.abs(zk - np.median(zk)))) or 1.0
        new = np.abs(z) <= clip * max(sig, 1.0)
        if new.sum() < min_points or np.array_equal(new, keep):
            break
        keep = new
    xs, hs, es = x[keep], h[keep], e[keep]
    if (xs < x0).sum() < 2 or (xs > x0).sum() < 2:
        raise FitError(f"hyperbola: best focus {x0:.1f} not bracketed by the points")
    err0 = _x0_error(x0, chi2, xs, hs, es, sym, float(xs.max() - xs.min()))
    return HyperbolaFit(float(x0), err0, a, mp, mm, chi2, idx[keep].tolist())


@dataclass
class ParabolaFit:
    x0: float
    x0_err: float
    coeffs: tuple[float, float, float]
    used: list[int] = field(default_factory=list)

    def __call__(self, x):
        return np.polyval(self.coeffs, np.asarray(x, dtype=float))


def fit_parabola(x, h, err=None, *, clip: float = 3.0, min_points: int = 4) -> ParabolaFit:
    """Weighted quadratic with sigma clipping (weights kept), vertex as the
    result. Raises :class:`FitError` for too few points, an upside-down
    parabola, or a vertex outside the points."""
    x, h, e = _arrays(x, h, err)
    if x.size < min_points:
        raise FitError(f"parabola: {x.size} points (need {min_points})")
    keep = np.ones(x.size, dtype=bool)
    xc = float(np.mean(x))  # centre for conditioning
    for _ in range(10):
        c, cov = np.polyfit(x[keep] - xc, h[keep], 2, w=1.0 / e[keep], cov="unscaled")
        z = (h - np.polyval(c, x - xc)) / e
        sig = 1.4826 * float(np.median(np.abs(z[keep] - np.median(z[keep])))) or 1.0
        new = np.abs(z) <= clip * max(sig, 1.0)
        if new.sum() < min_points or np.array_equal(new, keep):
            break
        keep = new
    a2, a1, _ = c
    if a2 <= 0:
        raise FitError("parabola: upside down")
    v = -a1 / (2 * a2)
    x0 = v + xc
    if (x[keep] < x0).sum() < 2 or (x[keep] > x0).sum() < 2:
        raise FitError(f"parabola: vertex {x0:.1f} not bracketed by the points")
    # Error of the vertex by propagation (scaled by the reduced chi^2 if > 1).
    chi2 = float((((h[keep] - np.polyval(c, x[keep] - xc)) / e[keep]) ** 2).sum())
    scale = max(1.0, chi2 / max(1, int(keep.sum()) - 3))
    g = np.array([a1 / (2 * a2**2), -1 / (2 * a2), 0.0])
    var = float(g @ cov @ g) * scale
    # Undo the centring for the reported coefficients.
    coeffs = np.polyfit(x[keep], h[keep], 2, w=1.0 / e[keep])
    return ParabolaFit(
        x0, math.sqrt(max(var, 0.0)), tuple(map(float, coeffs)), np.flatnonzero(keep).tolist()
    )


@dataclass
class LineFit:
    """``h = slope * (x - zero)``; ``slope`` signed."""

    slope: float
    zero: float
    slope_err: float
    zero_err: float


def fit_line(x, h, err=None) -> LineFit:
    """Weighted straight line through (x, h); its zero crossing."""
    x, h, e = _arrays(x, h, err)
    if x.size < 2:
        raise FitError(f"line: {x.size} points")
    w = 1.0 / e**2
    sw, sx, sy = w.sum(), (w * x).sum(), (w * h).sum()
    sxx, sxy = (w * x * x).sum(), (w * x * h).sum()
    det = sw * sxx - sx**2
    if det <= 0:
        raise FitError("line: degenerate")
    slope = (sw * sxy - sx * sy) / det
    icpt = (sxx * sy - sx * sxy) / det
    if slope == 0:
        raise FitError("line: zero slope")
    var_s, var_i, cov = sw / det, sxx / det, -sx / det
    if x.size > 2:
        chi2 = float((w * (h - icpt - slope * x) ** 2).sum())
        scale = max(1.0, chi2 / (x.size - 2))
        var_s, var_i, cov = var_s * scale, var_i * scale, cov * scale
    zero = -icpt / slope
    # zero = -i/s: gradient (-1/s, i/s^2)
    gz = np.array([-1 / slope, icpt / slope**2])
    cm = np.array([[var_i, cov], [cov, var_s]])
    return LineFit(float(slope), float(zero), math.sqrt(var_s), math.sqrt(float(gz @ cm @ gz)))


@dataclass
class TwoPoint:
    #: The arm's zero crossing.
    zero: float
    zero_err: float
    #: The seeing term ``a^2`` (may come out negative: inconsistent data).
    a2: float
    #: Secant slope between the two points over the calibrated slope, signed
    #: so that +1 means "as calibrated" (see :func:`two_point`).
    slope_ratio: float


def two_point(
    x1: float, h1: float, e1: float, x2: float, h2: float, e2: float, m: float, side: int
) -> TwoPoint:
    """Solve the V arm through two points (module doc). ``m`` is the arm's
    slope magnitude and ``side`` (+1/-1) the side of focus the arm is on, so
    the expected secant slope is ``side * m``."""
    if x1 == x2:
        raise FitError("two points at the same position")
    if m <= 0:
        raise FitError("slope must be positive")
    secant = (h2 - h1) / (x2 - x1)
    ratio = secant / (side * m)
    z = (x1 + x2) / 2 - (h1**2 - h2**2) / (2 * m**2 * (x1 - x2))
    a2 = h1**2 - m**2 * (x1 - z) ** 2
    err = math.hypot(h1 * e1, h2 * e2) / (m**2 * abs(x1 - x2))
    return TwoPoint(float(z), float(err), float(a2), float(ratio))


@dataclass
class ArmFit:
    """One V arm, ``h^2 = a^2 + m^2 (x - zero)^2`` (``m`` a magnitude)."""

    slope: float
    zero: float
    a2: float
    zero_err: float


def fit_arm(x, h, err=None) -> ArmFit:
    """Fit one arm's hyperbola: a weighted quadratic in ``h^2``
    (``h^2 = c2 x^2 + c1 x + c0``, so ``m^2 = c2``, ``zero = -c1 / 2 c2``,
    ``a^2 = c0 - c2 zero^2``). Unlike a straight line through the arm, its
    zero crossing is not dragged toward focus by the seeing's curvature,
    so it is the same quantity :func:`two_point` solves for."""
    x, h, e = _arrays(x, h, err)
    if x.size < 3:
        raise FitError(f"arm: {x.size} points (need 3)")
    xc = float(np.mean(x))
    w = 1.0 / (2.0 * h * e)
    c, cov = np.polyfit(x - xc, h**2, 2, w=w, cov="unscaled")
    c2, c1, c0 = (float(v) for v in c)
    if c2 <= 0:
        raise FitError("arm: not convex")
    zero = -c1 / (2 * c2)
    g = np.array([c1 / (2 * c2**2), -1 / (2 * c2), 0.0])
    zerr = math.sqrt(max(float(g @ cov @ g), 0.0))
    return ArmFit(math.sqrt(c2), zero + xc, c0 - c2 * zero**2, zerr)
