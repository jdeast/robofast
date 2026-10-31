"""Centroiders: where the star is in a guide frame (numpy only).

All run on the guide computer, on a small window, every frame; none needs
scipy. Ported in spirit from jdeast/tres-guider's ``centroid.py`` and
``annulus.py`` (J. Eastman, J. Kansky; relicensed BSD-3 for robofast),
keeping the fast and robust ones and replacing the scipy fits:

* :class:`WindowedCOM` -- iterative centre of mass in a circular window that
  follows the star, background-subtracted and thresholded at
  ``threshold`` x the noise. Fast, unbiased for a symmetric star well inside
  the window; the default.
* :class:`QuadraticPeak` -- a three-point fit through the peak pixel and its
  neighbours on each axis, ``"gaussian"`` (parabola through the logarithm,
  exact for a Gaussian) or ``"parabola"``. Cheapest; for a bright, compact
  star (sub-pixel accuracy for FWHM of ~2-4 pixels).
* :class:`GaussianFit` -- a least-squares circular Gaussian (x, y, sigma,
  amplitude, background) by Gauss-Newton, optionally with pixels masked
  out. The most precise for faint stars; ~10x the cost of the COM.
* :class:`AnnulusCentroider` -- for a star sitting on the fiber hole (or a
  donut): the hole's pixels are missing light, which drags a plain centre
  of mass away from the hole. It masks the hole (a disk of ``hole_radius``
  at the fiber position) *and the mirror image of the hole through the
  current estimate*, so the remaining weights stay point-symmetric about
  the star and the centre of mass is unbiased; iterated. With
  ``fit=True`` it instead fits the Gaussian with the hole masked (the
  tres-guider ``fit_annulus`` model with the hole fixed at its measured
  position, which makes the fit well-conditioned).

:func:`find_stars` finds candidate stars in a full frame (acquisition).

Each returns a :class:`~robofast_core.guider.base.Measurement` in detector
pixels with the flux, FWHM (second moments), peak, background and an SNR;
``valid`` is False (with a reason) when there is no star.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from robofast_core.guider.base import Frame, Measurement

SIGMA_TO_FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))


def background_and_noise(data: np.ndarray) -> tuple[float, float]:
    """Median and robust rms (1.4826 MAD) of a frame. A star covering a few
    per cent of the window barely moves either."""
    d = data.astype(float, copy=False).ravel()
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med)))
    noise = 1.4826 * mad
    if noise <= 0:
        noise = float(d.std()) or 1.0
    return med, noise


def _peak(img: np.ndarray, guess: tuple[float, float] | None) -> tuple[int, int]:
    """(row, col) of the brightest pixel of a lightly smoothed image, or
    of the brightest pixel within 5 pixels of ``guess`` (row, col)."""
    sm = img.copy()
    sm[1:-1, 1:-1] = (
        img[:-2, :-2]
        + img[:-2, 1:-1]
        + img[:-2, 2:]
        + img[1:-1, :-2]
        + img[1:-1, 1:-1]
        + img[1:-1, 2:]
        + img[2:, :-2]
        + img[2:, 1:-1]
        + img[2:, 2:]
    ) / 9.0
    if guess is not None:
        r0, c0 = guess
        h, w = img.shape
        if 0 <= r0 < h and 0 <= c0 < w:
            i0, i1 = max(0, int(r0) - 5), min(h, int(r0) + 6)
            j0, j1 = max(0, int(c0) - 5), min(w, int(c0) + 6)
            sub = sm[i0:i1, j0:j1]
            k = int(np.argmax(sub))
            return i0 + k // sub.shape[1], j0 + k % sub.shape[1]
    k = int(np.argmax(sm))
    return k // img.shape[1], k % img.shape[1]


def _soft_outside(cols: np.ndarray, rows: np.ndarray, x: float, y: float, r: float) -> np.ndarray:
    """0 inside a disk of radius ``r`` at (x, y), 1 outside, linear over
    one pixel across its edge."""
    d = np.sqrt((cols - x) ** 2 + (rows - y) ** 2)
    return np.clip(d - r + 0.5, 0.0, 1.0)


def _local_max(img: np.ndarray, r: int, c: int, half: int = 2) -> float:
    return float(img[max(0, r - half) : r + half + 1, max(0, c - half) : c + half + 1].max())


def _moments(
    img: np.ndarray, rows: np.ndarray, cols: np.ndarray, weights: np.ndarray
) -> tuple[float, float, float, float]:
    """Weighted centre and FWHM; (nan, nan, 0, nan) if no weight."""
    total = float(weights.sum())
    if total <= 0:
        return math.nan, math.nan, 0.0, math.nan
    yc = float((weights * rows).sum() / total)
    xc = float((weights * cols).sum() / total)
    var = float((weights * ((rows - yc) ** 2 + (cols - xc) ** 2)).sum() / total) / 2.0
    fwhm = SIGMA_TO_FWHM * math.sqrt(var) if var > 0 else math.nan
    return xc, yc, total, fwhm


@dataclass
class WindowedCOM:
    """Iterative windowed centre of mass (module doc)."""

    radius: float = 6.0
    threshold: float = 3.0
    #: The star's peak must exceed this many times the noise.
    min_snr: float = 5.0
    iterations: int = 8
    name: str = "com"

    def measure(self, frame: Frame, guess: tuple[float, float] | None = None) -> Measurement:
        img = frame.data.astype(float, copy=False)
        bkg, noise = background_and_noise(img)
        sub = img - bkg
        g = None if guess is None else (guess[1] - frame.roi.y, guess[0] - frame.roi.x)
        r0, c0 = _peak(sub, g)
        peak = _local_max(sub, r0, c0)
        if peak < self.min_snr * noise:
            return Measurement.invalid(f"no star: peak {peak:.1f} < {self.min_snr} x {noise:.1f}")
        return self._iterate(frame, sub, noise, float(r0), float(c0), bkg, peak, None)

    def _iterate(
        self,
        frame: Frame,
        sub: np.ndarray,
        noise: float,
        yc: float,
        xc: float,
        bkg: float,
        peak: float,
        hole: tuple[float, float, float] | None,
    ) -> Measurement:
        h, w = sub.shape
        rows, cols = np.mgrid[0:h, 0:w]
        wts = np.where(sub > self.threshold * noise, sub, 0.0)
        flux = 0.0
        fwhm = math.nan
        npix = 1
        for _ in range(self.iterations):
            d2 = (rows - yc) ** 2 + (cols - xc) ** 2
            inwin = d2 <= self.radius**2
            wv = np.where(inwin, wts, 0.0)
            if hole is not None:
                # Soft (anti-aliased) masks of the hole and of its mirror
                # image through the estimate, so the iteration is a smooth
                # map; damped, since the mirror moves with the estimate.
                hx, hy, hr = hole
                mx, my = 2 * xc - hx, 2 * yc - hy
                wv = wv * _soft_outside(cols, rows, hx, hy, hr)
                wv = wv * _soft_outside(cols, rows, mx, my, hr)
            nx, ny, flux, fwhm = _moments(sub, rows, cols, wv)
            npix = int(inwin.sum()) or 1
            if not math.isfinite(nx):
                return Measurement.invalid("no flux in the window")
            if hole is not None:
                nx, ny = xc + 0.25 * (nx - xc), yc + 0.25 * (ny - yc)
            shift = math.hypot(nx - xc, ny - yc)
            xc, yc = nx, ny
            if shift < 0.002:
                break
        aperture = float(
            np.where((rows - yc) ** 2 + (cols - xc) ** 2 <= self.radius**2, sub, 0).sum()
        )
        flux = max(flux, aperture)
        snr = flux / math.sqrt(max(flux, 0.0) / max(frame.gain or 1.0, 1e-9) + npix * noise**2)
        if not (0 <= xc < w and 0 <= yc < h):
            return Measurement.invalid("centroid outside the window")
        return Measurement(
            xc + frame.roi.x,
            yc + frame.roi.y,
            flux,
            fwhm,
            peak=peak,
            background=bkg,
            snr=snr,
        )


@dataclass
class QuadraticPeak:
    """Three-point peak fit on each axis (module doc)."""

    kind: str = "gaussian"  # or "parabola"
    min_snr: float = 5.0
    radius: float = 6.0
    name: str = "quadratic"

    def measure(self, frame: Frame, guess: tuple[float, float] | None = None) -> Measurement:
        img = frame.data.astype(float, copy=False)
        bkg, noise = background_and_noise(img)
        sub = img - bkg
        h, w = sub.shape
        g = None if guess is None else (guess[1] - frame.roi.y, guess[0] - frame.roi.x)
        r0, c0 = _peak(sub, g)
        # The true peak pixel near the smoothed one.
        i0, i1 = max(0, r0 - 1), min(h, r0 + 2)
        j0, j1 = max(0, c0 - 1), min(w, c0 + 2)
        k = int(np.argmax(sub[i0:i1, j0:j1]))
        r0, c0 = i0 + k // (j1 - j0), j0 + k % (j1 - j0)
        peak = float(sub[r0, c0])
        if peak < self.min_snr * noise:
            return Measurement.invalid(f"no star: peak {peak:.1f} < {self.min_snr} x {noise:.1f}")
        if not (1 <= r0 < h - 1 and 1 <= c0 < w - 1):
            return Measurement.invalid("peak on the window edge")

        def vertex(a: float, b: float, c: float) -> float:
            if self.kind == "gaussian":
                floor = 1e-3 * max(b, 1e-9)
                a, b, c = (math.log(max(v, floor)) for v in (a, b, c))
            den = a - 2 * b + c
            if den >= 0:
                return 0.0
            return max(-0.5, min(0.5, 0.5 * (a - c) / den))

        dx = vertex(sub[r0, c0 - 1], sub[r0, c0], sub[r0, c0 + 1])
        dy = vertex(sub[r0 - 1, c0], sub[r0, c0], sub[r0 + 1, c0])
        rows, cols = np.mgrid[0:h, 0:w]
        win = (rows - r0) ** 2 + (cols - c0) ** 2 <= self.radius**2
        flux = float(np.where(win, sub, 0.0).sum())
        wts = np.where(win & (sub > 3 * noise), sub, 0.0)
        _, _, _, fwhm = _moments(sub, rows, cols, wts)
        snr = flux / math.sqrt(max(flux, 0.0) + win.sum() * noise**2) if flux > 0 else 0.0
        return Measurement(
            c0 + dx + frame.roi.x,
            r0 + dy + frame.roi.y,
            flux,
            fwhm,
            peak=peak,
            background=bkg,
            snr=snr,
        )


@dataclass
class GaussianFit:
    """Least-squares circular Gaussian by Gauss-Newton (module doc).
    ``mask`` (set per call through :meth:`fit`) drops pixels from the fit."""

    radius: float = 8.0
    min_snr: float = 5.0
    iterations: int = 12
    name: str = "gaussian"

    def measure(self, frame: Frame, guess: tuple[float, float] | None = None) -> Measurement:
        return self.fit(frame, guess, None)

    def fit(
        self,
        frame: Frame,
        guess: tuple[float, float] | None,
        hole: tuple[float, float, float] | None,
        start: Measurement | None = None,
        sigma: float | None = None,
    ) -> Measurement:
        """Fit, with the pixels of ``hole`` (x, y, radius; detector pixels)
        left out, from ``start`` (default: the windowed centre of mass);
        ``sigma`` (pixels) fixes the width instead of fitting it."""
        m0 = start or WindowedCOM(radius=self.radius, min_snr=self.min_snr).measure(frame, guess)
        if not m0.valid:
            return m0
        img = frame.data.astype(float, copy=False)
        h, w = img.shape
        _, noise = background_and_noise(img)
        x0, y0 = m0.x - frame.roi.x, m0.y - frame.roi.y
        r = self.radius
        i0, i1 = max(0, int(y0 - r)), min(h, int(y0 + r) + 2)
        j0, j1 = max(0, int(x0 - r)), min(w, int(x0 + r) + 2)
        rows, cols = np.mgrid[i0:i1, j0:j1].astype(float)
        data = img[i0:i1, j0:j1]
        use = np.ones_like(data, dtype=bool)
        if hole is not None:
            hx, hy, hr = hole[0] - frame.roi.x, hole[1] - frame.roi.y, hole[2]
            use &= (cols - hx) ** 2 + (rows - hy) ** 2 > hr * hr
        y = data[use]
        rr, cc = rows[use], cols[use]
        s0 = (m0.fwhm / SIGMA_TO_FWHM) if math.isfinite(m0.fwhm) and m0.fwhm > 0 else 1.5
        if sigma is not None:
            s0 = sigma
        p = np.array([x0, y0, max(s0, 0.5), max(m0.peak, 1.0), m0.background])
        free = [0, 1, 3, 4] if sigma is not None else [0, 1, 2, 3, 4]
        for _ in range(self.iterations):
            xc, yc, s, a, b = p
            d2 = (cc - xc) ** 2 + (rr - yc) ** 2
            e = np.exp(-0.5 * d2 / (s * s))
            model = a * e + b
            jac = np.column_stack(
                [
                    a * e * (cc - xc) / (s * s),
                    a * e * (rr - yc) / (s * s),
                    a * e * d2 / (s**3),
                    e,
                    np.ones_like(e),
                ]
            )
            resid = y - model
            try:
                sub_step, *_ = np.linalg.lstsq(jac[:, free], resid, rcond=None)
            except np.linalg.LinAlgError:
                break
            step = np.zeros(5)
            step[free] = sub_step
            # Damp wild steps (a faint star, a poor start).
            step[:2] = np.clip(step[:2], -1.0, 1.0)
            p = p + step
            p[2] = min(max(p[2], 0.3), r)
            if abs(step[0]) < 1e-4 and abs(step[1]) < 1e-4:
                break
        xc, yc, s, a, b = p
        if not (0 <= xc < w and 0 <= yc < h) or a <= 0:
            return m0
        flux = 2 * math.pi * a * s * s
        snr = flux / math.sqrt(max(flux, 0.0) + 4 * math.pi * s * s * noise**2)
        return Measurement(
            float(xc + frame.roi.x),
            float(yc + frame.roi.y),
            float(flux),
            float(SIGMA_TO_FWHM * s),
            peak=float(a),
            background=float(b),
            snr=float(snr),
        )


@dataclass
class AnnulusCentroider:
    """A star on the fiber hole (module doc). ``hole_x``/``hole_y`` are the
    fiber's detector pixel, ``hole_radius`` its radius in pixels."""

    hole_x: float
    hole_y: float
    hole_radius: float = 3.0
    radius: float = 8.0
    threshold: float = 2.0
    min_snr: float = 5.0
    fit: bool = True
    #: The star's FWHM (pixels) for the fit; None: learned from frames in
    #: which the star is clear of the hole (free fit until then).
    fwhm: float | None = None
    name: str = "annulus"
    _learned: float | None = field(default=None, init=False, repr=False)

    def measure(self, frame: Frame, guess: tuple[float, float] | None = None) -> Measurement:
        img = frame.data.astype(float, copy=False)
        bkg, noise = background_and_noise(img)
        sub = img - bkg
        hx, hy = self.hole_x - frame.roi.x, self.hole_y - frame.roi.y
        h, w = sub.shape
        rows, cols = np.mgrid[0:h, 0:w]
        outside = (cols - hx) ** 2 + (rows - hy) ** 2 > self.hole_radius**2
        g = None if guess is None else (guess[1] - frame.roi.y, guess[0] - frame.roi.x)
        masked = np.where(outside, sub, 0.0)
        r0, c0 = _peak(masked, g)
        peak = _local_max(masked, r0, c0)
        if peak < self.min_snr * noise:
            return Measurement.invalid(f"no star: peak {peak:.1f} < {self.min_snr} x {noise:.1f}")
        # Start from the plain (biased) centre of mass of the light outside
        # the hole: much closer to the truth than the brightest pixel, which
        # sits on the rim of the hole.
        wts = np.where(outside & (sub > self.threshold * noise), sub, 0.0)
        d2 = (rows - r0) ** 2 + (cols - c0) ** 2
        wts = np.where(d2 <= (self.radius + self.hole_radius) ** 2, wts, 0.0)
        xs, ys, tot, fwhm = _moments(sub, rows, cols, wts)
        if not math.isfinite(xs):
            return Measurement.invalid("no flux around the hole")
        com = WindowedCOM(radius=self.radius, threshold=self.threshold)
        com.iterations = 80
        hole = (hx, hy, float(self.hole_radius))
        if not self.fit:
            return com._iterate(frame, sub, noise, ys, xs, bkg, peak, hole)
        start = Measurement(
            xs + frame.roi.x, ys + frame.roi.y, tot, fwhm, peak=peak, background=bkg
        )
        known = self.fwhm if self.fwhm else self._learned
        sigma = None if known is None else known / SIGMA_TO_FWHM
        m = GaussianFit(radius=self.radius, min_snr=self.min_snr).fit(
            frame, guess, (self.hole_x, self.hole_y, self.hole_radius), start, sigma
        )
        # With the core in the hole a free width is degenerate with the
        # amplitude: distrust a fit whose width or flux runs away, and fall
        # back to the symmetric centre of mass.
        sane = (
            m.valid
            and 0.2 * tot < m.flux < 5.0 * max(tot, 1.0)
            and math.isfinite(m.fwhm)
            and m.fwhm < self.radius
        )
        if not sane:
            return com._iterate(frame, sub, noise, ys, xs, bkg, peak, hole)
        if sigma is None:
            self.learn(m)
        return m

    def learn(self, m: Measurement) -> None:
        """Learn the seeing FWHM from a star clear of the hole."""
        if not (m.valid and math.isfinite(m.fwhm) and m.fwhm > 0):
            return
        if math.hypot(m.x - self.hole_x, m.y - self.hole_y) > self.hole_radius + 1.5 * m.fwhm:
            self._learned = m.fwhm if self._learned is None else 0.8 * self._learned + 0.2 * m.fwhm


def find_stars(
    data: np.ndarray, *, nsigma: float = 5.0, max_stars: int = 20, separation: int = 5
) -> list[tuple[float, float, float]]:
    """Candidate stars in a frame: local maxima of the 3x3-smoothed image
    above ``nsigma`` x the noise, brightest first, at least ``separation``
    pixels apart. Returns (column, row, smoothed peak) in frame pixels."""
    img = data.astype(float, copy=False)
    bkg, noise = background_and_noise(img)
    sub = img - bkg
    h, w = sub.shape
    if h < 3 or w < 3:
        return []
    sm = np.zeros_like(sub)
    sm[1:-1, 1:-1] = (
        sum(sub[1 + di : h - 1 + di, 1 + dj : w - 1 + dj] for di in (-1, 0, 1) for dj in (-1, 0, 1))
        / 9.0
    )
    noise_sm = noise / 3.0
    core = sm[1:-1, 1:-1]
    is_max = core > nsigma * noise_sm
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di or dj:
                nb = sm[1 + di : h - 1 + di, 1 + dj : w - 1 + dj]
                is_max &= core >= nb
    rr, cc = np.nonzero(is_max)
    peaks = core[rr, cc]
    order = np.argsort(-peaks)
    out: list[tuple[float, float, float]] = []
    for k in order:
        r, c = int(rr[k]) + 1, int(cc[k]) + 1
        if any((c - x) ** 2 + (r - y) ** 2 < separation**2 for x, y, _ in out):
            continue
        out.append((float(c), float(r), float(peaks[k])))
        if len(out) >= max_stars:
            break
    return out


CENTROIDERS = {"com": WindowedCOM, "quadratic": QuadraticPeak, "gaussian": GaussianFit}


def make_centroider(spec: dict | None, fiber: tuple[float, float, float] | None = None):
    """A centroider from config: ``{"kind": "com"|"quadratic"|"gaussian"|
    "annulus", ...its fields}``. ``annulus`` needs the fiber (x, y,
    radius)."""
    spec = dict(spec or {"kind": "com"})
    kind = spec.pop("kind", "com")
    if kind == "annulus":
        if fiber is None:
            raise ValueError("the annulus centroider needs the fiber position")
        spec.setdefault("hole_radius", fiber[2])
        return AnnulusCentroider(fiber[0], fiber[1], **spec)
    try:
        return CENTROIDERS[kind](**spec)
    except KeyError:
        raise ValueError(f"unknown centroider {kind!r}") from None
