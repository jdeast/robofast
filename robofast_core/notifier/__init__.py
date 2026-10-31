"""The alarm notifier: the observer console's alarms by email, chat or a
command, as its own process (docs/notifier.md).

``python -m robofast_core.notifier --config notifier.json``
"""

from robofast_core.notifier.core import (
    LEVELS,
    Notification,
    Notifier,
    Settings,
    SinkWorker,
)
from robofast_core.notifier.sinks import CommandSink, EmailSink, WebhookSink
from robofast_core.notifier.sources import AcksFile, AggregatorSource, ConsoleHealth

__all__ = [
    "LEVELS",
    "AcksFile",
    "AggregatorSource",
    "CommandSink",
    "ConsoleHealth",
    "EmailSink",
    "Notification",
    "Notifier",
    "Settings",
    "SinkWorker",
    "WebhookSink",
]
