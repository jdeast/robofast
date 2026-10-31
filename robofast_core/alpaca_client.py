"""A small asyncio ASCOM Alpaca client (Device API v1).

robofast talks to each Alpaca device through one :class:`AlpacaDevice`, which
owns its own ``httpx.AsyncClient`` (and so its own connection pool). There is
no process-wide lock and no fixed timeout: the caller (the device's
:class:`~robofast_core.owner.DeviceOwner`) supplies a timeout per request, and
a request to one device never waits on a request to another. (alpyca 3.1.x,
which this replaces, held one class-wide lock for every HTTP request with a
fixed 5 s timeout, so a single hung device delayed every other device in the
process, including the safety supervisor's ``CloseShutter``.)

The protocol, from the published Alpaca Device API v1:

* ``GET  /api/v1/{device_type}/{device_number}/{member}`` with parameters in
  the query string, ``PUT`` with parameters form-encoded in the body. URL
  elements are lower case.
* Every request carries ``ClientID`` (fixed per process, 1..2**32-1) and a
  ``ClientTransactionID`` that starts at 1 and increments on each request
  (per :class:`AlpacaDevice` here).
* The current spec says parameter *names* are case-insensitive, but older
  servers matched PUT body names exactly, so we always send the spec's own
  spelling (``Connected``, ``Duration``, ``Light``, ``RightAscension``, ...).
* A 200 response is JSON with ``ErrorNumber``/``ErrorMessage`` and, for
  reads, ``Value``. A non-zero ``ErrorNumber`` is an ASCOM exception (mapped
  below); HTTP 400 and 500 carry a plain-text message.

Every error is a :class:`~robofast_core.owner.DeviceError`; a request that
exceeds its timeout raises :class:`~robofast_core.owner.DeviceTimeout`.

Device methods (as opposed to properties) are listed in :data:`METHODS` with
their HTTP verb and parameter names; only the ones robofast_core uses are
there. :meth:`AlpacaDevice.get` and :meth:`AlpacaDevice.put` reach any member.

Camera images
-------------
``get("ImageArray")`` (so also ``Grant.get("ImageArray", timeout=...)``
through the camera's owner) returns an
:class:`~robofast_core.imagebytes.ImageArray`, not a nested list. It asks
for the ImageBytes binary transfer (``Accept: application/imagebytes``) and
takes whatever the server sends: ImageBytes, or JSON from a server without
it (and for errors). ``imagebytes=False`` asks for JSON only. The body is
read into one preallocated buffer when its length is known, and the pixels
are a zero-copy view of it (:mod:`robofast_core.imagebytes`). The caller's
timeout bounds the whole transfer, so size it by the image
(:func:`robofast_core.imaging.download_timeout`).
"""

from __future__ import annotations

import itertools
import random
from typing import Any

import httpx

from robofast_core import imagebytes
from robofast_core.owner import DeviceError, DeviceTimeout

#: One ClientID per process, as the spec suggests (a random 1..65535).
CLIENT_ID = random.randint(1, 65535)
_UINT32_MAX = 2**32 - 1


# -- errors -------------------------------------------------------------------


class AlpacaError(DeviceError):
    """The device answered with a non-zero ASCOM ``ErrorNumber``."""

    number: int = 0

    def __init__(self, message: str, number: int | None = None) -> None:
        super().__init__(message)
        if number is not None:
            self.number = number


class NotImplementedException(AlpacaError):
    number = 0x400


class InvalidValueException(AlpacaError):
    number = 0x401


class ValueNotSetException(AlpacaError):
    number = 0x402


class NotConnectedException(AlpacaError):
    number = 0x407


class ParkedException(AlpacaError):
    number = 0x408


class SlavedException(AlpacaError):
    number = 0x409


class InvalidOperationException(AlpacaError):
    number = 0x40B


class ActionNotImplementedException(AlpacaError):
    number = 0x40C


class OperationCancelledException(AlpacaError):
    number = 0x40E


class DriverException(AlpacaError):
    """A driver-specific error, 0x500..0xFFF."""


_BY_NUMBER: dict[int, type[AlpacaError]] = {
    cls.number: cls
    for cls in (
        NotImplementedException,
        InvalidValueException,
        ValueNotSetException,
        NotConnectedException,
        ParkedException,
        SlavedException,
        InvalidOperationException,
        ActionNotImplementedException,
        OperationCancelledException,
    )
}


def error_class(number: int) -> type[AlpacaError]:
    if number in _BY_NUMBER:
        return _BY_NUMBER[number]
    if 0x500 <= number <= 0xFFF:
        return DriverException
    return AlpacaError


class AlpacaHTTPError(DeviceError):
    """The server answered with an HTTP status other than 200."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class AlpacaBadRequest(AlpacaHTTPError):
    """400: the server could not interpret the request (bad device, value...)."""


class AlpacaNotFound(AlpacaHTTPError):
    """404: no such device or member on this server (not in the spec, but common)."""


class AlpacaServerError(AlpacaHTTPError):
    """500: an unexpected error inside the server."""


_BY_STATUS: dict[int, type[AlpacaHTTPError]] = {
    400: AlpacaBadRequest,
    404: AlpacaNotFound,
    500: AlpacaServerError,
}


class AlpacaTransportError(DeviceError):
    """The request never got an HTTP answer (refused, reset, unreachable)."""


class AlpacaProtocolError(DeviceError):
    """A 200 response that is not a valid Alpaca JSON response."""


# -- members --------------------------------------------------------------------

#: Device methods robofast_core calls: name -> (HTTP verb, parameter names).
METHODS: dict[str, tuple[str, tuple[str, ...]]] = {
    # camera
    "StartExposure": ("PUT", ("Duration", "Light")),
    "AbortExposure": ("PUT", ()),
    "StopExposure": ("PUT", ()),
    # telescope
    "SlewToCoordinatesAsync": ("PUT", ("RightAscension", "Declination")),
    "AbortSlew": ("PUT", ()),
    "Park": ("PUT", ()),
    "Unpark": ("PUT", ()),
    # dome
    "OpenShutter": ("PUT", ()),
    "CloseShutter": ("PUT", ()),
    # observingconditions
    "TimeSinceLastUpdate": ("GET", ("SensorName",)),
    # any device: a driver-specific action (the enclosure heartbeat)
    "Action": ("PUT", ("Action", "Parameters")),
}


def _encode(value: Any) -> str:
    # Alpaca servers (OmniSim, ASP.NET) parse booleans as "True"/"False".
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


# -- client ---------------------------------------------------------------------


class AlpacaDevice:
    """One Alpaca device: ``http://{address}/api/v1/{device_type}/{number}``.

    ``address`` is ``host:port``. Not safe for concurrent use by design: its
    :class:`~robofast_core.owner.DeviceOwner` sends one request at a time.
    """

    def __init__(
        self,
        address: str,
        device_type: str,
        number: int = 0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        imagebytes: bool = True,
    ) -> None:
        self.device_type = device_type.lower()
        #: Ask cameras for the ImageBytes transfer (module doc).
        self.imagebytes = imagebytes
        self.number = number
        self.label = f"{self.device_type}/{number}@{address}"
        self._ctid = itertools.count(1)
        # trust_env=False: never route device traffic through an HTTP proxy
        # picked up from the environment. ``transport`` is for tests.
        self._http = httpx.AsyncClient(
            base_url=f"http://{address}/api/v1/{self.device_type}/{number}/",
            trust_env=False,
            transport=transport,
        )

    def _next_ctid(self) -> int:
        n = next(self._ctid)
        if n > _UINT32_MAX:  # wrap; zero is reserved
            self._ctid = itertools.count(2)
            n = 1
        return n

    async def _request(self, verb: str, member: str, params: dict[str, Any], timeout: float) -> Any:
        args = {"ClientID": CLIENT_ID, "ClientTransactionID": self._next_ctid()}
        args.update({k: _encode(v) for k, v in params.items()})
        what = f"{self.label} {verb} {member}"
        try:
            if verb == "GET":
                r = await self._http.get(member.lower(), params=args, timeout=timeout)
            else:
                r = await self._http.put(member.lower(), data=args, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise DeviceTimeout(f"{what}: no answer within {timeout} s") from exc
        except httpx.TransportError as exc:
            raise AlpacaTransportError(f"{what}: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            cls = _BY_STATUS.get(r.status_code, AlpacaHTTPError)
            raise cls(f"{what}: HTTP {r.status_code}: {r.text.strip()[:200]}", r.status_code)
        try:
            body = r.json()
            number = int(body.get("ErrorNumber", 0))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AlpacaProtocolError(f"{what}: not an Alpaca response: {r.text[:200]!r}") from exc
        if number != 0:
            cls = error_class(number)
            msg = body.get("ErrorMessage") or ""
            raise cls(f"{what}: {cls.__name__} (0x{number:X}): {msg}", number)
        return body.get("Value")

    # -- generic -----------------------------------------------------------

    async def get(self, member: str, *, timeout: float, **params: Any) -> Any:
        """Read a property (or call a GET method); return ``Value``.
        ``ImageArray`` returns an :class:`~robofast_core.imagebytes.ImageArray`
        (module doc)."""
        if member.lower() == "imagearray" and not params:
            return await self.image_array(timeout=timeout)
        return await self._request("GET", member, params, timeout)

    async def image_array(
        self, *, timeout: float, binary: bool | None = None
    ) -> imagebytes.ImageArray:
        """The camera's last image (module doc); ``binary`` overrides
        :attr:`imagebytes` for this call."""
        want_bytes = self.imagebytes if binary is None else binary
        args = {"ClientID": CLIENT_ID, "ClientTransactionID": self._next_ctid()}
        accept = f"{imagebytes.MEDIA_TYPE}, application/json" if want_bytes else "application/json"
        what = f"{self.label} GET ImageArray"
        try:
            async with self._http.stream(
                "GET", "imagearray", params=args, headers={"Accept": accept}, timeout=timeout
            ) as r:
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                body = await _read_body(r)
        except httpx.TimeoutException as exc:
            raise DeviceTimeout(f"{what}: no complete answer within {timeout} s") from exc
        except httpx.TransportError as exc:
            raise AlpacaTransportError(f"{what}: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            cls = _BY_STATUS.get(r.status_code, AlpacaHTTPError)
            text = bytes(body[:200]).decode("utf-8", "replace").strip()
            raise cls(f"{what}: HTTP {r.status_code}: {text}", r.status_code)
        try:
            if ctype == imagebytes.MEDIA_TYPE:
                return imagebytes.parse_imagebytes(body)
            return imagebytes.parse_json(bytes(body))
        except imagebytes.ImageDeviceError as exc:
            cls = error_class(exc.number)
            raise cls(
                f"{what}: {cls.__name__} (0x{exc.number:X}): {exc.message}", exc.number
            ) from None
        except imagebytes.ImageFormatError as exc:
            raise AlpacaProtocolError(f"{what}: {exc}") from exc

    async def put(self, member: str, *, timeout: float, **params: Any) -> Any:
        """Write a property or call a method; ``params`` names are sent as given."""
        return await self._request("PUT", member, params, timeout)

    # -- what DeviceOwner calls ------------------------------------------------

    async def set(self, member: str, value: Any, *, timeout: float) -> None:
        """Set a property: the PUT parameter is named after the property."""
        await self.put(member, timeout=timeout, **{member: value})

    async def call(self, member: str, *args: Any, timeout: float) -> Any:
        """Call a method listed in :data:`METHODS` with positional arguments."""
        try:
            verb, names = METHODS[member]
        except KeyError:
            raise ValueError(f"{member} is not in METHODS; use get()/put()") from None
        if len(args) != len(names):
            raise TypeError(f"{member} takes {names}, got {len(args)} arguments")
        return await self._request(verb, member, dict(zip(names, args, strict=True)), timeout)

    async def connect(self, *, timeout: float) -> None:
        """Make sure the device is connected (``Connected`` = True)."""
        if not await self.get("Connected", timeout=timeout):
            await self.set("Connected", True, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()


async def _read_body(r: httpx.Response) -> bytearray | bytes:
    """The whole body, into one preallocated buffer when its length is known
    (an image is read once, not accumulated and then joined)."""
    try:
        length = int(r.headers.get("content-length", ""))
    except ValueError:
        length = -1
    if length < 0 or r.headers.get("content-encoding"):
        return b"".join([chunk async for chunk in r.aiter_bytes()])
    buf = bytearray(length)
    view = memoryview(buf)
    n = 0
    async for chunk in r.aiter_bytes():
        if n + len(chunk) > length:
            raise AlpacaProtocolError(f"body longer than its Content-Length {length}")
        view[n : n + len(chunk)] = chunk
        n += len(chunk)
    if n != length:
        raise AlpacaProtocolError(f"body of {n} bytes, Content-Length {length}")
    return buf
