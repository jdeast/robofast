"""Catalog importers: legacy target lists into robofast's intermediate form.

Today: the TRES / FLWO 60-inch telshell catalogs (:mod:`.tres`; ``.mct``
target lists, ``.off`` offset patterns, ``.sec`` confidential lists, read
from outside only), turned into :mod:`.model`'s intermediate form, request
groups (:func:`to_requests`) and back into ``.mct`` / ``.off`` for the
legacy software. docs/catalogs.md has the formats and the mapping. Command
line::

    python -m robofast_core.catalogs import foo.mct                 # JSON form
    python -m robofast_core.catalogs import foo.mct --to-requests   # request groups
    python -m robofast_core.catalogs check foo.mct bar.off          # validate only
    python -m robofast_core.catalogs export foo.json -o foo.mct     # back to telshell

MINERVA's target and B-star lists, from their Google Sheet, are
:mod:`.minerva_sheet` (its own command line; docs/minerva_sheet.md).
"""

from robofast_core.catalogs.model import (
    FORMAT,
    CatalogTarget,
    Offset,
    OffsetPattern,
    PrivateCatalog,
    Program,
    TargetList,
    from_dict,
    to_requests,
)
from robofast_core.catalogs.tres import (
    CatalogError,
    kind_of,
    load,
    parse_mct,
    parse_off,
    read_sec,
    resolve,
    write_mct,
    write_off,
)

__all__ = [
    "FORMAT",
    "CatalogError",
    "CatalogTarget",
    "Offset",
    "OffsetPattern",
    "PrivateCatalog",
    "Program",
    "TargetList",
    "from_dict",
    "kind_of",
    "load",
    "parse_mct",
    "parse_off",
    "read_sec",
    "resolve",
    "to_requests",
    "write_mct",
    "write_off",
]
