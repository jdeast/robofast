"""The morning report and nightly QA (docs/morning_report.md).

After dawn, ``python -m robofast_core.morning`` reads what the night left
behind -- the plan record (``plan_record.py``: decisions, outcomes,
autofocus results, ToOs), the services' timeline logs (the supervisor's
and the sequencer's: enclosure open/close and why, weather, alarms, errors,
guiding and its acquisition method), the nightly frame index of each
telescope (``*.frames.jsonl``: frames, exposure, plate solves, zero
points) and the spectrograph's sidecars (``*.fits.json``: spectra and
calibrations, guiding, exposure-meter counts) -- and writes a Markdown
report and its JSON next to each other (``report-<night>.md``/``.json``),
then sends it through the notifier's sinks.

Sections: the enclosure (open time, every close and why), weather
interruptions, time on sky per telescope, requests completed/failed,
autofocus (results and the seeing/FWHM trend), guiding (rms) and fiber flux
(exposure meter), zero points per telescope and filter against the
previous nights' (``--history``: earlier reports), pointing (plate-solve
offsets, pointing-model alarms), acquisition methods, the spectrograph's
frames (science and calibrations by kind), alarms and anomalies (errors,
gave up, preemptions, dropped telescopes, lost frames). A QA verdict lists
what needs a look.

Nothing here reads a device: it is a pure function of files
(:func:`build_report`), so it runs anywhere, at any time.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
import math
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["QAConfig", "build_report", "main", "parse_timeline_log", "render_markdown"]

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) \w+ robofast_core\.timeline: "
    r"(?P<source>\S+): (?P<what>\w+)(?: (?P<detail>\{.*\}))?\s*$"
)


@dataclass
class QAConfig:
    """What makes the QA verdict ask for a look."""

    #: A zero point fainter than the history's median by this (mag): throughput lost.
    zp_drop: float = 0.3
    #: Guiding rms above this (arcsec).
    guide_rms: float = 1.0
    #: Median plate-solve offset above this (arcsec): pointing.
    pointing_offset: float = 30.0
    #: Autofocus failures at or above this count.
    autofocus_failures: int = 2


def parse_timeline_log(path: str | Path) -> list[dict[str, Any]]:
    """Timeline events (``robofast_core.timeline`` log lines) of a service's log."""
    out = []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError as exc:
        log.warning("%s: %s", path, exc)
        return out
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        detail: Any = {}
        if m["detail"]:
            try:
                detail = ast.literal_eval(m["detail"])
            except (ValueError, SyntaxError):
                detail = {"raw": m["detail"][:200]}
        try:
            t = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            continue
        out.append({"t": t, "source": m["source"], "what": m["what"], "detail": detail})
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, int | float) and math.isfinite(x)]
    return round(statistics.median(xs), 3) if xs else None


@dataclass
class _Inputs:
    night: str
    frames: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sidecars: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


def gather(
    night: str,
    data_root: str | Path,
    *,
    plan_records: list[str | Path] = (),
    logs: list[str | Path] = (),
    history: str | Path | None = None,
) -> _Inputs:
    """Read the night's files (module doc)."""
    root = Path(data_root)
    inp = _Inputs(night)
    for p in sorted(root.rglob(f"{night}/*.frames.jsonl")):
        inp.frames[p.parent.parent.name] = _read_jsonl(p)
    for p in sorted(root.rglob(f"{night}/*.fits.json")):
        try:
            inp.sidecars.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    for p in plan_records:
        inp.plan += _read_jsonl(Path(p))
    for p in logs:
        inp.events += parse_timeline_log(p)
    inp.events.sort(key=lambda e: e["t"])
    if history is not None:
        for p in sorted(Path(history).glob("report-*.json")):
            if p.name == f"report-{night}.json":
                continue
            try:
                inp.history.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
    return inp


def build_report(inp: _Inputs, qa: QAConfig | None = None) -> dict[str, Any]:
    """The report as a dict (the JSON); :func:`render_markdown` formats it."""
    qa = qa or QAConfig()
    ev = inp.events
    rep: dict[str, Any] = {"night": inp.night, "generated": datetime.now().isoformat()}

    # -- the enclosure --
    opened: datetime | None = None
    intervals, closes = [], []
    last_reason = None
    for e in ev:
        w, d = e["what"], e["detail"]
        if w in ("unsafe", "lease_revoked", "idle_release", "close_requested"):
            last_reason = f"{w}: {d.get('reason') or d.get('why') or d}"[:200]
        if w == "enclosure_open" and opened is None:
            opened = e["t"]
        elif w == "enclosure_closed" and opened is not None:
            intervals.append((opened, e["t"]))
            closes.append({"time": e["t"].isoformat(), "why": last_reason or "end of observing"})
            opened, last_reason = None, None
    if opened is not None and ev:
        intervals.append((opened, ev[-1]["t"]))
    rep["enclosure"] = {
        "open_seconds": round(sum((b - a).total_seconds() for a, b in intervals), 1),
        "openings": len(intervals),
        "closes": closes,
    }
    rep["weather"] = [
        {"time": e["t"].isoformat(), "detail": e["detail"]}
        for e in ev
        if e["what"] in ("unsafe", "weather_unsafe")
    ]

    # -- telescopes: time on sky, zero points, pointing --
    tels: dict[str, Any] = {}
    for tel, frames in sorted(inp.frames.items()):
        light = [f for f in frames if f.get("imagetyp") in ("LIGHT", "FOCUS")]
        zps: dict[str, list[float]] = defaultdict(list)
        offsets = []
        solved = 0
        for f in light:
            s = f.get("solve") or {}
            if s.get("solved"):
                solved += 1
                if s.get("offset_arcsec") is not None:
                    offsets.append(float(s["offset_arcsec"]))
                if (s.get("zp") or {}).get("zp") is not None:
                    zps[str(f.get("filter"))].append(float(s["zp"]["zp"]))
        tels[tel] = {
            "frames": len(frames),
            "light_frames": len(light),
            "exposure_seconds": round(sum(float(f.get("exptime") or 0) for f in light), 1),
            "solved": solved,
            "pointing_offset_median": _median(offsets),
            "zero_points": {k: _median(v) for k, v in zps.items()},
        }
    for sc in inp.sidecars:
        if sc.get("kind") == "science":
            for t in sc.get("telescopes") or []:
                tt = tels.setdefault(t, {"frames": 0, "light_frames": 0, "exposure_seconds": 0.0})
                tt["spectrum_seconds"] = round(
                    tt.get("spectrum_seconds", 0.0) + float(sc.get("exptime") or 0), 1
                )
    # Zero points against the history.
    for tel, tt in tels.items():
        for flt, zp in (tt.get("zero_points") or {}).items():
            past = [
                h["telescopes"][tel]["zero_points"][flt]
                for h in inp.history
                if (h.get("telescopes", {}).get(tel, {}).get("zero_points") or {}).get(flt)
                is not None
            ]
            ref = _median(past)
            if ref is not None and zp is not None:
                tt.setdefault("zero_point_change", {})[flt] = round(zp - ref, 3)
    rep["telescopes"] = tels

    # -- requests --
    outcomes = Counter()
    failed = []
    for r in inp.plan:
        if r.get("kind") == "outcome":
            outcomes[r.get("outcome")] += 1
            if r.get("outcome") == "failed":
                failed.append({k: r.get(k) for k in ("telescope", "request", "observation")})
    rep["requests"] = {
        "outcomes": dict(outcomes),
        "failed": failed,
        "too": sum(1 for r in inp.plan if r.get("kind") == "too"),
        "preemptions": sum(1 for r in inp.plan if r.get("kind") == "preempt"),
    }

    # -- autofocus and seeing --
    af = [r for r in inp.plan if r.get("kind") == "autofocus"]
    rep["autofocus"] = {
        "runs": len(af),
        "failed": sum(1 for r in af if not r.get("success")),
        "results": [
            {
                k: (round(r[k], 3) if isinstance(r.get(k), float) else r.get(k))
                for k in ("t", "telescope", "success", "used", "focus", "seeing", "message")
            }
            for r in af
        ],
        "seeing_trend": [
            [r.get("t"), round(r["seeing"], 2)] for r in af if isinstance(r.get("seeing"), float)
        ],
    }

    # -- guiding, fiber flux, acquisition --
    rms: dict[str, list[float]] = defaultdict(list)
    for sc in inp.sidecars:
        for t, g in (sc.get("guide") or {}).items():
            v = ((g or {}).get("telemetry") or {}).get("residual_rms_arcsec")
            if isinstance(v, int | float):
                rms[t].append(float(v))
    science = [sc for sc in inp.sidecars if sc.get("kind") == "science"]
    flux = [
        float(sc["expmeter_counts"]) / float(sc["exptime"])
        for sc in science
        if sc.get("expmeter_counts") and sc.get("exptime")
    ]
    acq = Counter(
        e["detail"].get("acq_method") for e in ev if e["what"] == "guiding" and e["detail"]
    )
    acq.pop(None, None)
    rep["guiding"] = {
        "rms_arcsec_median": {t: _median(v) for t, v in rms.items()},
        "fiber_flux_median": _median(flux),
        "acquisition_methods": dict(acq),
    }

    # -- spectrograph frames --
    kinds = Counter(sc.get("kind") for sc in inp.sidecars)
    rep["spectrograph"] = {"frames": dict(kinds), "science": len(science)}

    # -- alarms and anomalies --
    def pick(*names: str) -> list[dict[str, Any]]:
        return [
            {"time": e["t"].isoformat(), "source": e["source"], "what": e["what"], **e["detail"]}
            for e in ev
            if e["what"] in names
        ]

    rep["alarms"] = pick("alarm", "giving_up", "frame_write_failed", "quarantined")
    rep["anomalies"] = pick(
        "telescope_error", "spectrum_dropped", "recovery_done", "observation_failed"
    )

    # -- the QA verdict --
    look = []
    for tel, tt in tels.items():
        for flt, dz in (tt.get("zero_point_change") or {}).items():
            if dz < -qa.zp_drop:
                look.append(f"{tel} {flt}: zero point {dz:+.2f} mag vs history (throughput?)")
        po = tt.get("pointing_offset_median")
        if po is not None and po > qa.pointing_offset:
            look.append(f"{tel}: median pointing offset {po:.0f} arcsec")
    for t, v in rep["guiding"]["rms_arcsec_median"].items():
        if v is not None and v > qa.guide_rms:
            look.append(f"{t}: guiding rms {v:.2f} arcsec")
    if rep["autofocus"]["failed"] >= qa.autofocus_failures:
        look.append(f"{rep['autofocus']['failed']} autofocus runs failed")
    if any(a["what"] == "giving_up" for a in rep["alarms"]):
        look.append("a telescope gave up for the night")
    if rep["requests"]["failed"]:
        look.append(f"{len(rep['requests']['failed'])} observations failed")
    rep["qa"] = {"ok": not look, "look_at": look}
    return rep


def _hm(seconds: float) -> str:
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def render_markdown(rep: dict[str, Any]) -> str:
    """The report for people (Markdown)."""
    L: list[str] = [f"# Morning report {rep['night']}", ""]
    qa = rep["qa"]
    L += ["**QA: all good**" if qa["ok"] else "**QA: needs a look**", ""]
    L += [f"- {x}" for x in qa["look_at"]] + ([""] if qa["look_at"] else [])
    enc = rep["enclosure"]
    L += [
        "## Enclosure",
        "",
        f"Open {_hm(enc['open_seconds'])} in {enc['openings']} opening(s).",
        "",
    ]
    L += [f"- closed {c['time']}: {c['why']}" for c in enc["closes"]] + [""]
    if rep["weather"]:
        L += ["## Weather interruptions", ""]
        L += [f"- {w['time']}: {w['detail']}" for w in rep["weather"]] + [""]
    L += [
        "## Telescopes",
        "",
        "| telescope | light frames | exposure | spectra | solved | pointing offset (median)"
        " | zero points (change) |",
        "|---|---|---|---|---|---|---|",
    ]
    for t, tt in rep["telescopes"].items():
        zp = ", ".join(
            f"{k}: {v}" + (f" ({tt['zero_point_change'][k]:+.2f})" if k in tt.get(
                "zero_point_change", {}) else "")
            for k, v in (tt.get("zero_points") or {}).items()
        )  # fmt: skip
        L.append(
            f"| {t} | {tt.get('light_frames', 0)} | {tt.get('exposure_seconds', 0)} s | "
            f"{tt.get('spectrum_seconds', 0)} s | {tt.get('solved', 0)} | "
            f"{tt.get('pointing_offset_median')} | {zp or '-'} |"
        )
    rq = rep["requests"]
    L += ["", "## Requests", "", f"Outcomes: {rq['outcomes']}; ToOs {rq['too']};"
          f" preemptions {rq['preemptions']}.", ""]  # fmt: skip
    L += [f"- failed: {f}" for f in rq["failed"]]
    af = rep["autofocus"]
    L += ["", "## Autofocus", "", f"{af['runs']} run(s), {af['failed']} failed.", ""]
    L += [
        f"- {r['t']} {r['telescope']}: {'ok' if r['success'] else 'FAILED'} {r.get('used')}"
        f" focus {r.get('focus')} seeing {r.get('seeing')}"
        for r in af["results"]
    ]
    g = rep["guiding"]
    L += [
        "",
        "## Guiding and fiber",
        "",
        f"Guiding rms (median, arcsec): {g['rms_arcsec_median']}; fiber flux (median"
        f" exposure-meter counts/s): {g['fiber_flux_median']}; acquisition methods:"
        f" {g['acquisition_methods'] or '-'}.",
        "",
        "## Spectrograph",
        "",
        f"Frames by kind: {rep['spectrograph']['frames']}.",
        "",
        "## Alarms",
        "",
    ]
    L += [f"- {a['time']} {a['source']} {a['what']}: {_say(a)}"
          for a in rep["alarms"]] or ["None."]  # fmt: skip
    L += ["", "## Anomalies", ""]
    L += [f"- {a['time']} {a['source']} {a['what']}: {a.get('error') or a.get('telescope', '')}"
          for a in rep["anomalies"]] or ["None."]  # fmt: skip
    return "\n".join(L) + "\n"


def _say(a: dict[str, Any]) -> str:
    for k in ("text", "message", "reason", "error", "alarm"):
        if a.get(k):
            return str(a[k])[:200]
    return ", ".join(f"{k}={v}" for k, v in a.items() if k not in ("time", "source", "what"))[:200]


async def notify(text: str, subject: str, notifier_config: str | Path, level: str) -> int:
    """Send the report through every sink of a notifier config; failures logged."""
    from robofast_core.notifier.core import Notification
    from robofast_core.notifier.service import build_sink_workers

    p = Path(notifier_config)
    raw = json.loads(await asyncio.to_thread(p.read_text))
    workers = build_sink_workers(raw.get("sinks"), p.parent)
    n = Notification("report", level, subject, text, time.time())
    sent = 0
    for w in workers:
        try:
            await asyncio.wait_for(w.sink.send(n), 60)
            sent += 1
        except Exception as exc:
            log.error("report via %s: %s", getattr(w.sink, "name", "?"), exc)
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast_core.morning")
    ap.add_argument("--night", required=True, help="nYYYYMMDD")
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--plan-record", action="append", default=[])
    ap.add_argument("--log", action="append", default=[], help="a service log (timeline lines)")
    ap.add_argument("--history", default=None, help="directory of earlier report-*.json")
    ap.add_argument("--out", required=True, help="directory for report-<night>.md/.json")
    ap.add_argument("--notifier", default=None, help="notifier config: send the report")
    args = ap.parse_args(argv)
    inp = gather(
        args.night,
        args.data_root,
        plan_records=args.plan_record,
        logs=args.log,
        history=args.history or args.out,
    )
    rep = build_report(inp)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md = render_markdown(rep)
    (out / f"report-{args.night}.json").write_text(json.dumps(rep, indent=1, default=str))
    (out / f"report-{args.night}.md").write_text(md)
    print(out / f"report-{args.night}.md")
    if args.notifier:
        subject = f"robofast {args.night}: " + ("all good" if rep["qa"]["ok"] else "needs a look")
        asyncio.run(notify(md, subject, args.notifier, "info" if rep["qa"]["ok"] else "warning"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
