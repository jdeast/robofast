"""Camera images over Alpaca: the ImageBytes binary format and the JSON form.

``GET /api/v1/camera/{n}/imagearray`` returns the last image. Asked with
``Accept: application/imagebytes``, a server that supports it answers with
``Content-Type: application/imagebytes`` and a binary body; otherwise (or for
an error, which OmniSim 0.4 always sends as JSON) it answers JSON.

The ImageBytes body (ASCOM Alpaca, ``ArrayMetadataV1``; checked against the
ASCOM library reference for ``ArrayMetadataV1`` and
``ImageArrayElementTypes``, and against OmniSim 0.4)::

    offset  field                    type
         0  MetadataVersion          int32   1
         4  ErrorNumber              int32   0: success
         8  ClientTransactionID      uint32
        12  ServerTransactionID      uint32
        16  DataStart                int32   offset of the data (44 for v1)
        20  ImageElementType         int32   the camera's element type
        24  TransmissionElementType  int32   the type on the wire (may be smaller)
        28  Rank                     int32   2, or 3 (colour planes)
        32  Dimension1               int32   NumX
        36  Dimension2               int32   NumY
        40  Dimension3               int32   planes; 0 for rank 2

all little-endian. From ``DataStart`` on: the pixels, ``Dimension1 x
Dimension2 [x Dimension3]`` elements of ``TransmissionElementType``,
little-endian, in .NET array order (the *last* index varies fastest), i.e.
``ImageArray[x, y]`` with ``y`` fastest. If ``ErrorNumber`` is not zero, the
bytes from ``DataStart`` are a UTF-8 error message instead.

The transmission type may be narrower than the camera's type (OmniSim sends
its Int32 images as Byte when every pixel fits); the values are the same,
so the array is kept in the transmission type (no widening copy) and the
FITS writer picks the storage type.

The JSON form: ``{"Type": <element type>, "Rank": 2|3, "Value": [[...]], ...}``
with ``Value[x][y]`` (``[x][y][plane]``). For a large image a generic JSON
parse makes a Python object per pixel (tens of bytes each); :func:`parse_json`
reads the numbers straight into a numpy array instead, and falls back to
``json.loads`` only for small or unusual bodies.

:func:`fits_order` turns either into FITS/numpy order: ``(NumY, NumX)``, or
``(planes, NumY, NumX)``.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass

import numpy as np

#: ASCOM ``ImageArrayElementTypes`` -> numpy dtype (little-endian).
ELEMENT_TYPES: dict[int, np.dtype] = {
    1: np.dtype("<i2"),  # Int16
    2: np.dtype("<i4"),  # Int32
    3: np.dtype("<f8"),  # Double
    4: np.dtype("<f4"),  # Single
    5: np.dtype("<u8"),  # UInt64
    6: np.dtype("u1"),  # Byte
    7: np.dtype("<i8"),  # Int64
    8: np.dtype("<u2"),  # UInt16
    9: np.dtype("<u4"),  # UInt32
}
ELEMENT_NAMES = {
    0: "Unknown",
    1: "Int16",
    2: "Int32",
    3: "Double",
    4: "Single",
    5: "UInt64",
    6: "Byte",
    7: "Int64",
    8: "UInt16",
    9: "UInt32",
    10: "Object",
}
_HEADER = struct.Struct("<iiIIiiiiiii")
HEADER_SIZE = _HEADER.size  # 44
METADATA_VERSION = 1
MEDIA_TYPE = "application/imagebytes"


class ImageFormatError(ValueError):
    """A malformed ImageBytes or JSON image."""


class ImageDeviceError(Exception):
    """The image response carried a non-zero ASCOM ``ErrorNumber``."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"ImageArray error 0x{number:X}: {message}")
        self.number = number
        self.message = message


@dataclass
class ImageArray:
    """An image as the camera returned it.

    ``data`` has the ASCOM shape ``(NumX, NumY)`` or ``(NumX, NumY,
    planes)``; ``element_type`` is the camera's ``ImageElementType`` and
    ``data.dtype`` the type it was transmitted in. ``transfer`` is
    ``"imagebytes"`` or ``"json"``; ``nbytes_wire`` the body size.
    """

    data: np.ndarray
    element_type: int
    transfer: str = "imagebytes"
    nbytes_wire: int = 0

    @property
    def rank(self) -> int:
        return self.data.ndim


def parse_imagebytes(payload: bytes | bytearray | memoryview) -> ImageArray:
    """Parse an ImageBytes body (module doc). Zero-copy: ``data`` is a view
    of ``payload``. Raises :class:`ImageDeviceError` for a device error and
    :class:`ImageFormatError` for anything malformed."""
    buf = memoryview(payload)
    if len(buf) < HEADER_SIZE:
        raise ImageFormatError(f"ImageBytes body of {len(buf)} bytes: shorter than its header")
    (version, error, _ctid, _stid, start, img_type, tx_type, rank, d1, d2, d3) = (
        _HEADER.unpack_from(buf)
    )
    if version != METADATA_VERSION:
        raise ImageFormatError(f"ImageBytes metadata version {version}, expected 1")
    if not HEADER_SIZE <= start <= len(buf):
        raise ImageFormatError(f"ImageBytes DataStart {start} outside 44..{len(buf)}")
    if error != 0:
        message = bytes(buf[start:]).decode("utf-8", errors="replace")
        raise ImageDeviceError(error, message)
    dtype = ELEMENT_TYPES.get(tx_type)
    if dtype is None:
        raise ImageFormatError(
            f"ImageBytes transmission element type {tx_type} "
            f"({ELEMENT_NAMES.get(tx_type, 'invalid')}) not supported"
        )
    if img_type not in ELEMENT_TYPES and img_type != 0:
        raise ImageFormatError(f"ImageBytes image element type {img_type} invalid")
    if rank == 2:
        shape: tuple[int, ...] = (d1, d2)
    elif rank == 3:
        shape = (d1, d2, d3)
    else:
        raise ImageFormatError(f"ImageBytes rank {rank}: expected 2 or 3")
    if any(n <= 0 for n in shape):
        raise ImageFormatError(f"ImageBytes dimensions {shape} not positive")
    count = int(np.prod(shape, dtype=np.int64))
    have = len(buf) - start
    if have != count * dtype.itemsize:
        raise ImageFormatError(
            f"ImageBytes {shape} of {ELEMENT_NAMES[tx_type]} needs "
            f"{count * dtype.itemsize} data bytes, got {have}"
        )
    data = np.frombuffer(buf, dtype=dtype, count=count, offset=start).reshape(shape)
    return ImageArray(data, img_type or tx_type, "imagebytes", len(buf))


def encode_imagebytes(
    data: np.ndarray,
    *,
    element_type: int | None = None,
    transmission_type: int | None = None,
    error: int = 0,
    message: str = "",
    ctid: int = 0,
    stid: int = 0,
) -> bytes:
    """An ImageBytes body for ``data`` (ASCOM shape); for servers and tests."""
    if error:
        body = message.encode()
        return _HEADER.pack(1, error, ctid, stid, HEADER_SIZE, 0, 0, 0, 0, 0, 0) + body
    by_dtype = {v: k for k, v in ELEMENT_TYPES.items()}
    if transmission_type is None:
        transmission_type = by_dtype[np.dtype(data.dtype).newbyteorder("<")]
    if element_type is None:
        element_type = transmission_type
    dims = (*data.shape, 0)[:3]
    head = _HEADER.pack(
        1, 0, ctid, stid, HEADER_SIZE, element_type, transmission_type, data.ndim, *dims
    )
    wire = np.ascontiguousarray(data, dtype=ELEMENT_TYPES[transmission_type])
    return head + wire.tobytes()


# -- JSON ---------------------------------------------------------------------

_VALUE = re.compile(rb'"Value"\s*:\s*\[')
_RANK = re.compile(rb'"Rank"\s*:\s*(\d+)')
_TYPE = re.compile(rb'"Type"\s*:\s*(\d+)')
_ERROR = re.compile(rb'"ErrorNumber"\s*:\s*(-?\d+)')
_MESSAGE = re.compile(rb'"ErrorMessage"\s*:\s*"((?:[^"\\]|\\.)*)"')
#: Below this, ``json.loads`` is cheap enough and the most robust.
FAST_JSON_MIN = 1 << 20


def parse_json(body: bytes) -> ImageArray:
    """Parse a JSON ``imagearray`` response (module doc)."""
    if len(body) >= FAST_JSON_MIN:
        image = _parse_json_fast(body)
        if image is not None:
            return image
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ImageFormatError(f"imagearray: not JSON: {body[:200]!r}") from exc
    if not isinstance(doc, dict):
        raise ImageFormatError("imagearray: JSON is not an object")
    error = int(doc.get("ErrorNumber", 0) or 0)
    if error:
        raise ImageDeviceError(error, str(doc.get("ErrorMessage", "")))
    value, rank = doc.get("Value"), doc.get("Rank")
    etype = int(doc.get("Type", 2) or 2)
    if not isinstance(value, list) or not value:
        raise ImageFormatError("imagearray: no Value array")
    dtype = ELEMENT_TYPES.get(etype, np.dtype("<f8"))
    try:
        data = np.array(value, dtype=dtype if dtype.kind == "f" else np.int64)
    except (ValueError, TypeError) as exc:
        raise ImageFormatError(f"imagearray: ragged or non-numeric Value: {exc}") from exc
    if rank is not None and data.ndim != int(rank):
        raise ImageFormatError(f"imagearray: Rank {rank} but Value has {data.ndim} dimensions")
    if data.ndim not in (2, 3) or 0 in data.shape:
        raise ImageFormatError(f"imagearray: Value shape {data.shape}")
    return ImageArray(_narrow(data, dtype), etype, "json", len(body))


def _narrow(data: np.ndarray, dtype: np.dtype) -> np.ndarray:
    if data.dtype == dtype:
        return data
    if dtype.kind in "iu":
        info = np.iinfo(dtype)
        if data.size and (data.min() < info.min or data.max() > info.max):
            raise ImageFormatError(f"imagearray: values outside {dtype}")
    return data.astype(dtype)


def _parse_json_fast(body: bytes) -> ImageArray | None:
    """Read ``Value`` straight into numpy; None if the body is not the plain
    shape this expects (the caller then uses ``json.loads``)."""
    error = _ERROR.search(body)
    if error and int(error.group(1)):
        m = _MESSAGE.search(body)
        msg = json.loads(b'"' + m.group(1) + b'"') if m else ""
        raise ImageDeviceError(int(error.group(1)), msg)
    vm, rm = _VALUE.search(body), _RANK.search(body)
    if vm is None or rm is None:
        return None
    rank = int(rm.group(1))
    if rank not in (2, 3):
        return None
    tm = _TYPE.search(body)
    etype = int(tm.group(1)) if tm else 2
    dtype = ELEMENT_TYPES.get(etype)
    if dtype is None:
        return None
    start = vm.end() - 1
    end = body.find(b"]" * rank, start)
    if end < 0:
        return None
    text = body[start : end + rank]
    if any(c in text for c in (b" ", b"\n", b"\t", b"\r", b'"')):
        return None
    if rank == 2:
        d1 = text.count(b"],[") + 1
        sizes = [d1]
    else:
        d1 = text.count(b"]],[[") + 1
        rows = text.count(b"],[") + 1
        if rows % d1:
            return None
        sizes = [d1, rows // d1]
    flat = np.fromstring(  # text mode (sep=","): not the deprecated binary mode
        text.translate(None, b"[]").decode("ascii"),
        dtype=np.float64 if dtype.kind == "f" else np.int64,
        sep=",",
    )
    per = int(np.prod(sizes))
    if flat.size == 0 or flat.size % per:
        return None
    shape = (*sizes, flat.size // per)
    try:
        data = _narrow(flat, dtype).reshape(shape)
    except ImageFormatError:
        return None
    return ImageArray(data, etype, "json", len(body))


def fits_order(data: np.ndarray) -> np.ndarray:
    """ASCOM ``[x, y(, plane)]`` -> FITS/numpy ``[y, x]`` (``[plane, y,
    x]``), as a view."""
    if data.ndim == 2:
        return data.T
    if data.ndim == 3:
        return data.transpose(2, 1, 0)
    raise ValueError(f"image rank {data.ndim}")


def wire_bytes_per_pixel(transfer: str) -> int:
    """A generous bound on the body size per pixel, for download timeouts:
    ImageBytes sends at most 8 bytes (Int64/Double, rarely more than 4);
    JSON up to ~12 characters for a 32-bit value and its comma."""
    return 8 if transfer == "imagebytes" else 12
