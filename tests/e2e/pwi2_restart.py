"""Test helper: (re)start a PWI2 simulator in a process of its own on a fixed
port, as a restart hook does for the real PWI2 (docs/recovery.md, "The
restart hook"): kill the one in ``--pidfile`` if it is still running, start a
new one, and exit 0 once it answers (1 if it does not within ``--wait`` s).

``--initial`` starts it as PWI after an operator set it up (connected,
enabled, homed, ``--model-file`` loaded); otherwise it starts as PWI does
after a restart (not connected, not homed, ``Default_Mount_Model.PXP``).
Loopback only (the simulator registers with robofast.hwgate)."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--pidfile", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--initial", action="store_true")
    ap.add_argument("--model-file", default="")
    ap.add_argument("--wait", type=float, default=30.0)
    args = ap.parse_args()
    pidfile = Path(args.pidfile)
    if pidfile.exists():
        old = int(pidfile.read_text())
        with contextlib.suppress(ProcessLookupError):
            os.kill(old, signal.SIGKILL)
        t0 = time.monotonic()
        while _alive(old) and time.monotonic() - t0 < 5:
            time.sleep(0.05)
    cmd = [
        sys.executable,
        "-m",
        "robofast.devices.pwi_sim",
        "--api",
        "pwi2",
        "--port",
        str(args.port),
        "--slew-rate",
        "20",
        "--home-time",
        "2",
        "--alt",
        "50",
        "--az",
        "180",
    ]
    if args.initial:
        cmd += ["--ready", "--homed", "--model-file", args.model_file]
    else:
        cmd += ["--restarted"] + (["--model-file", args.model_file] if args.model_file else [])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]),
    }
    with open(args.log, "ab") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=ROOT,
            env=env,
        )
    pidfile.write_text(str(proc.pid))
    url = f"http://127.0.0.1:{args.port}/?cmd=getsystem"
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"the simulator exited ({proc.returncode})", file=sys.stderr)
            return 1
        with contextlib.suppress(OSError):
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    print("PWI2 answers")
                    return 0
        time.sleep(0.1)
    proc.kill()
    print(f"PWI2 not answering after {args.wait} s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
