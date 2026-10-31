"""Filter selection against OmniSim's FilterWheel, in a closed generation
(dark frames, so no lease and no supervisor are involved)."""

from __future__ import annotations

import asyncio

import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.plan import StaticPlan
from robofast_core.sequencer import Sequencer, SequencerConfig, TelescopeUnit
from robofast_core.timeline import Timeline

from .conftest import COMMAND_TIMEOUT, SIM_SEQ, _Plain

pytestmark = pytest.mark.alpaca_sim


class NoLeases:
    """A lease source that must never be asked for a lease."""

    async def lease_status(self):
        return None

    async def recover(self, lease_id, holder):
        return False

    async def request_open(self, holder):
        raise AssertionError("dark frames must not request a lease")


async def test_filter_selected_by_name_on_omnisim(omnisim, truth):
    wheel = _Plain(omnisim, "filterwheel")
    try:
        names = wheel.get("Names")
        assert len(names) >= 2, names
        want = names[-1] if wheel.get("Position") != len(names) - 1 else names[0]
        tl = Timeline()

        def owner(kind):
            return alpaca_owner(kind, kind, omnisim, tl, default_timeout=COMMAND_TIMEOUT)

        owners = [owner(k) for k in ("telescope", "camera", "filterwheel")]
        plan = StaticPlan(
            [{"name": "dark", "light": False, "exptime": 0.5, "count": 1, "filter": want}],
            ["telescope"],
        )
        seq = Sequencer(
            "seq",
            supervisor=NoLeases(),
            telescopes=[TelescopeUnit("telescope", *owners[:2], filterwheel=owners[2])],
            plan=plan,
            timeline=tl,
            config=SequencerConfig(**SIM_SEQ),
        )
        try:
            await asyncio.wait_for(seq.run(), 120)
        finally:
            for o in owners:
                o.close()
        assert plan.history == [("telescope", "dark", "complete", 1)]
        assert wheel.get("Position") == names.index(want)
        assert [e.detail["filter"] for e in tl.events("filter_selected")] == [want]
        assert truth.parked() and truth.closed()
    finally:
        wheel.http.close()
