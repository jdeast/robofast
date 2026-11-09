"""Alerts to ToO requests, by the operator's rules: the part every source shares.

Each source (:mod:`robofast.alerts.rubin`, ``tocp``, ``gcn``, ``tns``, and the
microlensing surveys ``ogle``, ``kmtnet``, ``moa``) turns
its own messages into :class:`Alert` records. One set of rules (the config's
``rules``, tried in order) decides which become a ToO: the first rule whose
``match`` an alert meets makes the request from its ``too``; an alert no rule
takes is logged and dropped. ``match`` (every key optional):

* ``sources``: the alert came from one of these (``rubin``, ``tocp``,
  ``gcn``, ``tns``, ``ogle``, ``kmtnet``, ``moa``);
* ``kind``: ``sidereal``, ``solar_system`` or ``any`` (default);
* ``classifications``: the alert's classification starts with one of these,
  ignoring case (TNS ``SN Ia``, ``Nova``; TOCP ``PNV`` (possible nova),
  ``PSN`` (possible supernova), ``TCP``; GCN ``GRB`` or the instrument;
  KMTNet ``clear``, ``probable``, ``not-ulens``);
* ``dec``: ``[min, max]`` degrees;
* ``max_mag``: brighter than this (an alert without a magnitude never passes);
* ``bands``: the magnitude's band among these;
* ``max_error``: a localization no larger than this (degrees, radius);
* ``max_age_hours``: no older than this (from the alert's own time);
* ``min_a`` / ``max_a``: semimajor axis (au), for a Solar System object;
* ``designations``: only these Solar System objects;
* ``min_amax``: a microlensing event's predicted peak magnification at
  least this;
* ``peak_hours``: ``[min, max]``, its predicted peak from ``min`` to ``max``
  hours from now (``[-24, 240]``: from a day ago to ten days ahead).

For a microlensing event, ``mag`` is its predicted peak magnitude (I).

``too``: ``observation_type`` (``RAPID_RESPONSE``, default, or
``TIME_CRITICAL``), ``policy``, ``priority_class``, ``deadline_hours``
(default 6), ``telescopes``, ``instrument_type`` (default ``imager``),
``exposure_time``, ``exposure_count``, ``filter`` (optional), ``cadence``
(microlensing, below). A Solar System
object becomes an OCS ``ORBITAL_ELEMENTS`` target (``MPC_MINOR_PLANET``),
unguided; it is observed only by a mount that can track it
(docs/moving_targets.md).

A microlensing event is followed, not looked at once: ``observation_type``
``NORMAL`` with a ``cadence``, a list of tiers ``{"magnified": A,
"min_interval": s, "max_per_night": n}``. Each tier is one standing request
(``extra_params.cadence``) whose window is the time the event's own fit
(t0, tE, u0) predicts a magnification of at least ``A``, from now on; a tier
the event does not reach is left out. So the visits get denser toward the
peak, and denser still for a high-magnification one. A ToO cannot have a
cadence (docs/request_language.md), so these are NORMAL request groups,
submitted to ``POST /api/requestgroups`` (a token with the ``submit``
scope); ToOs go to ``/api/toos``.

ToOs go to the request API's ``POST /api/toos`` (a token with the ``too``
scope). This runs outside the control processes: a bad alert, a
source outage or a bug here costs ToOs, never the night.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SOURCES = ("rubin", "tocp", "gcn", "tns", "ogle", "kmtnet", "moa")
KINDS = ("sidereal", "solar_system", "any")
MATCH_KEYS = {
    "sources",
    "kind",
    "classifications",
    "dec",
    "max_mag",
    "bands",
    "max_error",
    "max_age_hours",
    "min_a",
    "max_a",
    "designations",
    "min_amax",
    "peak_hours",
}
TOO_KEYS = {
    "observation_type",
    "policy",
    "priority_class",
    "deadline_hours",
    "telescopes",
    "instrument_type",
    "exposure_time",
    "exposure_count",
    "filter",
    "cadence",
}
TIER_KEYS = {"magnified", "min_interval", "max_per_night"}
#: The orbit fields an MPC_MINOR_PLANET target needs (Rubin's mpc_orbits names).
ORBIT_FIELDS = ("a", "e", "i", "node", "argperi", "mean_anomaly", "epoch_mjd")


@dataclass
class Alert:
    """One alert, whatever its source."""

    source: str
    #: Unique within the source (de-duplication, the request group's id).
    id: str
    name: str
    ra: float | None = None  # deg, ICRS
    dec: float | None = None
    #: Localization radius (deg); 0 for a point source.
    error: float = 0.0
    mag: float | None = None
    band: str | None = None
    time: datetime | None = None
    classification: str | None = None
    #: A Solar System object's orbit (Rubin ``mpc_orbits`` field names).
    orbit: dict[str, Any] | None = None
    #: Whether the source says this is a test or a retraction (never followed).
    ignore: str | None = None
    #: A microlensing event's point-lens fit, as the survey gives it: the peak
    #: time, the Einstein time (days), the impact parameter (Einstein radii)
    #: and the peak magnification.
    peak: datetime | None = None
    te: float | None = None
    u0: float | None = None
    amax: float | None = None
    #: The same event under other names (``source:id``), as the source gives them.
    aliases: tuple[str, ...] = ()
    #: The source refits this alert in place (a microlensing survey's page),
    #: so one that no rule takes now is looked at again at the next poll.
    updates: bool = False

    @property
    def kind(self) -> str:
        return "solar_system" if self.orbit else "sidereal"


def magnification(u: float) -> float:
    """A point lens's magnification at separation ``u`` (Einstein radii)."""
    if u <= 0.0:
        return math.inf
    return (u * u + 2.0) / (u * math.sqrt(u * u + 4.0))


def magnified(alert: Alert, a: float) -> tuple[datetime, datetime] | None:
    """When ``alert``'s fit predicts a magnification of at least ``a`` (> 1):
    |t - t0| <= tE sqrt(u_a^2 - u0^2), with A(u_a) = a; None if never."""
    if alert.peak is None or alert.te is None or alert.u0 is None:
        raise ValueError("no microlensing fit (t0, tE, u0)")
    ua2 = 2.0 * (a / math.sqrt(a * a - 1.0) - 1.0)
    if alert.u0**2 >= ua2:
        return None
    half = timedelta(days=alert.te * math.sqrt(ua2 - alert.u0**2))
    return alert.peak - half, alert.peak + half


def jd_utc(jd: float) -> datetime:
    """A (heliocentric) Julian date as a UTC time. The surveys give HJD; the
    heliocentric correction (at most 8.3 min) and TT - UTC are left in, which
    against an Einstein time of days does not matter here."""
    return datetime(2000, 1, 1, 12, tzinfo=UTC) + timedelta(seconds=round((jd - 2451545.0) * 86400))


def _keys(d: Any, allowed: set[str], what: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ValueError(f"{what}: expected an object")
    unknown = set(d) - allowed - {"_note"}
    if unknown:
        raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
    return d


@dataclass
class Rule:
    name: str
    match: dict[str, Any]
    too: dict[str, Any]

    @classmethod
    def from_dict(cls, d: Any, i: int) -> Rule:
        what = f"rules[{i}]"
        _keys(d, {"name", "match", "too"}, what)
        match = _keys(d.get("match", {}), MATCH_KEYS, f"{what}.match")
        too = _keys(d.get("too", {}), TOO_KEYS, f"{what}.too")
        if match.get("kind", "any") not in KINDS:
            raise ValueError(f"{what}.match.kind: one of {list(KINDS)}")
        if set(match.get("sources", [])) - set(SOURCES):
            raise ValueError(f"{what}.match.sources: among {list(SOURCES)}")
        for k, unit in (("dec", "degrees"), ("peak_hours", "hours from now")):
            v = match.get(k)
            if v is not None and not (isinstance(v, list) and len(v) == 2 and v[0] < v[1]):
                raise ValueError(f"{what}.match.{k}: [min, max] {unit}")
        normal = too.get("observation_type") == "NORMAL"
        if normal != ("cadence" in too):
            raise ValueError(f"{what}.too: a cadence needs observation_type NORMAL, and NORMAL one")
        tiers = too.get("cadence", [])
        if normal and not (isinstance(tiers, list) and tiers):
            raise ValueError(f"{what}.too.cadence: a list of tiers")
        for k, tier in enumerate(tiers):
            _keys(tier, TIER_KEYS, f"{what}.too.cadence[{k}]")
            if not float(tier.get("magnified", 0)) > 1.0 or "min_interval" not in tier:
                raise ValueError(
                    f"{what}.too.cadence[{k}]: magnified (> 1) and min_interval (s) are required"
                )
        for k in ("exposure_time", "exposure_count", "telescopes"):
            if k not in too:
                raise ValueError(f"{what}.too.{k} is required")
        return cls(str(d.get("name") or f"rule {i}"), match, too)


def load_rules(raw: Any) -> list[Rule]:
    rules = [Rule.from_dict(r, i) for i, r in enumerate(raw or [])]
    if not rules:
        raise ValueError("alerts config: no rules (nothing would be followed)")
    return rules


def why_not(alert: Alert, rule: Rule, now: datetime) -> str | None:
    """Why ``rule`` does not take ``alert`` (None: it does)."""
    m = rule.match
    if alert.ignore:
        return alert.ignore
    if "sources" in m and alert.source not in m["sources"]:
        return f"source {alert.source}"
    if m.get("kind", "any") not in ("any", alert.kind):
        return f"kind {alert.kind}"
    if "classifications" in m:
        c = (alert.classification or "").lower()
        if not any(c.startswith(x.lower()) for x in m["classifications"]):
            return f"classification {alert.classification}"
    if "dec" in m and not (alert.dec is not None and m["dec"][0] <= alert.dec <= m["dec"][1]):
        return f"dec {alert.dec}"
    if "max_mag" in m and (alert.mag is None or alert.mag > m["max_mag"]):
        return f"magnitude {alert.mag}"
    if "bands" in m and alert.band not in m["bands"]:
        return f"band {alert.band}"
    if "max_error" in m and alert.error > m["max_error"]:
        return f"localization {alert.error} deg"
    if "max_age_hours" in m and (
        alert.time is None or now - alert.time > timedelta(hours=m["max_age_hours"])
    ):
        return f"age (time {alert.time})"
    a = (alert.orbit or {}).get("a")
    if ("min_a" in m or "max_a" in m) and (
        a is None or not m.get("min_a", 0.0) <= a <= m.get("max_a", math.inf)
    ):
        return f"semimajor axis {a}"
    if "designations" in m and (alert.orbit or {}).get("designation") not in m["designations"]:
        return f"designation {(alert.orbit or {}).get('designation')}"
    if "min_amax" in m and (alert.amax is None or alert.amax < m["min_amax"]):
        return f"peak magnification {alert.amax}"
    if "peak_hours" in m and (
        alert.peak is None
        or not m["peak_hours"][0] <= (alert.peak - now).total_seconds() / 3600 <= m["peak_hours"][1]
    ):
        return f"peak (t0 {alert.peak})"
    return None


def orbital_target(alert: Alert) -> dict[str, Any]:
    """An OCS ``ORBITAL_ELEMENTS`` target from ``alert.orbit`` (elements at
    ``epoch_mjd``, TT, as the MPC gives them)."""
    orb = alert.orbit or {}
    missing = [k for k in ORBIT_FIELDS if orb.get(k) is None]
    if missing:
        raise ValueError(f"orbit lacks {', '.join(missing)}")
    if not 0.0 <= orb["e"] < 1.0:
        raise ValueError(f"eccentricity {orb['e']}: only bound orbits (MPC_MINOR_PLANET)")
    return {
        "type": "ORBITAL_ELEMENTS",
        "name": alert.name,
        "scheme": "MPC_MINOR_PLANET",
        "epochofel": float(orb["epoch_mjd"]),
        "orbinc": float(orb["i"]),
        "longascnode": float(orb["node"]),
        "argofperih": float(orb["argperi"]),
        "meandist": float(orb["a"]),
        "eccentricity": float(orb["e"]),
        "meananom": float(orb["mean_anomaly"]) % 360.0,
    }


def group_id(alert: Alert) -> str:
    return f"{alert.source}-" + re.sub(r"[^A-Za-z0-9_+-]+", "_", alert.id)[:60]


def request_group(alert: Alert, rule: Rule, now: datetime, proposal: str) -> dict[str, Any]:
    """The ToO request group (OCS shape) for ``alert`` under ``rule``."""
    t = rule.too
    gid = group_id(alert)
    if alert.kind == "solar_system":
        target = orbital_target(alert)
    else:
        if alert.ra is None or alert.dec is None:
            raise ValueError("no position")
        target = {
            "type": "ICRS",
            "name": alert.name,
            "ra": float(alert.ra),
            "dec": float(alert.dec),
        }
    ic: dict[str, Any] = {
        "exposure_time": float(t["exposure_time"]),
        "exposure_count": int(t["exposure_count"]),
    }
    if t.get("filter"):
        ic["optical_elements"] = {"filter": t["filter"]}
    conf: dict[str, Any] = {
        "id": f"{gid}-c",
        "type": "EXPOSE",
        "instrument_type": t.get("instrument_type", "imager"),
        "target": target,
        "instrument_configs": [ic],
    }
    if target["type"] == "ORBITAL_ELEMENTS":
        conf["guiding_config"] = {"mode": "OFF"}
    telescopes = list(t["telescopes"])
    if "cadence" in t:
        return {
            "id": gid,
            "name": f"{rule.name}: {alert.name}",
            "proposal": proposal,
            "ipp_value": 10.0,
            "observation_type": "NORMAL",
            "requests": _tiers(alert, t["cadence"], conf, telescopes, gid, now),
        }
    too: dict[str, Any] = {
        "deadline": (now + timedelta(hours=float(t.get("deadline_hours", 6.0)))).isoformat()
    }
    for k in ("policy", "priority_class"):
        if k in t:
            too[k] = t[k]
    return {
        "id": gid,
        "name": f"{rule.name}: {alert.name}",
        "proposal": proposal,
        "ipp_value": 10.0,
        "operator": "SINGLE",
        "observation_type": t.get("observation_type", "RAPID_RESPONSE"),
        "extra_params": {"too": too},
        "requests": [
            {
                "id": f"{gid}-r",
                "configurations": [conf],
                "extra_params": {"telescopes": telescopes},
            }
        ],
    }


def _tiers(
    alert: Alert,
    tiers: list[dict[str, Any]],
    conf: dict[str, Any],
    telescopes: list[str],
    gid: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """One standing request per cadence tier, its window the time ``alert``
    is predicted magnified at least the tier's ``magnified``, from now on."""
    out = []
    for k, tier in enumerate(tiers, 1):
        when = magnified(alert, float(tier["magnified"]))
        if when is None or when[1] <= now:
            continue
        cadence: dict[str, Any] = {"min_interval": float(tier["min_interval"])}
        if "max_per_night" in tier:
            cadence["max_per_night"] = int(tier["max_per_night"])
        out.append(
            {
                "id": f"{gid}-r{k}",
                "windows": [{"start": max(now, when[0]).isoformat(), "end": when[1].isoformat()}],
                "configurations": [{**conf, "id": f"{gid}-c{k}"}],
                "extra_params": {"telescopes": telescopes, "cadence": cadence},
            }
        )
    if not out:
        raise ValueError(f"no cadence tier ahead (t0 {alert.peak}, Amax {alert.amax})")
    return out


def decide(
    alert: Alert, rules: list[Rule], now: datetime, proposal: str = "alerts-too"
) -> tuple[dict[str, Any] | None, str]:
    """(the request group, the rule's name) for one alert, or (None, why no
    rule took it)."""
    misses = []
    for rule in rules:
        why = why_not(alert, rule, now)
        if why is not None:
            misses.append(f"{rule.name}: {why}")
            continue
        try:
            return request_group(alert, rule, now, proposal), rule.name
        except (KeyError, ValueError, TypeError) as exc:
            misses.append(f"{rule.name}: {exc}")
    return None, "; ".join(misses) or "no rules"


@dataclass
class ApiConfig:
    url: str = "http://127.0.0.1:8300"
    token_file: str = ""
    enclosure: str | None = None


def submit(group: dict[str, Any], api: ApiConfig) -> dict[str, Any]:
    """POST one group to the request API (a ToO to ``/api/toos``, a NORMAL
    group to ``/api/requestgroups``); raises on refusal."""
    import httpx

    token = Path(api.token_file).read_text().strip()
    params = {"enclosure": api.enclosure} if api.enclosure else {}
    r = httpx.post(
        api.url.rstrip("/")
        + ("/api/requestgroups" if group.get("observation_type") == "NORMAL" else "/api/toos"),
        json=group,
        params=params,
        headers={"Authorization": f"Token {token}"},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"{group['id']}: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def whoami(api: ApiConfig) -> dict[str, Any]:
    """The request API's ``GET /api/`` for the alerts token: its ``name``
    and ``scopes``; raises if the token cannot be read or the API asked."""
    import httpx

    token = Path(api.token_file).read_text().strip()
    r = httpx.get(
        api.url.rstrip("/") + "/api/", headers={"Authorization": f"Token {token}"}, timeout=10
    )
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def utc(text: str | None) -> datetime | None:
    """An ISO time as aware UTC (naive means UTC); None if absent or bad."""
    if not text:
        return None
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@dataclass
class Seen:
    """Alerts already handled, by ``source:id`` (persisted: a restart does not
    resubmit). Kept for ``keep_days`` from their time (for a followed
    microlensing event, the end of its last window). ``where``: the followed
    microlensing events' positions, so another survey's name for the same
    star is not followed twice."""

    path: Path | None
    keep_days: float = 30.0
    ids: dict[str, str] = field(default_factory=dict)
    where: dict[str, list[float]] = field(default_factory=dict)

    def load(self) -> Seen:
        import json

        if self.path is not None and self.path.exists():
            d = json.loads(self.path.read_text())
            if "ids" in d and isinstance(d["ids"], dict):
                self.ids, self.where = d["ids"], d.get("where", {})
            else:  # the previous file: ids only
                self.ids = d
        return self

    def twin(self, alert: Alert, radius_arcsec: float = 2.0) -> str | None:
        """A followed event that is ``alert``'s star: one of its aliases, or
        one within ``radius_arcsec``."""
        for k in alert.aliases:
            if k in self.ids:
                return k
        if alert.ra is None or alert.dec is None:
            return None
        d1, r1 = math.radians(alert.dec), math.radians(alert.ra)
        for k, (ra, dec) in self.where.items():
            d2 = math.radians(dec)
            c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(
                r1 - math.radians(ra)
            )
            if math.degrees(math.acos(min(1.0, c))) * 3600.0 <= radius_arcsec:
                return k
        return None

    def __contains__(self, key: str) -> bool:
        return key in self.ids

    def prepare(self) -> None:
        """Make sure :meth:`save` can write: create the directory if it is
        missing; raise :class:`OSError` (with the reason) if it cannot be
        created or written."""
        import os

        if self.path is None:
            return
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not os.access(parent, os.W_OK | os.X_OK):
            raise PermissionError(f"{parent}: not writable")

    def add(self, key: str, now: datetime, where: tuple[float, float] | None = None) -> None:
        self.ids[key] = now.isoformat()
        if where is not None:
            self.where[key] = [float(where[0]), float(where[1])]

    def save(self, now: datetime) -> None:
        import json
        import os

        if self.path is None:
            return
        cutoff = now - timedelta(days=self.keep_days)
        self.ids = {k: v for k, v in self.ids.items() if (utc(v) or now) >= cutoff}
        self.where = {k: v for k, v in self.where.items() if k in self.ids}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ids": self.ids, "where": self.where}, indent=0))
        os.replace(tmp, self.path)
