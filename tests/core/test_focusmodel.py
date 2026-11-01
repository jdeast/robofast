"""The focus model's gravity term and its fit from autofocus history
(robofast_core/autofocus.py FocusModel, robofast_core/focusmodel.py)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from robofast_core.autofocus import FocusModel, FocusStore
from robofast_core.focusmodel import fit, legacy_model, main

T2 = {"F0": 16500.0, "C0": -52.94, "T0": 8.95, "C1": 46.18, "dT0": 0.2237, "C2": 2220.265,
      "G0": 0.7824}  # fmt: skip
TEMP, ALT = "{tel}.focuser.temperature", "sin:{tel}.tel.alt"


def guess_focus(c, m1temp, dt, alt_deg):
    """Legacy cdk700.guessFocus, verbatim in arithmetic."""
    gravity = math.sin(math.radians(alt_deg))
    return (
        c["F0"] + c["C0"] * (m1temp - c["T0"]) + c["C1"] * (dt - c["dT0"])
        + c["C2"] * (gravity - c["G0"])
    )  # fmt: skip


@pytest.mark.parametrize("temp,dt,alt", [(8.95, 0.2237, 51.5), (2.0, 1.0, 30.0), (15.0, -1.0, 85)])
def test_legacy_model_reproduces_guess_focus(temp, dt, alt):
    m = legacy_model(T2)
    vals = {TEMP: temp, "{tel}.focuser.delta_t": dt, ALT: alt}
    assert m.predict(vals) == pytest.approx(guess_focus(T2, temp, dt, alt), abs=1e-6)
    # Without an altitude the gravity term drops out (and is reported).
    _, missing = m.correction({TEMP: temp, "{tel}.focuser.delta_t": dt})
    assert missing == [ALT]


def history(n=40, c0=-52.94, c2=2220.265, noise=8.0, seed=0, key="t2/imager/-"):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        t, alt = rng.uniform(-2, 18), rng.uniform(25, 85)
        f = 16500 + c0 * (t - 8.95) + c2 * (math.sin(math.radians(alt)) - 0.7824)
        out.append(
            {"key": key, "success": True, "focus": f + rng.normal(0, noise),
             "model_values": {TEMP: t, ALT: alt}}
        )  # fmt: skip
    out.append({"key": key, "success": False, "focus": None, "model_values": {}})
    out.append({"key": "t1/imager/-", "success": True, "focus": 1.0, "model_values": {TEMP: 1}})
    return out


def test_fit_recovers_the_temperature_and_gravity_terms():
    model, info = fit(history(), [TEMP, ALT], key="t2/imager/-")
    assert model.terms[TEMP] == pytest.approx(-52.94, abs=4 * info["errors"][TEMP])
    assert model.terms[ALT] == pytest.approx(2220.265, abs=4 * info["errors"][ALT])
    assert info["n"] == 40 and info["rms"] < 10 and info["spread"] > 100
    # Its predictions agree with the truth's.
    v = {TEMP: 5.0, ALT: 40.0}
    truth = 16500 - 52.94 * (5 - 8.95) + 2220.265 * (math.sin(math.radians(40)) - 0.7824)
    assert model.predict(v) == pytest.approx(truth, abs=15)


def test_fit_drops_an_insignificant_term_and_needs_enough_points():
    model, _ = fit(history(c2=0.0), [TEMP, ALT], key="t2/imager/-", significance=3.0)
    assert list(model.terms) == [TEMP]
    with pytest.raises(ValueError, match="usable"):
        fit(history(n=3), [TEMP, ALT], key="t2/imager/-")


def test_command_line(tmp_path, capsys):
    store = FocusStore(tmp_path / "focus.json")
    store.data["history"] = history()
    store.save()
    assert main(["--store", str(tmp_path / "focus.json"), "--key", "t2/imager/-",
                 "--term", TEMP, "--term", ALT]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert '"sin:{tel}.tel.alt"' in out and "rms" in out


async def test_the_sequencer_starts_from_the_gravity_term_and_records_altitude(make_rig):
    """The start position includes C2 (sin(alt) - G0); the measured focus is
    still what is used."""
    from robofast_core.autofocus_plan import AutofocusTriggers
    from robofast_core.plan import StaticPlan

    from .test_autofocus_sequencer import FIELD, TRUE_FOCUS, focus_rig, run_to_end

    model = FocusModel({ALT: 400.0}, {ALT: 0.5}, zero_point=TRUE_FOCUS)
    plan = AutofocusTriggers(StaticPlan([FIELD], ["t1"]))
    rig, unit = focus_rig(make_rig, plan, model=model)
    plan.bind(rig.sequencer.registry)
    rig.tel.Altitude = 70.0
    await run_to_end(rig)
    [res] = unit.autofocus_results
    assert res.model_values[ALT] == 70.0 and res.model_values[TEMP] == 10.0
    assert res.predicted == pytest.approx(
        TRUE_FOCUS + 400.0 * (math.sin(math.radians(70.0)) - 0.5), abs=1e-6
    )
    assert res.success
