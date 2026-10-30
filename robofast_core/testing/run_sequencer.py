"""A sequencer process for the crash tests: one target, observed forever.

    python -m robofast_core.testing.run_sequencer --supervisor URL \\
        --devices HOST:PORT [--exptime S] [--config JSON] [--remote JSON]

The telescope and camera are device 0 at ``--devices``; the lease comes from
the supervisor service at ``--supervisor``. ``--config`` and ``--remote`` are
:class:`SequencerConfig` and :class:`RemoteSupervisor` keyword arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from robofast_core.alpaca import alpaca_owner
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.sequencer import Observation, Sequencer, SequencerConfig
from robofast_core.timeline import Timeline


async def run(args: argparse.Namespace) -> None:
    tl = Timeline()
    remote = RemoteSupervisor(args.supervisor, **json.loads(args.remote))
    seq = Sequencer(
        "seq",
        supervisor=remote,
        telescope=alpaca_owner("telescope", "telescope", args.devices, tl),
        camera=alpaca_owner("camera", "camera", args.devices, tl),
        plan=lambda: Observation("target", exptime=args.exptime, count=1000),
        timeline=tl,
        config=SequencerConfig(**json.loads(args.config)),
    )
    try:
        await seq.run()
    finally:
        await remote.aclose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--supervisor", required=True)
    ap.add_argument("--devices", required=True)
    ap.add_argument("--exptime", type=float, default=30.0)
    ap.add_argument("--config", default="{}")
    ap.add_argument("--remote", default="{}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
