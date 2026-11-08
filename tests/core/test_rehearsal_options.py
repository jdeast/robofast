"""The hardware rehearsal's opt-in options (robofast.testing.rehearsal;
docs/demo.md): ``--kiwispec URL`` (an external KiwiSpec service) and
``--catalog-guide`` (the simulated FAU guide cameras show the catalog's
star field around each target). Simulators and synthetic catalogs only."""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import httpx
import pytest

from robofast.guider.service import GuiderService, GuiderServiceConfig
from robofast.testing import rehearsal as reh
from robofast.testing.sky_fixture import synthetic_fields
from robofast.testing.skysim import GuiderSky

#: A field (alpha And's) and its target's G.
FIELD = (2.0965, 29.0904, 2.0)


class _Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str]]] = []

    def _reply(self) -> None:
        self.seen.append((self.command, self.path))
        body = json.dumps({"state": "idle"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *a) -> None:
        pass


@pytest.fixture
def fake_kiwispec():
    _Recorder.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Recorder.seen
    finally:
        srv.shutdown()


def _args(*extra: str):
    import argparse

    ns = argparse.Namespace(
        i_know_the_enclosure_is_closed=True,
        telescope=["t1=127.0.0.1:11121/0"],
        camera=["t1=127.0.0.1:11122/0"],
        focuser=[],
        rotator=[],
        end_action="leave",
        az_window=[],
        duration=3600.0,
        exptime=60.0,
        images=1,
        image_exptime=1.0,
        spectra=None,
        ignore_twilight=False,
        console_port=-1,
        kiwispec=None,
        catalog_guide=False,
        catalog_source=None,
        catalog_dir=None,
    )
    for e in extra:
        k, _, v = e.partition("=")
        setattr(ns, k, True if v == "" else v)
    return ns


def test_the_options_are_off_by_default():
    s = reh.settings_from_args(_args())
    assert s.kiwispec_url is None and s.catalog_guide is None


def test_the_options_from_the_command_line(tmp_path):
    s = reh.settings_from_args(
        _args(
            "kiwispec=http://127.0.0.1:8130",
            "catalog_guide",
            "catalog_source=ucac4",
            f"catalog_dir={tmp_path}",
        )
    )
    assert s.kiwispec_url == "http://127.0.0.1:8130"
    assert s.catalog_guide == {"source": "ucac4", "cache_dir": str(tmp_path)}
    with pytest.raises(reh.RehearsalRefused, match="need --catalog-guide"):
        reh.settings_from_args(_args("catalog_source=ucac4"))


def test_an_external_kiwispec_is_only_asked_its_status(fake_kiwispec):
    url, seen = fake_kiwispec
    assert reh.check_kiwispec(url)["state"] == "idle"
    assert seen == [("GET", "/status")]


def test_an_external_kiwispec_that_does_not_answer_is_refused():
    with pytest.raises(reh.RehearsalRefused, match="GET /status failed"):
        reh.check_kiwispec("http://127.0.0.1:9", timeout=2.0)


async def test_the_rehearsal_uses_the_external_kiwispec(tmp_path, fake_kiwispec):
    url, _ = fake_kiwispec
    night = reh.Rehearsal(tmp_path, reh.settings_from_args(_args(f"kiwispec={url}/")))
    await night._start_spectrograph()
    assert night.kiwi_sim is None and not night.services  # nothing simulated started
    assert night._spectrograph_url() == url
    await night.close()


def test_warm_catalog_refuses_without_a_catalog(tmp_path):
    cfg = {"cache_dir": str(tmp_path), "fetch": False, "bright": False}
    with pytest.raises(reh.RehearsalRefused, match="cannot answer around HD 358"):
        reh.warm_catalog(cfg, [{"name": "HD 358", "ra": FIELD[0], "dec": FIELD[1]}], note=print)


def test_warm_catalog_on_a_cached_catalog(tmp_path):
    synthetic_fields(tmp_path, [FIELD])
    cfg = {"cache_dir": str(tmp_path), "fetch": False, "bright": False}
    notes: list[str] = []
    assert reh.warm_catalog(cfg, [{"name": "a", "ra": FIELD[0], "dec": FIELD[1]}], notes.append)
    assert "stars" in notes[0]


def test_the_catalog_guider_config_is_the_real_faus(tmp_path):
    settings = reh.settings_from_args(_args("catalog_guide"))
    settings.catalog_guide = {"cache_dir": str(tmp_path)}
    night = reh.Rehearsal(tmp_path, settings)
    cfg = night._guider_config(
        "t1",
        {
            "guide": {"acq_roi": [1, 2, 3, 4], "plate_scale": 0.5},
            "sim": {"star": [1.0, 2.0], "fiber": {"x": 1.0, "y": 2.0, "radius": 3.0}},
        },
    )
    fau = reh.fau_example("t1")
    assert cfg["guide"]["plate_scale"] == fau["plate_scale"] == 0.265
    assert "acq_roi" not in cfg["guide"]  # full frame, as the real FAU
    assert cfg["sim"]["sensor"] == list(reh.FAU_SENSOR)
    assert [cfg["sim"]["fiber"]["x"], cfg["sim"]["fiber"]["y"]] == fau["fiber"]
    assert cfg["sim"]["sky"]["catalog"] == {"cache_dir": str(tmp_path), "fetch": False}
    # Off: the config is untouched.
    night.r.catalog_guide = None
    assert night._guider_config("t1", {"x": 1}) == {"x": 1}


def test_guider_sky_puts_the_target_at_the_pointing(tmp_path):
    synthetic_fields(tmp_path, [FIELD])
    sky = GuiderSky.from_config(
        {
            "catalog": {"cache_dir": str(tmp_path), "fetch": False, "bright": False},
            "plate_scale": 0.265,
            "pointing_offset": [6, -4],
            "match_radius": 90,
        },
        sensor=(1936, 1216),
        fiber=(949.0, 617.0),
    )
    # Commanded 30" east of the star: it is matched, put at the pointing.
    main, others, info = sky.field(FIELD[0] + 0.0096, FIELD[1], datetime.now(UTC), 2e6)
    assert main == (955.0, 613.0, 2e6)
    assert info["target_in_catalog"] and info["target_g"] == pytest.approx(2.0)
    assert 5 < len(others) == info["field_stars"]
    assert all(f < 2e6 for _, _, f in others)  # all fainter than the G 2 target
    assert all(-40 < x < 1976 and -40 < y < 1256 for x, y, _ in others)
    # Nowhere near a catalog star: the target is still there, assumed G.
    sky = dataclasses.replace(sky, match_radius=0.01)
    main, _, info = sky.field(FIELD[0] + 0.05, FIELD[1], datetime.now(UTC), 2e6)
    assert main[:2] == (955.0, 613.0) and not info["target_in_catalog"]


async def test_the_guider_service_points_its_sky_at_each_target(tmp_path):
    synthetic_fields(tmp_path / "cat", [FIELD])
    cfg = GuiderServiceConfig.from_dict(
        {
            "name": "t1-guider",
            "scope": "t1",
            "listen": "127.0.0.1:0",
            "port": "fau",
            "camera": {"kind": "sim"},
            "tiptilt": {"kind": "sim", "limits": [[-200, 200], [-200, 200]]},
            "guide": {"exposure": 0.05, "acq_exposure": 0.05, "plate_scale": 0.265},
            "sim": {
                "sensor": [1936, 1216],
                "star": [955.0, 613.0],
                "fiber": {"x": 949.0, "y": 617.0, "radius": 3.0, "depth": 0.0},
                "tt_matrix": [[0.08, 0.03], [-0.03, 0.08]],
                "sky": {
                    "catalog": {"cache_dir": str(tmp_path / "cat"), "fetch": False},
                    "plate_scale": 0.265,
                    "match_radius": 90,
                },
            },
            "calibrations": {
                "tiptilt": {"matrix": [[0.08, 0.03], [-0.03, 0.08]], "units": "steps"}
            },
        }
    )
    svc = await GuiderService(cfg).start()
    try:
        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=30) as http:
            st = (await http.get("/status")).json()
            assert st["sim_sky"] is None and svc.parts.world.others == []
            r = await http.post(
                "/start", json={"target": [949.0, 617.0], "target_radec": list(FIELD[:2])}
            )
            assert r.status_code == 200, r.text
            st = (await http.get("/status")).json()
            assert st["sim_sky"]["target_in_catalog"] and st["sim_sky"]["field_stars"] > 5
            assert svc.parts.world.star == (955.0, 613.0)
            assert len(svc.parts.world.others) == st["sim_sky"]["field_stars"]
            await http.post("/stop", json={})
            # A target the catalog does not cover: a lone star, logged.
            r = await http.post("/start", json={"target": [949.0, 617.0], "target_radec": [180, 0]})
            assert r.status_code == 200, r.text
            st = (await http.get("/status")).json()
            assert "error" in st["sim_sky"] and svc.parts.world.others == []
            await http.post("/stop", json={})
    finally:
        await svc.stop()


def test_cooling_from_the_command_line(tmp_path):
    """``--cooling NAME=C`` or ``NAME=FILE`` (docs/cooling.md)."""
    assert reh.settings_from_args(_args()).cooling == {}
    ns = _args()
    entry = {"setpoint": 5, "tolerance": 0.75, "settle_timeout": 600}
    (tmp_path / "t1.json").write_text(json.dumps(entry))
    ns.cooling = ["t1=-50"]
    assert reh.settings_from_args(ns).cooling == {"t1": {"setpoint": -50.0}}
    ns.cooling = [f"t1={tmp_path / 't1.json'}"]
    assert reh.settings_from_args(ns).cooling == {"t1": entry}
    for bad, why in (
        (["t2=-50"], "not a real telescope"),
        (["t1=-50", "t1=-40"], "given twice"),
        (["t1=-500"], "outside"),
        ([f"t1={tmp_path / 'missing.json'}"], "No such file"),
        (["t1"], "expected NAME=C"),
    ):
        ns.cooling = bad
        with pytest.raises(reh.RehearsalRefused, match=why):
            reh.settings_from_args(ns)
