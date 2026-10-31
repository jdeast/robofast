"""Run ConformU against the Aqawan Dome server, backed by the PAC simulator.

    python scripts/conformu_aqawan.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.aqawan_sim`` (heartbeat watchdog off, since
ConformU sends no heartbeats; the legacy settle quirk on) and
``robofast_core.devices.aqawan_server`` on free loopback ports, then runs
ConformU's ``conformance`` check (with the shutter-opening tests enabled by
``scripts/conformu-dome.settings.json``) and its ``alpacaprotocol`` check.
Exits non-zero if either reports any error or issue. Logs and results go to
``--out``; a summary is appended to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "scripts" / "conformu-dome.settings.json"


def start(args: list[str], log: Path) -> tuple[subprocess.Popen, str]:
    """Start ``python -m args...``; return it and its first stdout line."""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=open(log, "w"),  # lives as long as the process
        text=True,
    )
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 30)
    line = proc.stdout.readline() if ready else ""
    if not line:
        proc.kill()
        raise SystemExit(f"{args[0]} did not start; see {log}")
    return proc, line.strip()


def run_check(conformu: str, check: str, url: str, out: Path) -> dict:
    settings = out / f"{check}.settings.json"
    shutil.copy(SETTINGS, settings)  # ConformU rewrites its settings file
    results = out / f"{check}.results.json"
    cmd = [conformu, check, url, "-s", str(settings), "-n", str(out / f"{check}.log")]
    cmd += ["-r", str(results)]
    proc = subprocess.run(cmd, cwd=out, capture_output=True, text=True, timeout=900)
    (out / f"{check}.stdout.txt").write_text(proc.stdout + proc.stderr)
    summary = {"check": check, "exit": proc.returncode}
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
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    sim, line = start(
        [
            "robofast_core.devices.aqawan_sim",
            *("--port", "0", "--heartbeat-timeout", "0"),
            *("--open-time", "2", "--close-time", "2", "--settle", "0.3"),
        ],
        out / "sim.log",
    )
    pac = line.rsplit(" ", 1)[-1]
    host, port = pac.rsplit(":", 1)
    config = out / "aqawan.json"
    config.write_text(
        json.dumps(
            {"name": "conformu", "listen": "127.0.0.1:0", "pac": {"host": host, "port": int(port)}}
        )
    )
    procs = [sim]
    try:
        server, hello = start(
            ["robofast_core.devices.aqawan_server", "--config", str(config)], out / "server.log"
        )
        procs.append(server)
        url = json.loads(hello)["url"] + "/api/v1/dome/0"
        print(f"PAC simulator {pac}; Dome {url}", flush=True)
        summaries = [
            run_check(args.conformu, c, url, out) for c in ("conformance", "alpacaprotocol")
        ]
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(10)

    failed = False
    report = ["### ConformU (Aqawan Dome server vs PAC simulator)", ""]
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
