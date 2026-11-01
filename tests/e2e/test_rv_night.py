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
    morning_report(out)


def morning_report(out: Path) -> None:
    """The morning report (robofast_core/morning.py) on the night's files."""
    import json

    from robofast_core.morning import build_report, gather, render_markdown

    [night] = sorted({p.name for p in (out / "data").rglob("n2*") if p.is_dir()})
    logs = [out / "supervisor.log", *sorted(out.glob("sequencer-*.log"))]
    inp = gather(
        night, out / "data", plan_records=sorted((out / "plans").glob("*.jsonl")), logs=logs
    )
    rep = build_report(inp)
    (out / f"report-{night}.json").write_text(json.dumps(rep, indent=1, default=str))
    (out / f"report-{night}.md").write_text(render_markdown(rep))
    print(render_markdown(rep))
    assert rep["enclosure"]["openings"] >= 1 and rep["enclosure"]["open_seconds"] > 0
    assert rep["weather"], "the weather interruption"
    assert rep["requests"]["outcomes"].get("complete", 0) >= 3
    assert rep["requests"]["too"] == 1
    assert rep["autofocus"]["runs"] >= 2
    assert rep["spectrograph"]["science"] >= 3
    assert {"bias", "thar"} <= set(rep["spectrograph"]["frames"])
    assert set(rep["guiding"]["rms_arcsec_median"]) == {"t1", "t2"}
    assert any(a["what"] == "telescope_error" for a in rep["anomalies"]), "T2's fault"
    assert {"t1", "t2"} <= set(rep["telescopes"])
