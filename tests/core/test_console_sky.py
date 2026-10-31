"""The console's sky view (docs/console.md, "Sky"): telescope pointings,
the targets with altitude and airmass now and later, the Sun and the Moon,
the limits."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from robofast_core import sky
from robofast_core.console.skyview import sky_for

from . import test_console as _console
from .test_console import snapshot_doc

make_console = _console.make_console


async def test_the_sky_endpoint(make_console):
    rig = await make_console()
    (rig.tmp / "seq-status.json").write_text(json.dumps(snapshot_doc()))
    assert (await rig.client.get("/console/api/sky")).status_code == 401
    await rig.login("watcher")
    r = await rig.client.get("/console/api/sky")
    assert r.status_code == 200, r.text
    e = r.json()["enclosures"]["aqawan1"]
    assert e["site"]["latitude"] == pytest.approx(31.68, abs=0.01)
    t1 = e["telescopes"]["t1"]
    assert (t1["alt"], t1["az"]) == (61.5, 140.25)
    assert (t1["min_alt"], t1["max_alt"]) == (21.0, 83.0)
    assert e["telescopes"]["t2"]["alt"] is None
    assert -90 <= e["sun"]["alt"] <= 90 and 0 <= e["moon"]["illumination"] <= 1
    names = [t["name"] for t in e["targets"]]
    assert names, e
    for t in e["targets"]:
        assert 0 <= t["az"] < 360 and len(t["later"]) == 2
        if t["alt"] > 2:
            assert t["airmass"] == pytest.approx(sky.airmass(t["alt"]), rel=1e-3)
    # Cached: the same document within sky_cache.
    assert (await rig.client.get("/console/api/sky")).json() == r.json()


def test_altitudes_agree_with_the_scheduler(tmp_path):
    """The table's altitude is the scheduler's (robofast_core.sky)."""
    from robofast_core.console.aggregate import ConsoleEnclosure
    from tests.core.test_console import write_site

    write_site(tmp_path)
    enc = ConsoleEnclosure.from_spec("a", "aqawan1.json", tmp_path)
    when = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    doc = sky_for(enc, {}, {}, when)
    assert doc["targets"], "the plan's requests are the targets"
    site = enc.requests.view().site
    t = doc["targets"][0]
    expect = sky.altitudes(site, [t["ra_hours"]], [t["dec_deg"]], [[when]])[0, 0]
    assert t["alt"] == pytest.approx(expect, abs=0.02)
    # Current first: a telescope on it marks it.
    cid = t["configurations"][0]
    doc = sky_for(enc, {}, {"t2": {"configuration": cid}}, when)
    assert doc["targets"][0]["telescopes"] == ["t2"]


def test_without_a_site_there_are_no_targets(tmp_path):
    from robofast_core.console.aggregate import ConsoleEnclosure

    (tmp_path / "e.json").write_text(
        json.dumps({"name": "e", "telescopes": {"t1": {"telescope": "x:1", "camera": "x:1"}}})
    )
    enc = ConsoleEnclosure.from_spec("e", "e.json", tmp_path)
    doc = sky_for(enc, {"t1.tel.alt": {"value": 45.0}, "t1.tel.az": {"value": 10.0}}, {})
    assert doc["site"] is None and doc["targets"] == []
    assert doc["telescopes"]["t1"]["alt"] == 45.0


def test_the_page_has_the_sky():
    from robofast_core.console.page import render_page

    page = render_page("n")
    assert 'id="sky"' in page and "/console/api/sky" in page and "createElementNS" in page
