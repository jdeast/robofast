"""Run ConformU against the ADC server, on its simulated prism motors.

    python scripts/conformu_adc.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.adc_server --simulate`` (the in-process
simulator of the two prism motors, trusted as referenced, no telescope
input) on a free loopback port, then runs ConformU's ``conformance`` and
``alpacaprotocol`` checks against its two Rotators (prism A, prism B) and
then its Switch (the tracking service; the switch-writing tests enabled by
``scripts/conformu-switch.settings.json``: ConformU turns tracking on, which
holds without telescope input, and off, which turns the prisms to neutral).
Exits non-zero if any check reports an error or issue. Logs and results go
to ``--out``; a summary is appended to ``$GITHUB_STEP_SUMMARY`` when set.
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

#: (device type, number, tag), in the order run: the rotators first, while
#: tracking is off (ConformU's switch test turns it on and off).
DEVICES = (("rotator", 0, "rotator-a"), ("rotator", 1, "rotator-b"), ("switch", 0, "switch"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-adc")
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = out / "adc.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "port": {"type": "sim", "sim": {"speed": 4000.0}},
                "trust_counter": True,
                "telescope": None,
                "poll_interval": 0.05,
            }
        )
    )
    server, hello = start(
        ["robofast_core.devices.adc_server", "--config", str(config), "--simulate"],
        out / "server.log",
    )
    summaries = []
    try:
        base = json.loads(hello)["url"]
        print(f"ADC motor simulator; Alpaca {base}", flush=True)
        for device, number, tag in DEVICES:
            url = f"{base}/api/v1/{device}/{number}"
            for check in args.checks.split(","):
                summaries.append(run_check(args.conformu, device, check, url, out, tag=tag))
                print(json.dumps({k: v for k, v in summaries[-1].items() if k != "details"}))
    finally:
        server.terminate()
        server.wait(20)

    failed = False
    report = ["### ConformU (ADC Rotators and Switch vs the prism-motor simulator)", ""]
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
