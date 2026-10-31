"""Images from OmniSim's camera: the download in both transfer modes, and a
real FITS file written by the sequencer and read back."""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits

from robofast_core.alpaca_client import AlpacaDevice
from robofast_core.imaging import Imaging, ImagingConfig
from robofast_core.plan import StaticPlan
from robofast_core.sequencer import Sequencer, SequencerConfig

from .conftest import SIM_SEQ

pytestmark = pytest.mark.alpaca_sim

SITE = {"latitude": 31.680407, "longitude": -110.878977, "elevation": 2316.0}


async def _expose(cam: AlpacaDevice, seconds: float = 0.1) -> None:
    await cam.connect(timeout=10)
    await cam.call("StartExposure", seconds, True, timeout=10)
    deadline = time.monotonic() + 30
    while not await cam.get("ImageReady", timeout=10):
        assert time.monotonic() < deadline, "OmniSim image never ready"
        await asyncio.sleep(0.05)


async def test_full_frame_download_imagebytes_and_json(omnisim, truth, record_timing):
    cam = AlpacaDevice(omnisim, "camera")
    try:
        await _expose(cam)
        numx, numy = await cam.get("NumX", timeout=5), await cam.get("NumY", timeout=5)
        timings = {}
        images = {}
        for binary in (True, False, True, False, True, False, True, False, True, False):
            t0 = time.perf_counter()
            image = await cam.image_array(timeout=60, binary=binary)
            timings.setdefault(image.transfer, []).append(time.perf_counter() - t0)
            images[image.transfer] = image
        b, j = images["imagebytes"], images["json"]
        assert b.data.shape == j.data.shape == (numx, numy)
        np.testing.assert_array_equal(b.data, j.data)
        assert b.nbytes_wire < j.nbytes_wire
        med = {k: statistics.median(v) for k, v in timings.items()}
        record_timing(
            imagebytes_ms=1000 * med["imagebytes"],
            json_ms=1000 * med["json"],
            imagebytes_mb=b.nbytes_wire / 1e6,
            json_mb=j.nbytes_wire / 1e6,
            pixels=numx * numy,
        )
    finally:
        await cam.aclose()


async def test_the_sequencer_writes_a_real_fits_frame(make_sim_rig, tmp_path, truth):
    rig = make_sim_rig()
    imaging = Imaging(
        ImagingConfig(
            data_root=str(tmp_path),
            site="simsite",
            telescopes={"telescope": {"plate_scale": 1.0}},
            enclosure_interval=0.5,
        ),
        timeline=rig.timeline,
        site=SITE,
        supervisor=rig.supervisor,
        weather=rig.owners["observingconditions"],
    )
    plan = StaticPlan(
        [
            {"name": "sim field", "object": "SimField", "exptime": 1.0, "count": 2},
            {"name": "bias", "light": False, "exptime": 0.0, "count": 1},
        ],
        ["telescope"],
    )
    rig.sequencer = Sequencer(
        "seq",
        supervisor=rig.supervisor,
        telescope=rig.owners["telescope"],
        camera=rig.owners["camera"],
        plan=plan,
        timeline=rig.timeline,
        config=SequencerConfig(**SIM_SEQ),
        imaging=imaging,
    )
    t_start = datetime.now(UTC)
    telemetry = asyncio.create_task(imaging.telemetry.run())
    try:
        await rig.start()
        await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 120)
    finally:
        telemetry.cancel()
        await asyncio.gather(telemetry, return_exceptions=True)
        await imaging.aclose()
    assert [h[2] for h in plan.history] == ["complete", "complete"], plan.history
    files = [f for _, _, fs in plan.files for f in fs]
    assert len(files) == 3
    for rel in files:
        path = tmp_path / rel
        assert path.name.startswith("n") and ".telescope." in path.name
        with fits.open(path, checksum=True) as hdul:
            hdu = hdul[0]
            assert hdu.verify_checksum() == 1 and hdu.verify_datasum() == 1
            h = hdu.header
            assert hdu.data.shape == (600, 800)  # OmniSim: NumY x NumX
            assert h["TIMESRC"] == "camera" and h["TIMESYS"] == "UTC"
            date_obs = datetime.fromisoformat(h["DATE-OBS"]).replace(tzinfo=UTC)
            assert abs((date_obs - t_start).total_seconds()) < 120
            assert len(h["DATE-OBS"]) == 23 and abs(h["CLKSKEW"]) < 2.0
            assert h["SITELAT"] == SITE["latitude"] and "SWVER" in h
            assert h["CCD-TEMP"] is not None and h["XBINNING"] == 1
            if h["IMAGETYP"] == "LIGHT":
                assert h["OBJECT"] == "SimField" and h["EXPTIME"] == pytest.approx(1.0, abs=0.1)
                assert -90 <= h["DEC"] <= 90 and 0 <= h["RA"] < 360
                assert 0 < h["ALT"] <= 90 and h["AIRMASS"] >= 1.0
                assert h["CTYPE1"] == "RA---TAN" and h["SHUTTER"] == "OPEN"
            else:
                assert h["IMAGETYP"] == "BIAS" and "RA" not in h
