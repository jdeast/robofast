"""The Transient Name Server (TNS): every reported transient, with its
classification (``SN Ia``, ``Nova``, ...) once spectroscopically typed.
Needs a TNS bot (bot id, name and API key, from a TNS account). The
search and object replies' field names follow the TNS API documentation;
the live API is not yet checked here (no bot key)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from robofast.alerts.core import Alert, utc

API = "https://www.wis-tns.org/api/get"


def from_tns(o: dict[str, Any]) -> Alert:
    prefix = o.get("name_prefix") or ""
    name = f"{prefix} {o['objname']}".strip()
    otype = (o.get("object_type") or {}).get("name")
    band = (o.get("discmagfilter") or {}).get("name")
    mag = o.get("discoverymag")
    return Alert(
        source="tns",
        id=str(o["objname"]),
        name=name,
        ra=float(o["radeg"]),
        dec=float(o["decdeg"]),
        mag=float(mag) if mag not in (None, "") else None,
        band=band,
        time=utc(o.get("discoverydate")),
        classification=otype or prefix or None,
    )


class TNS:
    def __init__(self, bot_id: int, bot_name: str, api_key: str) -> None:
        marker = json.dumps({"tns_id": int(bot_id), "type": "bot", "name": bot_name})
        self.headers = {"User-Agent": f"tns_marker{marker}"}
        self.api_key = api_key

    def _post(self, what: str, data: dict[str, Any]) -> Any:
        import httpx

        r = httpx.post(
            f"{API}/{what}",
            headers=self.headers,
            data={"api_key": self.api_key, "data": json.dumps(data)},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["data"]["reply"]

    def recent(self, since: datetime) -> list[Alert]:
        """Objects made public or changed since ``since`` (UTC)."""
        stamp = since.strftime("%Y-%m-%d %H:%M:%S")
        rows = self._post("search", {"public_timestamp": stamp})
        out = []
        for row in rows:
            obj = self._post("object", {"objname": row["objname"], "photometry": "0"})
            out.append(from_tns(obj))
        return out
