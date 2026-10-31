"""The ImageBytes parser, the JSON image parser, and the client's image download."""

from __future__ import annotations

import json
import struct

import httpx
import numpy as np
import pytest

from robofast_core import imagebytes as ib
from robofast_core.alpaca_client import (
    AlpacaDevice,
    AlpacaProtocolError,
    AlpacaServerError,
    InvalidOperationException,
)
from robofast_core.imaging import download_timeout
from robofast_core.owner import DeviceTimeout


def header(**kw) -> bytes:
    f = dict(version=1, error=0, ctid=3, stid=4, start=44, img=2, tx=2, rank=2, d1=3, d2=2, d3=0)
    f.update(kw)
    return struct.pack("<iiIIiiiiiii", *f.values())


# -- ImageBytes --------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(ib.ELEMENT_TYPES))
@pytest.mark.parametrize("shape", [(5, 3), (4, 3, 3)])
def test_every_element_type_and_rank_round_trips(code, shape):
    dtype = ib.ELEMENT_TYPES[code]
    rng = np.random.default_rng(code)
    if dtype.kind == "f":
        data = rng.normal(size=shape).astype(dtype)
    else:
        info = np.iinfo(dtype)
        lo, hi = max(info.min, -(2**40)), min(info.max, 2**40)
        data = rng.integers(lo, hi, size=shape, endpoint=True).astype(dtype)
    payload = ib.encode_imagebytes(data, ctid=7, stid=9)
    image = ib.parse_imagebytes(payload)
    assert image.data.dtype == dtype and image.data.shape == shape
    np.testing.assert_array_equal(image.data, data)
    assert image.element_type == code and image.rank == len(shape)
    assert image.transfer == "imagebytes" and image.nbytes_wire == len(payload)


def test_layout_matches_the_spec_byte_for_byte():
    """Header fields at their offsets; pixels with the last index fastest."""
    data = np.arange(6, dtype="<i4").reshape(3, 2)  # [x][y]
    payload = ib.encode_imagebytes(data, ctid=11, stid=12)
    fields = struct.unpack_from("<11i", payload)
    assert fields == (1, 0, 11, 12, 44, 2, 2, 2, 3, 2, 0)
    assert payload[44:] == struct.pack("<6i", 0, 1, 2, 3, 4, 5)
    # Hand-built from the spec, not with our encoder:
    raw = header(d1=3, d2=2) + struct.pack("<6i", 0, 1, 2, 3, 4, 5)
    image = ib.parse_imagebytes(raw)
    assert image.data[2, 1] == 5 and image.data[1, 0] == 2


def test_narrower_transmission_type_keeps_the_values_and_the_camera_type():
    """OmniSim sends its Int32 images as Byte when every pixel fits."""
    data = np.array([[1, 2], [3, 255]], dtype="u1")
    image = ib.parse_imagebytes(ib.encode_imagebytes(data, element_type=2))
    assert image.element_type == 2 and image.data.dtype == np.uint8
    np.testing.assert_array_equal(image.data, data)


def test_parse_is_zero_copy():
    payload = bytearray(ib.encode_imagebytes(np.zeros((4, 4), "<u2")))
    image = ib.parse_imagebytes(payload)
    payload[44] = 7
    assert image.data[0, 0] == 7


def test_device_error_payload():
    payload = ib.encode_imagebytes(np.zeros(1), error=0x40B, message="no image available")
    with pytest.raises(ib.ImageDeviceError) as e:
        ib.parse_imagebytes(payload)
    assert e.value.number == 0x40B and e.value.message == "no image available"


@pytest.mark.parametrize(
    "payload, match",
    [
        (b"", "shorter than its header"),
        (header()[:43], "shorter than its header"),
        (header(version=2) + bytes(24), "metadata version 2"),
        (header(start=40) + bytes(24), "DataStart 40"),
        (header(start=99) + bytes(24), "DataStart 99"),
        (header(tx=0) + bytes(24), "transmission element type 0"),
        (header(tx=10) + bytes(24), "transmission element type 10"),
        (header(tx=11) + bytes(24), "transmission element type 11"),
        (header(img=42) + bytes(24), "image element type 42"),
        (header(rank=1) + bytes(24), "rank 1"),
        (header(rank=4) + bytes(24), "rank 4"),
        (header(d1=0) + bytes(0), "not positive"),
        (header(d2=-1) + bytes(24), "not positive"),
        (header(rank=3, d3=0) + bytes(24), "not positive"),
        (header() + bytes(23), "needs 24 data bytes, got 23"),
        (header() + bytes(25), "needs 24 data bytes, got 25"),
    ],
)
def test_malformed_payloads_are_rejected(payload, match):
    with pytest.raises(ib.ImageFormatError, match=match):
        ib.parse_imagebytes(payload)


def test_fits_order():
    data = np.zeros((5, 3), "u2")  # NumX=5, NumY=3
    data[4, 0] = 1
    out = ib.fits_order(data)
    assert out.shape == (3, 5) and out[0, 4] == 1
    cube = np.zeros((5, 3, 2), "u2")
    cube[4, 1, 0] = 1
    assert ib.fits_order(cube).shape == (2, 3, 5) and ib.fits_order(cube)[0, 1, 4] == 1


# -- JSON ---------------------------------------------------------------------------


def json_body(value, etype=2, rank=None, error=0, message="", pad=False) -> bytes:
    rank = rank if rank is not None else np.asarray(value).ndim
    doc = {
        "Type": etype,
        "Rank": rank,
        "Value": value,
        "ClientTransactionID": 1,
        "ServerTransactionID": 2,
        "ErrorNumber": error,
        "ErrorMessage": message,
    }
    return json.dumps(doc, separators=(",", ":") if not pad else (", ", ": ")).encode()


@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("shape, etype", [((7, 5), 2), ((6, 4, 3), 2), ((7, 5), 3), ((7, 5), 8)])
def test_json_images_both_parsers(monkeypatch, big, shape, etype):
    if big:  # force the fast path on a small image
        monkeypatch.setattr(ib, "FAST_JSON_MIN", 0)
    rng = np.random.default_rng(1)
    dtype = ib.ELEMENT_TYPES[etype]
    data = rng.normal(size=shape) if dtype.kind == "f" else rng.integers(0, 60000, shape)
    image = ib.parse_json(json_body(data.tolist(), etype))
    assert image.data.shape == shape and image.data.dtype == dtype
    assert image.transfer == "json"
    np.testing.assert_array_equal(image.data, data.astype(dtype))


def test_json_fast_path_falls_back_on_whitespace(monkeypatch):
    monkeypatch.setattr(ib, "FAST_JSON_MIN", 0)
    image = ib.parse_json(json_body([[1, 2], [3, 4], [5, 6]], pad=True))
    assert image.data.shape == (3, 2) and image.data[2, 1] == 6


@pytest.mark.parametrize("big", [False, True])
def test_json_error_and_malformed(monkeypatch, big):
    if big:
        monkeypatch.setattr(ib, "FAST_JSON_MIN", 0)
    with pytest.raises(ib.ImageDeviceError, match="no image"):
        ib.parse_json(json_body([], error=0x40B, message="no image"))
    with pytest.raises(ib.ImageFormatError):
        ib.parse_json(b"not json")
    with pytest.raises(ib.ImageFormatError):
        ib.parse_json(json_body([[1, 2], [3]], rank=2))
    with pytest.raises(ib.ImageFormatError):
        ib.parse_json(json_body([1, 2, 3], rank=1))
    with pytest.raises(ib.ImageFormatError, match="outside"):
        ib.parse_json(json_body([[1, 70000]], etype=8))


# -- the client ---------------------------------------------------------------------


def camera(handler) -> AlpacaDevice:
    return AlpacaDevice("cam:1", "camera", transport=httpx.MockTransport(handler))


async def test_client_asks_for_imagebytes_and_parses_them():
    data = np.arange(12, dtype="<u2").reshape(4, 3)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers["accept"]
        seen["path"] = request.url.path
        return httpx.Response(
            200, content=ib.encode_imagebytes(data), headers={"content-type": ib.MEDIA_TYPE}
        )

    dev = camera(handler)
    image = await dev.get("ImageArray", timeout=5)
    assert seen["path"] == "/api/v1/camera/0/imagearray"
    assert seen["accept"].startswith("application/imagebytes")
    np.testing.assert_array_equal(image.data, data)
    await dev.aclose()


async def test_client_falls_back_to_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Type": 2,
                "Rank": 2,
                "Value": [[1, 2], [3, 4]],
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    dev = camera(handler)
    image = await dev.image_array(timeout=5)
    assert image.transfer == "json" and image.data.tolist() == [[1, 2], [3, 4]]
    await dev.aclose()


async def test_client_json_only_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json={"Type": 2, "Rank": 2, "Value": [[1]], "ErrorNumber": 0})

    dev = AlpacaDevice("cam:1", "camera", transport=httpx.MockTransport(handler), imagebytes=False)
    assert (await dev.get("ImageArray", timeout=5)).data.shape == (1, 1)
    await dev.aclose()


@pytest.mark.parametrize("binary", [True, False])
async def test_client_maps_device_errors(binary):
    def handler(request: httpx.Request) -> httpx.Response:
        if binary:
            body = ib.encode_imagebytes(np.zeros(1), error=0x40B, message="no image")
            return httpx.Response(200, content=body, headers={"content-type": ib.MEDIA_TYPE})
        return httpx.Response(200, json={"ErrorNumber": 0x40B, "ErrorMessage": "no image"})

    dev = camera(handler)
    with pytest.raises(InvalidOperationException, match="no image"):
        await dev.image_array(timeout=5)
    await dev.aclose()


async def test_client_malformed_and_http_errors():
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=header() + bytes(3), headers={"content-type": ib.MEDIA_TYPE}
        )

    dev = camera(bad)
    with pytest.raises(AlpacaProtocolError, match="needs 24 data bytes"):
        await dev.image_array(timeout=5)
    await dev.aclose()

    dev = camera(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(AlpacaServerError, match="boom"):
        await dev.image_array(timeout=5)
    await dev.aclose()


async def test_client_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    dev = camera(handler)
    with pytest.raises(DeviceTimeout):
        await dev.image_array(timeout=0.1)
    await dev.aclose()


def test_download_timeout_scales_with_the_image():
    small = download_timeout(800 * 600)
    big = download_timeout(9600 * 6422)
    assert 30 < small < big
    assert download_timeout(10**6, "json") > download_timeout(10**6, "imagebytes")
