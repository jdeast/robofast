"""The Zenodo registry (robofast_core/zenodo_assets.py).

Offline checks always run; the comparison with the live record runs only
with ``ROBOFAST_NETWORK_TESTS=1`` (run it after pinning a new version).
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from robofast_core import zenodo_assets
from robofast_core.catalog import BRIGHT_FILE
from robofast_core.testing.sky_fixture import FIXTURE_FILE

#: The files of each record the code reads.
NEEDED = {zenodo_assets.GAIA_EXTRACTS: {BRIGHT_FILE, FIXTURE_FILE}}


def test_every_record_read_is_expected():
    assert set(NEEDED) == set(zenodo_assets.EXPECTED)
    assert set(zenodo_assets.RECORDS) <= set(zenodo_assets.EXPECTED)
    assert set(zenodo_assets.PENDING) == set(zenodo_assets.EXPECTED) - set(zenodo_assets.RECORDS)


def test_unknown_record_or_file_raises():
    with pytest.raises(KeyError, match="nope"):
        zenodo_assets.record("nope")


@pytest.mark.parametrize("name", sorted(NEEDED))
def test_pinned_record_has_the_files_the_code_reads(name):
    if not zenodo_assets.available(name):
        pytest.skip(f"Zenodo record {name!r} is not pinned yet (TODO: pin after publishing)")
    rec = zenodo_assets.record(name)
    assert NEEDED[name] <= set(rec.files)
    for f in rec.files.values():
        assert f.size > 0 and len(f.md5) == 32
    pin = zenodo_assets.file_pin(name, BRIGHT_FILE)
    assert pin["url"] == f"https://zenodo.org/records/{rec.record_id}/files/{BRIGHT_FILE}"
    with pytest.raises(KeyError, match="not a file"):
        rec.file("nope")


@pytest.mark.skipif(
    os.environ.get("ROBOFAST_NETWORK_TESTS") != "1", reason="set ROBOFAST_NETWORK_TESTS=1"
)
@pytest.mark.parametrize("name", sorted(zenodo_assets.RECORDS))
def test_pins_match_zenodo(name):  # pragma: no cover - network
    rec = zenodo_assets.record(name)
    req = urllib.request.Request(rec.api_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        live = json.load(r)
    files = {f["key"]: (int(f["size"]), f["checksum"].removeprefix("md5:")) for f in live["files"]}
    assert files == {n: (f.size, f.md5) for n, f in rec.files.items()}
    assert int(live["conceptrecid"]) == rec.concept_record_id
