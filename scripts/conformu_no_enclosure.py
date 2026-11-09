"""Run ConformU against the no-enclosure server: the sky (Dome) and the
operator's duty (SafetyMonitor).

    python scripts/conformu_no_enclosure.py --conformu PATH [--out DIR]

Starts ``robofast.devices.no_enclosure_server`` on a free loopback port,
with no supervisor behind it (so the SafetyMonitor reads unsafe: nobody is
on duty), then runs ConformU's ``conformance`` (shutter tests enabled by
``scripts/conformu-dome.settings.json``) and ``alpacaprotocol`` checks on
each device. Exits non-zero if any check reports an error or issue. Logs
and results go to ``--out``; a summary is appended to
``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_aqawan import start
from conformu_tcsgo import SETTINGS, run_check

SETTINGS.setdefault(
    "safetymonitor", Path(__file__).resolve().parent / "conformu-safetymonitor.settings.json"
)
DEVICES = (("dome", "sky"), ("safetymonitor", "duty"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-no-enclosure")
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = out / "sky.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                # Nothing listens there: no operator on duty, IsSafe false.
                "supervisor": "http://127.0.0.1:9",
                "request_timeout": 0.5,
            }
        )
    )
    server, hello = start(
        ["robofast.devices.no_enclosure_server", "--config", str(config)], out / "server.log"
    )
    summaries = []
    try:
        base = json.loads(hello)["url"]
        print(f"no-enclosure server; Alpaca {base}", flush=True)
        for device, tag in DEVICES:
            url = f"{base}/api/v1/{device}/0"
            for check in args.checks.split(","):
                summaries.append(run_check(args.conformu, device, check, url, out, tag=tag))
                print(json.dumps({k: v for k, v in summaries[-1].items() if k != "details"}))
    finally:
        server.terminate()
        server.wait(20)

    failed = False
    report = [
        "### ConformU (no-enclosure server: the sky Dome, the operator-duty SafetyMonitor)",
        "",
    ]
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
