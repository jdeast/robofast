"""Target vetting from the catalog, and acquisition a twin cannot fool.

nu Draconis is a real equal pair (G 4.79 and 4.84, 62 arcsec apart; WDS
STFA 35): both fall in MINERVA's FAU field. Observing the fainter one, nu2,
"take the brightest star" acquisition puts nu1 on the fiber; the catalog
acquisition (plate solve the acquisition frame, take the star at nu2's
position) puts nu2 there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from robofast_core.catalog import catalog_at
from robofast_core.dispatch import dispatch_from_config
from robofast_core.photometry import PortPhotometry, SkyConditions
from robofast_core.request_model import RequestGroup
from robofast_core.vetting import PortGeometry, Vetter, VettingConfig, vet

DATA = Path(__file__).parent / "data" / "catalog"
WHEN = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
HD185144 = (293.2035, 69.6611)
FAU = PortGeometry(
    shape=(1216, 1936),
    plate_scale=0.265,
    fiber=(948.0, 626.0),
    max_star_distance=400.0,
    capture_radius=8.0,
    exposure=2.0,
    photometry=PortPhotometry(zero_point=20.0, gain=1.5, read_noise=4.0, saturation=65535),
)


@pytest.fixture(scope="module")
def catalog():
    return catalog_at(DATA)


@pytest.fixture(scope="module")
def nu(catalog):
    s = catalog.cone(263.056, 55.179, 0.02, mag_limit=6.0)
    assert len(s) == 2
    return s  # nu1 (brighter), nu2


def test_twin_is_flagged(catalog, nu):
    r = vet(catalog, "nu2 Dra", nu.ra[1], nu.dec[1], FAU, port_name="fau", when=WHEN)
    codes = r.codes()
    assert "not_brightest" in codes  # nu1 is in the acquisition box
    assert not r.unambiguous
    assert r.match is not None and r.match["g"] == pytest.approx(4.84, abs=0.01)
    # nu1 is brighter by only 0.05 mag: just as ambiguous.
    r1 = vet(catalog, "nu1 Dra", nu.ra[0], nu.dec[0], FAU, when=WHEN)
    assert "not_brightest" in r1.codes() and not r1.unambiguous


def test_close_twin_within_companion_radius(catalog, nu):
    cfg = VettingConfig(companion_radius=70.0)
    r = vet(catalog, "nu2 Dra", nu.ra[1], nu.dec[1], FAU, cfg, when=WHEN)
    twin = [f for f in r.flags if f.code == "twin"]
    assert twin and twin[0].detail["separation"] == pytest.approx(62, abs=2)
    assert "wds_companion" in r.codes()  # STFA 35, found from the secondary


def test_clean_field_is_unambiguous(catalog):
    # sigma Draconis (G 4.4) dominates its field.
    s = catalog.cone(*HD185144, 0.1, epoch=WHEN).take([0])  # pm 1.8 arcsec/yr
    fau = PortGeometry(**{**FAU.__dict__, "exposure": 0.05})
    r = vet(catalog, "HD 185144", s.ra[0], s.dec[0], fau, when=WHEN)
    assert r.unambiguous, r.codes()
    assert "not_brightest" not in r.codes()


def test_saturation_faintness_and_few_stars(catalog, nu):
    long = PortGeometry(**{**FAU.__dict__, "exposure": 60.0})
    assert "saturates" in vet(catalog, "nu2", nu.ra[1], nu.dec[1], long).codes()
    # A G ~ 15.6 star in a 0.01 s exposure: too faint, and no stars to solve.
    s = catalog.cone(263.056, 55.179, 0.12)
    faint = s.take([int(np.argmax(s.g))])
    short = PortGeometry(**{**FAU.__dict__, "exposure": 0.01})
    codes = vet(catalog, "faint", faint.ra[0], faint.dec[0], short).codes()
    assert "too_faint" in codes and "few_stars" in codes


def test_high_proper_motion_and_not_in_catalog(catalog):
    from robofast_core.catalog import TILE_DTYPE, Catalog, Stars

    rec = np.array(
        [(1, 10.0, 10.0, 3000.0, 0.0, 9.0, 0.8), (2, 10.02, 10.0, 0.0, 0.0, 12.0, 0.8),
         (3, 10.0, 10.02, 0.0, 0.0, 12.5, 0.8), (4, 9.98, 10.0, 0.0, 0.0, 13.0, 0.8)],
        dtype=TILE_DTYPE,
    )  # fmt: skip
    one = Stars.from_records(rec)

    class Tiles:
        mag_limit = 16.0

        def cone(self, ra, dec, r):
            return one.cone(ra, dec, r)

    c = Catalog(Tiles(), bright=None)  # type: ignore[arg-type]
    # Coordinates at J2016 (the catalog epoch): found, and flagged.
    r = vet(c, "fast", 10.0, 10.0, FAU, pm=(3000.0, 0.0), epoch=2016.0, when=2016.0)
    assert "high_proper_motion" in r.codes()
    # Off by an arcminute: not in the catalog.
    assert "not_in_catalog" in vet(c, "x", 10.0, 10.02 + 1 / 60, FAU).codes()


def test_fiber_contamination(catalog):
    from robofast_core.catalog import TILE_DTYPE, Catalog, Stars

    rec = np.array(
        [(1, 10.0, 10.0, 0, 0, 9.0, 0.8), (2, 10.0, 10.0 + 1.5 / 3600, 0, 0, 11.0, 0.8)],
        dtype=TILE_DTYPE,
    )
    pair = Stars.from_records(rec)

    class Tiles:
        mag_limit = 16.0

        def cone(self, ra, dec, r):
            return pair.cone(ra, dec, r)

    r = vet(Catalog(Tiles(), bright=None), "t", 10.0, 10.0, FAU)  # type: ignore[arg-type]
    assert {"fiber_contamination", "blend"} <= set(r.codes())


def test_offline_catalog_is_a_warning(tmp_path):
    r = vet(catalog_at(tmp_path), "t", 10.0, 10.0, FAU)
    assert r.codes() == ["catalog_unavailable"] and not r.rejected


def _group(name: str, ra: float, dec: float, gid: str = "g1") -> dict:
    from tests.core.test_dispatch import group

    g = group(gid, instrument="spectrograph", exptime=300)
    c = g["requests"][0]["configurations"][0]
    c["type"] = "SPECTRUM"
    c["target"] = {"type": "ICRS", "name": name, "ra": float(ra), "dec": float(dec)}
    return g


def _vetting_cfg(reject=()):
    return {
        "catalog": {"cache_dir": str(DATA), "fetch": False},
        "reject": list(reject),
        "ports": {
            "fau": {
                "shape": [1216, 1936],
                "plate_scale": 0.265,
                "fiber": [948, 626],
                "exposure": 2.0,
                "photometry": {"zero_point": 20.0, "gain": 1.5},
            }
        },
    }


def _plan(tmp_path, reject=()):
    return {
        "site": {"latitude": 31.680407, "longitude": -110.878977, "elevation": 2316},
        "requests": [],
        "too_dropbox": str(tmp_path / "drop"),
        "record": str(tmp_path / "record"),
        "telescopes": {"t1": {"instruments": ["spectrograph"]}},
        "vetting": _vetting_cfg(reject),
    }


async def test_dispatch_intake_warns_records_and_rejects(tmp_path, nu):
    d = dispatch_from_config(_plan(tmp_path), ["t1"], clock=lambda: WHEN)
    gid = await d.submit(RequestGroup.from_dict(_group("nu2 Dra", nu.ra[1], nu.dec[1])))
    assert "not_brightest" in [f["code"] for f in d.vetting[gid][0]["flags"]]
    assert d.acquisition(gid, "nu2 Dra") == {
        "unambiguous": False,
        "flags": [f["code"] for f in d.vetting[gid][0]["flags"]],
    }
    rec = "\n".join(p.read_text() for p in (tmp_path / "record").rglob("*.jsonl"))
    assert '"vetting"' in rec
    strict = dispatch_from_config(
        _plan(tmp_path / "s", reject=("not_brightest",)), ["t1"], clock=lambda: WHEN
    )
    with pytest.raises(ValueError, match="not_brightest"):
        await strict.submit(RequestGroup.from_dict(_group("nu2 Dra", nu.ra[1], nu.dec[1], "g2")))
    # Through the drop box: filed as rejected, with the reason.
    drop = tmp_path / "s" / "drop"
    (drop / "g3.json").write_text(json.dumps(_group("nu2 Dra", nu.ra[1], nu.dec[1], "g3")))
    assert await strict.poll_dropbox() == []
    assert "not_brightest" in (drop / "rejected" / "g3.json.error").read_text()


def test_vetter_from_config_file(tmp_path):
    p = tmp_path / "sky.json"
    p.write_text(json.dumps(_vetting_cfg()))
    plan = {**_plan(tmp_path), "vetting": "sky.json"}
    d = dispatch_from_config(plan, ["t1"], base=tmp_path)
    assert isinstance(d.vetter, Vetter) and "fau" in d.vetter.ports
    with pytest.raises(ValueError, match="unknown flags"):
        Vetter.from_config({**_vetting_cfg(), "reject": ["nope"]})


# -- acquisition -----------------------------------------------------------------------


def _twin_rig(catalog, nu, select: str):
    from robofast_core.astrometry import SolverConfig
    from robofast_core.guider.base import VirtualClock
    from robofast_core.guider.calibration import Calibration
    from robofast_core.guider.identify import catalog_identifier
    from robofast_core.guider.loop import GuideConfig, Guider
    from robofast_core.guider.sim import SimCamera, SimMount, SimWorld
    from robofast_core.guider.tiptilt import MountOffset
    from robofast_core.testing.skysim import SkyCamera, SkyScene, sky_world_stars
    from robofast_core.testing.starfield import Detector

    pa = 30.0
    cam = SkyCamera((1216, 1936), 0.265, pa=pa, detector=Detector(shape=(1216, 1936)))
    sky = SkyScene(catalog, cam, FAU.photometry, conditions=SkyConditions(), seeing=1.5)
    ra2, dec2 = float(nu.ra[1]), float(nu.dec[1])
    main, others = sky_world_stars(sky, ra2, dec2, WHEN)
    clock = VirtualClock()
    world = SimWorld(
        clock=clock,
        sensor=(1936, 1216),
        star=main[:2],
        flux=main[2],
        others=others,
        fwhm=1.5 / 0.265,
        sky=5.0,
        gain=1.5,
        bias=1000.0,
        read_noise=4.0,
        plate_scale=0.265,
        pa=pa,
        parity=1,
    )
    sm = SimMount(clock=clock, tau=0.2, delay=0.05)
    world.mount = sm
    m = MountOffset(sm, max_offset=120.0)
    m.clock = clock
    cal = Calibration("mount", "fau", world.mount_matrix.tolist())
    cfg = GuideConfig(
        target=(948.0, 626.0),
        acq_exposure=2.0,
        exposure=0.1,
        roi_size=64,
        capture_radius=3.0,
        coarse_settle=1.0,
        select=select,
        max_star_distance=600.0,
        scope="t2",
    )
    g = Guider(SimCamera(world), cfg, mount=m, mount_cal=cal, clock=clock)
    if select == "catalog":
        solver = SolverConfig(0.265, search_radius=0.05, min_matches=3)
        g.identify = catalog_identifier(catalog, solver, ra2, dec2, epoch=WHEN)
    return g, world, clock


async def _acquired(g, world, clock) -> tuple[float, float]:
    """Distance (px) of nu2 and of nu1 from the fiber once guiding."""
    g.start()
    await g.wait_state(("guiding", "failed"), 120)
    assert g.state == "guiding", g.reason
    t = clock.now()
    star2 = world.star_at(t)
    # nu1: the brightest of the others, moved like everything else.
    k = int(np.argmax([f for _, _, f in world.others]))
    star1 = np.asarray(world.others[k][:2]) + world.motion_at(t)
    fiber = np.asarray(g.config.target)
    await g.stop()
    return float(np.hypot(*(star2 - fiber))), float(np.hypot(*(star1 - fiber)))


async def test_brightest_star_acquisition_takes_the_wrong_twin(catalog, nu):
    g, world, clock = _twin_rig(catalog, nu, "brightest")
    d2, d1 = await _acquired(g, world, clock)
    assert d1 < 5.0 and d2 > 100.0  # nu1 is on the fiber: the wrong star


async def test_catalog_acquisition_takes_the_right_twin(catalog, nu):
    g, world, clock = _twin_rig(catalog, nu, "catalog")
    d2, d1 = await _acquired(g, world, clock)
    assert d2 < 5.0 and d1 > 100.0  # nu2, the target, is on the fiber
    assert g.identified is not None


async def test_catalog_acquisition_without_a_solution(catalog, nu):
    g, _, _ = _twin_rig(catalog, nu, "catalog")
    g.identify = lambda frame: None  # e.g. clouds: no plate solution
    g.config.max_reacquire = 0
    g.start()
    await g.wait_state(("failed",), 60)
    assert "not identified" in g.reason
    # Vetting says the field is unambiguous: the brightest star is taken.
    g2, world2, clock2 = _twin_rig(catalog, nu, "catalog")
    g2.identify = lambda frame: None
    g2.unambiguous = True
    _, d1 = await _acquired(g2, world2, clock2)
    assert d1 < 5.0  # (here that is wrong -- which is why vetting must say so)
