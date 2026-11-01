"""Every file in ``sites/minerva`` loads through the loader that reads it in
production, and the values copied from minerva-control's ``config/*.ini``
agree with the reference catalog ``sites/minerva/instruments.json`` (see
``sites/minerva/README.md``), so a typo or a schema change fails here."""

from __future__ import annotations

import contextlib
import json
import math
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from robofast_core import config as enclosure_config
from robofast_core.autofocus import AutofocusSetup
from robofast_core.autofocus_plan import TriggerConfig
from robofast_core.devices.kiwispec_service import KiwiSpecConfig
from robofast_core.fits_writer import Optics
from robofast_core.guider.base import HardwareNotAllowed, RealClock
from robofast_core.guider.calibration import Calibration, from_sx_legacy, sky_matrix
from robofast_core.guider.loop import GuideConfig
from robofast_core.guider.service import GuiderServiceConfig, build_parts
from robofast_core.imaging import ImagingConfig
from robofast_core.plan import plan_from_config
from robofast_core.request_model import load_request_groups
from robofast_core.sequencer_service import build, load_plan
from robofast_core.supervisor_service import build as build_supervisor
from robofast_core.timeline import Timeline
from robofast_core.weather_bridge import build_sources, parse_config
from robofast_core.weather_bridge import load_config as load_weather

SITE = Path(__file__).resolve().parents[2] / "sites" / "minerva"
ARCSEC_PER_RAD = 206264.806

#: Every file and the test that loads it (a new file must be added here).
COVERED = {
    "README.md",
    "aqawan1.json",
    "autofocus.example.json",
    "calibration.example.json",
    "console.example.json",
    "dispatch.example.json",
    "guider_t1.example.json",
    "guider_t2.example.json",
    "instruments.json",
    "kiwispec.example.json",
    "request_api.example.json",
    "requests.example.json",
    "sky.example.json",
    "targets.example.json",
    "too.example.json",
    "weather.yaml",
}


def _json(name: str) -> dict:
    return json.loads((SITE / name).read_text())


@pytest.fixture(scope="module")
def catalog() -> dict:
    return _json("instruments.json")


def test_every_site_file_is_covered():
    assert {p.name for p in SITE.iterdir()} == COVERED


def test_no_addresses_or_credentials():
    """Only loopback placeholders; no LAN addresses, hostnames of site
    devices, or credentials (the weather pages' public URLs excepted)."""
    for path in SITE.iterdir():
        text = path.read_text()
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            assert ip == "127.0.0.1", f"{path.name}: {ip}"
        assert not re.search(r"(?i)\b(password|passwd|community)\s*[:=]", text), path.name


# -- each file through its loader ---------------------------------------------------------


async def test_enclosure_config_builds_both_services():
    config = enclosure_config.load_config(SITE / "aqawan1.json")
    assert sorted(enclosure_config.telescopes(config)) == ["t1", "t2"]
    build_supervisor(config, Timeline())
    seq, remote = build(config, Timeline(), base=SITE)
    try:
        assert sorted(seq.units) == ["t1", "t2"]
    finally:
        await remote.aclose()


async def test_autofocus_example_merges_into_the_enclosure_config(tmp_path):
    config = enclosure_config.load_config(SITE / "aqawan1.json")
    af = _json("autofocus.example.json")
    for name, entry in af["telescopes"].items():
        config["telescopes"][name].update(entry)
        # the store paths are absolute (/var/lib/robofast): keep the test local
        config["telescopes"][name]["autofocus"]["store"] = str(tmp_path / f"focus_{name}.json")
    config["autofocus_triggers"] = af["autofocus_triggers"]
    seq, remote = build(config, Timeline(), base=tmp_path)
    try:
        assert all(seq.units[n].autofocus is not None for n in ("t1", "t2"))
    finally:
        await remote.aclose()
    TriggerConfig.from_dict(af["autofocus_triggers"])


async def test_plans_load(tmp_path):
    for name in ("dispatch.example.json", "requests.example.json"):
        shutil.copy(SITE / name, tmp_path / name)
    plan = load_plan(tmp_path / "dispatch.example.json", ["t1", "t2"])
    assert plan is not None
    plan = load_plan(SITE / "calibration.example.json", ["t1", "t2"])
    first = await plan.peek("t1")
    assert first.kind == "spectrograph_calibration" and first.calibration[0]["kind"] == "bias"
    assert (await plan.peek("t2")).name == "bias"
    load_plan(SITE / "targets.example.json", ["t1", "t2"])
    plan_from_config(_json("aqawan1.json")["plan"], ["t1", "t2"])
    load_request_groups(SITE / "requests.example.json")
    load_request_groups([_json("too.example.json")])


def test_request_api_config_loads(tmp_path):
    from robofast_core.request_api import build_enclosures, load_api_config

    for name in ("aqawan1.json", "dispatch.example.json", "requests.example.json"):
        shutil.copy(SITE / name, tmp_path / name)
    cfg = _json("request_api.example.json")
    cfg["tokens"] = str(tmp_path / "tokens.json")
    cfg["audit_log"] = str(tmp_path / "audit.jsonl")
    (tmp_path / "api.json").write_text(json.dumps(cfg))
    _, spec, base = load_api_config(tmp_path / "api.json")
    assert build_enclosures(spec, base)["aqawan1"].telescopes == ["t1", "t2"]


def test_console_config_loads(tmp_path):
    from robofast_core.console.server import build_enclosures, load_console_config

    for name in ("aqawan1.json", "dispatch.example.json", "requests.example.json"):
        shutil.copy(SITE / name, tmp_path / name)
    cfg = _json("console.example.json")
    for key in ("tokens", "audit_log", "acks"):
        cfg[key] = str(tmp_path / f"{key}.json")
    (tmp_path / "console.json").write_text(json.dumps(cfg))
    config, spec, base = load_console_config(tmp_path / "console.json")
    enc = build_enclosures(spec, base)["aqawan1"]
    assert enc.telescopes == ["t1", "t2"] and enc.requests is not None
    assert enc.spectrograph_url == "http://127.0.0.1:8130"
    assert enc.weather_url == "http://127.0.0.1:11111"  # the safety monitor's server
    assert config.listen.startswith("127.0.0.1:")


def test_weather_config_loads():
    cfg = parse_config(load_weather(SITE / "weather.yaml"))
    assert build_sources(cfg)


def test_kiwispec_config_loads():
    cfg = KiwiSpecConfig.from_dict(_json("kiwispec.example.json"))
    assert not cfg.allow_hardware
    # Moved by name; the live server's positions are authoritative (README).
    assert cfg.stage_positions == {}


@pytest.mark.parametrize("tel", ["t1", "t2"])
def test_guider_config_loads_locked(tel):
    raw = _json(f"guider_{tel}.example.json")
    cfg = GuiderServiceConfig.from_dict(raw)
    assert not cfg.allow_hardware and cfg.scope == tel and cfg.tiptilt is None
    GuideConfig.from_dict({"scope": cfg.scope, **cfg.guide})
    for role, seed in raw["_legacy_seed_calibrations"].items():
        Calibration.from_dict({"device": role, "port": cfg.port, **seed})
    # The hardware gate refuses the real parts (at construction or at first
    # use); each part's spec is complete (no KeyError or ValueError first).
    with pytest.raises(HardwareNotAllowed):
        build_parts(cfg, allowed=False, clock=RealClock())
    parts = {"camera": raw["camera"], "mount": raw["mount"]}
    if "_tiptilt_uncommissioned" in raw:  # the AO block, once renamed for commissioning
        parts["tiptilt"] = raw["_tiptilt_uncommissioned"]
    sim = {"camera": {"kind": "sim"}, "mount": None, "tiptilt": None}
    for key, spec in parts.items():
        one = GuiderServiceConfig.from_dict({**raw, **sim, key: spec})
        with contextlib.suppress(HardwareNotAllowed):
            build_parts(one, allowed=False, clock=RealClock())
    assert (
        build_parts(
            GuiderServiceConfig.from_dict({**raw, **sim}), allowed=False, clock=RealClock()
        ).backlight
        is not None
    )


# -- the copies agree with instruments.json ------------------------------------------------


def test_catalog_is_self_consistent(catalog):
    for name, t in catalog["telescopes"].items():
        assert name in catalog["enclosures"][t["enclosure"]]["telescopes"]
        assert t["enabled"] == (name in ("t1", "t2"))
        assert sorted(t["ports"].values()) == [1, 2]
        assert t["imager"]["port"] == t["ports"]["imager"]
        assert t["fau"]["port"] == t["ports"]["fau"]
        assert set(t["rotator_offset"]) == {"1", "2"}
        for cam in ("imager", "fau"):
            c = t[cam]
            # pixel size and plate scale agree through the focal length (0.5%)
            derived = c["pixel_size"] * 1e-3 / t["focal_length"] * ARCSEC_PER_RAD
            assert derived == pytest.approx(c["plate_scale"], rel=5e-3), (name, cam)
            nx, ny = c["detector"]
            for x, y in [c.get("fiber", (0, 0)), c.get("center", (0, 0))]:
                assert 0 <= x < nx and 0 <= y < ny
        f = t["focus"]
        for port in ("1", "2"):
            assert 0 < f["min"][port] < f["default"][port] <= f["max"][port]
        Optics.from_dict(
            {k: t["imager"][k] for k in ("plate_scale", "position_angle", "flip")}
            | {"aperture": t["aperture"], "focal_length": t["focal_length"]}
        )


def test_site_agrees(catalog):
    site = catalog["site"]
    weather = load_weather(SITE / "weather.yaml")["site"]
    for other in (
        _json("aqawan1.json")["site"],
        _json("dispatch.example.json")["site"],
        weather,
    ):
        for key in ("latitude", "longitude", "elevation"):
            assert other[key] == site[key], key


def test_enclosure_optics_agree(catalog):
    images = ImagingConfig.from_dict(_json("aqawan1.json")["images"])
    for name, optics in images.telescopes.items():
        t = catalog["telescopes"][name]
        assert t["enabled"]
        assert optics == {
            "plate_scale": t["imager"]["plate_scale"],
            "position_angle": t["imager"]["position_angle"],
            "flip": t["imager"]["flip"],
            "aperture": t["aperture"],
            "focal_length": t["focal_length"],
        }


def test_autofocus_agrees(catalog):
    af = _json("autofocus.example.json")
    for name, entry in af["telescopes"].items():
        t = catalog["telescopes"][name]
        setup = AutofocusSetup.from_dict(entry["autofocus"])
        ports = {v: k for k, v in t["ports"].items()}  # number -> name
        for number in ("1", "2"):
            port = ports[int(number)]
            cfg = setup.config_for(port, None)
            cam = t[port]
            assert cfg.optics.pixel_scale == cam["plate_scale"]
            assert cfg.optics.pixel_size == cam["pixel_size"]
            assert cfg.optics.f_ratio == pytest.approx(t["focal_length"] / t["aperture"], abs=0.02)
            assert cfg.min_position == t["focus"]["min"][number]
            assert cfg.max_position == t["focus"]["max"][number]
        model = t["focus"]["model"]
        assert setup.model.terms == {
            "{tel}.focuser.temperature": model["C0"],
            "sin:{tel}.tel.alt": model["C2"],
        }
        assert setup.model.reference == {
            "{tel}.focuser.temperature": model["T0"],
            "sin:{tel}.tel.alt": model["G0"],
        }


def test_dispatch_limits_agree(catalog):
    d = _json("dispatch.example.json")
    for name, limits in d["telescopes"].items():
        t = catalog["telescopes"][name]
        assert t["enabled"]
        assert (limits["min_alt"], limits["max_alt"]) == (t["min_alt"], t["max_alt"])
        assert limits["filters"] == list(t["imager"]["filters"])


@pytest.mark.parametrize("tel", ["t1", "t2"])
def test_guider_agrees(catalog, tel):
    raw = _json(f"guider_{tel}.example.json")
    fau = catalog["telescopes"][tel]["fau"]
    assert raw["guide"]["target"] == fau["fiber"]
    assert raw["guide"]["plate_scale"] == fau["plate_scale"]
    seeds = raw["_legacy_seed_calibrations"]
    expect = np.linalg.inv(sky_matrix(fau["plate_scale"], fau["rotangle"], 1))
    np.testing.assert_allclose(seeds["mount"]["matrix"], expect, atol=1e-6)
    tt = catalog["telescopes"][tel]["tiptilt"]
    if tt is None:
        assert "tiptilt" not in seeds
        return
    assert not tt["enabled"]
    seed = from_sx_legacy(tt["arcsec_per_step"], tt["rotoffset"], fau["plate_scale"])
    np.testing.assert_allclose(seeds["tiptilt"]["matrix"], seed.matrix, atol=1e-6)
    assert raw["_tiptilt_uncommissioned"]["limits"] == tt["limits"]
    assert raw["_tiptilt_uncommissioned"]["port"] == tt["serial_port"]


def test_kiwispec_agrees():
    deployed = KiwiSpecConfig.from_dict(
        json.loads((SITE.parents[1] / "deploy" / "kiwispec.example.json").read_text())
    )
    site = KiwiSpecConfig.from_dict(_json("kiwispec.example.json"))
    assert deployed.stage_positions == site.stage_positions
    assert math.isclose(site.heater_setpoint, 55.0)


def test_sky_config_loads():
    """sky.example.json: the vetting config (docs/sky.md), with T2's measured
    FAU zero point and the fiber's 98 %."""
    from robofast_core.vetting import Vetter

    cfg = _json("sky.example.json")
    cfg["catalog"] = {"cache_dir": str(SITE.parents[1] / "tests/core/data/catalog"), "fetch": False}
    v = Vetter.from_config(cfg)
    fau2 = v.port("t2", "fau")
    assert fau2 is not None and fau2.photometry.zp == 19.70
    assert fau2.fiber == tuple(_json("guider_t2.example.json")["guide"]["target"])
    assert v.port("t1", "fau").fiber == tuple(_json("guider_t1.example.json")["guide"]["target"])
    est = v.port("t1", "fau").photometry
    # The FAU sees 2 %; the fiber the other 98 %: 2.5 log10(49) mag brighter.
    fiber = est.with_fraction("beamsplitter", 0.98)
    assert fiber.zp - est.zp == pytest.approx(4.225, abs=1e-3)
    # The chain's estimate agrees with T2's measured zero point (at ~1 e-/ADU).
    assert abs(est.zp_estimated - 19.70) < 0.5
