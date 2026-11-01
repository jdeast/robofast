"""Run ConformU against the lamp Switch server, on its simulated Pulizzi unit.

    python scripts/conformu_pulizzi.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.pulizzi_server --simulate`` (the in-process
Pulizzi simulator) on a free loopback port with two lamp outlets (writable)
and one other outlet (read-only), then runs ConformU's ``conformance``
check (with the switch-writing tests enabled by
``scripts/conformu-switch.settings.json``) and its ``alpacaprotocol``
check. The lamps' ``max_on`` is long enough that the limit never trips
during the run. Exits non-zero if either check reports any error or issue.
Logs and results go to ``--out``; a summary is appended to
``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_pdu import run_check, start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-pulizzi")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = out / "pulizzi.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "port": {"type": "sim", "sim": {"on": [3]}},
                "outlets": [
                    {"outlet": 2, "name": "ThAr1", "lamp": True, "max_on": 3600},
                    {"outlet": 4, "name": "Incand", "lamp": True, "max_on": 3600},
                    {"outlet": 3, "name": "other"},
                ],
                "poll_interval": 0.5,
                "status_max_age": 0.05,
            }
        )
    )
    server, hello = start(
        ["robofast_core.devices.pulizzi_server", "--config", str(config), "--simulate"],
        out / "server.log",
    )
    try:
        url = json.loads(hello)["url"] + "/api/v1/switch/0"
        print(f"Pulizzi simulator; Switch {url}", flush=True)
        summaries = [
            run_check(args.conformu, c, url, out) for c in ("conformance", "alpacaprotocol")
        ]
    finally:
        server.terminate()
        server.wait(20)

    failed = False
    report = ["### ConformU (lamp Switch server vs the Pulizzi simulator)", ""]
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
