"""The value registry, the FITS header builder and the frame writer."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits

from robofast_core import fits_writer as fw
from robofast_core.fits_writer import FrameInfo, FrameStore, Optics
from robofast_core.imagebytes import ImageArray
from robofast_core.registry import Snapshot, ValueRegistry, publish_static, sun_and_moon

T0 = datetime(2026, 11, 14, 5, 6, 7, 891234, tzinfo=UTC)
SITE = {"latitude": 31.68, "longitude": -110.88, "elevation": 2316.0}


def registry(now: datetime = T0) -> ValueRegistry:
    r = ValueRegistry()
    publish_static(r, site_name="minerva", site=SITE, version="v1.2-3-gabc")
    r.publish("t1.tel.ra", 5.5, max_age=60, measured=now, unit="h")
    r.publish("t1.tel.dec", 22.0, max_age=60, measured=now, unit="deg")
    r.publish("t1.tel.eqsys", 2, max_age=None, measured=now)
    r.publish(
        "t1.tel.alt", 45.0, max_age=60, measured=now, unit="deg", keyword="ALT", comment="Altitude"
    )
    r.publish("t1.tel.lst", 6.0, max_age=60, measured=now, unit="h", keyword="LST", comment="LST")
    r.publish("t1.camera.last_start", "2026-11-14T05:06:08.1234567", max_age=60, measured=now)
    r.publish("t1.camera.last_duration", 30.0, max_age=60, measured=now)
    r.publish(
        "t1.camera.ccd_temp",
        -20.1,
        max_age=60,
        measured=now,
        unit="C",
        keyword="CCD-TEMP",
        comment="CCD temperature",
    )
    r.publish("t1.camera.binx", 2, max_age=60, measured=now, keyword="XBINNING")
    r.publish("t1.camera.xsize", 64, max_age=None, measured=now)
    r.publish("t1.camera.ysize", 48, max_age=None, measured=now)
    # Weather: fresh humidity, stale temperature, a failed sky temperature.
    r.publish(
        "weather.humidity",
        41.0,
        max_age=300,
        measured=now - timedelta(seconds=10),
        unit="%",
        keyword="HUMIDITY",
        comment="Relative humidity, %",
    )
    r.publish(
        "weather.temperature",
        12.5,
        max_age=300,
        measured=now - timedelta(seconds=900),
        unit="C",
        keyword="AMBTEMP",
        comment="Ambient temperature, C",
    )
    r.publish(
        "weather.skytemperature",
        -30.0,
        max_age=300,
        measured=now,
        unit="C",
        keyword="SKYTEMP",
        comment="Sky temperature, C",
    )
    r.failed("weather.skytemperature", "TimeoutError")
    # Another telescope's value never reaches t1's header.
    r.publish("t2.camera.ccd_temp", 5.0, max_age=60, measured=now, keyword="CCD-TEMP")
    return r


def info(r: ValueRegistry, **kw) -> FrameInfo:
    snap = r.snapshot(["t1", "site", "software", "weather", "sky", "enclosure"], now=T0)
    d = dict(
        telescope="t1",
        object="HD 1234",
        obs_name="HD 1234 [c1#0]",
        imagetyp="LIGHT",
        light=True,
        exptime_requested=30.0,
        host_start=T0,
        index=0,
        sequencer="aqawan1-sequencer",
        generation=3,
        ids={"group": "g1", "request": "r1", "configuration": "c1"},
        target=(5.5, 22.0),
        filter="V",
        start=snap,
        end=snap,
        optics=Optics(plate_scale=0.5, position_angle=0.0),
    )
    d.update(kw)
    return FrameInfo(**d)


# -- registry ------------------------------------------------------------------------


def test_registry_staleness_and_scopes():
    r = registry()
    snap = r.snapshot(["t1", "weather"], now=T0)
    assert snap.current("weather.humidity") == 41.0
    assert snap.stale("weather.temperature") and snap.current("weather.temperature") is None
    assert snap.stale("weather.skytemperature")  # its last read failed
    assert snap.get("t2.camera.ccd_temp") is None and snap.get("site.name") is None
    # Never stale: max_age None.
    assert not r.snapshot(now=T0 + timedelta(days=400)).stale("site.latitude")
    # A fresh publish clears a failure.
    r.publish("weather.skytemperature", -31.0, max_age=300, measured=T0)
    assert r.snapshot(now=T0).current("weather.skytemperature") == -31.0
    js = r.as_json(T0)
    assert js["weather.temperature"]["stale"] and js["weather.temperature"]["age"] == 900
    with pytest.raises(ValueError, match="longer than 8"):
        r.publish("x.y", 1, max_age=None, keyword="TOOLONGKEY")


def test_sun_and_moon_values():
    v = sun_and_moon(SITE, T0)
    assert -90 <= v["sun_alt"][0] <= 90 and 0 <= v["moon_illum"][0] <= 1
    assert v["sun_alt"][2] == "SUNALT"


# -- header ------------------------------------------------------------------------


def test_header_times_are_the_cameras_utc_with_milliseconds():
    h, date_obs = fw.build_header(info(registry()), (48, 32))
    assert h["DATE-OBS"] == "2026-11-14T05:06:08.123"
    assert date_obs == datetime(2026, 11, 14, 5, 6, 8, 123456, tzinfo=UTC)
    assert h["DATE-END"] == "2026-11-14T05:06:38.123"
    assert h["TIMESRC"] == "camera" and h["TIMESYS"] == "UTC"
    assert h["EXPTIME"] == 30.0
    assert h["DATE-CMD"] == "2026-11-14T05:06:07.891"
    assert h["CLKSKEW"] == pytest.approx(0.232, abs=1e-3)
    assert h["MJD-OBS"] == pytest.approx(61358.2125940, abs=1e-6)


def test_header_without_a_camera_time_uses_the_host_clock():
    r = registry()
    r.failed("t1.camera.last_start", "NotConnected")
    h, _ = fw.build_header(info(r, end=r.snapshot(now=T0)), (48, 32))
    assert h["DATE-OBS"] == "2026-11-14T05:06:07.891" and h["TIMESRC"] == "host"


def test_parse_camera_time_variants():
    assert fw.parse_camera_time("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert fw.parse_camera_time("2026-01-02T03:04:05.5Z").microsecond == 500000
    assert fw.parse_camera_time("2026-01-02T03:04:05.1234567").microsecond == 123456
    for bad in (None, "", "yesterday", "2026-01-02 03:04:05", "2026-01-02T03:04:05.x"):
        assert fw.parse_camera_time(bad) is None


def test_header_pointing_ids_and_values():
    h, _ = fw.build_header(info(registry()), (48, 32))
    assert h["OBJECT"] == "HD 1234" and h["IMAGETYP"] == "LIGHT" and h["TELESCOP"] == "t1"
    assert (h["REQGRP"], h["REQUEST"], h["CONFIGID"]) == ("g1", "r1", "c1")
    assert h["RA"] == pytest.approx(82.5) and h["DEC"] == pytest.approx(22.0)
    assert h["RADESYS"] == "FK5" and h["EQUINOX"] == 2000.0
    assert h["OBJRA"] == pytest.approx(82.5) and h["OBJDEC"] == 22.0
    assert h["HA"] == pytest.approx(0.5)
    assert h["AIRMASS"] == pytest.approx(1.413, abs=2e-3)
    assert h["CCD-TEMP"] == -20.1 and "[C]" in h.comments["CCD-TEMP"]
    assert h["HUMIDITY"] == 41.0
    assert h["OBSERVAT"] == "minerva" and h["SWVER"] == "v1.2-3-gabc"
    assert h["SITELONG"] == -110.88 and h["FILTER"] == "V"
    # The approximate WCS: the binned scale, north up, east left.
    assert h["CTYPE1"] == "RA---TAN" and h["CRVAL1"] == pytest.approx(82.5)
    assert h["CD1_1"] == pytest.approx(-2 * 0.5 / 3600) and h["CD2_2"] == pytest.approx(1 / 3600)
    assert h["CRPIX1"] == pytest.approx(16.5) and h["PIXSCALE"] == 1.0
    assert all(len(c.image) == 80 for c in h.cards)


def undefined(v) -> bool:
    return v is None or isinstance(v, fits.card.Undefined)


def test_stale_values_are_written_undefined_never_as_current():
    h, _ = fw.build_header(info(registry()), (48, 32))
    assert undefined(h["AMBTEMP"])
    assert "STALE (900s old): was 12.5 C" in h.comments["AMBTEMP"]
    assert "STALE (read failed): was -30 C" in h.comments["SKYTEMP"]
    assert "AMBTEMP" in str(h["COMMENT"]) and "SKYTEMP" in str(h["COMMENT"])
    # A stale mount position: no RA/DEC values and no WCS.
    r = registry()
    r.publish("t1.tel.ra", 5.5, max_age=60, measured=T0 - timedelta(seconds=600), unit="h")
    h, _ = fw.build_header(info(r), (48, 32))
    assert undefined(h["RA"]) and "STALE" in h.comments["RA"]
    assert "CTYPE1" not in h and "RADESYS" not in h


def test_dark_frames_carry_no_pointing():
    h, _ = fw.build_header(
        info(registry(), light=False, imagetyp="DARK", target=None, filter=None), (48, 32)
    )
    for key in ("RA", "DEC", "ALT", "LST", "AIRMASS", "CTYPE1", "OBJRA"):
        assert key not in h
    assert h["IMAGETYP"] == "DARK" and h["CCD-TEMP"] == -20.1


def test_topocentric_mount_coordinates_are_converted_to_icrs():
    r = registry()
    r.publish("t1.tel.eqsys", 1, max_age=None, measured=T0)
    h, _ = fw.build_header(info(r), (48, 32))
    assert h["RADESYS"] == "ICRS" and "EQUINOX" not in h
    # J2026.9 apparent -> ICRS: about 0.36 deg of precession in RA at this Dec.
    assert 0.1 < abs(h["RA"] - 82.5) < 0.6


def test_imagetyp():
    assert fw.imagetyp(False, 0, "bias") == "BIAS"
    assert fw.imagetyp(False, 60, "dark60") == "DARK"
    assert fw.imagetyp(True, 1, "SkyFlat V") == "FLAT"
    assert fw.imagetyp(True, 30, "M31") == "LIGHT"


def test_storage_types():
    def stored(data):
        return fw.storage_array(ImageArray(data, 2))

    out = stored(np.full((5, 3), 255, "u1"))
    assert out.dtype == np.uint16 and out.shape == (3, 5)
    assert stored(np.full((5, 3), 70000, "<i4")).dtype == np.dtype(">i4")
    assert stored(np.full((5, 3), 60000, "<i4")).dtype == np.uint16
    assert stored(np.full((5, 3), -1, "<i2")).dtype == np.dtype(">i4")
    assert stored(np.full((5, 3), 2**40, "<i8")).dtype == np.dtype(">i8")
    assert stored(np.zeros((5, 3), "<f4")).dtype == np.dtype(">f4")


# -- writer ------------------------------------------------------------------------


def image(nx=32, ny=24, value=100) -> ImageArray:
    data = np.full((nx, ny), value, "<u2")
    data[nx - 1, 0] = 65535
    return ImageArray(data, 8)


def test_layout_night_and_round_trip(tmp_path):
    store = FrameStore(tmp_path, "minerva", longitude=SITE["longitude"])
    w = store.write(info(registry()), image())
    # 05:06 UTC is 21:42 local mean time at -110.88: the night of the 13th.
    assert w.night == date(2026, 11, 13)
    assert w.relpath == "minerva/t1/n20261113/n20261113.t1.HD_1234.0001.fits"
    assert w.path == tmp_path / w.relpath
    with fits.open(w.path, checksum=True) as hdul:
        hdu = hdul[0]
        assert hdu.verify_checksum() == 1 and hdu.verify_datasum() == 1
        assert hdu.header["BITPIX"] == 16 and hdu.header["BZERO"] == 32768
        assert hdu.data.shape == (24, 32) and hdu.data[0, 31] == 65535
        assert hdu.header["FILENAME"] == w.path.name and hdu.header["SEQNUM"] == 1
        assert hdu.header["NIGHT"] == "n20261113"
        assert hdu.header["DATE-OBS"] == "2026-11-14T05:06:08.123"
    (line,) = (w.path.parent / "n20261113.t1.frames.jsonl").read_text().splitlines()
    rec = json.loads(line)
    assert rec["file"] == w.relpath and rec["seq"] == 1 and rec["request"] == "r1"
    assert [p.name for p in w.path.parent.iterdir() if p.name.startswith(".")] == []


def test_night_by_timezone(tmp_path):
    store = FrameStore(tmp_path, "s", timezone="America/Phoenix", rollover_hour=12)
    assert store.night(datetime(2026, 11, 14, 18, 59, tzinfo=UTC)) == date(2026, 11, 13)
    assert store.night(datetime(2026, 11, 14, 19, 0, tzinfo=UTC)) == date(2026, 11, 14)


def test_never_overwrites_and_numbers_count_up(tmp_path):
    store = FrameStore(tmp_path, "minerva", longitude=SITE["longitude"])
    d = store.directory("t1", date(2026, 11, 13))
    d.mkdir(parents=True)
    # Something already holds number 7 (another object): the next is 8.
    existing = d / "n20261113.t1.other.0007.fits"
    existing.write_bytes(b"precious")
    w1 = store.write(info(registry()), image())
    assert w1.seq == 8
    # A second store (another process) that does not know about w1 picks
    # the next free number too, never w1's.
    other = FrameStore(tmp_path, "minerva", longitude=SITE["longitude"])
    other._next[("t1", date(2026, 11, 13))] = 8  # it believes 8 is free
    w2 = other.write(info(registry(), object="HD 1234"), image(value=5))
    assert w2.seq == 9 and existing.read_bytes() == b"precious"
    with fits.open(w1.path) as a, fits.open(w2.path) as b:
        assert a[0].data[1, 1] == 100 and b[0].data[1, 1] == 5
        assert b[0].header["SEQNUM"] == 9 and b[0].header["FILENAME"] == w2.path.name
    assert store.write(info(registry()), image()).seq == 10


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch):
    store = FrameStore(tmp_path, "minerva", longitude=SITE["longitude"])

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="No space"):
        store.write(info(registry()), image())
    d = store.directory("t1", date(2026, 11, 13))
    assert list(d.iterdir()) == []


def test_rice_compression(tmp_path):
    store = FrameStore(tmp_path, "minerva", longitude=SITE["longitude"], compress=True)
    w = store.write(info(registry()), image())
    assert w.path.name.endswith(".0001.fits.fz")
    with fits.open(w.path, checksum=True, disable_image_compression=True) as raw:
        # The checksums are of the HDUs as stored (the compressed table).
        assert all(h.verify_checksum() == 1 and h.verify_datasum() == 1 for h in raw)
    with fits.open(w.path) as hdul:
        assert isinstance(hdul[1], fits.CompImageHDU)
        assert hdul[1].header["OBJECT"] == "HD 1234"
        assert hdul[1].data.dtype == np.uint16 and hdul[1].data[0, 31] == 65535
    # Floats are never quantized: written uncompressed.
    w = store.write(info(registry()), ImageArray(np.ones((4, 4), "<f4") / 3, 4))
    assert w.path.name.endswith(".fits")
    with fits.open(w.path) as hdul:
        assert hdul[0].data[0, 0] == np.float32(1 / 3)


def test_safe_names():
    assert fw.safe_name("HD 1234 [c1#0]") == "HD_1234__c1_0"
    assert fw.safe_name("a.b/c") == "a_b_c" and fw.safe_name("...") == "unknown"


def test_snapshot_judged_at_its_own_time():
    r = ValueRegistry()
    r.publish("t1.x", 1, max_age=10, measured=T0)
    assert Snapshot(T0 + timedelta(seconds=5), dict(r._values)).current("t1.x") == 1
    assert Snapshot(T0 + timedelta(seconds=11), dict(r._values)).current("t1.x") is None
