"""The dispatch decision must not hold the event loop (night 2026-11-21,
night1121): with ~220 requests, a re-evaluation at the end of each visit held
the GIL for about 3 s, in one astropy transform per candidate
(DispatchScheduler._sky_checks -> sky.moon_separation). Every thread of the
process starved: the rehearsal's simulated weather feeds and PAC timed out
together, the bridge went unknown and the roof closed, about every 9 min. In
production the same stall holds the sequencer's loop: lease renewals,
guider polls, heartbeats.

Budgets: on the MINERVA gateway (4 slow cores, i5-2400) a 500-request
decision now takes well under 0.1 s (sky_now ~0.03 s, the vectorized
altitudes ~0.004 s, the rest Python); the budget below is several times that
for a loaded CI runner, and still an order of magnitude under the 3 s that
stalled the night. The ticker bound (0.3 s) is what a 1 s client timeout
(the bridge's sources, the PAC) can absorb with room to spare.
"""

from __future__ import annotations

import asyncio
import random
import time

from robofast import sky
from tests.core.test_dispatch import NIGHT, Clock, group, make

#: Seconds one decision over the pool may take (see the module doc).
BUDGET = 0.5
#: The longest gap a ticker task may see while decisions run.
MAX_GAP = 0.3
POOL = 500


def _pool():
    rng = random.Random(7)
    groups = []
    for i in range(POOL):
        tel = ["t1"] if i % 3 == 0 else ["t2"] if i % 3 == 1 else ["t1", "t2"]
        groups.append(
            group(
                f"g{i}",
                ha=rng.uniform(-5.0, 5.0),
                dec=rng.uniform(-30.0, 80.0),
                priority=rng.uniform(1.0, 5.0),
                telescopes=tel,
            )
        )
    return groups


def test_a_decision_over_500_requests_is_fast():
    d = make(_pool(), clock=Clock(NIGHT))
    sn = sky.sky_now(d.site, NIGHT)
    d._best("t1", NIGHT, sn)  # warm (first use of any cache)
    for tel in ("t1", "t2"):
        t0 = time.perf_counter()
        best, cands = d._best(tel, NIGHT, sn)
        took = time.perf_counter() - t0
        assert best is not None and len(cands) == POOL
        assert took < BUDGET, f"{tel}: _best over {POOL} requests took {took:.2f} s"


async def test_decisions_never_hold_the_event_loop():
    d = make(_pool(), clock=Clock(NIGHT))
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    for tel in ("t1", "t2", "t1", "t2"):
        obs = await d.next_observation(tel)
        assert obs is not None
        await asyncio.sleep(0.02)
    stop.set()
    await task
    assert max(gaps) < MAX_GAP, f"the loop was held {max(gaps):.2f} s"


def test_the_fast_sky_agrees_with_astropy_within_an_arcminute():
    """sky.altitudes (one precession-nutation matrix, the sidereal time
    advanced linearly) and sky.moon_separation (plain spherical trig against
    the Moon's GCRS place) against astropy's full transforms: they differ by
    aberration (about 20 arcsec), far below the limits they serve."""
    from datetime import timedelta

    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, SkyCoord
    from astropy.time import Time

    site = sky.Site(31.680407, -110.878977, 2316.0)
    rng = np.random.default_rng(3)
    ra, dec = rng.uniform(0, 24, 200), rng.uniform(-60, 89, 200)
    times = [[NIGHT + timedelta(hours=h) for h in (0.0, 1.5, 3.0)] for _ in ra]
    fast = sky.altitudes(site, ra, dec, times)
    c = SkyCoord(ra=ra[:, None] * 15 * u.deg, dec=dec[:, None] * u.deg)
    frame = AltAz(obstime=Time(np.asarray(times, dtype=object)), location=site.location)
    assert np.max(np.abs(fast - c.transform_to(frame).alt.deg)) < 1 / 60
    sn = sky.sky_now(site, NIGHT)
    sep = sky.moon_separation(sn, ra, dec)
    tgt = SkyCoord(ra=ra * 15 * u.deg, dec=dec * u.deg, frame="icrs")
    ref = sn.moon.separation(tgt.transform_to(sn.moon.frame)).deg
    assert np.max(np.abs(sep - ref)) < 1 / 60
    # The other forms of `moon` give the same answer.
    assert np.allclose(sky.moon_separation(sn.moon, ra, dec), sep)
    assert np.allclose(sky.moon_separation(sn.moon_radec, ra, dec), sep)
