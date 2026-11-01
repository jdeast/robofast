"""Run ConformU against the MEarth South roof Dome server, on its simulator.

    python scripts/conformu_mearth_roof.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.mearth_roof_server`` with ``port.type`` "sim"
(the in-process roof simulator, a 3 s stroke) on a free loopback port, then
runs ConformU's ``conformance`` check (shutter tests enabled by
``scripts/conformu-dome.settings.json``) and its ``alpacaprotocol`` check.
Exits non-zero if either reports any error or issue. Logs and results go to
``--out``; a summary is appended to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_aqawan import run_check, start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-mearth-roof")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = out / "roof.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "port": {"type": "sim", "sim": {"travel_time": 3.0}},
                "departure_timeout": 5.0,
                "open_timeout": 20.0,
                "close_timeout": 20.0,
            }
        )
    )
    server, hello = start(
        ["robofast_core.devices.mearth_roof_server", "--config", str(config)], out / "server.log"
    )
    try:
        url = json.loads(hello)["url"] + "/api/v1/dome/0"
        print(f"MEarth roof simulator; Dome {url}", flush=True)
        summaries = [
            run_check(args.conformu, c, url, out) for c in ("conformance", "alpacaprotocol")
        ]
    finally:
        server.terminate()
        server.wait(10)

    failed = False
    report = ["### ConformU (MEarth South roof Dome server vs its simulator)", ""]
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
