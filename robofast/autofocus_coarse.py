"""The autofocus's coarse mode: from far out of focus, a bright object's
light, then its donut, then the fine sweep (docs/autofocus.md, "Coarse
mode"). Run through :class:`robofast.autofocus.Autofocus` (``mode="coarse"``,
or automatically when the fine mode fails: ``coarse_fallback``); this module
has the measurement. First proposed by Guven Gurkan (PR #132).

A telescope plugged in for the first time, or one whose focus was lost, can
be so far from focus that a star's image covers the chip: no star is found,
and the fine mode (V-curve, donut, sweep), which starts from a focus it can
see, has nothing to measure. Far from focus a star is the image of the
pupil, of diameter ``D = |dz| / N``, and its light is spread over that
disk: so the signal is the frame's level over the sky beside the target at
the same exposure, and it rises as the disk shrinks onto the chip.
:func:`approach`:

1. **light**: the level at the start, then a step either way, the step
   doubled until either way is lit (the whole travel unlit: no light);
2. **climb**: steps the way the level rises, reversing at half the step
   when it falls, until the star is measured as one whole donut
   (:func:`robofast.starmeasure.measure`); at an end of the focuser's
   travel with the level still rising, the focus is beyond it;
3. **donut**: two frames a known step apart give the donut's diameter per
   step as measured (the focuser's units and how far it moves the focal
   plane are not assumed), and the focus where the diameter reaches zero;
   it moves there, again until the donut is small, and hands over to the
   fine mode's sweep, which fits the curve itself: the measured slope sizes
   the sweep, and a few per cent of error in it (the seeing, near focus)
   does not bias the focus as it would the two-point V-curve's.

Every step is one line of named numbers. :func:`bright_target` names a
bright planet or star to focus on from a site at a time.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from robofast.starmeasure import MeasureConfig, measure

#: The level counts as light above this many standard errors of the dark's mean.
LIGHT_SIGMA = 5.0
#: A step "rises" when the level gains more than LIGHT_SIGMA and this
#: fraction of it (a sky that changes a little between frames).
RISE = 0.005
#: The donut is small enough to hand to the autofocus at this many seeing FWHM.
HANDOVER_FWHM = 8.0
#: A frame with a pixel above this fraction of the camera's full scale is
#: taken again at a quarter of the exposure (clipped light is lost light:
#: the level would fall as the donut shrinks).
FULL = 0.9


class _Exposure:
    """Frames that do not saturate: the exposure shortened as the light
    gathers (while that clips fewer pixels); levels at the first exposure's
    scale, so steps compare."""

    def __init__(
        self, expose, exptime: float, saturation: float, dark: Level, floor: float = 0.0
    ) -> None:
        self.expose, self.t0, self.t = expose, exptime, exptime
        self.saturation, self.dark = saturation, dark
        #: The camera's shortest exposure (ExposureMin, s): never asked below.
        self.floor = floor

    async def frame(self) -> tuple[np.ndarray, float]:
        img = np.asarray(await self.expose(self.t))
        clipped = int(np.count_nonzero(img >= FULL * self.saturation))
        for _ in range(8):
            shorter = max(self.t / 4.0, self.floor)
            if not clipped or shorter >= self.t:
                break
            short = np.asarray(await self.expose(shorter))
            n = int(np.count_nonzero(short >= FULL * self.saturation))
            if n >= clipped:
                # A shorter exposure clips as many pixels: they are not the
                # light it gathers (a hot pixel); keep this exposure.
                break
            img, clipped, self.t = short, n, shorter
        return img, self.dark.sigma(img, self.t)

    def shown(self, img: np.ndarray, star: _Sizes) -> tuple[float | None, float, str]:
        """What a line shows: (how bright, as a multiple of what; the
        exposure, s; of what: "the sky around it" or "the noise around it",
        a measured star's brightest pixel in the same frame, over the sky
        if the sky shows above the frame's noise; else "the sky beside it",
        the frame's light over the sky beside the target)."""
        if star.above is not None and star.sky is not None:
            sky, noise = star.sky - self.dark.bias, star.noise or 0.0
            if sky > noise:
                return star.above / sky, self.t, "the sky around it"
            return star.above / max(noise, 1e-9), self.t, "the noise around it"
        return self.dark.times(img, self.t), self.t, "the sky beside it"


def _rises(now: Step, before: Step) -> bool:
    return now.level > before.level + max(LIGHT_SIGMA, RISE * abs(before.level))


class FocusError(RuntimeError):
    """Why the focus could not be found (one line, what to do)."""


class TooBright(FocusError):
    """The target needs a shorter exposure (``need``, s) than the camera's
    ``shortest``."""

    def __init__(self, need: float, shortest: float) -> None:
        self.need, self.shortest = need, shortest
        super().__init__(
            f"the target is too bright for this camera's shortest exposure ({shortest:g} s;"
            f" focus needs about {need:.2g} s)"
        )


@dataclass
class Level:
    """The reference's level: its mean at the first exposure (ADU), the
    standard error of a frame's mean (ADU), and the part of the mean a
    shorter exposure keeps (the camera's bias, ADU), from a second frame at
    a quarter of the exposure."""

    mean: float
    err: float
    bias: float
    exptime: float

    @classmethod
    def of(
        cls, dark: np.ndarray, other: np.ndarray, exptime: float, other_t: float | None = None
    ) -> Level:
        """From ``dark`` (``exptime`` s) and ``other`` (``other_t`` s, by
        default a quarter of it): the mean is the bias plus the sky's rate
        times the exposure."""
        t2 = exptime / 4.0 if other_t is None else other_t
        d = np.asarray(dark, dtype=float)
        m, m2 = float(d.mean()), float(np.asarray(other, dtype=float).mean())
        err = max(float(d.std()) / math.sqrt(d.size), 1e-9)
        rate = (m - m2) / (exptime - t2)
        return cls(m, err, m - rate * exptime, exptime)

    def times(self, image: np.ndarray, t: float) -> float | None:
        """``image``'s (``t`` s) light above the bias and the sky, as a
        multiple of the sky beside the target at ``t``; None if that sky
        is within its own error (a multiple of it would be noise)."""
        sky = (self.mean - self.bias) * t / self.exptime
        if sky <= LIGHT_SIGMA * self.err:
            return None
        mean = float(np.asarray(image, dtype=float).mean())
        return (mean - self.bias - sky) / sky

    def sigma(self, image: np.ndarray, t: float) -> float:
        """``image``'s (``t`` s) level over the reference at ``t``, in
        standard errors at the first exposure's scale."""
        at_t = self.bias + (self.mean - self.bias) * t / self.exptime
        mean = float(np.asarray(image, dtype=float).mean())
        return (mean - at_t) / self.err * (self.exptime / t)


@dataclass
class Step:
    """One frame of the approach (a line of the log)."""

    stage: str
    position: float
    #: The frame's level over the reference (standard errors at the first
    #: exposure's scale): compared between frames, never printed (its
    #: reference is a field 2 deg away).
    level: float
    donut: float | None = None
    note: str = ""
    #: What the line shows (:meth:`_Exposure.shown`): how bright, as a
    #: multiple of the sky, the exposure (s), and whether of a measured star.
    times: float | None = None
    exptime: float = 0.0
    of: str = "the sky beside it"

    @property
    def lit(self) -> bool:
        """Light on the chip: the level above the reference's noise, or a
        star measured."""
        return self.level >= LIGHT_SIGMA or self.donut is not None

    def line(self) -> str:
        d = "-" if self.donut is None else f"{self.donut:.1f}"
        out = f"focus: {self.stage:<6} position {self.position:.0f}"
        t = self.times
        n = "" if t is None else f"{t:,.0f}" if abs(t) >= 100 else f"{t:.2g}"
        if self.of != "the sky beside it":
            out += f"  star {n} times {self.of}"
        elif self.lit:
            out += f"  light {n} times {self.of}" if n else "  light on the chip"
        else:
            out += "  no light above the sky beside it"
        out += f"  in {self.exptime:g} s"
        return out + f"  donut {d} px" + (f"  {self.note}" if self.note else "")


@dataclass
class Approach:
    """Where :func:`approach` ended: the position, the donut's measured
    diameter per focuser step (px per step), for the autofocus; the last
    exposure (s), at which the star did not clip, and the star's last size
    (donut or HFD, px), for the sweep's settings (:func:`sweep_settings`)."""

    position: float
    slope: float | None
    steps: list[Step] = field(default_factory=list)
    exptime: float = 0.0
    donut: float | None = None


@dataclass
class _Sizes:
    """The brightest star whole on the chip (fainter stars' rings in the
    same field are not it): its donut diameter and HFD (px), its brightest
    pixel above the sky around it, that sky and the frame's noise (ADU);
    None if none."""

    donut: float | None = None
    hfd: float | None = None
    above: float | None = None
    sky: float | None = None
    noise: float | None = None


def _sizes(image: np.ndarray, fwhm_px: float) -> _Sizes:
    smooth = max(1.0, fwhm_px / 2.0)
    try:
        m = measure(
            image,
            MeasureConfig(smooth=smooth, min_snr=10.0, max_ellipticity=0.5, background_box=512),
        )
    except ValueError:
        return _Sizes()
    if not m.stars:
        return _Sizes()
    best = max(m.stars, key=lambda st: st.flux)
    return _Sizes(
        _finite(best.donut), _finite(best.hfd), float(best.peak - m.sky), float(m.sky),
        _finite(m.noise),
    )  # fmt: skip


def _finite(v: float) -> float | None:
    return float(v) if np.isfinite(v) else None


async def approach(
    expose: Callable[[float], Awaitable[np.ndarray]],
    move: Callable[[float], Awaitable[None]],
    *,
    start: float,
    limits: tuple[float, float],
    dark: Level,
    fwhm_px: float,
    step: float,
    exptime: float,
    saturation: float,
    say: Callable[[str], None] = print,
    exposure_min: float = 0.0,
) -> Approach:
    """Approach the focus from ``start`` (module doc). ``expose(t)`` takes a
    ``t`` s frame of the bright object at the current position (``dark``:
    the reference, :meth:`Level.of`; ``saturation``: the camera's full scale, ADU,
    from which the exposure is shortened); ``move(x)`` moves
    the focuser; ``step`` the first step (focuser units); ``fwhm_px`` the
    seeing's FWHM (px), for when to hand over. :class:`FocusError` saying
    why it cannot."""
    lo, hi = limits
    log: list[Step] = []
    exposure = _Exposure(expose, exptime, saturation, dark, exposure_min)

    sizes: list[float | None] = []

    async def at(x: float, stage: str, note: str = "") -> Step:
        x = min(hi, max(lo, x))
        await move(x)
        img, level = await exposure.frame()
        z = _sizes(img, fwhm_px)
        sizes.append(z.hfd)
        s = Step(stage, x, level, z.donut, note, *exposure.shown(img, z))
        log.append(s)
        say(s.line())
        return s

    here = await at(start, "light")
    # Near focus already: a small star and no donut to measure.
    if here.donut is None and sizes[-1] is not None and sizes[-1] <= HANDOVER_FWHM * fwhm_px:
        here.note = "near focus already: the autofocus takes it from here"
        say(f"focus: near focus already (star {sizes[-1]:.1f} px across)")
        return Approach(here.position, None, log, exposure.t, sizes[-1])
    # 1. Light: either way, the step doubled until one way is lit.
    s = step
    while not here.lit:
        up = await at(here.position + s, "light")
        down = await at(here.position - s, "light")
        # A measured donut wins (a star field: its level hardly changes).
        best = max((up, down), key=lambda t: (t.donut is not None, t.level))
        if best.lit:
            here = best
            break
        if up.position >= hi and down.position <= lo:
            raise FocusError(
                "no light at any focus position: the bright object is not on the chip,"
                " or the focus is very far off (focus by day on something far away first)"
            )
        s *= 2.0
    # 2. Climb the level until the donut is whole on the chip.
    way = 1.0
    if here.donut is None:
        ahead = await at(here.position + way * s, "climb")
        if not _rises(ahead, here):
            way = -1.0
            ahead = await at(here.position + way * s, "climb")
        while ahead.donut is None:
            if _rises(ahead, here):
                here = ahead
                if here.position in (lo, hi):
                    raise _beyond(here.position == hi, here.position)
            else:
                way, s = -way, s / 2.0
                if s < 1.0:
                    raise FocusError(
                        "the star's light peaks with no whole donut: it is at the chip's"
                        " edge (point at it again) or the sky is changing"
                    )
            ahead = await at(here.position + way * s, "climb")
        here = ahead
    # 3. The donut: its diameter per step, measured, and its zero.
    slope = None
    target = HANDOVER_FWHM * fwhm_px
    for _ in range(8):
        if here.donut is None:
            raise FocusError("the donut was lost: the sky or the pointing changed")
        if here.donut <= target and slope is not None:
            break
        probe = s if slope is None else max(1.0, here.donut / slope / 4.0)
        nxt = await at(here.position + way * probe, "donut")
        if nxt.donut is None:
            raise FocusError("the donut was lost: the sky or the pointing changed")
        moved = abs(nxt.position - here.position)
        if not moved:  # at an end of the travel, the focus still that way
            raise _beyond(nxt.position == hi, nxt.position)
        if nxt.donut > here.donut:  # growing: the focus is the other way
            way = -way
        slope = abs(nxt.donut - here.donut) / moved if nxt.donut != here.donut else slope
        if not slope:
            raise FocusError("the donut does not change with the focuser: is it moving?")
        # Where the donut's diameter reaches zero: beyond the travel, say so.
        focus = nxt.position + way * nxt.donut / slope
        if not lo <= focus <= hi:
            raise _beyond(focus > hi, focus)
        # Stop short, on this side: the autofocus measures from there.
        go = max(0.0, (nxt.donut - target / 2.0) / slope)
        here = await at(nxt.position + way * go, "donut", f"slope {slope:.4f} px/step")
    return Approach(here.position, slope, log, exposure.t, log[-1].donut or sizes[-1])


def _beyond(upper: bool, where: float) -> FocusError:
    end = "upper" if upper else "lower"
    fix = "add a spacer or extension" if upper else "remove a spacer or extension"
    return FocusError(
        f"the focus is beyond the focuser's {end} end (near position {where:.0f}): the"
        f" star still shrinks that way; {fix} (if larger positions rack the focuser out)"
    )


#: The planets to focus on from far out, brightest first (typical V mag).
PLANETS = (("Venus", -4.0), ("Jupiter", -2.5), ("Mars", 0.0), ("Saturn", 0.7))
#: Then the brightest stars (name, RA deg, Dec deg J2000, V mag).
BRIGHT_STARS = (
    ("Sirius", 101.287, -16.716, -1.46),
    ("Canopus", 95.988, -52.696, -0.74),
    ("Arcturus", 213.915, 19.182, -0.05),
    ("Vega", 279.235, 38.784, 0.03),
    ("Capella", 79.172, 45.998, 0.08),
    ("Rigel", 78.634, -8.202, 0.13),
    ("Procyon", 114.825, 5.225, 0.34),
    ("Betelgeuse", 88.793, 7.407, 0.50),
    ("Altair", 297.696, 8.868, 0.77),
    ("Aldebaran", 68.980, 16.509, 0.85),
    ("Antares", 247.352, -26.432, 0.96),
    ("Spica", 201.298, -11.161, 0.97),
    ("Pollux", 116.329, 28.026, 1.14),
    ("Fomalhaut", 344.413, -29.622, 1.16),
    ("Deneb", 310.358, 45.280, 1.25),
    ("Regulus", 152.093, 11.967, 1.35),
)
#: Lowest altitude (deg) for a focus target.
MIN_ALT = 30.0


@dataclass
class Target:
    name: str
    ra: float  # deg, of date for a planet, J2000 for a star
    dec: float
    mag: float
    alt: float
    #: Its angle from the Sun (deg).
    sun: float = 180.0


def targets_up(
    site: tuple[float, float, float], when, min_sun_separation: float = 0.0
) -> list[Target]:
    """Every listed planet and star above :data:`MIN_ALT` from ``site`` at
    ``when`` and at least ``min_sun_separation`` deg from the Sun, the
    planets first, each list brightest first."""
    return [t for t in _targets(site, when) if t.alt >= MIN_ALT and t.sun >= min_sun_separation]


def _targets(site: tuple[float, float, float], when) -> list[Target]:
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, solar_system_ephemeris
    from astropy.time import Time

    from robofast.sky import offline_iers

    offline_iers()
    t = Time(when)
    loc = EarthLocation(lat=site[0] * u.deg, lon=site[1] * u.deg, height=site[2] * u.m)
    frame = AltAz(obstime=t, location=loc)
    out: list[Target] = []
    with solar_system_ephemeris.set("builtin"):
        sun = get_body("sun", t, loc)
        for name, mag in PLANETS:
            c = get_body(name.lower(), t, loc)
            alt = float(c.transform_to(frame).alt.deg)
            sep = float(c.separation(sun).deg)
            out.append(Target(name, float(c.ra.deg), float(c.dec.deg), mag, alt, sep))
    for name, ra, dec, mag in BRIGHT_STARS:
        c = SkyCoord(ra * u.deg, dec * u.deg)
        alt = float(c.transform_to(frame).alt.deg)
        sep = float(c.separation(SkyCoord(sun.ra, sun.dec)).deg)
        out.append(Target(name, ra, dec, mag, alt, sep))
    return out


def bright_target(
    site: tuple[float, float, float],
    when,
    name: str | None = None,
    *,
    min_sun_separation: float = 0.0,
) -> Target | None:
    """What to focus on now from ``site``: the brightest planet above
    :data:`MIN_ALT`, else the brightest star above it, either at least
    ``min_sun_separation`` deg from the Sun (the Moon is not used: on a
    small chip it is a disk with no edge in the frame); None: nothing.
    With ``name``, that planet or star, or :class:`FocusError` if it is not
    listed, is below :data:`MIN_ALT` or is too near the Sun."""
    if name is None:
        up = targets_up(site, when, min_sun_separation)
        return up[0] if up else None
    every = {t.name.lower(): t for t in _targets(site, when)}
    t = every.get(name.strip().lower())
    if t is None:
        raise FocusError(
            f"focus target {name}: not one robofast knows; these are:"
            f" {', '.join(n for n, _ in PLANETS)}, {', '.join(s[0] for s in BRIGHT_STARS)}"
        )
    if t.alt < MIN_ALT:
        raise FocusError(
            f"focus target {t.name}: at {t.alt:.0f} deg it is below {MIN_ALT:.0f} deg now;"
            " choose one higher"
        )
    if t.sun < min_sun_separation:
        raise FocusError(
            f"focus target {t.name}: {t.sun:.0f} deg from the Sun, nearer than"
            f" {min_sun_separation:g} deg; choose another"
        )
    return t


def fainter_star(
    up: list[tuple[str, float]], mag: float, *, need: float, shortest: float
) -> tuple[str, float] | None:
    """For a target of ``mag`` that needs ``need`` s, shorter than the
    camera's ``shortest``: the brightest of ``up`` (name, V mag) at least
    2.5 log10(shortest / need) magnitudes fainter; None if none is."""
    fainter = mag + 2.5 * math.log10(shortest / need)
    ok = sorted((s for s in up if s[1] >= fainter), key=lambda s: s[1])
    return ok[0] if ok else None


def sweep_settings(
    frame: np.ndarray,
    exptime: float,
    a: Approach,
    *,
    fwhm_px: float,
    saturation: float,
    exposure_min: float = 0.0,
) -> dict[str, Any]:
    """The autofocus sweep's ``exptime`` and ``min_stars``, from one
    ``frame`` at ``exptime`` s at the handover. A star measured in it
    unclipped (a field): as they are. Only clipped stars (a planet alone):
    the approach's last exposure times (FWHM / its last size)^2, the star
    gathering into the seeing's disk at focus, and that one star is enough;
    :class:`FocusError` if that is shorter than the camera's ``exposure_min``."""
    m = measure(frame, MeasureConfig(saturation=saturation))
    if m.n or not m.rejected.get("saturated"):
        return {"exptime": exptime}
    size = max(a.donut or fwhm_px, fwhm_px)
    t = min(exptime, a.exptime * (fwhm_px / size) ** 2)
    if t < exposure_min:
        raise TooBright(t, exposure_min)
    return {"exptime": t, "min_stars": 1}
