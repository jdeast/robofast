"""Every Zenodo record robofast downloads from, pinned in one place.

This is the ONE table of what robofast fetches from Zenodo: for each record
its id, its concept record (the DOI naming "this dataset, any version"),
and every file's name, size and md5. The download machinery is
:mod:`robofast_core.zenodo`; nothing else in the tree may carry a Zenodo
record id, size or md5. Adapted from EXOZIPPy (BSD 3-Clause, same author).

Records robofast reads
----------------------
``gaia_extracts`` -- Gaia DR3 extracts (CC BY-NC 3.0 IGO, so not in the
BSD repository; docs/sky.md):

* ``bright_stars.npy``: every Gaia DR3 star with G < 6.5, read by
  :func:`robofast_core.catalog.bright_stars`;
* ``test_fixture_tiles.npz``: the real Gaia DR3 tiles around the test
  fields, read by the test suite (``tests/core/skydata.py``);
* ``README.md``, ``MANIFEST.txt``: provenance, licence, checksums.

Built by ``python scripts/build_catalog.py bundle --out DIR`` from the Gaia
archive; uploaded with ``scripts/zenodo_publish.py`` (docs/zenodo.md).

Publishing and pinning
----------------------
1. ``scripts/zenodo_publish.py new-record`` (or ``new-version``) makes a
   DRAFT; a human reviews it on zenodo.org and clicks Publish.
2. ``python scripts/zenodo_publish.py pin --record <id> --name gaia_extracts
   --update-registry`` reads the PUBLISHED record's public API and writes its
   entry into ``RECORDS`` below (and drops it from ``PENDING``).
3. ``pytest tests/core/test_zenodo_assets.py`` (with
   ``ROBOFAST_NETWORK_TESTS=1`` it also re-reads the record from Zenodo).

A new version gets a new record id; every cached file is checked by size
(and md5 when downloaded), so the old version's copies are fetched again.

There is deliberately no logic here beyond lookup: an unknown record or
file raises KeyError naming it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

_DOI_PREFIX = "10.5281/zenodo."
ZENODO = "https://zenodo.org"

#: The registry name of the Gaia DR3 extracts record.
GAIA_EXTRACTS = "gaia_extracts"


@dataclass(frozen=True)
class ZenodoFile:
    """One pinned file: its byte count and md5, as the record's API reports."""

    size: int
    md5: str


@dataclass(frozen=True)
class ZenodoRecord:
    """One published Zenodo record version and its files.

    ``record_id`` is the published VERSION the pins describe;
    ``concept_record_id`` groups every version of the dataset. ``files``
    lists every file of the record."""

    record_id: int
    concept_record_id: int
    title: str
    creators: tuple[str, ...]
    files: Mapping[str, ZenodoFile]

    @property
    def doi(self) -> str:
        return f"{_DOI_PREFIX}{self.record_id}"

    @property
    def concept_doi(self) -> str:
        return f"{_DOI_PREFIX}{self.concept_record_id}"

    @property
    def record_url(self) -> str:
        return f"{ZENODO}/records/{self.record_id}"

    @property
    def api_url(self) -> str:
        return f"{ZENODO}/api/records/{self.record_id}"

    def file(self, filename: str) -> ZenodoFile:
        if filename not in self.files:
            raise KeyError(
                f"{filename!r} is not a file of Zenodo record {self.record_id} "
                f"({self.title}); its files are {sorted(self.files)}"
            )
        return self.files[filename]

    def file_url(self, filename: str) -> str:
        self.file(filename)
        return f"{self.record_url}/files/{filename}"


def _pins(table: Mapping[str, tuple[int, str]]) -> Mapping[str, ZenodoFile]:
    """Freeze a ``{filename: (size, md5)}`` table."""
    return MappingProxyType({n: ZenodoFile(size=s, md5=m) for n, (s, m) in table.items()})


RECORDS: Mapping[str, ZenodoRecord] = MappingProxyType(
    {
        # One entry per published record, written by
        # `python scripts/zenodo_publish.py pin --record <id> --name <key> --update-registry`.
    }
)

#: Every record the code reads, and what to do while it is not pinned.
#: TODO(JDE): gaia_extracts is a Zenodo DRAFT awaiting review; once it is
#: published, pin it (docs/zenodo.md) and the tests that need real Gaia data
#: stop skipping.
EXPECTED: Mapping[str, str] = MappingProxyType(
    {
        GAIA_EXTRACTS: "Zenodo draft awaiting review; build the files locally with "
        "`python scripts/build_catalog.py bundle`",
    }
)
#: The expected records that are not pinned yet. Until then the code that
#: reads them uses a local copy if one is in place and otherwise does
#: without (docs/sky.md).
PENDING: Mapping[str, str] = MappingProxyType(
    {k: v for k, v in EXPECTED.items() if k not in RECORDS}
)


def available(name: str) -> bool:
    """True when record ``name`` is pinned (published and in RECORDS)."""
    return name in RECORDS


def record(name: str) -> ZenodoRecord:
    """The record registered as ``name``; KeyError naming it if unknown."""
    if name not in RECORDS:
        raise KeyError(
            f"{name!r} is not a pinned Zenodo record; pinned: {sorted(RECORDS)}, "
            f"pending: {sorted(PENDING)} (robofast_core/zenodo_assets.py)"
        )
    return RECORDS[name]


def file_pin(name: str, filename: str) -> dict:
    """``{"url", "size", "md5"}`` for one file, as ``zenodo.fetch_assets``
    takes it (a fresh dict: the registry cannot be mutated through it)."""
    rec = record(name)
    pin = rec.file(filename)
    return {"url": rec.file_url(filename), "size": pin.size, "md5": pin.md5}


def assets(name: str, filenames: Iterable[str] | None = None) -> dict:
    """``{filename: file_pin(...)}`` for ``filenames`` (default: all)."""
    rec = record(name)
    names = sorted(rec.files) if filenames is None else list(filenames)
    return {f: file_pin(name, f) for f in names}
