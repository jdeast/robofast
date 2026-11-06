"""Rubin alerts (LSST alert schema v11: ``diaSource``; ``ssSource`` and
``mpc_orbits`` for a Solar System object), as a broker delivers them in JSON
or as the decoded Avro, to :class:`~robofast.alerts.core.Alert`."""

from __future__ import annotations

import math
from datetime import UTC
from typing import Any

from robofast.alerts.core import Alert


def _ab(flux_njy: Any) -> float | None:
    """The AB magnitude of a (difference) flux in nJy; None unless positive,
    so a fading or negative source never passes a magnitude limit."""
    if not isinstance(flux_njy, int | float) or flux_njy <= 0:
        return None
    return -2.5 * math.log10(flux_njy) + 31.4


def from_rubin(d: dict[str, Any]) -> Alert:
    from astropy.time import Time

    src = d["diaSource"]
    ss, orb = d.get("ssSource"), d.get("mpc_orbits")
    sso = bool(ss and orb)
    mag, band = (ss.get("ephVmag"), "V") if sso else (None, None)
    if mag is None:
        mag, band = _ab(src.get("psfFlux")), src.get("band")
    t = src.get("midpointMjdTai")
    when = Time(t, format="mjd", scale="tai").utc.to_datetime(timezone=UTC) if t else None
    if sso:
        name = str(orb.get("designation") or ss.get("designation"))
    else:
        obj = (d.get("diaObject") or {}).get("diaObjectId")
        name = f"Rubin {obj if obj is not None else src['diaSourceId']}"
    return Alert(
        source="rubin",
        id=str(src["diaSourceId"]),
        name=name,
        ra=src.get("ra"),
        dec=src.get("dec"),
        mag=mag,
        band=band,
        time=when,
        classification="Solar System" if sso else None,
        orbit={**orb, "designation": name} if sso else None,
    )
