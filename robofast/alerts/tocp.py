"""CBAT's Transient Object Confirmation Page (TOCP): new possible novae
(``PNV``), possible supernovae (``PSN``) and other transients (``TCP``),
often the first report of a bright one. Public, no account. Each row:
designation, discovery date (UT), J2000 position, magnitude and passband
code (``U`` unfiltered, ``C`` clear, ``V`` ...), offset, locale."""

from __future__ import annotations

import logging
import re
import urllib.request
from datetime import UTC, datetime, timedelta

from robofast.alerts.core import Alert

log = logging.getLogger(__name__)

URL = "http://www.cbat.eps.harvard.edu/unconf/tocp.html"
ROW = re.compile(
    r'<a href="[^"]*">(?P<des>(?P<kind>PNV|PSN|TCP) J[0-9.+-]+)</a>\s+'
    r"(?P<y>\d{4}) (?P<m>\d\d) ?(?P<d>\d{1,2}(?:\.\d+)?)\s*\*?\s+"
    r"(?P<rah>\d\d) (?P<ram>\d\d) (?P<ras>\d\d(?:\.\d+)?) "
    r"(?P<dsg>[+-])(?P<dd>\d\d) (?P<dm>\d\d) (?P<ds>\d\d(?:\.\d+)?)\.?"
    r"\s+(?P<mag>\d+(?:\.\d+)?)?\s?(?P<band>[A-Za-z])?"
)


def parse(html: str) -> list[Alert]:
    out = []
    for m in ROW.finditer(html):
        day = float(m["d"])
        when = datetime(int(m["y"]), int(m["m"]), 1, tzinfo=UTC) + timedelta(days=day - 1)
        ra = 15.0 * (int(m["rah"]) + int(m["ram"]) / 60 + float(m["ras"]) / 3600)
        dec = int(m["dd"]) + int(m["dm"]) / 60 + float(m["ds"]) / 3600
        out.append(
            Alert(
                source="tocp",
                id=m["des"],
                name=m["des"],
                ra=ra,
                dec=-dec if m["dsg"] == "-" else dec,
                mag=float(m["mag"]) if m["mag"] else None,
                band=m["band"],
                time=when,
                classification=m["kind"],
            )
        )
    rows = html.count('<a href="unconf/followups/')
    if rows > len(out):
        log.debug("TOCP: %d of %d rows not understood (malformed)", rows - len(out), rows)
    return out


def fetch(url: str = URL, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "robofast alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("latin-1")
