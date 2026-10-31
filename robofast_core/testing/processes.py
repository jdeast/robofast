"""Run robofast services as child processes, for the crash tests."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: The repository root (the services run with it on their path).
ROOT = Path(__file__).resolve().parents[2]


class Service:
    """``python -m module args...``; stderr goes to ``log_path``."""

    def __init__(self, module: str, *args: str, log_path: Path) -> None:
        self.log_path = log_path
        self._log = open(log_path, "w")  # closed in stop()
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", module, *args],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
        )

    @property
    def pid(self) -> int:
        return self.proc.pid

    def first_line(self, timeout: float = 30.0) -> str:
        """The first line the process prints on stdout."""
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline() if ready else ""
        if not line:
            self.stop()
            raise RuntimeError(f"{self.proc.args} did not start; see {self.log_path}")
        return line

    def log_line(self, text: str) -> str | None:
        """The first line of the process's log that contains ``text``."""
        return next((ln for ln in self.log_path.read_text().splitlines() if text in ln), None)

    def signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)  # in case a test stopped it
            self.proc.kill()
        self.proc.wait(10)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self._log.close()


class SupervisorProcess(Service):
    """``robofast_core.supervisor_service`` with ``config``; ready on return."""

    def __init__(self, config: dict[str, Any], workdir: Path, tag: str = "supervisor") -> None:
        path = workdir / f"{tag}.json"
        path.write_text(json.dumps(config))
        t0 = time.monotonic()
        super().__init__(
            "robofast_core.supervisor_service",
            "--config",
            str(path),
            log_path=workdir / f"{tag}.log",
        )
        hello = json.loads(self.first_line())
        self.startup = time.monotonic() - t0
        self.url: str = hello["url"]
        self.epoch: str = hello["epoch"]


class SequencerProcess(Service):
    """``robofast_core.sequencer_service`` with the enclosure ``config``."""

    def __init__(self, config: dict[str, Any], workdir: Path, tag: str = "sequencer") -> None:
        path = workdir / f"{tag}.json"
        path.write_text(json.dumps(config))
        super().__init__(
            "robofast_core.sequencer_service",
            "--config",
            str(path),
            log_path=workdir / f"{tag}.log",
        )
