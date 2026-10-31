"""The console's images (docs/console.md, "Images"): the guider's ``GET
/frame``, the scaling and PNG encoding, and the console's image, pixel and
FITS endpoints for guider, imager and spectrograph frames."""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest
from astropy.io import fits

from robofast_core.frameview import (
    bin_to,
    decode_array,
    encode_array,
    png_gray8,
    render,
    scale_limits,
    to_8bit,
    zscale,
)
from robofast_core.guider.service import GuiderRemote, GuiderService

from . import test_console as _console
from .test_guider_loop import _service_config

make_console = _console.make_console


def unpng(data: bytes) -> np.ndarray:
    """Decode our own 8-bit grayscale PNG (filter Sub or None)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", body[:10])
            assert (depth, color) == (8, 0)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, w + 1)
    out = np.zeros((h, w), np.uint8)
    for r in range(h):
        f, line = rows[r, 0], rows[r, 1:]
        if f == 0:
            out[r] = line
        else:
            assert f == 1
            out[r] = np.cumsum(line.astype(np.int64)) % 256
    return out


def sky(h: int = 200, w: int = 300, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    data = rng.normal(1000.0, 10.0, (h, w))
    yy, xx = np.mgrid[:h, :w]
    data += 20000.0 * np.exp(-((xx - 120.0) ** 2 + (yy - 80.0) ** 2) / (2 * 2.0**2))
    return np.round(data)


# -- frameview ---------------------------------------------------------------------------


def test_png_is_lossless_and_scaling_is_sane():
    img = (np.arange(37 * 53) % 256).astype(np.uint8).reshape(37, 53)
    assert np.array_equal(unpng(png_gray8(img)), img)
    data = sky()
    lo, hi = zscale(data)
    assert 950 < lo < 1000 < hi < 1100  # the sky, not the star
    lo2, hi2 = scale_limits(data, "minmax")
    assert lo2 < 1000 and hi2 > 20000
    assert scale_limits(data, "99.5")[1] < hi2
    eight = to_8bit(data, lo, hi, "asinh")
    assert eight.dtype == np.uint8 and eight[80, 120] == 255
    b, f = bin_to(data, 100)
    assert f == 3 and b.shape == (66, 100)
    png, view = render(data, region=(100, 60, 140, 100), max_side=512)
    assert view["region"] == [100, 60, 140, 100] and view["size"] == [40, 40]
    assert unpng(png).shape == (40, 40)


def test_array_transport_round_trips():
    ints = sky(20, 30)
    doc = encode_array(ints)
    assert doc["dtype"] == "uint16"
    assert np.array_equal(decode_array(doc), ints)
    floats = ints / 3.0
    floats[0, 0] = np.nan
    back = decode_array(encode_array(floats))
    assert np.isnan(back[0, 0]) and np.allclose(back[1:], floats[1:], rtol=1e-6)
    with pytest.raises(ValueError):
        decode_array({**doc, "shape": [21, 30]})
    with pytest.raises(ValueError):
        decode_array({**doc, "shape": [100000, 100000]})


# -- the guider's frame endpoint, and the console reading it ------------------------------------


async def test_the_guider_serves_its_last_frame_and_the_console_shows_it(tmp_path, make_console):
    svc = await GuiderService(_service_config(tmp_path / "g", calibrations={})).start()
    remote = GuiderRemote(svc.url, scope="t2", acquire_timeout=20, poll_interval=0.2)
    rig = await make_console(http=httpx.AsyncClient(trust_env=False))
    rig.app.enclosures["aqawan1"].guiders["t1"] = svc.url
    try:
        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=30) as http:
            assert (await http.get("/frame")).status_code == 404
            assert (await http.post("/locate", json={})).status_code == 200
            doc = (await http.get("/frame?max=64")).json()
            assert doc["roi"] == {"x": 200, "y": 160, "width": 240, "height": 160}
            assert doc["binning"] == 4 and doc["shape"] == [40, 60]
            assert doc["target"] == pytest.approx([321.5, 239.5], abs=0.1)
            # The console: the acquisition frame, the fiber where it is.
            await rig.login("watcher")
            srcs = (await rig.client.get("/console/api/images")).json()["sources"]["aqawan1"]
            assert {
                "id": "guider-t1",
                "kind": "guider",
                "telescope": "t1",
                "label": "t1 guider",
            } in srcs
            r = await rig.client.get("/console/api/image/aqawan1/guider-t1?max=512")
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            meta = json.loads(r.headers["x-frame"])
            assert meta["binning"] == 1 and meta["origin"] == [200, 160]
            t = meta["overlays"]["target"]
            assert (t["x"], t["y"]) == pytest.approx((121.5, 79.5), abs=0.1)
            assert unpng(r.content).shape == (160, 240)

            # Guiding: the 48-pixel window, at a few Hz; how many bytes each costs.
            for device, step, extra in (("tiptilt", 80, {}), ("mount", 5, {"settle": 0.3})):
                r = await http.post(
                    "/calibrate",
                    json={"device": device, "step": step, "exposure": 0.02, "frames": 5, **extra},
                )
                assert r.status_code == 200, r.text
            await remote.start(dec=20.0)
            await asyncio.sleep(0.5)
            sizes = []
            etag = None
            for _ in range(4):
                await asyncio.sleep(0.45)
                r = await rig.client.get(
                    "/console/api/image/aqawan1/guider-t1?max=128",
                    headers={"If-None-Match": etag} if etag else {},
                )
                assert r.status_code == 200, r.text
                etag = r.headers["etag"]
                meta = json.loads(r.headers["x-frame"])
                sizes.append(len(r.content))
            assert meta["state"] == "guiding" and meta["shape"] == [48, 48]
            assert "star" in meta["overlays"] and "window" in meta["overlays"]
            # Each thumbnail is a few kB: at 2 Hz well under 10 kB/s per guider.
            assert max(sizes) < 4000, sizes
            print(f"guider thumbnail PNG bytes: {sizes}")
            # Same frame, same view: 304, no pixels.
            r1 = await rig.client.get("/console/api/image/aqawan1/guider-t1?max=128")
            r2 = await rig.client.get(
                "/console/api/image/aqawan1/guider-t1?max=128",
                headers={"If-None-Match": r1.headers["etag"]},
            )
            assert r2.status_code in (200, 304)
            if r2.status_code == 304:
                assert r2.content == b""
            # A pixel, and the frame as FITS.
            r = await rig.client.get("/console/api/pixel/aqawan1/guider-t1?x=24&y=24")
            assert r.status_code == 200 and r.json()["value"] is not None
            r = await rig.client.get("/console/api/fits/aqawan1/guider-t1")
            assert r.status_code == 200 and r.content[:6] == b"SIMPLE"
            assert "attachment" in r.headers["content-disposition"]
    finally:
        await remote.stop("teardown")
        await svc.stop()


# -- saved frames: the imager and the spectrograph ----------------------------------------------


def write_frames(root: Path) -> tuple[Path, Path]:
    night = root / "data" / "minerva" / "t1" / "n20261109"
    night.mkdir(parents=True)
    f1 = night / "n20261109.t1.M13.0001.fits"
    hdu = fits.PrimaryHDU(sky().astype(np.uint16))
    hdu.header["OBJECT"] = "M13"
    hdu.header["EXPTIME"] = 30.0
    hdu.writeto(f1)
    index = night / "n20261109.t1.frames.jsonl"
    index.write_text(json.dumps({"file": "minerva/t1/n20261109/n20261109.t1.M13.0001.fits"}) + "\n")
    spec = root / "kiwispec" / "n20261109"
    spec.mkdir(parents=True)
    f2 = spec / "n20261109.HD1.0003.fits"
    fits.PrimaryHDU(sky(100, 400, 2).astype(np.float32)).writeto(f2)
    return f1, f2


async def test_saved_frames_pixels_and_fits(make_console, tmp_path):
    rig = await make_console()
    f1, f2 = write_frames(tmp_path)
    enc = rig.app.enclosures["aqawan1"]
    enc.images_root, enc.images_site = tmp_path / "data", "minerva"
    enc.spectrograph_frames = tmp_path / "kiwispec"
    assert (await rig.client.get("/console/api/image/aqawan1/imager-t1")).status_code == 401
    await rig.login("watcher")
    ids = [s["id"] for s in rig.app.images.sources()["aqawan1"]]
    assert ids == ["guider-t1", "imager-t1", "imager-t2", "spectrograph"]
    r = await rig.client.get("/console/api/image/aqawan1/imager-t1?max=100&stretch=asinh")
    assert r.status_code == 200
    meta = json.loads(r.headers["x-frame"])
    assert meta["file"] == f1.name and meta["object"] == "M13" and meta["exposure"] == 30.0
    assert meta["view_binning"] == 3 and meta["shape"] == [300, 200]
    # Zoomed on the star: the same limits (from the whole frame), full resolution.
    r = await rig.client.get("/console/api/image/aqawan1/imager-t1?x0=100&y0=60&x1=140&y1=100")
    meta2 = json.loads(r.headers["x-frame"])
    assert meta2["region"] == [100, 60, 140, 100] and meta2["view_binning"] == 1
    assert meta2["limits"] == meta["limits"]
    r = await rig.client.get("/console/api/pixel/aqawan1/imager-t1?x=120&y=80")
    assert r.json()["value"] > 15000 and r.json()["detector"] == [120, 80]
    r = await rig.client.get("/console/api/fits/aqawan1/imager-t1")
    assert r.content == f1.read_bytes()
    r = await rig.client.get("/console/api/image/aqawan1/spectrograph?max=200")
    assert json.loads(r.headers["x-frame"])["file"] == f2.name
    # Refusals.
    for path, code in (
        ("/console/api/image/aqawan1/imager-t9", 404),
        ("/console/api/image/nowhere/imager-t1", 404),
        ("/console/api/image/aqawan1/imager-t2", 404),  # no frames yet
        ("/console/api/image/aqawan1/imager-t1?scale=bogus", 400),
        ("/console/api/image/aqawan1/guider-t1", 404),  # the fake guider has no /frame
        ("/console/api/pixel/aqawan1/imager-t1?x=abc&y=1", 400),
    ):
        r = await rig.client.get(path)
        assert r.status_code == code, (path, r.status_code, r.text)
    # Images have their own rate limit, apart from the page's.
    from robofast_core.api_auth import RateLimiter

    rig.app.image_limiter = RateLimiter(0.0, 0.0)
    assert (await rig.client.get("/console/api/image/aqawan1/imager-t1")).status_code == 429
    assert (await rig.client.get("/console/api/status")).status_code == 200


def test_the_page_has_the_viewer():
    from robofast_core.console.page import render_page

    page = render_page("n")
    for needle in ('id="img-canvas"', 'id="guider-strip"', "/console/api/image/", "If-None-Match"):
        assert needle in page
