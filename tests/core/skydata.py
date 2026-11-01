"""Which test sky the catalog tests run on: real Gaia DR3 or synthetic.

``sky_dir()`` is a catalog cache directory (``catalog_at(sky_dir())``)
holding the five test fields (``robofast_core.testing.sky_fixture``):

* **real**: the Gaia DR3 fixture tiles and bright stars from the Zenodo
  record ``gaia_extracts``, once it is pinned in
  ``robofast_core/zenodo_assets.py`` -- downloaded once into the data
  directory (``$ROBOFAST_DATA_DIR``; CI caches it) and unpacked there;
* **synthetic** otherwise: ``tests/core/data/catalog``, a seeded random sky
  with hand-placed stars where the tests need them (no Gaia data; the
  repository is BSD).

``ROBOFAST_TEST_SKY=synthetic`` forces the synthetic sky,
``ROBOFAST_TEST_SKY=gaia`` requires the real one (an error if it cannot be
had). Both carry the real WDS lines (``wds.npy``: a US Government work).
Tests that are only meaningful on real Gaia data call ``require_gaia()``.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
from pathlib import Path

import pytest

from robofast_core import zenodo, zenodo_assets
from robofast_core.catalog import BRIGHT_FILE
from robofast_core.testing.sky_fixture import FIXTURE_FILE, unpack_tiles

SYNTHETIC = Path(__file__).parent / "data" / "catalog"
log = logging.getLogger(__name__)


@functools.cache
def _real() -> Path | None:
    want = os.environ.get("ROBOFAST_TEST_SKY", "").strip().lower()
    if want == "synthetic":
        return None
    name = zenodo_assets.GAIA_EXTRACTS
    try:
        if not zenodo_assets.available(name):
            raise zenodo.AssetUnavailable(f"Zenodo record {name!r} is not pinned yet")
        npz = zenodo.fetch_file(name, FIXTURE_FILE)
        bright = zenodo.fetch_file(name, BRIGHT_FILE)
    except zenodo.AssetUnavailable as exc:
        if want == "gaia":
            raise
        log.info("test sky: synthetic (%s)", exc)
        return None
    root = npz.parent / "test-sky"
    stamp = root / ".unpacked"
    key = f"{npz.stat().st_size} {zenodo_assets.record(name).record_id}"
    if not stamp.is_file() or stamp.read_text() != key:
        unpack_tiles(npz, root)
        shutil.copyfile(bright, root / BRIGHT_FILE)
        shutil.copyfile(SYNTHETIC / "wds.npy", root / "wds.npy")
        stamp.write_text(key)
    return root


def sky_dir() -> Path:
    """The test sky's catalog directory (module doc)."""
    return _real() or SYNTHETIC


def is_real() -> bool:
    return _real() is not None


def require_gaia() -> None:
    """Skip the calling test unless the real Gaia DR3 sky is in use: for a
    test that only means something on real data."""
    if not is_real():
        pytest.skip(
            "needs the real Gaia DR3 test sky (Zenodo record gaia_extracts: not pinned yet "
            "or not available); the synthetic sky stands in elsewhere"
        )
