"""A sequencer process for the crash tests: one target, observed forever.

    python -m robofast_core.testing.run_sequencer --supervisor URL \\
        --devices HOST:PORT [--name NAME] [--exptime S] [--config JSON] [--remote JSON]

The telescope and camera are device 0 at ``--devices``; the lease comes from
the supervisor service at ``--supervisor``. ``--name`` is the lease holder
identity, the same across restarts. ``--config`` and ``--remote`` are
:class:`SequencerConfig` and :class:`RemoteSupervisor` keyword arguments.

SIGTERM stops it cleanly: the observation is cancelled, the devices cleaned
up and the lease released. Exit status 3 means "needs an operator" (quarantined, or gave up for the
night): the process manager should not restart it (``RestartPreventExitStatus``
in ``deploy/robofast-sequencer@.service``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from robofast_core.alpaca import alpaca_owner
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.sequencer import Observation, Sequencer, SequencerConfig
from robofast_core.timeline import Timeline

#: Exit status for "do not restart me; an operator must look".
NEEDS_OPERATOR = 3


async def run(args: argparse.Namespace) -> Sequencer:
    tl = Timeline()
    remote = RemoteSupervisor(args.supervisor, **json.loads(args.remote))
    seq = Sequencer(
        args.name,
        supervisor=remote,
        telescope=alpaca_owner("telescope", "telescope", args.devices, tl),
        camera=alpaca_owner("camera", "camera", args.devices, tl),
        plan=lambda: Observation("target", exptime=args.exptime, count=1000),
        timeline=tl,
        config=SequencerConfig(**json.loads(args.config)),
    )
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await seq.run()
    except asyncio.CancelledError:
        pass  # SIGTERM: run() cleaned up and released the lease on its way out
    finally:
        await remote.aclose()
    return seq


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--supervisor", required=True)
    ap.add_argument("--devices", required=True)
    ap.add_argument("--name", default="seq")
    ap.add_argument("--exptime", type=float, default=30.0)
    ap.add_argument("--config", default="{}")
    ap.add_argument("--remote", default="{}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    seq = asyncio.run(run(args))
    if seq.quarantined or seq.gave_up:
        sys.exit(NEEDS_OPERATOR)


if __name__ == "__main__":
    main()
