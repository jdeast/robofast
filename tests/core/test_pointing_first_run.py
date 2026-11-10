"""A first pointing calibration from a few plate solves
(docs/pointing_model.md, "A first calibration"): a 3-point pointing run on
the simulated sky builds a model of the index terms only, and the next
target lands near the axis."""

from __future__ import annotations

import pytest

from robofast.catalog import separation
from robofast.plan import StaticPlan

from .test_pointmodel_sequencer import field_now, run, sky_rig


async def test_three_solves_calibrate_the_pointing(make_rig, tmp_path):
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
    rig, _, _, scene, imaging = sky_rig(make_rig, plan, tmp_path)
    await run(rig, imaging)
    assert [h[2] for h in plan.history] == ["complete", "complete"]
    assert not rig.timeline.events("telescope_error")
    [built] = rig.sequencer.pointing_models
    # Three points carry the zero points only (pointmodel.select_terms).
    assert sorted(built.model.terms) == ["ID", "IH"] and built.n == 3
    assert built.rms_after < built.rms_before / 2
    truth = scene.last_truth
    err = separation(truth["ra"], truth["dec"], ra_h * 15, dec) * 3600
    assert err < 40.0, err  # the injected model's ~130" rms, less its zero points


async def test_fixed_terms_are_the_models_terms(make_rig, tmp_path):
    """``terms`` fixes the model's terms: four points, which select_terms
    could give a third term, carry IH and ID only."""
    plan = StaticPlan(
        [
            {
                "name": "first-pointing",
                "kind": "pointing_model",
                "count": 4,
                "exptime": 0.02,
                "pointing": {"min_points": 3, "terms": ["IH", "ID"]},
            },
        ],
        ["t1"],
    )
    rig, _, _, _, imaging = sky_rig(make_rig, plan, tmp_path)
    await run(rig, imaging)
    assert [h[2] for h in plan.history] == ["complete"]
    [built] = rig.sequencer.pointing_models
    assert sorted(built.model.terms) == ["ID", "IH"]


@pytest.mark.parametrize(
    ("terms", "why"),
    [
        (["IH", "XX"], "must be a list of equatorial TPOINT terms"),
        ("IH", "must be a list"),
        ([], "must be a list"),
        (["IH", "ID", "NP", "CH"], "min_points 3 cannot carry 4 terms"),
    ],
)
async def test_bad_fixed_terms_fail_before_the_telescope_moves(make_rig, tmp_path, terms, why):
    plan = StaticPlan(
        [
            {
                "name": "first-pointing",
                "kind": "pointing_model",
                "count": 3,
                "exptime": 0.02,
                "pointing": {"min_points": 3, "terms": terms},
            },
        ],
        ["t1"],
    )
    rig, _, _, _, imaging = sky_rig(make_rig, plan, tmp_path)
    await run(rig, imaging)
    # A config error: reported unsupported once, never retried.
    assert [h[2] for h in plan.history] == ["unsupported"]
    [refused] = rig.timeline.events("observation_unsupported")
    assert why in refused.detail["reason"]
    assert not rig.sequencer.pointing_models
    assert not [c for c in rig.timeline.commands() if c.what.startswith("SlewTo")]
