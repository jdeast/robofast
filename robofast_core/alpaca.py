"""Build a :class:`DeviceOwner` for an Alpaca device.

The driver is robofast's own :class:`~robofast_core.alpaca_client.AlpacaDevice`
(asyncio, one HTTP client per device, per-call timeouts, no process-wide
lock). It is built lazily by the owner, and connected (``Connected`` = True)
before its first command; a fresh one replaces it after a timeout.
"""

from __future__ import annotations

from robofast_core.alpaca_client import AlpacaDevice
from robofast_core.owner import DeviceOwner
from robofast_core.timeline import Timeline

#: The device types robofast_core drives.
DEVICE_TYPES = frozenset(
    {"telescope", "camera", "filterwheel", "dome", "safetymonitor", "observingconditions"}
)


def alpaca_owner(
    name: str,
    device_type: str,
    address: str,
    timeline: Timeline,
    *,
    number: int = 0,
    default_timeout: float = 5.0,
    privileged_members: frozenset[str] | set[str] = frozenset(),
) -> DeviceOwner:
    """An owner for device ``number`` of ``device_type`` at ``address`` (host:port)."""
    if device_type.lower() not in DEVICE_TYPES:
        raise ValueError(f"unsupported device type {device_type!r}")
    return DeviceOwner(
        name,
        lambda: AlpacaDevice(address, device_type, number),
        timeline,
        default_timeout=default_timeout,
        privileged_members=privileged_members,
    )
