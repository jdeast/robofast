"""Autofocus against OmniSim's Focuser and Camera over Alpaca.

OmniSim's focuser is absolute (MaxStep 50000, StepSize 20 um, about 700
steps/s); its camera's pixels are not a star field, so each exposure is
taken on OmniSim's camera (StartExposure, ImageReady, the ImageBytes
download) and its pixels replaced by the synthetic star field
(robofast_core.testing.starfield) rendered at the focuser position OmniSim
reports. That exercises the real Alpaca focuser path: Move, IsMoving,
Position, the backlash approach and the units taken from ``StepSize``.
"""

from __future__ import annotations

import numpy as np
import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.autofocus import Autofocus, AutofocusConfig, FocusOptics, camera_exposer
from robofast_core.testing.focus_sim import FocusScene
from robofast_core.testing.starfield import Detector, SimOptics, random_stars
from robofast_core.timeline import Timeline

from .conftest import COMMAND_TIMEOUT

pytestmark = pytest.mark.alpaca_sim


@pytest.mark.parametrize("method,exposures", [("vcurve", 2), ("donut", 1)])
async def test_autofocus_on_omnisim(omnisim, method, exposures, record_timing):
    tl = Timeline()
    focuser = alpaca_owner("focuser", "focuser", omnisim, tl, default_timeout=COMMAND_TIMEOUT)
    camera = alpaca_owner("camera", "camera", omnisim, tl, default_timeout=COMMAND_TIMEOUT)
    fg, cg = focuser.grant("af"), camera.grant("af")
    try:
        await fg.set("Connected", True)
        await cg.set("Connected", True)
        step = float(await fg.get("StepSize"))  # um per step: 20 on OmniSim
        start = float(await fg.get("Position"))
        true_focus = start + 37  # the model is 37 steps (740 um) off
        optics = FocusOptics(
            f_ratio=6.5,
            pixel_size=13.5,
            pixel_scale=1.0,
            obstruction=0.47,
            um_per_step=step,
        )
        rng = np.random.default_rng(4)
        det = Detector(shape=(512, 512))
        scene = FocusScene(
            random_stars(6, det.shape, flux=(1.5e5, 6e5), margin=60, min_separation=110, rng=rng),
            optics=SimOptics(pixel_scale=1.0, pixel_size=13.5, f_ratio=6.5, obstruction=0.47),
            detector=det,
            true_focus=true_focus,
            focal_um_per_step=step,
            seeing=2.0,
        )
        real = camera_exposer(cg, readout_margin=30.0, poll=0.1)
        at: list[float] = []

        async def expose(exptime: float) -> np.ndarray:
            await real(exptime)  # OmniSim's exposure and download
            pos = float(await fg.get("Position"))
            at.append(pos)
            return scene.render(pos, exptime, None, 1)

        cfg = AutofocusConfig(
            optics=optics,
            exptime=0.1,
            max_start_error=40,
            backlash=5,
            poll_interval=0.1,
            move_timeout=60.0,
        )
        af = Autofocus("t1", cfg, fg, cg, exposer=expose)
        res = await af.run(method, start=start)
        record_timing(**{f"autofocus_{method}_seconds": res.duration})
        assert res.success, res.message
        assert res.exposures == exposures
        # A quarter of the seeing FWHM of blur: 0.25 * 2 px * 6.5 * 13.5 um.
        assert abs(res.focus - true_focus) * step < 0.25 * 2.0 * 6.5 * 13.5
        assert float(await fg.get("Position")) == pytest.approx(round(res.focus), abs=1)
        assert not await fg.get("IsMoving")
        near, _ = af.offsets()
        # Every exposure far from focus (the start was 37 steps off).
        assert all(abs(p - true_focus) >= near - 38 for p in at), (at, near, true_focus)
    finally:
        focuser.close()
        camera.close()
