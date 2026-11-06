"""Rubin alerts to ToO requests, by the operator's rules (design.md 3.7.1).

The broker-side half of a target of opportunity: a Rubin alert (the LSST
alert schema, v11: ``diaSource``, and for a Solar System object ``ssSource``
and ``mpc_orbits``; as the JSON a broker delivers or the decoded Avro), the
site's rules for which alerts to follow, and the ToO request group each
followed alert becomes, submitted to the request API (``POST /api/toos``,
a token with the ``too`` scope). It runs outside the control processes: a
bad alert, a broker outage or a bug here costs ToOs, never the night.

Which alerts pass is the config's ``rules``, tried in order; the first
whose ``match`` the alert meets makes the ToO from its ``too``; an alert no
rule matches is logged and dropped. ``match`` (every key optional):

* ``kind``: ``sidereal`` (no Solar System association), ``solar_system``
  (an ``ssSource`` with an MPC orbit) or ``any`` (default);
* ``dec``: ``[min, max]`` degrees;
* ``max_mag``: brighter than this (a Solar System object's predicted V,
  ``ssSource.ephVmag``; otherwise the AB magnitude of ``diaSource.psfFlux``,
  the difference flux, so a fading or negative source never passes);
* ``bands``: the alert's band among these;
* ``min_a`` / ``max_a``: semimajor axis limits, au (a TNO: ``min_a`` 30);
* ``designations``: only these Solar System objects.

``too`` (the request): ``observation_type`` (``RAPID_RESPONSE``, default,
or ``TIME_CRITICAL``), ``policy``, ``priority_class``, ``deadline_hours``
(from now; default 6), ``telescopes``, ``instrument_type`` (default
``imager``), ``exposure_time``, ``exposure_count``, ``filter`` (optional).
A Solar System object becomes an OCS ``ORBITAL_ELEMENTS`` target
(``MPC_MINOR_PLANET``, from ``mpc_orbits``), unguided; it is observed only
by a telescope whose mount can track it (docs/moving_targets.md), so on a
mount without rates the request is taken and that telescope refuses it.

Config (``sites/minerva/rubin_alerts.example.json``)::

    {"api": {"url": "http://127.0.0.1:8300", "token_file": "...", "enclosure": "aqawan1"},
     "proposal": "rubin-too",
     "rules": [{"name": "...", "match": {...}, "too": {...}}, ...]}

Run: ``python -m robofast.rubin_alerts submit --config C alert.json ...``
(``--dry-run`` prints the request groups and submits nothing).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("sidereal", "solar_system", "any")
MATCH_KEYS = {"kind", "dec", "max_mag", "bands", "min_a", "max_a", "designations"}
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
}
#: The mpc_orbits fields an MPC_MINOR_PLANET target needs.
ORBIT_FIELDS = ("a", "e", "i", "node", "argperi", "mean_anomaly", "epoch_mjd")


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
        if "dec" in match and not (
            isinstance(match["dec"], list)
            and len(match["dec"]) == 2
            and match["dec"][0] < match["dec"][1]
        ):
            raise ValueError(f"{what}.match.dec: [min, max] degrees")
        for k in ("exposure_time", "exposure_count", "telescopes"):
            if k not in too:
                raise ValueError(f"{what}.too.{k} is required")
        return cls(str(d.get("name") or f"rule {i}"), match, too)


@dataclass
class AlertConfig:
    rules: list[Rule]
    api: dict[str, Any] = field(default_factory=dict)
    proposal: str = "rubin-too"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AlertConfig:
        _keys(d, {"api", "proposal", "rules"}, "rubin alerts config")
        rules = [Rule.from_dict(r, i) for i, r in enumerate(d.get("rules") or [])]
        if not rules:
            raise ValueError("rubin alerts config: no rules (nothing would be followed)")
        api = _keys(d.get("api", {}), {"url", "token_file", "enclosure"}, "api")
        return cls(rules, api, str(d.get("proposal", "rubin-too")))


# -- reading an alert -----------------------------------------------------------------


def _mag_ab(flux_njy: Any) -> float | None:
    if not isinstance(flux_njy, int | float) or flux_njy <= 0:
        return None
    return -2.5 * math.log10(flux_njy) + 31.4


def alert_kind(alert: dict[str, Any]) -> str:
    return "solar_system" if alert.get("ssSource") and alert.get("mpc_orbits") else "sidereal"


def why_not(alert: dict[str, Any], rule: Rule) -> str | None:
    """Why ``rule`` does not match ``alert`` (None: it matches)."""
    m = rule.match
    src = alert.get("diaSource") or {}
    kind = alert_kind(alert)
    if m.get("kind", "any") not in ("any", kind):
        return f"kind {kind}"
    dec = src.get("dec")
    if "dec" in m and not (isinstance(dec, int | float) and m["dec"][0] <= dec <= m["dec"][1]):
        return f"dec {dec}"
    if "bands" in m and src.get("band") not in m["bands"]:
        return f"band {src.get('band')}"
    if "max_mag" in m:
        mag = (alert.get("ssSource") or {}).get("ephVmag") if kind == "solar_system" else None
        if mag is None:
            mag = _mag_ab(src.get("psfFlux"))
        if mag is None or mag > m["max_mag"]:
            return f"magnitude {mag}"
    orb = alert.get("mpc_orbits") or {}
    if ("min_a" in m or "max_a" in m) and (
        orb.get("a") is None or not m.get("min_a", 0.0) <= orb["a"] <= m.get("max_a", math.inf)
    ):
        return f"semimajor axis {orb.get('a')}"
    if "designations" in m and orb.get("designation") not in m["designations"]:
        return f"designation {orb.get('designation')}"
    return None


def orbital_target(alert: dict[str, Any]) -> dict[str, Any]:
    """An OCS ``ORBITAL_ELEMENTS`` target from the alert's ``mpc_orbits``
    (elements at ``epoch_mjd``, TT, as the MPC gives them)."""
    orb = alert["mpc_orbits"]
    missing = [k for k in ORBIT_FIELDS if orb.get(k) is None]
    if missing:
        raise ValueError(f"mpc_orbits lacks {', '.join(missing)}")
    if not 0.0 <= orb["e"] < 1.0:
        raise ValueError(f"eccentricity {orb['e']}: only bound orbits (MPC_MINOR_PLANET)")
    return {
        "type": "ORBITAL_ELEMENTS",
        "name": str(orb.get("designation") or alert["ssSource"].get("designation")),
        "scheme": "MPC_MINOR_PLANET",
        "epochofel": float(orb["epoch_mjd"]),
        "orbinc": float(orb["i"]),
        "longascnode": float(orb["node"]),
        "argofperih": float(orb["argperi"]),
        "meandist": float(orb["a"]),
        "eccentricity": float(orb["e"]),
        "meananom": float(orb["mean_anomaly"]) % 360.0,
    }


def request_group(
    alert: dict[str, Any], rule: Rule, now: datetime, proposal: str = "rubin-too"
) -> dict[str, Any]:
    """The ToO request group (OCS shape) for ``alert`` under ``rule``."""
    src = alert["diaSource"]
    t = rule.too
    gid = f"rubin-{src['diaSourceId']}"
    if alert_kind(alert) == "solar_system":
        target = orbital_target(alert)
    else:
        obj = (alert.get("diaObject") or {}).get("diaObjectId")
        target = {
            "type": "ICRS",
            "name": f"Rubin {obj if obj is not None else src['diaSourceId']}",
            "ra": float(src["ra"]),
            "dec": float(src["dec"]),
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
    too: dict[str, Any] = {
        "deadline": (now + timedelta(hours=float(t.get("deadline_hours", 6.0)))).isoformat()
    }
    for k in ("policy", "priority_class"):
        if k in t:
            too[k] = t[k]
    return {
        "id": gid,
        "name": f"{rule.name}: {target['name']}",
        "proposal": proposal,
        "ipp_value": 10.0,
        "operator": "SINGLE",
        "observation_type": t.get("observation_type", "RAPID_RESPONSE"),
        "extra_params": {"too": too},
        "requests": [
            {
                "id": f"{gid}-r",
                "configurations": [conf],
                "extra_params": {"telescopes": list(t["telescopes"])},
            }
        ],
    }


def decide(
    alert: dict[str, Any], config: AlertConfig, now: datetime
) -> tuple[dict[str, Any] | None, str]:
    """(the request group, why) for one alert: the first matching rule's
    ToO, or (None, why no rule took it)."""
    misses = []
    for rule in config.rules:
        why = why_not(alert, rule)
        if why is not None:
            misses.append(f"{rule.name}: {why}")
            continue
        try:
            return request_group(alert, rule, now, config.proposal), rule.name
        except (KeyError, ValueError, TypeError) as exc:
            misses.append(f"{rule.name}: {exc}")
    return None, "; ".join(misses) or "no rules"


# -- submitting ------------------------------------------------------------------------


def submit(group: dict[str, Any], api: dict[str, Any]) -> dict[str, Any]:
    """POST one ToO to the request API (``/api/toos``); raises on refusal."""
    import httpx

    token = Path(api["token_file"]).read_text().strip()
    url = api["url"].rstrip("/") + "/api/toos"
    params = {"enclosure": api["enclosure"]} if api.get("enclosure") else {}
    r = httpx.post(
        url, json=group, params=params, headers={"Authorization": f"Token {token}"}, timeout=30
    )
    if r.status_code >= 300:
        raise RuntimeError(f"{group['id']}: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast.rubin_alerts")
    ap.add_argument("command", choices=["submit"])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the requests; submit nothing")
    ap.add_argument("alerts", type=Path, nargs="+", help="alert JSON files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = AlertConfig.from_dict(json.loads(args.config.read_text()))
    failed = 0
    for path in args.alerts:
        alert = json.loads(path.read_text())
        group, why = decide(alert, config, datetime.now(UTC))
        if group is None:
            log.info("%s: not followed (%s)", path.name, why)
            continue
        if args.dry_run:
            print(json.dumps(group, indent=1))
            continue
        try:
            reply = submit(group, config.api)
            log.info(
                "%s: %s submitted by rule %r: %s", path.name, group["id"], why, reply.get("intake")
            )
        except Exception as exc:  # one alert's failure is not the others'
            failed += 1
            log.error("%s: %s", path.name, exc)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
