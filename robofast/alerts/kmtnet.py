"""KMTNet's microlensing events of the season, with their point-lens fits,
from the page's own text list (``listpage.dat``). Public; the KMTNet page
says its alerts exist "to encourage and enable followup observations", and
that its data are proprietary until the yearly release (read its data policy
before using any of it beyond follow-up). Each line: event, field.star,
class (1 ``clear``, 2 ``probable``, 4 ``not-ulens``: the HTML table's words;
a ``not-ulens`` event is never followed),
J2000 position, t0 (HJD - 2400000), tE (days), u0, Isource, Ibase, Icat,
Ctype, A_I, then any related events (``OB261234``: OGLE-2026-BLG-1234;
``PB``: PRIME)."""

from __future__ import annotations

import logging
import math
import urllib.request
from datetime import datetime

from robofast.alerts.core import Alert, jd_utc, magnification
from robofast.alerts.ogle import _sexagesimal

log = logging.getLogger(__name__)

URL = "https://kmtnet.kasi.re.kr/~ulens/event/{year}/listpage.dat"
CLASSES = {"1": "clear", "2": "probable", "4": "not-ulens"}


def _alias(related: str) -> str | None:
    """KMTNet's name for an OGLE event (``OB261234``) as its id here."""
    if len(related) == 8 and related.startswith("OB") and related[2:].isdigit():
        return f"ogle:OGLE-20{related[2:4]}-BLG-{related[4:]}"
    return None


def _peak_mag(isource: float, ibase: float, a: float) -> float:
    """The baseline's I brightened by the source's flux times (A - 1)."""
    if math.isinf(a):
        return -math.inf
    flux = 10 ** (-0.4 * ibase) + 10 ** (-0.4 * isource) * (a - 1.0)
    return -2.5 * math.log10(flux)


def parse(text: str) -> list[Alert]:
    out = []
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines:
        f = line.split()
        try:
            name, cls, ra, dec = f[0], f[2], f[3], f[4]
            t0, te, u0, isource, ibase = (float(x) for x in f[5:10])
        except (IndexError, ValueError):
            continue
        a = magnification(u0)
        out.append(
            Alert(
                source="kmtnet",
                id=name,
                name=name,
                ra=15.0 * _sexagesimal(ra),
                dec=_sexagesimal(dec),
                mag=_peak_mag(isource, ibase, a),
                band="I",
                classification=CLASSES.get(cls, f"class {cls}"),
                peak=jd_utc(t0 + 2400000.0),
                te=te,
                u0=u0,
                amax=a,
                aliases=tuple(x for x in map(_alias, f[13:]) if x),
                updates=True,
                ignore="KMTNet: not microlensing" if cls == "4" else None,
            )
        )
    if len(lines) > len(out):
        log.warning("KMTNet: %d of %d lines not understood", len(lines) - len(out), len(lines))
    return out


def fetch(url: str | None = None, timeout: float = 60.0) -> str:
    url = url or URL.format(year=datetime.now().year)
    req = urllib.request.Request(url, headers={"User-Agent": "robofast alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("latin-1")
