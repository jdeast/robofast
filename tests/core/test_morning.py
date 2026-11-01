"""The morning report and nightly QA (robofast_core/morning.py) on a small
synthetic night; the e2e test runs it on the whole simulated night."""

from __future__ import annotations

import json
import sys

from robofast_core.morning import QAConfig, build_report, gather, main, render_markdown

NIGHT = "n20261109"


def write_night(root, zp=19.7):
    d = root / "data" / "minerva" / "t1" / NIGHT
    d.mkdir(parents=True)
    frames = [
        {"telescope": "t1", "imagetyp": "LIGHT", "exptime": 60.0, "filter": "V",
         "solve": {"solved": True, "offset_arcsec": 4.0, "zp": {"zp": zp}}},
        {"telescope": "t1", "imagetyp": "LIGHT", "exptime": 60.0, "filter": "V",
         "solve": {"solved": True, "offset_arcsec": 6.0, "zp": {"zp": zp + 0.02}}},
        {"telescope": "t1", "imagetyp": "BIAS", "exptime": 0.0},
    ]  # fmt: skip
    (d / f"{NIGHT}.t1.frames.jsonl").write_text("".join(json.dumps(f) + "\n" for f in frames))
    k = root / "data" / "minerva" / "kiwispec" / NIGHT
    k.mkdir(parents=True)
    sci = {"kind": "science", "telescopes": ["t1"], "exptime": 600.0, "expmeter_counts": 6e6,
           "guide": {"t1": {"telemetry": {"residual_rms_arcsec": 0.4}}}}  # fmt: skip
    (k / f"{NIGHT}.RV1.0001.fits.json").write_text(json.dumps(sci))
    (k / f"{NIGHT}.ThAr.0002.fits.json").write_text(json.dumps({"kind": "thar"}))
    plan = root / "plan.jsonl"
    plan.write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in [
                {"kind": "outcome", "telescope": "t1", "request": "RV1-r", "outcome": "complete"},
                {"kind": "outcome", "telescope": "t1", "request": "X-r", "outcome": "failed",
                 "observation": "X"},
                {"kind": "autofocus", "t": "2026-11-10T02:00:00+00:00", "telescope": "t1",
                 "success": True, "used": "vcurve", "focus": 20000.0, "seeing": 1.8},
            ]
        )
    )  # fmt: skip
    log = root / "supervisor.log"
    lines = [
        "2026-11-10 01:00:00,000 INFO robofast_core.timeline: aqawan1: enclosure_open {'lease': 1}",
        "2026-11-10 03:00:00,000 INFO robofast_core.timeline: aqawan1: unsafe {'reason': 'rain'}",
        "2026-11-10 03:00:05,000 INFO robofast_core.timeline: aqawan1: lease_revoked {'reason': 'unsafe: rain'}",  # noqa: E501
        "2026-11-10 03:01:00,000 INFO robofast_core.timeline: aqawan1: enclosure_closed {'t': 5}",
        "2026-11-10 04:00:00,000 INFO robofast_core.timeline: seq: alarm {'telescope': 't1', 'text': 't1 port None: pointing drift 30 arcsec'}",  # noqa: E501
        "2026-11-10 04:10:00,000 INFO robofast_core.timeline: seq: guiding {'telescope': 't1', 'acq_method': 'pair', 'acq_stars': 2}",  # noqa: E501
        "2026-11-10 04:20:00,000 INFO robofast_core.timeline: seq: telescope_error {'telescope': 't1', 'error': 'camera timeout'}",  # noqa: E501
        "garbage line",
    ]
    log.write_text("\n".join(lines) + "\n")
    return plan, log


def test_report_sections(tmp_path):
    plan, log = write_night(tmp_path)
    rep = build_report(gather(NIGHT, tmp_path / "data", plan_records=[plan], logs=[log]))
    assert rep["enclosure"] == {
        "open_seconds": 7260.0,
        "openings": 1,
        "closes": [{"time": "2026-11-10T03:01:00", "why": "lease_revoked: unsafe: rain"}],
    }
    assert rep["weather"][0]["detail"] == {"reason": "rain"}
    t1 = rep["telescopes"]["t1"]
    assert t1["light_frames"] == 2 and t1["exposure_seconds"] == 120.0
    assert t1["spectrum_seconds"] == 600.0 and t1["pointing_offset_median"] == 5.0
    assert t1["zero_points"] == {"V": 19.71}
    assert rep["requests"]["outcomes"] == {"complete": 1, "failed": 1}
    assert rep["autofocus"]["seeing_trend"] == [["2026-11-10T02:00:00+00:00", 1.8]]
    assert rep["guiding"]["rms_arcsec_median"] == {"t1": 0.4}
    assert rep["guiding"]["fiber_flux_median"] == 10000.0
    assert rep["guiding"]["acquisition_methods"] == {"pair": 1}
    assert rep["spectrograph"]["frames"] == {"science": 1, "thar": 1}
    assert "pointing drift" in rep["alarms"][0]["text"]
    assert rep["anomalies"][0]["what"] == "telescope_error"
    assert not rep["qa"]["ok"] and "1 observations failed" in rep["qa"]["look_at"]
    md = render_markdown(rep)
    assert "# Morning report n20261109" in md and "pointing drift" in md and "pair" in md


def test_zero_point_drop_against_history_and_notifier(tmp_path):
    out = tmp_path / "reports"
    out.mkdir()
    past = {"telescopes": {"t1": {"zero_points": {"V": 20.2}}}}
    (out / "report-n20261101.json").write_text(json.dumps(past))
    plan, log = write_night(tmp_path, zp=19.7)
    got = tmp_path / "sent.json"
    notifier = tmp_path / "notifier.json"
    notifier.write_text(
        json.dumps(
            {
                "sinks": {
                    "cmd": {
                        "type": "command",
                        "argv": [sys.executable, "-c",
                                 f"import sys; open({str(got)!r}, 'w').write(sys.stdin.read())"],
                    }
                }
            }
        )
    )  # fmt: skip
    args = ["--night", NIGHT, "--data-root", str(tmp_path / "data"), "--plan-record", str(plan)]
    assert main([*args, "--log", str(log), "--out", str(out), "--notifier", str(notifier)]) == 0
    rep = json.loads((out / f"report-{NIGHT}.json").read_text())
    assert rep["telescopes"]["t1"]["zero_point_change"] == {"V": -0.49}
    assert any("zero point -0.49" in x for x in rep["qa"]["look_at"])
    sent = json.loads(got.read_text())
    assert sent["kind"] == "report" and "needs a look" in sent["subject"]
    assert "Morning report" in sent["body"]
    assert QAConfig(zp_drop=1.0).zp_drop == 1.0
