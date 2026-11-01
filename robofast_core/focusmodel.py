"""Fit the focus model's terms from autofocus history (docs/autofocus.md
section 6): the calibration routine for the temperature and gravity
(``sin(alt)``) terms.

Every autofocus result in the focus store (``FocusStore`` ``history``)
carries the values the model reads -- and, since this module, always the
focuser temperature and the mount's altitude (``FOCUS_CONTEXT``) -- with the
measured focus. :func:`fit` solves

    focus = Z + sum_i c_i (f_i(value_i) - ref_i)

by least squares over the successful results of one telescope/port/filter
(``f_i`` the sine for a ``sin:`` term), each ``ref_i`` the mean of its
values (so ``Z`` is the mean focus), with formal errors and the rms. A term
whose coefficient is not ``significance`` sigma from zero can be dropped
(``prune``). The model it returns only predicts where an autofocus starts:
each measured focus re-anchors its zero point, and it never replaces a
measurement.

    python -m robofast_core.focusmodel --store focus_t1.json --key t1/imager/- \\
        --term "{tel}.focuser.temperature" --term "sin:{tel}.tel.alt"

prints the fit and the ``model`` entry for the telescope's autofocus config.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from typing import Any

import numpy as np

from robofast_core.autofocus import FocusModel, FocusStore, term_value

__all__ = ["fit", "legacy_model", "main"]


def legacy_model(c: dict[str, float]) -> FocusModel:
    """The legacy ``guessFocus`` model (``telescope_N.ini`` F0, C0, T0, C1,
    dT0, C2, G0; ``sites/minerva/instruments.json``) as a :class:`FocusModel`
    on robofast's values: C0 on the primary's temperature (here the
    focuser's until the primary's is in the registry), C1 on M1 minus
    ambient (``{tel}.focuser.delta_t``, when published), C2 on sin(alt)."""
    return FocusModel(
        terms={
            "{tel}.focuser.temperature": float(c["C0"]),
            "{tel}.focuser.delta_t": float(c["C1"]),
            "sin:{tel}.tel.alt": float(c["C2"]),
        },
        reference={
            "{tel}.focuser.temperature": float(c["T0"]),
            "{tel}.focuser.delta_t": float(c["dT0"]),
            "sin:{tel}.tel.alt": float(c["G0"]),
        },
        zero_point=float(c["F0"]),
    )


def fit(
    history: Sequence[dict[str, Any]],
    terms: Sequence[str],
    *,
    key: str | None = None,
    min_points: int = 6,
    significance: float = 0.0,
) -> tuple[FocusModel, dict[str, Any]]:
    """Fit ``terms`` to the successful results in ``history`` (``key``: only
    that telescope/port/filter). With ``significance`` > 0, terms less
    significant than that are dropped one at a time and the fit repeated."""
    rows = []
    for h in history:
        if key is not None and h.get("key") != key:
            continue
        if not h.get("success") or h.get("focus") is None:
            continue
        vals = h.get("model_values") or {}
        xs = [term_value(t, vals.get(t)) for t in terms]
        if any(x is None for x in xs):
            continue
        rows.append((float(h["focus"]), xs))
    terms = list(terms)
    if len(rows) < max(min_points, len(terms) + 2):
        raise ValueError(f"{len(rows)} usable autofocus results; at least {min_points} needed")
    while True:
        y = np.array([r[0] for r in rows])
        x = np.array([[r[1][i] for i, _ in enumerate(terms)] for r in rows]).reshape(len(rows), -1)
        ref = x.mean(axis=0) if terms else np.zeros(0)
        a = np.column_stack([np.ones(len(rows)), x - ref])
        coef, *_ = np.linalg.lstsq(a, y, rcond=None)
        resid = y - a @ coef
        dof = max(1, len(rows) - a.shape[1])
        s2 = float(resid @ resid) / dof
        err = np.sqrt(np.clip(np.diag(np.linalg.pinv(a.T @ a)) * s2, 0, None))
        sig = np.abs(coef[1:]) / np.where(err[1:] > 0, err[1:], np.inf)
        if significance > 0 and terms and sig.min() < significance:
            j = int(np.argmin(sig))
            rows = [(f, xs[:j] + xs[j + 1 :]) for f, xs in rows]
            terms.pop(j)
            continue
        break
    model = FocusModel(
        {t: float(c) for t, c in zip(terms, coef[1:], strict=True)},
        {t: float(r) for t, r in zip(terms, ref, strict=True)},
        float(coef[0]),
    )
    info = {
        "n": len(rows),
        "rms": round(math.sqrt(float(resid @ resid) / len(rows)), 3),
        "errors": {t: round(float(e), 4) for t, e in zip(terms, err[1:], strict=True)},
        "zero_point_error": round(float(err[0]), 3),
        "spread": round(float(np.std(y)), 3),
    }
    return model, info


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m robofast_core.focusmodel")
    p.add_argument("--store", required=True, help="the telescope's focus store (JSON)")
    p.add_argument("--key", required=True, help="telescope/port/filter, as the store keys it")
    p.add_argument("--term", action="append", required=True)
    p.add_argument("--significance", type=float, default=3.0)
    a = p.parse_args(argv)
    store = FocusStore(a.store)
    model, info = fit(store.data.get("history", []), a.term, key=a.key, significance=a.significance)
    for t, c in model.terms.items():
        e, r = info["errors"][t], model.reference[t]
        print(f"{t}: {c:+.2f} +- {e:.2f} steps per unit (reference {r:.4f})")
    print(
        f"zero point {model.zero_point:.1f}; rms {info['rms']}"
        f" (spread {info['spread']}); n {info['n']}"
    )
    # For the telescope's autofocus config (its zero point is learned at run time).
    print(json.dumps({"model": {"terms": model.terms, "reference": model.reference}}, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
