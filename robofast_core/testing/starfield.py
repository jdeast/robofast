"""Synthetic star images for tests: autofocus, the guider, the image pipeline.

One renderer serves both uses. A star's image is the **geometric image of
the pupil** (a uniform disk, or an annulus with a central obstruction: the
"donut" of a defocused reflector) **convolved with the seeing** (a Moffat or
Gaussian of the given FWHM), sampled on sub-pixels and binned to pixels.
In focus the pupil image shrinks to nothing and the star is the seeing
profile; far from focus it is a donut whose outer diameter is::

    D = |dz| / N                       (focal plane, same units as dz)

where ``dz`` is the focal-plane defocus and ``N`` the focal ratio of the
beam. In pixels that is ``|dz| / (N * pixel_size)`` (:func:`blur_diameter`).
How a focuser position maps to ``dz`` belongs to the caller (a focuser that
moves the instrument moves the focal plane one for one; one that moves the
secondary moves it by a larger factor).

Two optional terms let a test give the V-curve the shape of a real one:
``SimOptics.arm_asymmetry`` scales the blur on the two sides of focus
differently (unequal V-curve slopes), and ``SimOptics.blur_offset`` adds a
constant to the geometric blur away from focus (the arms' extrapolated
zero-points then lie off the true focus, as with spherical aberration).

Per frame: tip/tilt **jitter** (a random shift of the whole field, rms in
arcsec, plus an extra Gaussian blur of the same rms for the motion during the
exposure), a deterministic **offset** (drift, for the guider), sky,
Poisson and read noise, bias, gain, saturation, a **region of interest** and
**binning** (as the Alpaca camera's ``StartX``/``StartY``/``NumX``/``NumY``/
``BinX``). Pixel coordinates are 0-based, unbinned, full frame, with
``(x, y)`` = (column, row) and the centre of the first pixel at ``(0, 0)``;
images are numpy ``(rows, columns)``.

Only numpy: this module is imported by the fast test tier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

__all__ = [
    "Detector",
    "Frame",
    "SimOptics",
    "Star",
    "blur_diameter",
    "random_stars",
    "render",
    "render_stamp",
]

#: Moffat beta for atmospheric seeing (Trujillo et al. 2001).
MOFFAT_BETA = 4.765


@dataclass(frozen=True)
class SimOptics:
    """The telescope + camera geometry the renderer needs."""

    #: arcsec per unbinned pixel.
    pixel_scale: float = 0.6
    #: Unbinned pixel size, microns.
    pixel_size: float = 9.0
    #: Focal ratio of the converging beam at the detector.
    f_ratio: float = 6.5
    #: Central obstruction, as a fraction of the pupil *diameter* (0: none).
    obstruction: float = 0.47
    #: Blur on the positive side of focus is ``(1 + a)`` times, and on the
    #: negative side ``(1 - a)`` times, the symmetric value.
    arm_asymmetry: float = 0.0
    #: Microns of focal-plane defocus added to ``|dz|`` (the geometric blur
    #: is ``max(0, |dz| + blur_offset) / N``).
    blur_offset: float = 0.0

    def blur_um(self, dz: float) -> float:
        """Geometric blur diameter (outer edge of the donut) in microns."""
        scale = 1.0 + self.arm_asymmetry * (1.0 if dz > 0 else -1.0)
        return max(0.0, abs(dz) + self.blur_offset) * scale / self.f_ratio


def blur_diameter(dz: float, optics: SimOptics) -> float:
    """Geometric blur (donut outer) diameter in unbinned pixels for a
    focal-plane defocus ``dz`` in microns."""
    return optics.blur_um(dz) / optics.pixel_size


@dataclass(frozen=True)
class Star:
    """A star at unbinned full-frame pixel ``(x, y)``; ``flux`` is its total
    signal in electrons per second of exposure."""

    x: float
    y: float
    flux: float


@dataclass(frozen=True)
class Detector:
    """A camera. ``shape`` is the full unbinned frame, (rows, columns)."""

    shape: tuple[int, int] = (512, 512)
    #: electrons per ADU.
    gain: float = 1.5
    #: electrons rms.
    read_noise: float = 6.0
    #: ADU.
    bias: float = 1000.0
    #: ADU; pixels are clipped here (and at 0).
    saturation: float = 65535.0
    #: Sky, electrons per unbinned pixel per second.
    sky: float = 20.0
    #: Dark current, electrons per unbinned pixel per second.
    dark: float = 0.0


@dataclass
class Frame:
    """A rendered frame. ``image`` is ``(rows, columns)`` int32 ADU (float64
    with ``noise=False``); its first pixel is unbinned full-frame ``(x0,
    y0)`` and each pixel spans ``binning`` unbinned pixels. ``truth`` holds
    what the renderer used (seeing, defocus, blur, field shift, ...)."""

    image: np.ndarray
    x0: int = 0
    y0: int = 0
    binning: int = 1
    truth: dict = field(default_factory=dict)

    def to_full(self, x: float, y: float) -> tuple[float, float]:
        """Frame pixel ``(x, y)`` -> unbinned full-frame coordinates."""
        b = self.binning
        return self.x0 + (x + 0.5) * b - 0.5, self.y0 + (y + 0.5) * b - 0.5


def random_stars(
    n: int,
    shape: tuple[int, int],
    *,
    flux: tuple[float, float] = (2e4, 2e5),
    margin: float = 40.0,
    min_separation: float = 0.0,
    rng: np.random.Generator | None = None,
) -> list[Star]:
    """``n`` stars uniformly placed at least ``margin`` pixels from the
    edges of a ``shape`` (rows, columns) frame, with log-uniform fluxes, at
    least ``min_separation`` pixels apart (best effort)."""
    rng = np.random.default_rng() if rng is None else rng
    ny, nx = shape
    stars: list[Star] = []
    tries = 0
    while len(stars) < n and tries < 100 * max(n, 1):
        tries += 1
        x = rng.uniform(margin, nx - 1 - margin)
        y = rng.uniform(margin, ny - 1 - margin)
        if min_separation and any(math.hypot(s.x - x, s.y - y) < min_separation for s in stars):
            continue
        f = math.exp(rng.uniform(math.log(flux[0]), math.log(flux[1])))
        stars.append(Star(x, y, f))
    return stars


def _profile(r2: np.ndarray, fwhm: float, profile: str) -> np.ndarray:
    if fwhm <= 0:
        out = np.zeros_like(r2)
        out[np.unravel_index(np.argmin(r2), r2.shape)] = 1.0
        return out
    if profile == "gaussian":
        sigma = fwhm / (2.0 * math.sqrt(2.0 * math.log(2.0)))
        return np.exp(-0.5 * r2 / sigma**2)
    if profile == "moffat":
        alpha = fwhm / (2.0 * math.sqrt(2.0 ** (1.0 / MOFFAT_BETA) - 1.0))
        return (1.0 + r2 / alpha**2) ** (-MOFFAT_BETA)
    raise ValueError(f"unknown profile {profile!r}")


def render_stamp(
    size: int,
    dx: float,
    dy: float,
    *,
    fwhm_px: float,
    blur_px: float,
    obstruction: float = 0.0,
    jitter_px: float = 0.0,
    profile: str = "moffat",
    oversample: int = 4,
) -> np.ndarray:
    """A unit-flux star image, ``size x size`` unbinned pixels, centred at
    ``(size - 1) / 2 + (dx, dy)``: the pupil image (``blur_px`` outer
    diameter, ``obstruction`` fractional inner diameter) convolved with the
    seeing (``fwhm_px``) and a Gaussian of ``jitter_px`` rms. Flux falling
    beyond the stamp wraps around (pick ``size`` generously)."""
    k = max(1, int(oversample))
    n = size * k
    # Everything is built centred on sub-pixel (0, 0) (wrapped), convolved,
    # then moved to the star by a Fourier phase ramp: exact sub-pixel
    # placement for any pupil size, including a point.
    g = np.fft.fftfreq(n, d=1.0 / n) / k  # wrapped offsets, pixels
    r2 = g[None, :] ** 2 + g[:, None] ** 2
    kern = _profile(r2, fwhm_px, profile)
    fk = np.fft.rfft2(kern / kern.sum())
    if jitter_px > 0:
        jit = np.exp(-0.5 * r2 / jitter_px**2)
        fk = fk * np.fft.rfft2(jit / jit.sum())
    radius = blur_px / 2.0
    if radius * k < 0.5:
        fp = 1.0  # a point
    else:
        r = np.sqrt(r2)
        # Anti-aliased annulus: each sub-pixel's coverage by a linear ramp.
        w = 1.0 / k
        pupil = np.clip((radius - r) / w + 0.5, 0.0, 1.0)
        if obstruction > 0:
            pupil -= np.clip((obstruction * radius - r) / w + 0.5, 0.0, 1.0)
        fp = np.fft.rfft2(pupil / pupil.sum())
    # Sub-pixel 0 is at pixel coordinate 0.5/k - 0.5; move it to the star.
    c = (size - 1) / 2.0
    sx = (c + dx - (0.5 / k - 0.5)) * k
    sy = (c + dy - (0.5 / k - 0.5)) * k
    u = np.fft.rfftfreq(n)[None, :]
    v = np.fft.fftfreq(n)[:, None]
    ramp = np.exp(-2j * np.pi * (u * sx + v * sy))
    img = np.fft.irfft2(fp * fk * ramp, s=(n, n))
    img = np.clip(img, 0.0, None)
    binned = img.reshape(size, k, size, k).sum(axis=(1, 3))
    total = binned.sum()
    return binned / total if total > 0 else binned


def render(
    stars: list[Star],
    optics: SimOptics,
    detector: Detector,
    *,
    seeing: float = 2.0,
    defocus: float = 0.0,
    exposure: float = 1.0,
    roi: tuple[int, int, int, int] | None = None,
    binning: int = 1,
    offset: tuple[float, float] = (0.0, 0.0),
    jitter: float = 0.0,
    profile: str = "moffat",
    noise: bool = True,
    oversample: int = 4,
    rng: np.random.Generator | None = None,
) -> Frame:
    """Render one exposure.

    ``seeing`` is the FWHM in arcsec; ``defocus`` the focal-plane defocus in
    microns (sign matters only through ``optics.arm_asymmetry``);
    ``exposure`` seconds. ``roi`` = ``(x0, y0, nx, ny)`` in unbinned
    pixels (``nx``, ``ny`` multiples of ``binning``), default the full
    frame. ``offset`` shifts every star by ``(dx, dy)`` unbinned pixels;
    ``jitter`` adds a random field shift of that rms (arcsec, each axis) and
    the same Gaussian blur. With ``noise=False`` the image is the noiseless
    expectation in ADU (float) without clipping."""
    rng = np.random.default_rng() if rng is None else rng
    ny_full, nx_full = detector.shape
    x0, y0, nx, ny = roi if roi is not None else (0, 0, nx_full, ny_full)
    b = max(1, int(binning))
    if nx % b or ny % b:
        raise ValueError("roi width and height must be multiples of binning")
    fwhm_px = seeing / optics.pixel_scale
    blur_px = blur_diameter(defocus, optics)
    jit_px = jitter / optics.pixel_scale
    shift = (offset[0], offset[1])
    if jit_px > 0:
        shift = (shift[0] + rng.normal(0, jit_px), shift[1] + rng.normal(0, jit_px))
    electrons = np.zeros((ny, nx))
    # Stamp: enough for the donut plus generous seeing wings.
    size = math.ceil(blur_px + 8 * max(fwhm_px, 1.0) + 6 * jit_px) | 1
    size = max(size, 15)
    for s in stars:
        sx, sy = s.x + shift[0] - x0, s.y + shift[1] - y0
        cx, cy = round(sx), round(sy)
        half = size // 2
        if cx + half < 0 or cy + half < 0 or cx - half >= nx or cy - half >= ny:
            continue
        stamp = render_stamp(
            size,
            sx - cx,
            sy - cy,
            fwhm_px=fwhm_px,
            blur_px=blur_px,
            obstruction=optics.obstruction,
            jitter_px=jit_px,
            profile=profile,
            oversample=oversample,
        )
        ya, xa = cy - half, cx - half
        ys, xs = max(0, ya), max(0, xa)
        ye, xe = min(ny, ya + size), min(nx, xa + size)
        electrons[ys:ye, xs:xe] += s.flux * exposure * stamp[ys - ya : ye - ya, xs - xa : xe - xa]
    background = (detector.sky + detector.dark) * exposure
    if noise:
        electrons = rng.poisson(np.clip(electrons + background, 0, None)).astype(float)
        electrons += rng.normal(0.0, detector.read_noise, electrons.shape)
    else:
        electrons = electrons + background
    if b > 1:
        electrons = electrons.reshape(ny // b, b, nx // b, b).sum(axis=(1, 3))
    adu = electrons / detector.gain + detector.bias
    if noise:
        adu = np.clip(np.rint(adu), 0, detector.saturation).astype(np.int32)
    truth = {
        "seeing": seeing,
        "fwhm_px": fwhm_px,
        "defocus": defocus,
        "blur_px": blur_px,
        "shift": shift,
        "exposure": exposure,
        "sky_adu": background * b * b / detector.gain + detector.bias,
    }
    return Frame(adu, x0, y0, b, truth)


def with_defaults(optics: SimOptics | None = None, **changes: float) -> SimOptics:
    """``optics`` (default :class:`SimOptics`) with ``changes`` applied."""
    return replace(optics or SimOptics(), **changes)
