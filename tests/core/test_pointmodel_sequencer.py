"""robofast's pointing model in the sequencer (docs/pointing_model.md): a
pointing run on the simulated sky, the model applied to the next slew, the
headers, the drift alarm, the request type and the service config."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast_core import pointing as pm
from robofast_core.astrometry import SolverConfig
from robofast_core.catalog import separation
from robofast_core.config import load_config
from robofast_core.framesolve import FrameSolver
from robofast_core.imaging import Imaging, ImagingConfig
from robofast_core.owner import DeviceOwner
from robofast_core.plan import Observation, StaticPlan
from robofast_core.pointmodel import BuiltModel, ModelStore, PointingCorrector, SampleLog
from robofast_core.request_model import load_request_groups
from robofast_core.sequencer_service import build, snapshot
from robofast_core.testing.focus_sim import SimFocuser, StarfieldCamera
from robofast_core.testing.pointing_sim import SkyMount
from robofast_core.testing.skysim import SkyFocusScene, lst_deg
from robofast_core.timeline import Timeline

from .test_imaging import SITE, headers
from .test_pointmodel import EQ, LAT, LON, sky_scene

SITES = Path(__file__).resolve().parents[2] / "sites"


def now() -> datetime:
    return datetime.now(UTC)


def sky_rig(make_rig, plan, tmp_path, truth=EQ, **pointing):
    rig = make_rig(plan=plan)
    unit = rig.sequencer.units["t1"]
    mount = SkyMount(now, LAT, LON, slew_time=0.05)
    sky = sky_scene(truth)
    scene = SkyFocusScene(sky, mount, now, effective_exposure=10.0)
    cam = StarfieldCamera(SimFocuser(position=20000), scene)
    unit.telescope = DeviceOwner("t1.telescope", lambda: mount, rig.timeline, default_timeout=5)
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=5)
    solver = SolverConfig(plate_scale=sky.camera.plate_scale)
    unit.pointing = PointingCorrector(
        "t1",
        latitude=LAT,
        longitude=LON,
        solver=solver,
        catalog=sky.catalog,
        store=ModelStore(tmp_path / "pointing" / "models"),
        samples=SampleLog(tmp_path / "pointing" / "samples"),
        run={"settle": 0, "min_points": 10, "moon_distance": 0},
        **pointing,
    )
    imaging = Imaging(
        ImagingConfig(
            data_root=str(tmp_path / "data"),
            site="testsite",
            telescopes={"t1": {"plate_scale": sky.camera.plate_scale}},
            enclosure_interval=0.1,
        ),
        timeline=rig.timeline,
        site=SITE,
        supervisor=rig.supervisor,
        weather=rig.owners["observingconditions"],
    )
    imaging.store.solver = FrameSolver(sky.catalog, {"t1": solver})
    rig.sequencer.imaging = imaging
    rig.sequencer.registry = imaging.registry
    imaging.set_clock(rig.sequencer.clock)
    return rig, unit, mount, scene, imaging


async def run(rig, imaging, timeout=120.0):
    telemetry = asyncio.create_task(imaging.telemetry.run())
    try:
        await rig.start()
        await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), timeout)
    finally:
        telemetry.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await telemetry
        await imaging.aclose()


def field_now(dec=40.0, east=20.0):
    """A field ``east`` deg east of the meridian, now (RA hours, Dec)."""
    return ((lst_deg(now(), LON) + east) % 360) / 15.0, dec


async def test_pointing_run_builds_a_model_that_puts_the_next_target_on_the_axis(
    make_rig, tmp_path
):
    ra_h, dec = field_now()
    plan = StaticPlan(
        [
            {"name": "pm", "kind": "pointing_model", "count": 18, "exptime": 0.02},
            {"name": "field", "ra_hours": ra_h, "dec_deg": dec, "exptime": 0.02, "count": 2},
        ],
        ["t1"],
    )
    rig, _, _, scene, imaging = sky_rig(make_rig, plan, tmp_path)
    await run(rig, imaging)
    assert [h[2] for h in plan.history] == ["complete", "complete"]
    [built] = rig.sequencer.pointing_models
    assert sorted(built.model.terms) == sorted(EQ)
    for k, v in EQ.items():
        assert built.model.terms[k] == pytest.approx(v, abs=1.0)
    assert built.cv_rms < 0.5 and built.sources == {"run": 18}
    # Saved, with every sample logged.
    assert ModelStore(tmp_path / "pointing" / "models").load("t1", None).model == built.model
    assert len(SampleLog(tmp_path / "pointing" / "samples").read("t1", None)) >= 18
    # The field: commanded through the model, the telescope really on the target.
    truth = scene.last_truth
    err = separation(truth["ra"], truth["dec"], ra_h * 15, dec) * 3600
    assert err < 1.0, err
    [applied] = rig.timeline.events("pointing_applied")
    assert applied.detail["model"] and applied.detail["offset_arcsec"] > 60
    # Headers: RA/DEC where it pointed, MNTRA/MNTDEC what the mount said.
    light = [h for _, h in headers(tmp_path, "t1") if h.get("OBJECT") == "field"]
    assert len(light) == 2
    for h in light:
        assert separation(h["RA"], h["DEC"], ra_h * 15, dec) * 3600 < 2.0
        assert separation(h["MNTRA"] * 15, h["MNTDEC"], ra_h * 15, dec) * 3600 > 60
        assert h["WCSSRC"] == "robofast"
    # The solved field frames were samples, close to the model.
    res = imaging.registry.get("t1.pointing.residual")
    assert res is not None and res.value < 1.0
    assert not rig.timeline.events("telescope_error")


async def test_drift_alarm_reaches_the_status_snapshot(make_rig, tmp_path):
    ra_h, dec = field_now(dec=30.0)
    plan = StaticPlan(
        [{"name": "field", "ra_hours": ra_h, "dec_deg": dec, "exptime": 0.02, "count": 8}],
        ["t1"],
    )
    # The model is the truth as it was; the mount has since shifted 60" in HA.
    drifted = {**EQ, "IH": EQ["IH"] + 60.0}
    rig, unit, mount, _, imaging = sky_rig(
        make_rig,
        plan,
        tmp_path,
        truth=drifted,
        drift={"min_samples": 5},
        vendor_off={"action": "model_off", "parameters": "1"},
    )
    model = pm.PointingModel(EQ, latitude=LAT)
    unit.pointing.set_model(BuiltModel(model, "t1", None, {}, 140.0, 0.3, 0.4, 30))
    await run(rig, imaging)
    assert mount.actions == [("model_off", "1")]
    [text] = unit.alarms
    assert "pointing drift" in text and text.startswith("t1 port None")
    [event] = rig.timeline.events("alarm")
    assert event.detail["telescope"] == "t1"
    snap = snapshot(rig.sequencer)
    assert snap["telescopes"]["t1"]["alarms"] == [text]
    summary = unit.pointing.night_summary()["None"]
    assert summary["alarmed"] and summary["shift"] > 40


def test_pointing_model_configuration_dispatches():
    from .test_dispatch import group, make

    g = group("pointing", ics=[{"exptime": 5.0, "count": 40}])
    g["requests"][0]["configurations"][0]["type"] = "POINTING_MODEL"
    [rg] = load_request_groups([g])
    assert rg.requests[0].configurations[0].type == "POINTING_MODEL"

    async def go():
        d = make([g], limits=None)
        obs = await d.next_observation("t1")
        assert obs.kind == "pointing_model" and obs.count == 40 and obs.exptime == 5.0

    asyncio.run(go())
    with pytest.raises(ValueError, match="light"):
        Observation("x", kind="pointing_model", light=False)
    with pytest.raises(ValueError, match="pointing"):
        Observation("x", pointing={"min_alt": 30})


async def test_service_builds_the_pointing_entry(tmp_path):
    config = load_config(SITES / "minerva" / "aqawan1.json")
    config["telescopes"]["t1"]["pointing"] = {
        "latitude": LAT,
        "longitude": LON,
        "store": "pointing_t1",
        "local_correction": True,
    }
    tl = Timeline()
    seq, remote = build(config, tl, base=tmp_path)
    try:
        pc = seq.units["t1"].pointing
        assert pc is not None and pc.store.root == tmp_path / "pointing_t1" / "models"
        assert pc.local is not None and seq.units["t2"].pointing is None
    finally:
        await remote.aclose()
    config["telescopes"]["t1"]["pointing"]["oops"] = 1
    with pytest.raises(ValueError, match="t1"):
        build(config, tl, base=tmp_path)
    json.dumps(snapshot(seq))  # the snapshot (with alarms) is JSON
