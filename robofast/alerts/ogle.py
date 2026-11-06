"""OGLE-IV's Early Warning System (EWS): every microlensing event OGLE has
found this season, with its point-lens fit. Public, no account; OGLE asks
to be contacted before its photometry is used for publication (the page's
own words: provided "to facilitate the microlensing community monitoring the
on-going events and follow up observations"). Each row of ``ews.html``:
event, field, star, J2000 position, Tmax (HJD and UT), tau (tE, days), umin,
Amax, Dmag (the blend's brightening at peak), fbl, I_bl (the blend's base I),
I0. The page lists Bulge (``BLG``) and disk (``DG``, ``GD``) events. Where
the fit's umin is 0.000 the page gives Amax 99999 and Dmag 12.5: such an
event has no peak magnification or magnitude here, so no rule with
``min_amax`` or ``max_mag`` takes it until a refit gives one."""

from __future__ import annotations

import logging
import re
import urllib.request
from datetime import datetime

from robofast.alerts.core import Alert, jd_utc

log = logging.getLogger(__name__)

URL = "https://ogle.astrouw.edu.pl/ogle4/ews/{year}/ews.html"
#: The page's Amax where umin is 0.000 (and Dmag its cap, 12.5): no peak known.
CAPPED = 99999.0
_N = r"\s*<TD>\s*(-?[\d.]+)"
ROW = re.compile(
    r'<A HREF="[^"]*" TARGET="event">(?P<name>\d{4}-[A-Z]+-\d{4})</A>\s*'
    r"<TD>[^<]*<TD[^>]*>\s*\d+\s*"
    r"<TD>(?P<ra>\d{1,2}:\d\d:\d\d(?:\.\d+)?)\s*<TD>(?P<dec>[+-]?\d{1,2}:\d\d:\d\d(?:\.\d+)?)"
    r"\s*<TD>(?P<tmax>[\d.]+)\s*<TD>[^<]*"
    + "".join(
        rf"\s*<TD>\s*(?P<{k}>-?[\d.]+)" for k in ("tau", "umin", "amax", "dmag", "fbl", "ibl")
    ),
    re.IGNORECASE,
)


def _sexagesimal(text: str) -> float:
    sign = -1.0 if text.startswith("-") else 1.0
    h, m, s = (float(x) for x in text.lstrip("+-").split(":"))
    return sign * (h + m / 60 + s / 3600)


def parse(html: str) -> list[Alert]:
    out = []
    for m in ROW.finditer(html):
        name = f"OGLE-{m['name']}"
        # 99999 (with Dmag capped at 12.5): umin 0.000, the fit has no peak to give
        fit = float(m["amax"]) < CAPPED
        out.append(
            Alert(
                source="ogle",
                id=name,
                name=name,
                ra=15.0 * _sexagesimal(m["ra"]),
                dec=_sexagesimal(m["dec"]),
                # the blend's I at peak: its base less the page's own brightening
                mag=float(m["ibl"]) - float(m["dmag"]) if fit else None,
                band="I",
                peak=jd_utc(float(m["tmax"])),
                te=float(m["tau"]),
                u0=float(m["umin"]),
                amax=float(m["amax"]) if fit else None,
                updates=True,
            )
        )
    rows = len(re.findall(r'TARGET="event">\d{4}-[A-Z]+-\d{4}<', html))
    if rows > len(out):
        log.warning("OGLE EWS: %d of %d rows not understood", rows - len(out), rows)
    return out


def fetch(url: str | None = None, timeout: float = 60.0) -> str:
    url = url or URL.format(year=datetime.now().year)
    req = urllib.request.Request(url, headers={"User-Agent": "robofast alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("latin-1")
