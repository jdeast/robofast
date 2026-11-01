"""Run ConformU against the TRES front-end server, on its simulated controller.

    python scripts/conformu_tres_frontend.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.tres_frontend_server`` with ``port.type``
"sim" (the in-process PI C-663 simulator, referenced, 50 mm/s) on a free
loopback port, then runs ConformU's ``conformance`` and ``alpacaprotocol``
checks against its FilterWheel (the calibration stage by named position)
and its Focuser (the same stage in microns). Every named position has a
value here, so ConformU can visit them all. Exits non-zero if any check
reports an error or issue. Logs and results go to ``--out``; a summary is
appended to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_aqawan import start
from conformu_tcsgo import run_check


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-tres-frontend")
    ap.add_argument("--devices", default="filterwheel,focuser")
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = out / "tres_frontend.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "port": {"type": "sim", "sim": {"velocity": 50.0}},
                "positions": {
                    "out": 0.0,
                    "sky": 10.0,
                    "science": 20.0,
                    "in": 5.0,
                    "block_sky": 15.0,
                    "block_science": 25.0,
                },
                "poll_interval": 0.05,
                "status_max_age": 0.05,
            }
        )
    )
    server, hello = start(
        ["robofast_core.devices.tres_frontend_server", "--config", str(config)],
        out / "server.log",
    )
    summaries = []
    try:
        base = json.loads(hello)["url"]
        print(f"TRES front-end simulator; Alpaca {base}", flush=True)
        for device in args.devices.split(","):
            url = f"{base}/api/v1/{device}/0"
            for check in args.checks.split(","):
                summaries.append(run_check(args.conformu, device, check, url, out))
                print(json.dumps({k: v for k, v in summaries[-1].items() if k != "details"}))
    finally:
        server.terminate()
        server.wait(10)

    failed = False
    report = ["### ConformU (TRES front-end FilterWheel and Focuser vs the C-663 simulator)", ""]
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
