"""Wait for the Sun: start the night's processes at a Sun altitude.

::

    python -m robofast_core.when --sun-below -6 --config aqawan1.json
    python -m robofast_core.when --sun-above 0 --check --config aqawan1.json

waits until the Sun's altitude at the site is below (or above) the given
number of degrees, then exits 0; if it already is, it exits at once. With
``--check`` it never waits: exit 0 if the condition holds now, 1 if not.
With ``--max-wait S``, it exits 1 instead of waiting longer than S seconds.
It is meant for a process manager (``ExecStartPre`` in
``deploy/robofast-sequencer@.service``): the sequencer then starts in the
evening at that altitude, whenever the unit itself was started.

The site comes from the enclosure config: its ``site`` entry, or else the
dispatch plan's (``plan.site``): ``{"latitude": ..., "longitude": ...
(east), "elevation": ...}``. ``--site LAT,LON[,ELEV]`` overrides it.

The altitude is geometric (no refraction) and computed with astropy, with
IERS downloads turned off (:func:`robofast_core.sky.offline_iers`). The wait
is planned from a sampled curve (one point a minute over the next 36 h,
refined to a second), and re-planned at least every ``--poll`` seconds, so
a clock step or a suspended machine cannot make it oversleep by more than
that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from robofast_core.config import load_config
from robofast_core.sky import Site, offline_iers

log = logging.getLogger(__name__)

__all__ = ["condition_holds", "next_time", "site_from_config", "sun_altitudes", "wait_for_sun"]


def sun_altitudes(site: Site, times: list[datetime]) -> np.ndarray:
    """Geometric altitude (deg) of the Sun's centre at each of ``times``."""
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time

    offline_iers()
    t = Time(times)
    frame = AltAz(obstime=t, location=site.location)
    return np.asarray(get_body("sun", t, site.location).transform_to(frame).alt.deg)


def condition_holds(alt: float, below: float | None, above: float | None) -> bool:
    return (below is None or alt < below) and (above is None or alt > above)


def next_time(
    site: Site,
    start: datetime,
    *,
    below: float | None = None,
    above: float | None = None,
    horizon: timedelta = timedelta(hours=36),
    step: timedelta = timedelta(minutes=1),
) -> datetime | None:
    """The first time at or after ``start`` when the Sun is below ``below``
    and above ``above`` (degrees; either may be None), to within a second;
    None if not within ``horizon`` (polar day or night)."""
    if below is None and above is None:
        raise ValueError("give below, above, or both")
    n = int(horizon / step) + 1
    times = [start + i * step for i in range(n)]
    alts = sun_altitudes(site, times)
    ok = [condition_holds(float(a), below, above) for a in alts]
    if ok[0]:
        return start
    if not any(ok):
        return None
    i = ok.index(True)
    lo, hi = times[i - 1], times[i]  # false at lo, true at hi
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if condition_holds(float(sun_altitudes(site, [mid])[0]), below, above):
            hi = mid
        else:
            lo = mid
    return hi


async def wait_for_sun(
    site: Site,
    *,
    below: float | None = None,
    above: float | None = None,
    poll: float = 300.0,
    max_wait: float | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> bool:
    """Wait until the condition holds (True), or give up (False) when it
    will not within ``max_wait`` seconds or 36 h."""
    t0 = now()
    while True:
        t = now()
        target = next_time(site, t, below=below, above=above)
        if target is None:
            log.error("the Sun will not get there within 36 h")
            return False
        if target <= t:
            return True
        if max_wait is not None and (target - t0).total_seconds() > max_wait:
            log.error("not before %s, beyond --max-wait", target.isoformat())
            return False
        delay = (target - t).total_seconds()
        log.info("waiting %.0f s, until %s", delay, target.isoformat())
        await sleep(min(delay, poll))


def site_from_config(config: dict[str, Any]) -> Site:
    """The site of an enclosure config: ``site``, or the plan's ``site``."""
    site = config.get("site")
    if site is None and isinstance(config.get("plan"), dict):
        site = config["plan"].get("site")
    if not isinstance(site, dict):
        raise ValueError("no 'site' {latitude, longitude, elevation} in the config (or its plan)")
    return Site(**site)


def _parse_site(text: str) -> Site:
    parts = [float(x) for x in text.split(",")]
    if len(parts) not in (2, 3):
        raise ValueError(f"--site {text!r}: expected LAT,LON[,ELEV]")
    return Site(*parts)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sun-below", type=float, help="wait until the Sun is below this (deg)")
    ap.add_argument("--sun-above", type=float, help="wait until the Sun is above this (deg)")
    ap.add_argument("--config", help="enclosure config file (for its site)")
    ap.add_argument("--site", help="LAT,LON[,ELEV] (deg north, deg east, m)")
    ap.add_argument("--check", action="store_true", help="do not wait: exit 0 if it holds now")
    ap.add_argument("--max-wait", type=float, help="exit 1 rather than wait longer (s)")
    ap.add_argument("--poll", type=float, default=300.0, help="re-plan the wait this often (s)")
    ap.add_argument("--now", help=argparse.SUPPRESS)  # an ISO time, for tests
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.sun_below is None and args.sun_above is None:
        ap.error("give --sun-below and/or --sun-above")
    try:
        if args.site:
            site = _parse_site(args.site)
        elif args.config:
            site = site_from_config(load_config(args.config))
        else:
            ap.error("give --config or --site")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        ap.error(f"{args.config or args.site}: {exc}")
    if args.now and not args.check:
        ap.error("--now only goes with --check")
    now = (lambda: datetime.fromisoformat(args.now)) if args.now else (lambda: datetime.now(UTC))
    if args.check:
        alt = float(sun_altitudes(site, [now()])[0])
        holds = condition_holds(alt, args.sun_below, args.sun_above)
        log.info("Sun at %.2f deg: %s", alt, "yes" if holds else "no")
        sys.exit(0 if holds else 1)
    ok = asyncio.run(
        wait_for_sun(
            site,
            below=args.sun_below,
            above=args.sun_above,
            poll=args.poll,
            max_wait=args.max_wait,
            now=now,
        )
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
