"""A whole simulated MINERVA RV night, end to end (docs/demo.md).

Run it with ``python -m pytest -m e2e tests/e2e`` (the ``e2e`` CI job). The
night itself is :mod:`robofast_core.testing.rv_night`; this test runs it and
fails on any of :func:`~robofast_core.testing.rv_night.check`'s findings:
the timeline (calibrations with the roof closed, autofocus, spectra fed by
both telescopes, the imaging field, the weather interruption and the
re-observation, the ToO, T2's fault, the console's pause and resume, dawn),
the safety invariants and the files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from robofast_core.testing.rv_night import check, simulate, summary

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


async def test_a_whole_rv_night(tmp_path):
    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path)
    outcome = await simulate(out)
    print(summary(outcome))
    problems = check(outcome)
    assert not problems, "\n".join(problems)
    print(f"e2e night: {outcome.runtime:.1f} s")
