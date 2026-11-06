"""MOA's microlensing alerts of the year, with their point-source point-lens
fits. Public, no account. Each row of the year's index: event, alert date
(UT), J2000 position, t0 (UT date), tE (days), Amax, I0 (the fit's
baseline I, unblended). MOA's index gives no u0; for an unblended fit it is
the u with A(u) = Amax."""

from __future__ import annotations

import logging
import math
import re
import urllib.request
from datetime import UTC, datetime, timedelta

from robofast.alerts.core import Alert, utc
from robofast.alerts.ogle import _sexagesimal

log = logging.getLogger(__name__)

URL = "https://moaprime.massey.ac.nz/alerts/index/moa/{year}"
_CELL = r"\s*<td[^>]*>\s*([^<]*?)\s*</td>"
ROW = re.compile(
    r"display/(?P<name>MOA-\d{4}-[A-Z]+-\d{4})[^>]*>[^<]*</a></td>"
    + "".join(
        _CELL.replace("(", f"(?P<{k}>", 1)
        for k in ("alerted", "ra", "dec", "t0", "te", "amax", "i0")
    ),
    re.IGNORECASE,
)


def _u(a: float) -> float:
    """The separation u at which a point lens magnifies ``a`` (> 1)."""
    return math.sqrt(2.0 * (a / math.sqrt(a * a - 1.0) - 1.0)) if a > 1.0 else math.inf


def parse(html: str) -> list[Alert]:
    out = []
    for m in ROW.finditer(html):
        try:
            day, frac = m["t0"].split(".") if "." in m["t0"] else (m["t0"], "0")
            peak = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(
                days=float(f"0.{frac}")
            )
            amax, i0 = float(m["amax"]), float(m["i0"])
            alert = Alert(
                source="moa",
                id=m["name"],
                name=m["name"],
                ra=15.0 * _sexagesimal(m["ra"]),
                dec=_sexagesimal(m["dec"]),
                mag=i0 - 2.5 * math.log10(amax),
                band="I",
                time=utc(m["alerted"]),
                peak=peak,
                te=float(m["te"]),
                u0=_u(amax),
                amax=amax,
                updates=True,
            )
        except ValueError:
            continue
        out.append(alert)
    rows = len(re.findall(r"display/MOA-", html))
    if rows > len(out):
        log.warning("MOA: %d of %d rows not understood", rows - len(out), rows)
    return out


def fetch(url: str | None = None, timeout: float = 60.0) -> str:
    url = url or URL.format(year=datetime.now().year)
    req = urllib.request.Request(url, headers={"User-Agent": "robofast alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")
