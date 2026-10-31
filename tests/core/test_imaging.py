"""The sequencer saving frames (robofast_core.imaging) on fake devices."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path

import pytest
from astropy.io import fits

from robofast_core.imaging import Imaging, ImagingConfig
from robofast_core.plan import StaticPlan
from robofast_core.testing.checks import wait_until

SITE = {"latitude": 31.68, "longitude": -110.88, "elevation": 2316.0}
SITES = Path(__file__).resolve().parents[2] / "sites" / "minerva"


def attach(rig, tmp_path: Path, **cfg) -> Imaging:
    """Give the rig's sequencer a frame pipeline writing under ``tmp_path``."""
    imaging = Imaging(
        ImagingConfig(
            data_root=str(tmp_path / "data"),
            site="testsite",
            telescopes={"t1": {"plate_scale": 0.6, "aperture": 700.0}},
            **{"enclosure_interval": 0.1, **cfg},
        ),
        timeline=rig.timeline,
        site=SITE,
        supervisor=rig.supervisor,
        weather=rig.owners["observingconditions"],
        version="test-version",
    )
    rig.sequencer.imaging = imaging
    return imaging


async def run_night(rig, imaging: Imaging, timeout: float = 30.0) -> None:
    telemetry = asyncio.create_task(imaging.telemetry.run())
    try:
        await imaging.telemetry.poll_enclosure()
        await imaging.telemetry.poll_weather()
        await rig.start()
        await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), timeout)
    finally:
        telemetry.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await telemetry
        await imaging.aclose()


def headers(root: Path, telescope: str) -> list[tuple[Path, fits.Header]]:
    out = []
    for p in sorted(root.glob(f"data/testsite/{telescope}/n*/*.fits")):
        with fits.open(p, checksum=True) as hdul:
            assert hdul[0].verify_checksum() == 1 and hdul[0].verify_datasum() == 1
            out.append((p, hdul[0].header.copy()))
    return out


async def test_frames_are_saved_with_complete_headers_and_reported(make_rig, tmp_path):
    plan = StaticPlan(
        [
            {
                "name": "M31 field",
                "object": "M31",
                "ra_hours": 0.7,
                "dec_deg": 41.3,
                "exptime": 0.2,
                "count": 3,
                "filter": "V",
            },
            {"name": "dark", "light": False, "exptime": 0.1, "count": 2},
        ],
        ["t1", "t2"],
    )
    rig = make_rig(telescopes=2, plan=plan, filterwheels=True)
    imaging = attach(rig, tmp_path)
    await run_night(rig, imaging)

    assert sorted(plan.history) == sorted(
        [(t, n, "complete", c) for t in ("t1", "t2") for n, c in (("M31 field", 3), ("dark", 2))]
    )
    # Every complete report carries its frames' paths, and they exist.
    assert len(plan.files) == 4
    for tel, name, files in plan.files:
        assert len(files) == (3 if name == "M31 field" else 2)
        for f in files:
            assert f.startswith(f"testsite/{tel}/n") and (tmp_path / "data" / f).is_file()
    for tel in ("t1", "t2"):
        frames = headers(tmp_path, tel)
        assert [h["SEQNUM"] for _, h in frames] == [1, 2, 3, 4, 5]
        lights = [h for _, h in frames if h["IMAGETYP"] == "LIGHT"]
        darks = [h for _, h in frames if h["IMAGETYP"] == "DARK"]
        assert len(lights) == 3 and len(darks) == 2
        h = lights[0]
        assert h["OBJECT"] == "M31" and h["TELESCOP"] == tel and h["FILTER"] == "V"
        assert h["FILTNAME"] == "V" and h["FILTPOS"] == 1
        assert h["RA"] == pytest.approx(10.5) and h["DEC"] == pytest.approx(41.3)
        assert h["OBJRA"] == pytest.approx(10.5) and h["RADESYS"] == "FK5"
        assert h["ALT"] == 60.0 and h["AIRMASS"] == pytest.approx(1.154, abs=1e-3)
        assert h["TIMESRC"] == "camera" and abs(h["CLKSKEW"]) < 1.0
        assert h["EXPTIME"] == 0.2 and h["EXPREQ"] == 0.2
        assert h["CCD-TEMP"] == -20.0 and h["INSTRUME"] == "FakeCamera"
        assert h["READMODE"] == "Normal" and h["EGAIN"] == 1.5 and h["XBINNING"] == 1
        assert h["HUMIDITY"] == 30.0 and h["AMBTEMP"] == 11.5
        assert h["SHUTTER"] == "OPEN" and h["WXSAFE"] is True
        assert "SUNALT" in h and "MOONILLU" in h and "MOONSEP" in h
        assert h["OBSERVAT"] == "testsite" and h["SWVER"] == "test-version"
        assert h["SEQUENCR"] == "seq" and h["OBSNAME"] == "M31 field"
        assert ("CTYPE1" in h) == (tel == "t1")  # only t1 has a plate scale
        assert "RA" not in darks[0] and "ALT" not in darks[0]
        assert darks[0]["OBJECT"] == "dark" and darks[0]["CCD-TEMP"] == -20.0
    # The pixels, in FITS order, and the nightly index.
    path, h = headers(tmp_path, "t1")[0]
    with fits.open(path) as hdul:
        assert hdul[0].data.shape == (24, 32) and hdul[0].data[0, 31] == 1000
    index = list(path.parent.glob("*.frames.jsonl"))
    assert len(index) == 1
    lines = [json.loads(x) for x in index[0].read_text().splitlines()]
    assert [x["seq"] for x in lines] == [1, 2, 3, 4, 5]
    assert len(imaging.downloads) == 10
    assert len(rig.timeline.events("frame_written")) == 10


async def test_a_lost_frame_stops_that_telescope_and_alarms(make_rig, tmp_path):
    plan = StaticPlan(
        [
            {"name": f"f{i}", "ra_hours": 1.0, "dec_deg": 10.0, "exptime": 0.1, "count": 3}
            for i in range(3)
        ],
        ["t1", "t2"],
    )
    rig = make_rig(telescopes=2, plan=plan)
    imaging = attach(rig, tmp_path)
    real_write = imaging.store.write

    def write(info, image):
        if info.telescope == "t1":
            raise OSError(28, "No space left on device")
        return real_write(info, image)

    imaging.store.write = write
    await run_night(rig, imaging)

    t1 = rig.sequencer.units["t1"]
    assert t1.gave_up and rig.sequencer.needs_operator
    assert next(h for h in plan.history if h[0] == "t1")[2] == "failed"
    assert [h[2] for h in plan.history if h[0] == "t2"] == ["complete"] * 3
    alarms = rig.timeline.events("frame_write_failed")
    assert alarms and alarms[0].detail["level"] == "critical"
    assert "No space left" in alarms[0].detail["error"] and "f0 #0" in alarms[0].detail["error"]
    assert rig.timeline.events("giving_up")
    # t1 took no exposure after the failure was known.
    assert rig.cams[0].exposures_started <= 3
    assert len(headers(tmp_path, "t1")) == 0 and len(headers(tmp_path, "t2")) == 9
    assert rig.parked(0)


async def test_writes_overlap_exposures_but_the_queue_is_bounded(make_rig, tmp_path):
    plan = StaticPlan(
        [{"name": "fast", "ra_hours": 1.0, "dec_deg": 10.0, "exptime": 0.05, "count": 6}], ["t1"]
    )
    rig = make_rig(plan=plan)
    imaging = attach(rig, tmp_path, queue_size=2)
    real_write = imaging.store.write
    writing = threading.Event()
    overlapped = []
    peak = []

    def slow_write(info, image):
        writing.set()
        peak.append(imaging._writers["t1"].queue.qsize())
        time.sleep(0.3)
        try:
            return real_write(info, image)
        finally:
            writing.clear()

    imaging.store.write = slow_write
    cam = rig.cams[0]
    real_start = cam.StartExposure

    def start(duration, light):
        overlapped.append(writing.is_set())
        real_start(duration, light)

    cam.StartExposure = start
    await run_night(rig, imaging)
    assert plan.history == [("t1", "fast", "complete", 6)]
    assert len(plan.files[0][2]) == 6
    assert any(overlapped)  # an exposure ran while a frame was being written
    assert max(peak) <= 2  # never more than queue_size frames waiting


async def test_a_failed_download_is_an_ordinary_error(make_rig, tmp_path):
    plan = StaticPlan(
        [{"name": "x", "ra_hours": 1.0, "dec_deg": 10.0, "exptime": 0.05, "count": 2}], ["t1"]
    )
    rig = make_rig(plan=plan, seq={"error_backoff": 0.2, "max_consecutive_errors": 3})
    imaging = attach(rig, tmp_path)
    rig.cams[0].image_error = "USB reset"
    await rig.start()
    try:
        await wait_until(lambda: rig.timeline.events("telescope_error"), 10)
        rig.cams[0].image_error = None
        await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    finally:
        await imaging.aclose()
    assert "USB reset" in rig.timeline.events("telescope_error")[0].detail["error"]
    assert plan.history[0][2] == "failed" and plan.history[-1] == ("t1", "x", "complete", 2)
    assert not rig.sequencer.units["t1"].gave_up
    assert len(headers(tmp_path, "t1")) == 2


async def test_without_imaging_nothing_changes(make_rig):
    plan = StaticPlan([{"name": "x", "ra_hours": 1.0, "dec_deg": 10.0, "exptime": 0.05}], ["t1"])
    rig = make_rig(plan=plan)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    assert plan.history == [("t1", "x", "complete", 1)] and plan.files == []
    assert rig.cams[0].downloads == 0


async def test_the_service_builds_the_pipeline_from_the_config(tmp_path):
    from robofast_core.config import load_config
    from robofast_core.sequencer_service import build, snapshot
    from robofast_core.timeline import Timeline

    config = load_config(SITES / "aqawan1.json")
    assert "images" in config  # the example saves frames
    config["images"] = {**config["images"], "data_root": "frames", "imagebytes": False}
    config["telescopes"]["t1"]["focuser"] = "127.0.0.1:11121/0"
    seq, remote = build(config, Timeline(), base=tmp_path)
    try:
        im = seq.imaging
        assert im is not None and im.store.root == tmp_path / "frames"
        assert im.store.longitude == config["site"]["longitude"]
        assert im.telemetry.weather is not None and im.telemetry.supervisor is remote
        assert set(im._aux) == {"t1"} and len(im.owners) == 2  # focuser, weather
        assert im.registry.get("site.latitude").value == config["site"]["latitude"]
        assert "values" in snapshot(seq) and "software.version" in snapshot(seq)["values"]
        cam = seq.units["t1"].camera._factory()
        assert cam.imagebytes is False
        await cam.aclose()
    finally:
        await remote.aclose()
    with pytest.raises(ValueError, match="unknown keys"):
        ImagingConfig.from_dict({"data_root": "x", "bogus": 1})
    with pytest.raises(ValueError, match="data_root"):
        ImagingConfig.from_dict({})
    with pytest.raises(ValueError, match="optics"):
        ImagingConfig.from_dict({"data_root": "x", "telescopes": {"t1": {"scale": 1}}})
