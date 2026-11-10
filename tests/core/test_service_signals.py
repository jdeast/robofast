"""The services where the event loop has no signal handlers (Windows'
loops raise NotImplementedError from ``add_signal_handler``; Ctrl-C arrives
as KeyboardInterrupt and asyncio.run cancels), and a caller that handles
the signals itself (``handle_signals=False``)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from robofast import sequencer_service


class _Seq:
    def __init__(self) -> None:
        self.plan = SimpleNamespace()
        self.imaging = None
        self.units: dict = {}
        self.ran = False

    async def run(self) -> None:
        self.ran = True


class _Remote:
    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("handle_signals", [True, False])
async def test_the_sequencer_runs_where_the_loop_has_no_signal_handlers(
    monkeypatch, handle_signals
):
    loop = asyncio.get_running_loop()
    asked: list[int] = []

    def unavailable(sig, *args):
        asked.append(sig)
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unavailable)
    seq = _Seq()
    await sequencer_service.run(seq, _Remote(), handle_signals=handle_signals)  # type: ignore[arg-type]
    assert seq.ran
    # Asked for SIGTERM and SIGINT, and ran on; or not asked at all.
    assert len(asked) == (2 if handle_signals else 0)
