"""The hardware rehearsal (robofast.testing.rehearsal; docs/demo.md) on
fake mounts and cameras served over Alpaca on loopback *as if* they were the
real ones: separate servers the rehearsal only knows by address, with a
faster clock (test only). It covers the external-endpoint path, the mount
guard, the end action and the checks without hardware."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera, FakeDevice, FakeFocuser, FakeTelescope
from robofast.testing.mount_guard import Endpoint
from robofast.testing.rehearsal import (
    EndAction,
    RealTelescope,
    RehearsalSettings,
    check,
    rehearse,
    summary,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


class Mount(FakeTelescope):
    """A fake mount that can also slew to alt/az (the end action) and counts
    every Park and Unpark it is sent (the rehearsal must send none)."""

    CanSlewAltAzAsync = True

    def __init__(self) -> None:
        super().__init__(slew_time=0.5)
        self.parks = 0
        self.unparks_sent = 0

    def Park(self) -> None:
        self.parks += 1
        super().Park()

    def Unpark(self) -> None:
        self.unparks_sent += 1
        super().Unpark()

    def SlewToAltAzAsync(self, az: float, alt: float) -> None:
        if self._tracking:
            raise RuntimeError("SlewToAltAz while tracking")
        self.Altitude, self.Azimuth = alt, az
        self._slew_until = self._now() + self._slew_time


class Rotator(FakeDevice):
    def __init__(self) -> None:
        super().__init__()
        self.Position = 12.0
        self.MechanicalPosition = 30.0


def serve_real(name: str) -> tuple[Mount, FakeCamera, list[FakeAlpacaServer]]:
    mount = Mount()
    mount._parked = False  # as the operator leaves it: unparked
    mount_srv = FakeAlpacaServer(
        {"telescope": mount, "focuser": FakeFocuser(), "rotator": Rotator()}
    ).start()
    cam = FakeCamera(256, 256)
    cam.cooler_off_on_connect = True  # as MINERVA T1's Andor driver
    cam_srv = FakeAlpacaServer({"camera": cam}).start()
    return mount, cam, [mount_srv, cam_srv]


async def test_a_rehearsal_on_fake_real_hardware(tmp_path):
    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path) / "rehearsal"
    servers = []
    real, mounts, cams = {}, {}, {}
    for name in ("t1", "t2"):
        mount, cam, srvs = serve_real(name)
        servers += srvs
        mounts[name], cams[name] = mount, cam
        ep = Endpoint.parse(f"{srvs[0].address}/0")
        real[name] = RealTelescope(ep, Endpoint.parse(f"{srvs[1].address}/0"), ep, ep)
    settings = RehearsalSettings(
        real=real,
        end_action=EndAction.parse("altaz:60,180"),
        duration=3600.0,
        exptime=2.0,
        images=1,
        image_exptime=0.5,
        spectra=3,
        console_port=None,
        idle_poll=1.0,
        # Test only: a dark night, ten times faster than real time.
        start=datetime(2026, 11, 10, 4, 0, tzinfo=UTC),
        scale=10.0,
        # Each camera is kept cooled as the site config says (docs/cooling.md):
        # sites/minerva/aqawan1.json, T1 -50 C and T2 +5 C (legacy SETTEMP).
    )
    for cam in cams.values():
        cam.CoolerOn = False
        cam.Connected = False  # the first connection is robofast's
    try:
        outcome = await rehearse(out, settings)
    finally:
        for s in servers:
            s.stop()
    print(summary(outcome))
    problems = check(outcome)
    assert not problems, "\n".join(problems)
    for name, mount in mounts.items():
        # robofast's parks became "tracking off" at the guard; nothing else.
        assert mount.parks == 0 and mount.unparks_sent == 0, name
        assert not mount.Tracking
        assert (mount.Altitude, mount.Azimuth) == (60.0, 180.0)
        assert cams[name].exposures_started >= 2, name
    assert cams["t1"].CoolerOn and cams["t1"].SetCCDTemperature == -50.0
    assert cams["t2"].CoolerOn and cams["t2"].SetCCDTemperature == 5.0
    assert all(c == "COMPLETED" for c in outcome.statuses.values()), outcome.statuses
    assert len(outcome.slews) >= 4 and all(r["error_arcsec"] < 1 for r in outcome.slews)
    assert outcome.why_end == "every request done"


async def test_a_rehearsal_with_an_external_kiwispec_and_catalog_guiding(tmp_path):
    """``--kiwispec URL`` (here a KiwiSpec service on its simulator, started
    outside the rehearsal: it stands for the real one) and
    ``--catalog-guide`` (a synthetic catalog around every candidate target:
    the guiders acquire and guide on its star fields at the real FAU's
    geometry)."""
    from robofast.testing.rehearsal import Rehearsal, catalog
    from robofast.testing.rv_night import SimulatedNight
    from robofast.testing.sky_fixture import synthetic_fields

    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path) / "rehearsal-external"
    cat_dir = tmp_path / "catalog"
    synthetic_fields(cat_dir, [(t["ra"], t["dec"], 4.0) for t in catalog()])
    servers = []
    real, mounts = {}, {}
    for name in ("t1", "t2"):
        mount, _, srvs = serve_real(name)
        servers += srvs
        mounts[name] = mount
        ep = Endpoint.parse(f"{srvs[0].address}/0")
        real[name] = RealTelescope(ep, Endpoint.parse(f"{srvs[1].address}/0"), ep, ep)
    # The "external" KiwiSpec: the simulated night's own, on the same clock
    # file as the rehearsal (the rehearsal rewrites it with its clock).
    external = SimulatedNight(out)
    await external._start_spectrograph()
    url = external._spectrograph_url()
    settings = RehearsalSettings(
        real=real,
        end_action=EndAction.parse("leave"),
        duration=3600.0,
        exptime=2.0,
        images=1,
        image_exptime=0.5,
        spectra=2,
        console_port=None,
        idle_poll=1.0,
        start=datetime(2026, 11, 10, 4, 0, tzinfo=UTC),
        scale=10.0,
        kiwispec_url=url,
        catalog_guide={"cache_dir": str(cat_dir), "fetch": False, "bright": False},
    )
    night = Rehearsal(out, settings)
    try:
        await night.start()
        outcome = await night.run()
    finally:
        await night.close()
        await external.close()
        for s in servers:
            s.stop()
    print(summary(outcome))
    problems = check(outcome)
    assert not problems, "\n".join(problems)
    assert night.kiwi_sim is None and outcome.spectrograph_files == {}
    # The spectra were the external service's.
    assert len([f for f in external.kiwi_sim.files.values() if f.get("kind") != "bias"]) >= 2
    for name in ("t1", "t2"):
        cfg = json.loads((out / f"guider_{name}.json").read_text())
        assert cfg["flux_report"]["url"] == url and "sky" in cfg["sim"]
        log = (out / f"guider_{name}.log").read_text()
        assert "target_in_catalog': True" in log, name
    assert outcome.why_end == "every request done"
