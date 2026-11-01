"""Which star in the acquisition frame is the target: by its catalog position.

"Take the brightest star" picks the wrong one when the target has a
near-equal neighbour (a visual binary, a field star of similar brightness):
the guider then puts the wrong star on the fiber. :func:`catalog_identifier`
instead plate-solves the acquisition frame near the target
(:func:`robofast_core.astrometry.solve_near`) and predicts the target's
pixel from the solution; the guide loop (``select: "catalog"``) takes the
detected star nearest that pixel. When the frame cannot be solved (too few
stars, clouds), the loop falls back to the brightest star **only** if
vetting found the field unambiguous (:mod:`robofast_core.vetting`), and
otherwise reports the target as not identified.

The guider service builds one per acquisition from its ``catalog`` and
``solver`` config and the target coordinates the sequencer sends with
``POST /start`` (``target_radec``, ``unambiguous``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from robofast_core.astrometry import SolveError, SolverConfig, solve_near
from robofast_core.catalog import Catalog, CatalogUnavailable, Stars

log = logging.getLogger(__name__)

__all__ = ["Identifier", "catalog_identifier"]

#: Frame -> the target's detector pixel (x, y), or None if not identified.
Identifier = Callable[[Any], "tuple[float, float] | None"]


def catalog_identifier(
    catalog: Catalog | Stars,
    solver: SolverConfig,
    ra: float,
    dec: float,
    *,
    epoch: float | datetime | None = None,
    binning: int = 1,
) -> Identifier:
    """An :data:`Identifier` for a target at (ra, dec) deg (ICRS, moved to
    ``epoch`` by the caller if it has a proper motion)."""

    def identify(frame: Any) -> tuple[float, float] | None:
        try:
            sol = solve_near(frame.data, catalog, ra, dec, solver, binning=binning, epoch=epoch)
        except (SolveError, CatalogUnavailable) as exc:
            log.info("acquisition frame not solved: %s", exc)
            return None
        x, y = sol.pixel(ra, dec)
        roi = frame.roi
        return float(x[0]) + roi.x, float(y[0]) + roi.y

    return identify
