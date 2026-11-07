"""Seeded nights of faults at the shipped timings (``-m fault_nights``).

Each test is one 10 h night of the real supervisor, sequencer and dispatch
scheduler on the fakes, on a virtual clock (robofast/testing/virtual_time.py),
with the timings robofast ships (SafetyConfig and SequencerConfig defaults:
a 20 min reopen hold, a 60 s lease, a 60 s staleness limit, a 5 min error
backoff), a 45 s shutter and 30-40 s slews and parks. Random faults are thrown
at it, seeded: unsafe weather, a sensor flickering at its threshold, a frozen
feed, a SafetyMonitor that fails or hangs, cameras and mounts that fail or
hang; the weather turning unsafe moments after a reopen hold ran out
(``reopen_race``, half the nights); and targets of opportunity dropped into
the scheduler's drop box and rung (robofast.doorbell) at random moments.
Faults of one kind on one device may overlap (the device is faulted while
any is active).

Ground truth (the fakes, not the timeline) is sampled every simulated second:

I1  bad weather always wins: whenever the truth is unsafe (unsafe weather, a
    feed frozen longer than ``stale_after``, a failing or hung
    SafetyMonitor), the dome is closed within poll + read timeout + dome
    timeout + travel (+5 s).
I2  OpenShutter only right after a SafetyMonitor reading (within 1 s) that
    saw the truth safe for ``reopen_hold`` (less a poll); an unsafe blip
    shorter than one poll can go unseen by design, so it does not restart the
    hold here either. (The weather turning between that reading and the open,
    in the same pass, is the read's own latency: not checked.)
I3  generations are disjoint and fenced (``assert_generations_disjoint``).
I4  recovery is automatic: an exposure after the last fault clears, unless
    every telescope gave up for the night.
I5  no target of opportunity is lost: every ToO rung into the drop box is
    taken in at once (within 1 s) and started before the night ends, unless
    its telescope gave up.
I6  no light exposure starts while the dome is not open.

``FAULT_NIGHTS`` (default 20) and ``FAULT_SEED`` (default 0) choose the
seeds; a failure names its seed, and the same seed replays the same night.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

import pytest

from robofast.doorbell import ring
from robofast.testing.checks import assert_generations_disjoint
from tests.core.test_dispatch import group, make, too

pytestmark = [pytest.mark.fault_nights, pytest.mark.virtual_time, pytest.mark.timeout(600)]

#: SafetyConfig's and SequencerConfig's shipped defaults, over the fast ones
#: in conftest's FAST_SAFETY and FAST_SEQ.
SHIPPED_SAFETY = dict(
    poll_interval=5.0,
    read_timeout=5.0,
    stale_after=60.0,
    reopen_hold=1200.0,
    lease_ttl=60.0,
    shutter_timeout=120.0,
    dome_timeout=5.0,
)
SHIPPED_SEQ = dict(
    poll_interval=0.5,
    slew_timeout=120.0,
    readout_margin=60.0,
    lease_renew_interval=10.0,
    cancel_ack_timeout=10.0,
    park_timeout=120.0,
    error_backoff=300.0,
    max_consecutive_errors=3,
    recover_poll=1.0,
    mode_idle=10.0,
)
NIGHT = 10 * 3600.0
TRAVEL = 45.0  # shutter travel, s
KINDS = (
    "unsafe", "unsafe", "unsafe", "freeze", "sm_fail", "sm_hang",
    "cam_fail", "cam_hang", "tel_fail", "tel_hang", "flicker", "too", "too",
)  # fmt: skip
SEEDS = range(
    int(os.environ.get("FAULT_SEED", "0")),
    int(os.environ.get("FAULT_SEED", "0")) + int(os.environ.get("FAULT_NIGHTS", "20")),
)


def schedule(rng: random.Random) -> list[tuple[float, float, str, int]]:
    """(start, end, kind, telescope index) over the night, seconds."""
    faults = []
    t = rng.uniform(600, 3600)
    while t < NIGHT - 3 * 3600:
        faults.append((t, t + rng.expovariate(1 / 900.0) + 5, rng.choice(KINDS), rng.randrange(2)))
        t += rng.expovariate(1 / 2400.0) + 60
    return faults


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_night_of_faults(make_rig, tmp_path, seed):
    rng = random.Random(seed)
    box = tmp_path / "box"
    box.mkdir()
    forever = {"min_interval": 0}
    plan = make(
        [
            # Standing requests that outlast the night's failures (dispatch
            # would mark one FAILED after max_attempts).
            group(
                "a", telescopes=["t1"], exptime=300.0, count=3, cadence=forever, max_attempts=10**6
            ),
            group(
                "b", telescopes=["t2"], exptime=300.0, count=3, cadence=forever, max_attempts=10**6
            ),
        ],
        too_dropbox=box,
    )
    rig = make_rig(
        safety=SHIPPED_SAFETY, seq=SHIPPED_SEQ, command_timeout=5.0, telescopes=2, plan=plan
    )
    rig.dome._travel = TRAVEL
    for tel in rig.tels:
        tel._slew_time, tel._park_time = 30.0, 40.0
    sp = SHIPPED_SAFETY
    viol: list[str] = []

    # The truth at each SafetyMonitor reading, and at each OpenShutter the
    # last reading's: (sent, read at, safe since at the read). The weather
    # changes only in the loop below, so its last state is the truth now.
    truth: dict[str, float | None] = {"safe_since": None}
    last_read: list[tuple[float, float | None]] = [(-1e9, None)]
    opens: list[tuple[float, float, float | None]] = []
    sm_get = rig.sm.get

    async def read(member, **kw):
        value = await sm_get(member, **kw)
        last_read[0] = (time.monotonic(), truth["safe_since"])
        return value

    rig.sm.get = read
    open_shutter = rig.dome.OpenShutter

    def opened() -> None:
        opens.append((time.monotonic(), *last_read[0]))
        open_shutter()

    rig.dome.OpenShutter = opened
    for i, cam in enumerate(rig.cams):
        start = cam.StartExposure

        def started(duration, light, _start=start, _i=i) -> None:
            if light and not rig.dome_open():
                viol.append(
                    f"I6 t={time.monotonic() - t0:.0f}s: t{_i + 1} light frame,"
                    f" dome {rig.dome.state()}"
                )
            _start(duration, light)

        cam.StartExposure = started

    faults = schedule(rng)
    refs: dict[tuple[str, int | None], int] = {}
    toos: dict[str, tuple[str, float]] = {}  # group id -> (telescope, when rung)
    taken_in: dict[str, float] = {}  # group id -> when seen in accepted/

    async def apply(f, on: bool) -> None:
        _, _, kind, i = f
        if kind == "too":
            if on:
                gid = f"too{len(toos)}"
                g = group(
                    gid,
                    telescopes=[f"t{i + 1}"],
                    too=too(rng.choice(["now", "now-after-readout", "next"])),
                    exptime=60.0,
                    count=1,
                )
                tmp = box / f".{gid}.tmp"
                tmp.write_text(json.dumps(g))
                os.replace(tmp, box / f"{gid}.json")
                toos[gid] = (f"t{i + 1}", time.monotonic())
                await ring(box)
            return
        key = (kind, i if kind[:3] in ("cam", "tel") else None)
        refs[key] = refs.get(key, 0) + (1 if on else -1)
        if refs[key] != (1 if on else 0):
            return  # another fault of this kind on this device still holds
        if kind == "freeze":
            rig.weather.freeze() if on else rig.weather.thaw()
        elif kind == "sm_fail":
            rig.sm.fail("HTTP 500") if on else rig.sm.unfail()
        elif kind == "sm_hang":
            rig.sm.hang() if on else rig.sm.unhang()
        elif kind[:3] in ("cam", "tel"):
            dev = (rig.cams if kind.startswith("cam") else rig.tels)[i]
            if kind.endswith("fail"):
                dev.fail(f"{kind} {i}") if on else dev.unfail()
            else:
                dev.hang() if on else dev.unhang()

    # reopen_race: in some nights the weather turns unsafe moments after a
    # reopen hold has run out, while the enclosure is about to open.
    race = rng.random() < 0.5
    race_until = 0.0
    races = 0  # at most three a night, so the night still observes
    serve = asyncio.create_task(plan.serve())
    await rig.start()
    t0 = time.monotonic()
    active: set[tuple] = set()
    unsafe_since: float | None = None
    safe_since: float | None = t0
    held_before: float | None = None
    reported = False
    bound = sp["poll_interval"] + sp["read_timeout"] + sp["dome_timeout"] + TRAVEL + 5
    try:
        while (now := time.monotonic()) - t0 < NIGHT:
            rel = now - t0
            for f in faults:
                on = f[0] <= rel < f[1]
                if on and f not in active:
                    active.add(f)
                    await apply(f, True)
                elif not on and f in active:
                    active.discard(f)
                    await apply(f, False)
            for gid in toos.keys() - taken_in.keys():
                if (box / "accepted" / f"{gid}.json").exists():
                    taken_in[gid] = now
            kinds = {f[2] for f in active}
            if (
                race
                and races < 3
                and safe_since is not None
                and race_until < now
                and now - safe_since >= sp["reopen_hold"] + rng.uniform(0, 2 * sp["poll_interval"])
                and not rig.dome_open()
            ):
                race_until = now + rng.uniform(30, 120)
                races += 1
            if "flicker" in kinds:
                rig.weather.set_safe(int(rel / 7) % 2 == 0)
            else:
                rig.weather.set_safe("unsafe" not in kinds and now >= race_until)
            frozen = rig.weather.frozen_at
            stale = frozen is not None and now - frozen > sp["stale_after"]
            unsafe = not rig.weather.safe or stale or bool(kinds & {"sm_fail", "sm_hang"})
            if unsafe:
                if unsafe_since is None:
                    unsafe_since, held_before = now, safe_since
                if not reported and not rig.dome_closed() and now - unsafe_since > bound:
                    viol.append(
                        f"I1 t={rel:.0f}s: unsafe ({sorted(kinds)}) {now - unsafe_since:.0f}s,"
                        f" dome {rig.dome.state()}"
                    )
                    reported = True  # one report per episode
                safe_since = None
            else:
                if unsafe_since is not None and now - unsafe_since < sp["poll_interval"]:
                    # An unsafe blip shorter than one poll can go unseen by
                    # design: the hold the supervisor counts goes on.
                    safe_since = held_before
                unsafe_since, reported = None, False
                safe_since = now if safe_since is None else safe_since
            truth["safe_since"] = safe_since
            await asyncio.sleep(1.0)
    finally:
        serve.cancel()
    for t_open, t_read, since in opens:
        held = t_read - since if since is not None else -1.0
        if held < sp["reopen_hold"] - sp["poll_interval"] - 1:
            viol.append(
                f"I2 t={t_open - t0:.0f}s: OpenShutter on a reading after {held:.0f}s of safe truth"
            )
        if t_open - t_read > 1.0:
            viol.append(
                f"I2 t={t_open - t0:.0f}s: OpenShutter {t_open - t_read:.1f}s after its reading"
            )
    tl = rig.timeline
    working = {u.name for u in rig.sequencer.units.values() if not u.gave_up}
    last_fault = max((f[1] for f in faults if f[2] != "too"), default=0.0)
    if (
        working
        and last_fault < NIGHT - sp["reopen_hold"] - 1800
        and not [e for e in tl.entries() if e.what == "StartExposure" and e.t > t0 + last_fault]
    ):
        viol.append(f"I4: no exposure after the last fault cleared at {last_fault:.0f}s")
    try:
        assert_generations_disjoint(tl, "seq")
    except AssertionError as exc:
        viol.append(f"I3: {exc}")
    for gid, (tel, rung) in toos.items():
        if gid not in taken_in:
            viol.append(f"I5: {gid} never taken in from the drop box")
            continue
        if taken_in[gid] - rung > 1.0 + 1e-3:  # sampled each second
            viol.append(f"I5: {gid} taken in {taken_in[gid] - rung:.1f}s after its ring")
        started = [e for e in tl.events("target_start") if e.detail["target"].startswith(f"{gid} ")]
        if not started and tel in working:
            viol.append(f"I5: {gid} (rung at {rung - t0:.0f}s, {tel}) never started")
    assert not viol, f"seed {seed}: " + "; ".join(viol)
