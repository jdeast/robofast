"""Run ConformU against the TCSGO Telescope, Focuser and Dome server, backed by
the TCSGalil simulator.

    python scripts/conformu_tcsgo.py --conformu PATH [--out DIR] [--devices telescope,...]

Starts ``robofast_core.devices.tcsgo_sim`` and
``robofast_core.devices.tcsgo_server`` (with its TCS-controlled dome
enabled) on free loopback ports, then runs ConformU's ``conformance`` and
``alpacaprotocol`` checks on each device. Exits non-zero if any check reports
an error or an issue. Logs and results go to ``--out``; a summary is appended
to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_aqawan import start

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {
    "telescope": ROOT / "scripts" / "conformu-telescope.settings.json",
    "focuser": ROOT / "scripts" / "conformu-focuser.settings.json",
    "dome": ROOT / "scripts" / "conformu-dome.settings.json",
    "filterwheel": ROOT / "scripts" / "conformu-filterwheel.settings.json",
    "rotator": ROOT / "scripts" / "conformu-rotator.settings.json",
    "switch": ROOT / "scripts" / "conformu-switch.settings.json",
}


def run_check(
    conformu: str, device: str, check: str, url: str, out: Path, tag: str | None = None
) -> dict:
    tag = f"{tag or device}-{check}"
    settings = out / f"{tag}.settings.json"
    shutil.copy(SETTINGS[device], settings)  # ConformU rewrites its settings file
    results = out / f"{tag}.results.json"
    cmd = [conformu, check, url, "-s", str(settings), "-n", str(out / f"{tag}.log")]
    cmd += ["-r", str(results)]
    proc = subprocess.run(cmd, cwd=out, capture_output=True, text=True, timeout=1500)
    (out / f"{tag}.stdout.txt").write_text(proc.stdout + proc.stderr)
    summary = {"check": tag, "exit": proc.returncode}
    if results.exists():
        r = json.loads(results.read_text())
        summary |= {
            "errors": r.get("ErrorCount"),
            "issues": r.get("IssueCount"),
            "alerts": r.get("ConfigurationAlertCount"),
            "details": r.get("Errors", []) + r.get("Issues", []),
        }
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    summary["verdict"] = next(
        (ln.split(None, 1)[-1] for ln in reversed(lines) if "errors" in ln or "issues" in ln),
        lines[-1] if lines else "",
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-out")
    ap.add_argument("--devices", default="telescope,focuser,dome")
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    sim, line = start(
        [
            "robofast_core.devices.tcsgo_sim",
            *("--port", "0", "--slew-rate", "15", "--shutter-time", "2"),
            *("--focus-rate", "5000", "--dome-rate", "20", "--min-alt", "0"),
            *("--dec-min", "-89", "--ha-limit", "11.9"),
        ],
        out / "sim.log",
    )
    tcs = line.rsplit(" ", 1)[-1]
    host, port = tcs.rsplit(":", 1)
    config = out / "tcsgo.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "tcs": {"host": host, "port": int(port), "min_interval": 0.01, "ack_window": 0.05},
                "dome": True,
                # ConformU picks its own slew targets, some low in the sky.
                "min_alt": 0.0,
                "status_max_age": 0.1,
            }
        )
    )
    procs = [sim]
    summaries = []
    try:
        server, hello = start(
            ["robofast_core.devices.tcsgo_server", "--config", str(config)], out / "server.log"
        )
        procs.append(server)
        base = json.loads(hello)["url"]
        print(f"TCS simulator {tcs}; Alpaca {base}", flush=True)
        for device in args.devices.split(","):
            url = f"{base}/api/v1/{device}/0"
            for check in args.checks.split(","):
                summaries.append(run_check(args.conformu, device, check, url, out))
                print(json.dumps({k: v for k, v in summaries[-1].items() if k != "details"}))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(10)

    failed = False
    report = ["### ConformU (TCSGO server vs TCSGalil simulator)", ""]
    for s in summaries:
        bad = s["exit"] != 0 or bool(s.get("errors")) or bool(s.get("issues"))
        failed |= bad
        report.append(f"- `{s['check']}`: exit {s['exit']}; {s['verdict']}")
        for d in s.get("details", []):
            report.append(f"  - {d}")
    text = "\n".join(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
