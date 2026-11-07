"""robofast-discover's plate scale from what the telescope and camera
report (docs/discover.md), and a first pointing calibration from it on the
simulated sky (docs/pointing_model.md, "A first calibration")."""

from __future__ import annotations

import contextlib
import json

import pytest

from robofast.astrometry import SolverConfig
from robofast.catalog import separation
from robofast.discover import (
    ARCSEC_PER_UM_PER_MM,
    OpticsUnavailable,
    configured_devices,
    main,
    read_optics,
)
from robofast.plan import StaticPlan
from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera, FakeTelescope

from .test_pointmodel_sequencer import field_now, run, sky_rig


def _served(telescope, camera):
    server = FakeAlpacaServer({"telescope": telescope, "camera": camera}).start()
    devices = {d.type: d for d in configured_devices(server.address)}
    return server, devices["Telescope"], devices["Camera"]


@pytest.fixture
def scope():
    tel, cam = FakeTelescope(), FakeCamera()
    tel.FocalLength = 0.5  # m
    cam.PixelSizeX = cam.PixelSizeY = 9.0  # um
    server, t, c = _served(tel, cam)
    yield tel, cam, t, c
    server.stop()


def test_plate_scale_from_focal_length_and_pixel_size(scope):
    _, _, t, c = scope
    optics = read_optics(t, c)
    assert optics == {"focal_length": 500.0, "plate_scale": round(206.264806 * 9.0 / 500.0, 5)}


def test_a_disconnected_device_is_left_disconnected(scope):
    tel, cam, t, c = scope
    tel.Connected = cam.Connected = False
    assert read_optics(t, c)["plate_scale"] > 0
    assert tel.Connected is False and cam.Connected is False


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda tel, cam: delattr(tel, "FocalLength"), "FocalLength: Alpaca error"),
        (lambda tel, cam: setattr(tel, "FocalLength", 0.0), "FocalLength: 0.0 m"),
        (lambda tel, cam: setattr(cam, "PixelSizeY", 4.5), "are not square"),
        (lambda tel, cam: setattr(cam, "PixelSizeX", 0), "PixelSizeX: 0 um"),
    ],
)
def test_a_value_the_devices_cannot_report_raises_naming_it(scope, change, message):
    tel, cam, t, c = scope
    change(tel, cam)
    with pytest.raises(OpticsUnavailable, match=message):
        read_optics(t, c)


def test_write_stops_naming_it_and_writes_nothing(scope, tmp_path):
    tel, _, t, _ = scope
    del tel.FocalLength
    path = tmp_path / "observatory.json"
    with pytest.raises(SystemExit) as exc:
        main(["--server", t.server, "--write", str(path)])
    assert exc.value.code == 2 and not path.exists()
    main(["--server", t.server, "--write", str(path), "--no-optics"])
    assert "telescopes" not in json.loads(path.read_text())["images"]


def test_write_puts_the_plate_scale_in_images(scope, tmp_path):
    _, _, t, c = scope
    path = tmp_path / "observatory.json"
    main(["--server", t.server, "--write", str(path)])
    config = json.loads(path.read_text())
    assert config["images"]["telescopes"] == {"t1": read_optics(t, c)}


@pytest.mark.parametrize("focal_error", [1.0, 1.1])
async def test_first_pointing_calibration_from_the_devices(make_rig, tmp_path, focal_error):
    """The plate scale read from the simulated mount and camera over Alpaca
    solves the simulated sky: three solves calibrate the pointing. A focal
    length reported 10 % off (beyond the solver's 3 % scale tolerance)
    solves nothing, and the run fails saying so."""
    ra_h, dec = field_now()
    plan = StaticPlan(
        [
            {
                "name": "first-pointing",
                "kind": "pointing_model",
                "count": 3,
                "exptime": 0.02,
                "pointing": {"min_points": 3},
            },
            {"name": "field", "ra_hours": ra_h, "dec_deg": dec, "exptime": 0.02, "count": 1},
        ],
        ["t1"],
    )
    rig, unit, mount, scene, imaging = sky_rig(make_rig, plan, tmp_path)
    cam = unit.camera._factory()
    true_scale = scene.sky.camera.plate_scale
    # The optics the hardware would report: the focal length that gives the
    # simulated sky's plate scale with the camera's pixels (times the error).
    mount.FocalLength = ARCSEC_PER_UM_PER_MM * cam.PixelSizeX / true_scale / 1000.0 * focal_error
    server, t, c = _served(mount, cam)
    try:
        optics = read_optics(t, c)
    finally:
        server.stop()
    unit.pointing.solver = SolverConfig(plate_scale=optics["plate_scale"])
    if focal_error == 1.0:
        await run(rig, imaging)
    else:  # the failed run is offered again (a static plan): stop after a few
        with contextlib.suppress(TimeoutError):
            await run(rig, imaging, timeout=15.0)
    if focal_error == 1.0:
        assert optics["plate_scale"] == pytest.approx(true_scale, rel=1e-4)
        assert [h[2] for h in plan.history] == ["complete", "complete"]
        [built] = rig.sequencer.pointing_models
        assert sorted(built.model.terms) == ["ID", "IH"] and built.n == 3
        truth = scene.last_truth
        assert separation(truth["ra"], truth["dec"], ra_h * 15, dec) * 3600 < 40.0
    else:
        assert not rig.sequencer.pointing_models
        unsolved = rig.timeline.events("pointing_point_unsolved")
        assert len(unsolved) >= 3
        assert not rig.timeline.events("pointing_point")  # not one point solved
