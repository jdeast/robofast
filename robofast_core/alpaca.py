"""Factories that build connected alpyca devices for a :class:`DeviceOwner`.

alpyca's global request lock
----------------------------
In alpyca 3.1.x, ``alpaca.device.Device._get``/``_put`` hold a *class-wide*
lock (``Device._ctid_lock``) for the whole HTTP request, and use a fixed 5 s
HTTP timeout. Within one process that serializes every request to every
Alpaca device: one unresponsive device (a hung camera, a hung weather
server) holds the lock for 5 s per request and delays everything else,
including the safety supervisor's ``CloseShutter``. The simulator test
``test_stock_alpyca_lock_blocks_other_devices`` demonstrates it.

By default this module therefore builds thin subclasses of the alpyca device
classes whose ``_get``/``_put`` do the same request *without* the global lock,
with a per-instance transaction counter, and with an HTTP timeout the owner
sets per call (``_robofast_http_timeout``), so an abandoned worker thread ends
when its call's timeout does. Everything else (URL building, error mapping
via ``Device.__check_error``) is alpyca's. ``isolate_requests=False`` gives
stock alpyca.

This relies on two alpyca internals (``_get``/``_put`` signatures and the
name-mangled ``_Device__check_error``); ``tests/core/test_alpaca_shim.py``
pins them so an alpyca upgrade that changes them fails loudly. Raising this
upstream is better than carrying the shim long-term.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from robofast_core.owner import DeviceOwner
from robofast_core.timeline import Timeline


def _device_class(device_type: str) -> type:
    device_type = device_type.lower()
    if device_type == "telescope":
        from alpaca.telescope import Telescope

        return Telescope
    if device_type == "camera":
        from alpaca.camera import Camera

        return Camera
    if device_type == "dome":
        from alpaca.dome import Dome

        return Dome
    if device_type == "safetymonitor":
        from alpaca.safetymonitor import SafetyMonitor

        return SafetyMonitor
    if device_type == "observingconditions":
        from alpaca.observingconditions import ObservingConditions

        return ObservingConditions
    raise ValueError(f"unsupported device type {device_type!r}")


class _IsolatedRequests:
    """Mixin: alpyca requests without the process-wide lock."""

    _robofast_http_timeout: float = 5.0
    _robofast_ctid = itertools.count(1)

    def _headers(self) -> dict[str, str]:
        if self.address.startswith("[") and not self.address.startswith("[::1]"):
            return {"Host": f"{self.address.split('%')[0]}]"}
        return {}

    def _params(self, data: dict[str, Any]) -> dict[str, Any]:
        from alpaca.device import Device

        return {
            "ClientTransactionID": f"{next(self._robofast_ctid)}",
            "ClientID": f"{Device._client_id}",
            **data,
        }

    def _get(self, attribute: str, tmo: float = 5.0, **data: Any) -> Any:
        response = self.rqs.get(
            f"{self.base_url}/{attribute}",
            params=self._params(data),
            timeout=self._robofast_http_timeout,
            headers=self._headers(),
        )
        self._Device__check_error(response)
        return response.json()["Value"]

    def _put(self, attribute: str, tmo: float = 5.0, **data: Any) -> Any:
        response = self.rqs.put(
            f"{self.base_url}/{attribute}",
            data=self._params(data),
            timeout=self._robofast_http_timeout,
            headers=self._headers(),
        )
        self._Device__check_error(response)
        return response.json()


_isolated_classes: dict[type, type] = {}


def isolated_class(cls: type) -> type:
    if cls not in _isolated_classes:
        _isolated_classes[cls] = type(f"Isolated{cls.__name__}", (_IsolatedRequests, cls), {})
    return _isolated_classes[cls]


def alpaca_factory(
    device_type: str,
    address: str,
    number: int = 0,
    *,
    isolate_requests: bool = True,
) -> Callable[[], Any]:
    """Return a callable that builds and connects one alpyca device.

    It runs on the owner's worker thread (it does network I/O). A fresh
    object (with a fresh HTTP session) is built after a timeout.
    """
    cls = _device_class(device_type)
    if isolate_requests:
        cls = isolated_class(cls)

    def make() -> Any:
        dev = cls(address, number)
        if not dev.Connected:
            dev.Connected = True
        return dev

    return make


def alpaca_owner(
    name: str,
    device_type: str,
    address: str,
    timeline: Timeline,
    *,
    number: int = 0,
    default_timeout: float = 5.0,
    privileged_members: frozenset[str] | set[str] = frozenset(),
    isolate_requests: bool = True,
) -> DeviceOwner:
    return DeviceOwner(
        name,
        alpaca_factory(device_type, address, number, isolate_requests=isolate_requests),
        timeline,
        default_timeout=default_timeout,
        privileged_members=privileged_members,
    )
