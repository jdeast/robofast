"""Alerts to ToO requests: sources (Rubin, CBAT's TOCP, GCN, TNS), the
operator's rules, and the service that runs them (docs/alerts.md).

``python -m robofast.alerts run|submit|check --config alerts.json``."""

from robofast.alerts.core import Alert, Rule, decide, request_group, why_not

__all__ = ["Alert", "Rule", "decide", "request_group", "why_not"]
