"""GCN notices (NASA's General Coordinates Network, the JSON notices on its
Kafka stream: Swift BAT-GUANO, Einstein Probe WXT, SVOM, Fermi GBM, ...).
A retraction or a test notice is never followed. The localization's
``ra_dec_error`` (degrees) is the alert's ``error``: a rule's ``max_error``
keeps arcminute-class localizations (what an imager can cover) and drops
Fermi GBM's degrees. Credentials: gcn.nasa.gov (client id and secret); the
Kafka client is the optional ``gcn-kafka`` package."""

from __future__ import annotations

from typing import Any

from robofast.alerts.core import Alert, utc


def from_gcn(topic: str, n: dict[str, Any]) -> Alert:
    ids = n.get("id")
    trig = ids[0] if isinstance(ids, list) and ids else ids or n.get("trigger_time", "")
    inst = n.get("instrument") or topic.split(".")[-2]
    cls = n.get("classification")
    if isinstance(cls, dict) and cls:
        label = max(cls, key=lambda k: cls[k])
    else:
        label = n.get("follow_up_type") or inst
    ignore = None
    if n.get("alert_type") == "retraction":
        ignore = "a retraction"
    elif n.get("alert_tense") == "test":
        ignore = "a test notice"
    return Alert(
        source="gcn",
        id=f"{inst}-{trig}",
        name=f"{n.get('mission') or inst} {trig}",
        ra=n.get("ra"),
        dec=n.get("dec"),
        error=float(n.get("ra_dec_error") or 0.0) if n.get("ra") is not None else 180.0,
        time=utc(n.get("trigger_time")),
        classification=f"{label} ({inst})" if label != inst else inst,
        ignore=ignore,
    )


def consume(client_id: str, client_secret: str, topics: list[str], deliver) -> None:
    """Block, calling ``deliver(alert)`` for each notice (run in a thread)."""
    import json

    from gcn_kafka import Consumer  # optional dependency

    consumer = Consumer(client_id=client_id, client_secret=client_secret)
    consumer.subscribe(topics)
    while True:
        for message in consumer.consume(timeout=1):
            if message.error():
                continue
            try:
                deliver(from_gcn(message.topic(), json.loads(message.value())))
            except (ValueError, TypeError, KeyError):
                continue
