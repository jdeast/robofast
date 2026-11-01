"""Twilight sky flats (docs/skyflats.md; legacy ``control.py`` ``doSkyFlat``).

A sky flat is a frame of the twilight sky at a target level (``target_adu``
above the bias). The sky fades (evening) or brightens (morning) by about
1.2 magnitudes per degree of the Sun's depression
(:func:`robofast_core.photometry.sky_brightness`), so each exposure time is
**predicted**: the model's rate at the Sun's altitude at mid-exposure,
scaled by what the last frame actually measured (its median), and solved
for the target level, the Sun moving during the exposure included.

Filters go in order of the light they need: in the evening the least
sensitive first (it needs the brightest sky), in the morning the most
sensitive first. A filter is done when it has ``count`` good frames, or
when its predicted exposure leaves ``[min_exptime, max_exptime]`` in the
direction the sky is going (too faint in the evening, too bright in the
morning); in the other direction the routine waits for the sky. A frame
whose median is above ``max_adu`` (near saturation) or below ``min_adu``
(too faint) is discarded and the scale relearned. Frames are dithered
(``dither`` arcsec, a spiral) so stars median out. Everything stops when
the Sun leaves ``[min_sun_alt, max_sun_alt]``.

The routine (:func:`take_flats`) is device-independent (callables); the
sequencer runs it for ``kind="sky_flat"`` (a ``SKY_FLAT`` request) with the
enclosure open: flats need an open lease, never a closed roof.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from robofast_core.photometry import SkyConditions, sky_brightness

log = logging.getLogger(__name__)

__all__ = ["FlatConfig", "FlatFrame", "flat_spot", "take_flats", "twilight_rate"]


@dataclass
class FlatConfig:
    """A telescope's sky-flat settings (``sky_flats`` in its config)."""

    #: Filter name -> relative sensitivity (counts per second relative to
    #: the others on the same sky; the order of the run comes from it).
    filters: dict[str, float] = field(default_factory=lambda: {"clear": 1.0})
    count: int = 5
    target_adu: float = 25000.0
    min_adu: float = 10000.0
    max_adu: float = 45000.0
    bias: float = 1000.0
    #: The camera's clipping level (ADU): a frame there teaches only "brighter".
    saturation: float = 60000.0
    min_exptime: float = 1.0
    max_exptime: float = 60.0
    #: Readout, dither and filter change per frame (s), for the prediction.
    overhead: float = 15.0
    dither: float = 20.0
    min_sun_alt: float = -12.0
    max_sun_alt: float = -1.0
    #: How long to wait for the sky (s) each time it is still too bright
    #: (evening) or too faint (morning), at most ``max_wait`` per filter, and
    #: the most frames tried per filter.
    wait: float = 30.0
    max_wait: float = 1800.0
    max_tries: int = 20
    #: First guess: ADU/s per pixel for sensitivity 1 at the Sun's altitude -6.
    rate_at_minus6: float = 20000.0
    #: Site (for the Sun and the flat spot).
    latitude: float = 0.0
    longitude: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> FlatConfig:
        d = {k: v for k, v in dict(d or {}).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"sky_flats: unknown keys {sorted(set(d) - known)}")
        c = cls(**d)
        if not c.filters or any(v <= 0 for v in c.filters.values()):
            raise ValueError("sky_flats: filters need positive sensitivities")
        return c


def twilight_rate(sun_alt: float) -> float:
    """The model's twilight sky rate at ``sun_alt`` relative to -6 deg
    (the zenith sky; :func:`~robofast_core.photometry.sky_brightness`)."""

    def mu(alt: float) -> float:
        return sky_brightness(SkyConditions(sun_alt=alt, target_alt=75.0))

    return 10.0 ** (-0.4 * (mu(sun_alt) - mu(-6.0)))


@dataclass
class FlatFrame:
    filter: str
    exptime: float
    median: float
    sun_alt: float
    kept: bool
    why: str
    dither: tuple[float, float] = (0.0, 0.0)


def flat_spot(latitude: float, longitude: float, when: datetime) -> tuple[float, float, float]:
    """(RA hours, Dec deg, Sun altitude): the sky 75 deg high opposite the
    Sun, where the twilight sky is flattest."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body
    from astropy.time import Time

    from robofast_core.sky import offline_iers

    offline_iers()
    loc = EarthLocation.from_geodetic(longitude * u.deg, latitude * u.deg)
    t = Time(when)
    frame = AltAz(obstime=t, location=loc)
    sun = get_body("sun", t, loc).transform_to(frame)
    spot = SkyCoord(alt=75 * u.deg, az=(sun.az.deg + 180.0) % 360 * u.deg, frame=frame).icrs
    return float(spot.ra.deg) / 15.0, float(spot.dec.deg), float(sun.alt.deg)


def _spiral(i: int, step: float) -> tuple[float, float]:
    if i == 0:
        return 0.0, 0.0
    a = i * 2.39996
    r = step * math.sqrt(i)
    return r * math.cos(a), r * math.sin(a)


async def take_flats(
    cfg: FlatConfig,
    *,
    evening: bool,
    sun_alt: Callable[[], float],
    expose: Callable[[str, float, tuple[float, float]], Awaitable[float]],
    wait: Callable[[float], Awaitable[None]],
    sun_rate: float | None = None,
    event: Callable[..., None] | None = None,
) -> list[FlatFrame]:
    """Take the flats (module doc). ``expose(filter, exptime, dither)``
    returns the frame's median ADU; ``sun_alt()`` the Sun's altitude now;
    ``sun_rate`` deg/s (default -/+ 0.25 deg/min, evening/morning)."""
    rate = sun_rate if sun_rate is not None else (-1 if evening else 1) * 0.25 / 60.0
    order = sorted(cfg.filters, key=lambda f: cfg.filters[f], reverse=not evening)
    scale = cfg.rate_at_minus6  # ADU/s for sensitivity 1 at -6, relearned per frame
    frames: list[FlatFrame] = []
    say = event or (lambda what, **kw: None)

    def predict(flt: str, alt0: float) -> float:
        want = cfg.target_adu - cfg.bias
        t = cfg.min_exptime
        for _ in range(4):  # the Sun moves while it exposes
            tt = min(t, 2.0 * cfg.max_exptime)  # (beyond that it is "too faint" anyway)
            r = scale * cfg.filters[flt] * twilight_rate(alt0 + rate * (tt / 2 + cfg.overhead))
            t = want / max(r, 1e-9)
        return t

    def in_window(alt: float) -> bool:
        return cfg.min_sun_alt <= alt <= cfg.max_sun_alt

    for flt in order:
        good = tries = 0
        waited = 0.0

        async def pause(why: str, flt: str = flt) -> bool:
            nonlocal waited
            if waited >= cfg.max_wait:
                say("flat_filter_done", filter=flt, why=f"waited {waited:.0f} s: {why}")
                return False
            say("flat_wait", filter=flt, why=why)
            await wait(cfg.wait)
            waited += cfg.wait
            return True

        while good < cfg.count and tries < cfg.max_tries:
            alt = sun_alt()
            if not in_window(alt):
                early = alt > cfg.max_sun_alt if evening else alt < cfg.min_sun_alt
                if early:
                    if await pause(f"Sun at {alt:.1f}: not yet"):
                        continue
                    break
                say("flats_stopped", why=f"Sun at {alt:.1f} deg, outside the window")
                return frames
            t = predict(flt, alt)
            if t < cfg.min_exptime:
                if evening:  # the sky will fade into range
                    if await pause(f"too bright ({t:.2f} s)"):
                        continue
                    break
                say("flat_filter_done", filter=flt, why=f"sky too bright ({t:.2f} s)")
                break
            if t > cfg.max_exptime:
                if not evening:  # the sky will brighten into range
                    if await pause(f"too faint ({t:.0f} s)"):
                        continue
                    break
                say("flat_filter_done", filter=flt, why=f"sky too faint ({t:.0f} s)")
                break
            t = round(t, 2)
            off = _spiral(len(frames), cfg.dither)
            median = await expose(flt, t, off)
            tries += 1
            level = median - cfg.bias
            mid_alt = alt + rate * (t / 2 + cfg.overhead)
            if median >= cfg.saturation:
                scale *= 4.0  # clipped: the sky is brighter than any level says
            elif level > 0:
                scale = level / t / cfg.filters[flt] / twilight_rate(mid_alt)
            if median > cfg.max_adu:
                kept, why = False, "near saturation"
            elif median < cfg.min_adu:
                kept, why = False, "too faint"
            else:
                kept, why = True, "ok"
                good += 1
            frames.append(FlatFrame(flt, t, round(median, 1), round(alt, 3), kept, why, off))
            say("flat", filter=flt, exptime=t, median=round(median), kept=kept, why=why)
    return frames
