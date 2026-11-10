"""robofast.testing.sky_device: the simulated telescope first light is
tested on (its catalog source, its optics, its held sky)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pytest

from robofast.catalog import catalog_source
from robofast.discover import configured_devices, read_optics
from robofast.testing.sky_device import SkyDevice, _held


def test_the_synthetic_source_is_the_same_sky_every_time():
    fetch = catalog_source("synthetic").fetcher(None)
    a, b = fetch(7, 21140, 16.0), fetch(7, 21140, 16.0)
    assert len(a) > 0 and np.array_equal(a, b)


def test_it_reports_the_optics_of_its_sky(tmp_path):
    d = SkyDevice(catalog={"cache_dir": str(tmp_path)}, plate_scale=2.0, discovery_port=None)
    d.start()
    try:
        devices = {x.type: x for x in configured_devices(d.address)}
        optics = read_optics(devices["Telescope"], devices["Camera"])
    finally:
        d.stop()
    assert optics["plate_scale"] == pytest.approx(2.0, rel=1e-4)


def test_a_held_sky_does_not_move(tmp_path):
    clock = _held("2026-11-07T13:30:00")
    assert clock() == datetime(2026, 11, 7, 13, 30, tzinfo=UTC)
    held = SkyDevice(catalog={"cache_dir": str(tmp_path)}, clock=clock, discovery_port=None)
    live = SkyDevice(catalog={"cache_dir": str(tmp_path)}, discovery_port=None)
    held.start(), live.start()
    try:
        first = held.mount.SiderealTime, live.mount.SiderealTime
        time.sleep(0.05)
        assert held.mount.SiderealTime == first[0]
        assert live.mount.SiderealTime != first[1]  # the control: a live sky moves
    finally:
        held.stop(), live.stop()


@pytest.mark.parametrize("focuser", [True, False])
def test_a_focuser_is_served_off_focus_or_not_at_all(tmp_path, focuser):
    d = SkyDevice(
        catalog={"cache_dir": str(tmp_path)},
        focus_error=3000,
        focuser=focuser,
        discovery_port=None,
    ).start()
    try:
        types = {x.type for x in configured_devices(d.address)}
    finally:
        d.stop()
    assert ("Focuser" in types) is focuser
    assert d.focuser.Position == 23000  # true focus is 20000
