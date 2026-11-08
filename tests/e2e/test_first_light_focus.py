"""First light from far out of focus, end to end (docs/first_light.md,
"Focus"): the sky device's focuser 30000 steps off, the brightest planet
up, the approach, the sweep, the calibration and a solved frame. A few
minutes, so with the e2e runs: ``python -m pytest -m e2e tests/e2e``."""

from __future__ import annotations

import pytest

import robofast.first_light
from robofast.first_light import main
from robofast.testing.sky_device import SkyDevice

from ..core.test_first_light import HELD, _args, _HeldClock, _without_site

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


def test_first_light_focuses_first_from_far_out_of_focus(tmp_path, capsys, monkeypatch):
    """A telescope plugged in far out of focus (its stars are disks larger
    than the field): first light focuses on the brightest planet or star up
    before the calibration, and the frame solves."""
    monkeypatch.setattr(robofast.first_light, "datetime", _HeldClock)
    d = SkyDevice(
        catalog={"cache_dir": str(tmp_path / "sky")},
        pointing_error={"IH": 150.0, "ID": -40.0, "NP": 20.0},
        clock=lambda: HELD,
        focus_error=30000.0,
        bright=True,
    ).start()
    try:
        main(_without_site(_args(d, tmp_path, "--no-enclosure", "--yes")))
        position = d.focuser.Position
    finally:
        d.stop()
    out = capsys.readouterr().out
    assert any(ln.startswith("focus: done") for ln in out.splitlines()), out[-2000:]
    assert '"first_light"' in out
    assert abs(position - 20000) < 100, position
