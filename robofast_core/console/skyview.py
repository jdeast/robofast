"""The console's sky: where each telescope points, the targets now and next,
the Sun and the Moon, the limits (docs/console.md, "Sky").

Read only. Positions come from the sequencer's status snapshot (each
telescope's ``tel.alt``/``tel.az`` in the value registry, and what it is
observing); targets from the dispatch scheduler's requests (through the
request API's read-only view of its state file); the site and the altitude
limits from the dispatch plan (else the enclosure config's ``site`` and the
design defaults, 21 and 83 deg). Every target's altitude, azimuth and
airmass are computed now and ``later`` hours ahead, with astropy and the
bundled IERS tables (never a download).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from robofast_core.console.aggregate import ConsoleEnclosure

log = logging.getLogger(__name__)

#: Hours ahead for "later" (the target table's columns).
LATER = (1.0, 2.0)
#: Most targets listed per enclosure.
MAX_TARGETS = 60
DEFAULT_LIMITS = (21.0, 83.0)


def _airmass(alt: float) -> float | None:
    return round(1.0 / math.sin(math.radians(alt)), 3) if alt > 1.0 else None


def _site(enc: ConsoleEnclosure, view: Any) -> Any:
    from robofast_core.sky import Site

    if view is not None and getattr(view, "site", None) is not None:
        return view.site
    s = enc.site
    if isinstance(s, dict):
        return Site(float(s["latitude"]), float(s["longitude"]), float(s.get("elevation", 0.0)))
    return None


def _targets(view: Any, current: dict[str, Any]) -> list[dict[str, Any]]:
    """Unfinished targets of the dispatch scheduler, current ones first."""
    if view is None:
        return []
    busy = {}
    for tel, c in current.items():
        if isinstance(c, dict) and c.get("configuration"):
            busy.setdefault(c["configuration"], []).append(tel)
    out: dict[tuple[str, float, float], dict[str, Any]] = {}
    for g in view.groups.values():
        for r in getattr(g, "requests", []):
            for c in getattr(r, "configurations", []):
                t = getattr(c, "target", None)
                if t is None or getattr(t, "ra_hours", None) is None:
                    continue
                status = str(getattr(c, "status", ""))
                status = status.rsplit(".", 1)[-1]
                now_on = busy.get(c.id, [])
                if status in ("COMPLETED", "CANCELED", "EXPIRED", "FAILED") and not now_on:
                    continue
                key = (t.name, round(t.ra_hours, 5), round(t.dec_deg, 5))
                e = out.setdefault(
                    key,
                    {
                        "name": t.name,
                        "ra_hours": t.ra_hours,
                        "dec_deg": t.dec_deg,
                        "configurations": [],
                        "telescopes": [],
                        "priority": getattr(g, "priority", None),
                        "too": getattr(g, "too", None) is not None,
                        "status": status,
                    },
                )
                e["configurations"].append(c.id)
                e["telescopes"] += [x for x in now_on if x not in e["telescopes"]]
    targets = list(out.values())
    targets.sort(key=lambda e: (not e["telescopes"], not e["too"], -(e["priority"] or 0)))
    return targets[:MAX_TARGETS]


def sky_for(
    enc: ConsoleEnclosure,
    values: dict[str, Any],
    current: dict[str, Any],
    when: datetime | None = None,
) -> dict[str, Any]:
    """One enclosure's sky (module doc). ``values`` is the sequencer
    snapshot's value registry, ``current`` its per-telescope current."""
    from astropy import units as u
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time

    from robofast_core import sky

    sky.offline_iers()
    when = when or datetime.now(UTC)
    view = None
    if enc.requests is not None:
        try:
            view = enc.requests.view()
        except Exception as exc:  # a broken state file: no targets, not a crash
            log.warning("console sky: %s: %r", enc.name, exc)
    site = _site(enc, view)
    out: dict[str, Any] = {"time": when.isoformat(), "site": None}
    tels: dict[str, Any] = {}
    for t in enc.telescopes:
        alt, az = values.get(f"{t}.tel.alt"), values.get(f"{t}.tel.az")
        lim = DEFAULT_LIMITS
        if view is not None and t in getattr(view, "limits", {}):
            lim = (view.limits[t].min_alt, view.limits[t].max_alt)
        cur = current.get(t) if isinstance(current.get(t), dict) else {}
        tels[t] = {
            "alt": alt.get("value") if isinstance(alt, dict) and not alt.get("stale") else None,
            "az": az.get("value") if isinstance(az, dict) and not az.get("stale") else None,
            "min_alt": lim[0],
            "max_alt": lim[1],
            "observing": cur.get("observation") or cur.get("configuration"),
        }
    out["telescopes"] = tels
    if site is None:
        out["targets"] = []
        return out
    out["site"] = {"latitude": site.latitude, "longitude": site.longitude}
    now = sky.sky_now(site, when)
    out["lst_hours"] = round(now.lst_hours, 4)
    times = Time([when, *(when + timedelta(hours=h) for h in LATER)])
    frame = AltAz(obstime=times[0], location=site.location)
    for body in ("sun", "moon"):
        aa = get_body(body, times[0], site.location).transform_to(frame)
        out[body] = {"alt": round(float(aa.alt.deg), 2), "az": round(float(aa.az.deg), 2)}
    sun = get_body("sun", times[0], site.location)
    moon = get_body("moon", times[0], site.location)
    elong = float(sun.separation(moon).rad)
    out["moon"]["illumination"] = round((1 - math.cos(elong)) / 2, 3)
    out["sun"]["rising"] = now.sun_rising
    targets = _targets(view, current)
    if targets:
        import numpy as np
        from astropy.coordinates import SkyCoord

        ra = np.array([e["ra_hours"] for e in targets]) * 15.0
        dec = np.array([e["dec_deg"] for e in targets])
        coords = SkyCoord(ra=ra[:, None] * u.deg, dec=dec[:, None] * u.deg, frame="icrs")
        aa = coords.transform_to(AltAz(obstime=times[None, :], location=site.location))
        alts, azs = np.asarray(aa.alt.deg), np.asarray(aa.az.deg)
        for i, e in enumerate(targets):
            e["alt"] = round(float(alts[i, 0]), 2)
            e["az"] = round(float(azs[i, 0]), 2)
            e["airmass"] = _airmass(e["alt"])
            e["later"] = [
                {
                    "hours": h,
                    "alt": round(float(alts[i, k + 1]), 2),
                    "az": round(float(azs[i, k + 1]), 2),
                    "airmass": _airmass(float(alts[i, k + 1])),
                }
                for k, h in enumerate(LATER)
            ]
            e["hour_angle"] = round(sky.hour_angle(now.lst_hours, e["ra_hours"]), 3)
    out["targets"] = targets
    return out
