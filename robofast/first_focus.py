"""Focus from far out: a bright object's light, then its donut, then the
autofocus (docs/first_light.md, "Focus").

A telescope plugged in for the first time can be so far from focus that a
star's image covers the chip: no star is found, and the autofocus
(:mod:`robofast.autofocus`), which starts from a focus it can see, has
nothing to measure. Far from focus a star is the image of the pupil, of
diameter ``D = |dz| / N``, and its light is spread over that disk: so the
signal is the frame's level over a dark of the same exposure, and it rises
as the disk shrinks onto the chip. :func:`approach`:

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
   autofocus's sweep (:meth:`robofast.autofocus.Autofocus.run`, ``"sweep"``),
   which fits the curve itself: the measured slope sizes the sweep, and a
   few per cent of error in it (the seeing, near focus) does not bias the
   focus as it would the two-point V-curve's.

The person's version (:func:`walk_through`) is the same measurement with a
focus knob: the frames are taken in a loop and each line says what the
star did and which way to turn. Every step is one line of named numbers.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

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

    def __init__(self, expose, exptime: float, saturation: float, dark: Level) -> None:
        self.expose, self.t0, self.t = expose, exptime, exptime
        self.saturation, self.dark = saturation, dark

    async def frame(self) -> tuple[np.ndarray, float]:
        img = np.asarray(await self.expose(self.t))
        clipped = int(np.count_nonzero(img >= FULL * self.saturation))
        for _ in range(8):
            if not clipped:
                break
            short = np.asarray(await self.expose(self.t / 4.0))
            n = int(np.count_nonzero(short >= FULL * self.saturation))
            if n >= clipped:
                # A shorter exposure clips as many pixels: they are not the
                # light it gathers (a hot pixel); keep this exposure.
                break
            img, clipped, self.t = short, n, self.t / 4.0
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
    def of(cls, dark: np.ndarray, quarter: np.ndarray, exptime: float) -> Level:
        """From ``dark`` (``exptime`` s) and ``quarter`` (``exptime / 4`` s)."""
        d = np.asarray(dark, dtype=float)
        m, mq = float(d.mean()), float(np.asarray(quarter, dtype=float).mean())
        err = max(float(d.std()) / math.sqrt(d.size), 1e-9)
        return cls(m, err, (4.0 * mq - m) / 3.0, exptime)

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
    """Where :func:`approach` ended: the position, and the donut's measured
    diameter per focuser step (px per step), for the autofocus."""

    position: float
    slope: float | None
    steps: list[Step] = field(default_factory=list)


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
    exposure = _Exposure(expose, exptime, saturation, dark)

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
        return Approach(here.position, None, log)
    # 1. Light: either way, the step doubled until one way is lit.
    s = step
    while not here.lit:
        up = await at(here.position + s, "light")
        down = await at(here.position - s, "light")
        best = max((up, down), key=lambda t: t.level)
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
    return Approach(here.position, slope, log)


def _beyond(upper: bool, where: float) -> FocusError:
    end = "upper" if upper else "lower"
    fix = "add a spacer or extension" if upper else "remove a spacer or extension"
    return FocusError(
        f"the focus is beyond the focuser's {end} end (near position {where:.0f}): the"
        f" star still shrinks that way; {fix} (if larger positions rack the focuser out)"
    )


#: Far from focus (the donut larger than this many FWHM) the donut's size
#: is a straight line of the knob's turn; nearer, the star's HFD is used.
LINEAR_FWHM = 4.0
#: The sweep by hand: points at these fractions of its half-width W either
#: side of the estimated focus (W: the donut at 6 FWHM, as the autofocus's
#: near offset), then a turn to the hyperbola's minimum (robofast.focusfit).
SWEEP = (1.0, 0.5, 0.0, -0.5, -1.0)


async def walk_through(
    expose: Callable[[float], Awaitable[np.ndarray]],
    ask: Callable[[str], Awaitable[str]],
    *,
    dark: Level,
    fwhm_px: float,
    exptime: float,
    saturation: float,
    say: Callable[[str], None] = print,
    max_frames: int = 60,
) -> list[Step]:
    """The person turns the focus knob; each frame says what the star did
    and what to do next (module doc). ``ask(prompt)`` returns their answer:
    empty to take the next frame, ``q`` to stop. Every turn asked for is a
    multiple of their last, so the knob is followed in units of their first
    quarter turn. Far from focus the donut's size per unit says where the
    focus is; there, the person is taken through five points around it and
    then to the minimum of the star sizes' fitted curve (the autofocus's
    sweep, by hand)."""
    from robofast.focusfit import FitError, fit_hyperbola

    log: list[Step] = []
    exposure = _Exposure(expose, exptime, saturation, dark)
    knob, last = 0.0, 0.0  # in units of the first quarter turn
    per_unit: float | None = None
    prev: Step | None = None
    plan: list[float] = []  # knob positions still to visit (the sweep)
    points: list[tuple[float, float]] = []  # (knob, HFD) on the sweep
    final = False
    for _ in range(max_frames):
        img, level = await exposure.frame()
        z = _sizes(img, fwhm_px)
        donut, hfd = z.donut, z.hfd
        s = Step("knob", knob, level, donut, "", *exposure.shown(img, z))
        far = donut is not None and donut > LINEAR_FWHM * fwhm_px
        if far and prev is not None and prev.donut is not None and last:
            if prev.donut > LINEAR_FWHM * fwhm_px and prev.donut != donut:
                per_unit = abs(prev.donut - donut) / abs(last)
        turn: float | None
        if final:
            turn, s.note = (
                None,
                (f"in focus: the star is {hfd or 0:.1f} px across (HFD); stop here"),
            )
        elif plan or points:
            if hfd is not None:
                points.append((knob, hfd))
            if plan:
                target = plan.pop(0)
                turn, s.note = target - knob, _relative(target - knob, last, "the next point")
            else:
                try:
                    fit = fit_hyperbola([p[0] for p in points], [p[1] for p in points])
                    turn = fit.x0 - knob
                    s.note = _relative(turn, last, "the smallest star (the fitted curve's minimum)")
                    final = True
                except FitError:
                    best = min(points, key=lambda p: p[1])
                    turn, final = best[0] - knob, True
                    s.note = _relative(turn, last, "the smallest star seen")
        elif far and per_unit and prev is not None and prev.donut is not None:
            shrank = donut < prev.donut
            way = (1.0 if last > 0 else -1.0) * (1.0 if shrank else -1.0)
            focus = knob + way * donut / per_unit
            w = 6.0 * fwhm_px / per_unit
            said = (
                f"{'shrinking' if shrank else 'growing'} (donut {prev.donut:.1f} to"
                f" {donut:.1f} px): "
            )
            if abs(focus - knob) > 2.5 * w:
                # Far yet: a hand's error on a long turn is large, so stop
                # short on this side, by more than a hand's error (a quarter
                # of the way), and measure again from there.
                short = max(1.5 * w, 0.25 * abs(focus - knob))
                turn = focus - way * short - knob
                s.note = said + _relative(turn, last, "just short of focus (measured again there)")
            else:
                plan = [focus + f * w for f in SWEEP]
                turn = plan.pop(0) - knob
                s.note = said + _relative(turn, last, "the first of five points near focus")
        else:
            turn, s.note = _start(prev, s, last)
        log.append(s)
        say(s.line())
        if turn is None:
            return log
        answer = (await ask("turn the knob as it says, then Enter (q: stop) ")).strip().lower()
        if answer.startswith("q"):
            return log
        knob, last, prev = knob + turn, turn, s
    say(f"focus: stopped after {max_frames} frames without reaching focus")
    return log


def _relative(turn: float, last: float, what: str) -> str:
    """A turn said as a multiple of the last one (its sign: the same way or back)."""
    k = abs(turn) / abs(last) if last else abs(turn)
    way = "the same way" if turn * last > 0 else "back"
    return f"turn {way}, {k:.2f} times your last turn, to {what}"


def _start(prev: Step | None, now: Step, last: float) -> tuple[float, str]:
    """Before the donut's size per turn is known: find light, then a donut."""
    if not now.lit:
        way = 1.0 if last >= 0 else -1.0
        return 4.0 * way, (
            "no light yet: turn the knob one full turn, the same way as before"
            " (the first time: either way)"
        )
    if prev is None or not last:
        return 1.0, "turn the knob a quarter turn, either way"
    if now.donut is None or prev.donut is None:  # follow the level
        if _rises(now, prev):
            return last, "brighter: turn the same way, as far as your last turn"
        return -last / 2.0, "fainter: turn back, half your last turn"
    # A donut, but not yet two far ones: a quarter turn more, the way it shrinks.
    way = 1.0 if (now.donut < prev.donut) == (last > 0) else -1.0
    return way * 1.0, "turn a quarter turn, " + ("the same way" if way * last > 0 else "back")


#: The planets first light may focus on, brightest first (typical V mag).
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


def bright_target(site: tuple[float, float, float], when) -> Target | None:
    """What to focus on now from ``site``: the brightest planet above
    :data:`MIN_ALT`, else the brightest star above it (the Moon is not used:
    on a small chip it is a disk with no edge in the frame). None: nothing."""
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, solar_system_ephemeris
    from astropy.time import Time

    from robofast.sky import offline_iers

    offline_iers()
    t = Time(when)
    loc = EarthLocation(lat=site[0] * u.deg, lon=site[1] * u.deg, height=site[2] * u.m)
    frame = AltAz(obstime=t, location=loc)
    with solar_system_ephemeris.set("builtin"):
        for name, mag in PLANETS:
            c = get_body(name.lower(), t, loc)
            alt = float(c.transform_to(frame).alt.deg)
            if alt >= MIN_ALT:
                return Target(name, float(c.ra.deg), float(c.dec.deg), mag, alt)
    for name, ra, dec, mag in BRIGHT_STARS:
        alt = float(SkyCoord(ra * u.deg, dec * u.deg).transform_to(frame).alt.deg)
        if alt >= MIN_ALT:
            return Target(name, ra, dec, mag, alt)
    return None


# -- the observation kind: first light's focus step, in the sequencer --------------


async def _ask(prompt: str) -> str:
    import asyncio

    return await asyncio.to_thread(input, prompt)


def _optics(opts: dict, slope: float | None):
    """The autofocus's optics from what the devices reported (``opts``), its
    focal-plane factor from the donut's measured slope (px per step)."""
    from robofast.autofocus import FocusOptics

    o = FocusOptics(
        f_ratio=float(opts["f_ratio"]),
        pixel_size=float(opts["pixel_size"]),
        pixel_scale=float(opts["plate_scale"]),
        obstruction=float(opts.get("obstruction", 0.0)),
        um_per_step=float(opts.get("um_per_step", 1.0)),
    )
    if slope:
        o.focal_plane_factor = slope / o.donut_slope
    return o


class FirstFocus:
    """First light's focus step (``kind="first_focus"``): on the bright
    target, from far out of focus to focus (module doc): the sky beside it
    for the level's reference, :func:`approach` then the autofocus's sweep
    with a focuser, :func:`walk_through` without one. Settings in the
    observation's ``autofocus`` entry: the optics (``f_ratio``,
    ``pixel_size``, ``plate_scale``, ``obstruction``, ``um_per_step``),
    ``fwhm_px``, ``saturation``, ``exptime`` (the bright frames), ``sweep_exptime``."""

    name = "first_focus"
    request_types: tuple[str, ...] = ()
    light = True
    fields = ("autofocus",)
    stage = "on_target"
    spectrograph = False
    fiber = False
    shutter = False
    always_guided = False


def _kind():
    from robofast.kinds import ObservationKind

    class _FirstFocus(FirstFocus, ObservationKind):
        async def run(self, r) -> None:
            await run_first_focus(r)

    return _FirstFocus()


def register() -> None:
    """Register the ``first_focus`` kind (a plug-in entry, docs/plugins.md)."""
    from robofast.kinds import OBSERVATION_KINDS

    if "first_focus" not in OBSERVATION_KINDS:
        OBSERVATION_KINDS.register("first_focus", _kind())


async def run_first_focus(r) -> None:
    import asyncio

    from robofast.autofocus import Autofocus, AutofocusConfig, camera_exposer
    from robofast.sequencer import ObservationFailed

    seq, state, unit, tel, cam, obs = r.sequencer, r.state, r.unit, r.tel, r.cam, r.obs
    opts = dict(obs.autofocus or {})
    exptime = float(opts.get("exptime", 0.01))
    fwhm_px = float(opts["fwhm_px"])
    saturation = float(opts["saturation"])
    expose = camera_exposer(cam, readout_margin=30.0, poll=0.05)

    def say(line: str) -> None:
        print(line, flush=True)
        seq._event("first_focus_step", telescope=unit.name, line=line)

    try:
        # The level's reference: the sky beside the target (a camera with no
        # shutter cannot take a dark), at the exposure and at a quarter of it
        # (what a shortened frame keeps: the bias).
        ra_h, dec = float(obs.ra_hours), float(obs.dec_deg)
        await seq._goto(state, unit, tel, ra_h, dec + (2.0 if dec < 80 else -2.0))
        dark = Level.of(await expose(exptime), await expose(exptime / 4.0), exptime)
        await seq._goto(state, unit, tel, ra_h, dec)
        fg = state.focusers.get(unit.name)
        if fg is None:
            if not opts.get("walk_through"):
                raise FocusError(
                    "no focuser, and nobody to turn the knob (--yes): focus by hand first"
                )
            await walk_through(
                expose, _ask, dark=dark, fwhm_px=fwhm_px, exptime=exptime,
                saturation=saturation, say=say,
            )  # fmt: skip
            seq._event("first_focus_done", telescope=unit.name, method="walk_through")
            return
        if await fg.get("Absolute") is not True:
            raise FocusError(
                "the focuser moves by steps, not to positions (Absolute false): not supported"
                " yet; focus by hand"
            )
        lo, hi = 0.0, float(await fg.get("MaxStep"))

        async def move(x: float) -> None:
            await fg.call("Move", round(x))
            while await fg.get("IsMoving"):
                await asyncio.sleep(0.05)

        start = float(await fg.get("Position"))
        step = max(1.0, (hi - lo) / 64.0)
        a = await approach(
            expose, move, start=start, limits=(lo, hi), dark=dark, fwhm_px=fwhm_px,
            step=step, exptime=exptime, saturation=saturation, say=say,
        )  # fmt: skip
        cfg = AutofocusConfig(
            optics=_optics(opts, a.slope),
            method="sweep",
            exptime=float(opts.get("sweep_exptime", 1.0)),
            max_start_error=0.0,
            measure={"saturation": 0.9 * saturation},
            min_position=lo,
            max_position=hi,
        )
        res = await Autofocus(unit.name, cfg, fg, cam).run("sweep", start=a.position)
        if not res.success:
            raise FocusError(f"the sweep did not find focus: {res.message}")
        say(f"focus: done at position {res.focus:.0f} (+-{res.focus_err or 0:.0f} steps)")
        seq._event("first_focus_done", telescope=unit.name, focus=res.focus, method="sweep")
    except FocusError as exc:
        seq._event("first_focus_failed", telescope=unit.name, error=str(exc))
        raise ObservationFailed(f"focus: {exc}") from exc
