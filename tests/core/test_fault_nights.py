"""Seeded nights of faults at the shipped timings (``-m fault_nights``).

Each test is one 10 h night of the real supervisor, sequencer and dispatch
scheduler on the fakes, on a virtual clock (robofast/testing/virtual_time.py),
with the timings robofast ships (SafetyConfig and SequencerConfig defaults:
a 20 min reopen hold, a 60 s lease, a 60 s staleness limit, a 5 min error
backoff), a 45 s shutter and 30-40 s slews and parks. Random faults are thrown
at it, seeded: unsafe weather, a sensor flickering at its threshold, a frozen
feed, a SafetyMonitor that fails or hangs, cameras and mounts that fail or
hang or whose device server restarts (``Connected`` false); the weather
turning unsafe moments after a reopen hold ran out (``reopen_race``, half
the nights); and targets of opportunity dropped into the scheduler's drop
box and rung (robofast.doorbell) at random moments.

The night is MINERVA's RV night: spectra fed by both telescopes (fake
KiwiSpec service and guiders, MINERVA's guider recovery ladder) above
imaging, which takes over while the spectrograph is down; the enclosure
has the Aqawan's fault channel. The failures that hit MINERVA:
``dome_flaky`` (the fault channel unreadable for one or two polls at a
time, night1121-1124), ``dome_fault`` (the enclosure reports a fault while
open, then it clears: April 2026's shutter faults), ``dome_hang`` (the dome
stops answering), ``spec_hang``/``spec_down`` (the KiwiSpec service runs
every request into its timeout, night1123, or refuses it),
``guide_lost`` (clouds over one telescope: its star is lost, and its
acquisitions time out) and ``cam_warm`` (a camera's cooler on at full
power but its CCD stuck at room temperature, MINERVA T4 2026-11-27/28:
both cameras are cooled to -50 C here, with no camera ladder, so the
camera is disabled for the night).
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
    its telescope gave up (or its camera was disabled: they are imaging).
I6  no light exposure (image or spectrum) starts while the dome is not open.
I7  the lease is revoked for an unreadable fault channel only once it has
    been unreadable for ``fault_unreadable_grace`` (never one failed read).
I8  a dome that hangs longer than the grace (and a few polls) is alarmed.
I9  no OpenShutter while the enclosure has reported a fault for a poll.
    (I1 counts a reported fault as unsafe; a hung dome's time to close
    counts from when it answers again.)
I10 at most ``spectrograph_max_failures`` spectra are sent to a
    spectrograph that is out, per outage (then it is marked down).
I11 no spectrum exposes for long with no telescope of its feed guiding
    (unless the spectrograph is out and so cannot be aborted).
I12 no guider stays failed (guiding into nothing): its telescope drops out.
I13 no telescope guides one spectrum past the feed's wait, the
    spectrograph's, the exposure, its margin and an unanswered abort.
I14 spectroscopy resumes after the spectrograph answers again.
I15 imaging goes on while the spectrograph is marked down.
I16 clouds never put a telescope out for the night: the guider ladder
    brings it back.
I17 no light frame starts on a CCD that is too warm (beyond the tolerance
    above its setpoint, its cooler on) once its cooler was last set more
    than ``settle_timeout`` ago; a telescope whose camera is disabled does
    not give up (it feeds spectra).

``FAULT_NIGHTS`` (default 20) and ``FAULT_SEED`` (default 0) choose the
seeds; a failure names its seed, and the same seed replays the same night.

Every real-world failure that is fixed gets a simulated regression test
(docs/design.md, "Testing and CI"): here when it is a whole-night or timing
failure (one that needs the shipped timings, many hours, or faults
overlapping), otherwise in the most fitting tier (a unit or scenario test
in ``tests/core``, the simulator tier, the e2e night). To add one here:

1. Reproduce it on the fakes: a device behaviour the fakes lack goes into
   ``robofast/testing/fakes.py`` (or the device's fake server) first, with
   a test of its own.
2. Add a fault kind: a name in ``KINDS`` (listed as often as it should be
   drawn), and its on and off in ``apply()`` (``on`` True when it starts,
   False when it ends; overlapping faults of one kind on one device are
   reference-counted there already). If the truth changes (the fault makes
   the sky unsafe, say), say so where ``unsafe`` is computed in the loop.
3. If no invariant above would catch it, add one, checked against the
   fakes' truth, not the timeline, with its own ``I<n>`` prefix in the
   violation message.
4. Check that it has teeth: with the fix reverted, some seed fails (a
   ``FAULT_SEED``/``FAULT_NIGHTS`` range wide enough to draw the fault);
   with the fix, ``FAULT_NIGHTS=100`` passes. Name the failure (night
   report, issue or PR) in the commit message.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import timedelta
from typing import Any

import pytest

from robofast import SequencerConfig
from robofast.cooling import CameraCooling, CoolingConfig
from robofast.doorbell import ring
from robofast.recovery import RecoveryLadder
from robofast.spectra import SpectrumRecorder
from robofast.testing.checks import assert_generations_disjoint
from robofast.testing.fakes import FakeDeviceError
from tests.core.test_dispatch import NIGHT as MIDNIGHT
from tests.core.test_dispatch import group, make, too
from tests.core.test_rv_spectra import (
    KIWISPEC,
    MTHOPKINS,
    CloudyGuider,
    OutageSpectrograph,
    _limits,
    _rv_group,
)

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
    "cam_restart", "tel_restart",
    "dome_flaky", "dome_flaky", "dome_fault", "dome_hang",
    "spec_hang", "spec_down", "guide_lost", "guide_lost", "cam_warm",
)  # fmt: skip
#: Kinds that hit one telescope (the fault's index), not the enclosure.
PER_TELESCOPE = ("cam", "tel", "guide")
#: MINERVA's enclosure fault channel (the Aqawan's ``Faults``) and its grace
#: for an unreadable read (SafetyConfig's default; night1124).
FAULT_CHANNEL = dict(fault_action="Faults", fault_unreadable_grace=15.0)
#: MINERVA T1's camera cooling (sites/minerva/aqawan1.json), at the
#: shipped poll; ``cam_warm`` sticks the CCD at ``WARM``.
COOLING = CoolingConfig(setpoint=-50.0, tolerance=0.75, settle_timeout=900.0)
WARM = 20.7
#: MINERVA's guider recovery ladder (its tN-recovery.json).
GUIDER_LADDER = {
    "guider": {"steps": ["reconnect"], "escalate_window": 300, "requires_parked": False}
}
#: SequencerConfig's shipped spectrum timings (SHIPPED_SEQ leaves them at
#: their defaults).
SPEC = SequencerConfig()
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


def _during(spans: list[list[float]], t: float) -> bool:
    return any(s <= t < e for s, e in spans)


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_night_of_faults(make_rig, tmp_path, seed):
    rng = random.Random(seed)
    box = tmp_path / "box"
    box.mkdir()
    forever = {"min_interval": 0}
    born = time.monotonic()
    # Spectra fed by both telescopes (MINERVA's RV night), above imaging,
    # which takes over while the spectrograph is down.
    rv = _rv_group("rv", priority=2.0)
    rv["requests"][0].setdefault("extra_params", {}).update(cadence=forever, max_attempts=10**6)
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
            rv,
        ],
        limits=_limits(),
        instruments=KIWISPEC,
        too_dropbox=box,
        # Near midnight all night (the targets stay up), but moving: a
        # spectrum's id is its visit's start time.
        clock=lambda: MIDNIGHT + timedelta(seconds=(time.monotonic() - born) / 100.0),
    )
    rig = make_rig(
        safety={**SHIPPED_SAFETY, **FAULT_CHANNEL},
        seq=SHIPPED_SEQ,
        command_timeout=5.0,
        telescopes=2,
        plan=plan,
    )
    rig.dome._travel = TRAVEL
    for tel in rig.tels:
        tel._slew_time, tel._park_time = 30.0, 40.0
    seqr = rig.sequencer
    spec = seqr.spectrograph = OutageSpectrograph(rig.timeline)
    seqr.spectra = SpectrumRecorder(tmp_path / "spectra", "minerva", MTHOPKINS, "kiwispec")
    guiders: list[CloudyGuider] = []
    for name in ("t1", "t2"):
        guiders.append(CloudyGuider(rig.timeline, name))
        seqr.units[name].guider = guiders[-1]
        seqr.units[name].recovery = RecoveryLadder.from_config(name, GUIDER_LADDER)
        seqr.units[name].cooling = CameraCooling(name, COOLING)
    sp = SHIPPED_SAFETY
    grace = FAULT_CHANNEL["fault_unreadable_grace"]
    viol: list[str] = []

    # The enclosure's fault channel: a fault it reports (dome_fault), or a
    # read that fails (dome_flaky: night1121-1124, the PAC STATUS read late,
    # 2-20 a night; a hung dome fails it through the fault gate).
    chan: dict[str, Any] = {"fault": False, "fail_until": -1.0}
    dome_action = rig.dome.Action

    def faults_action(name: str, parameters: str = "") -> str:
        if name != "Faults":
            return dome_action(name, parameters)
        if time.monotonic() < chan["fail_until"]:
            raise FakeDeviceError("no complete reply within 2 s")
        if chan["fault"]:
            return json.dumps({"fault": True, "error": False, "faults": "shutter fault"})
        return json.dumps({"fault": False, "error": False})

    rig.dome.Action = faults_action
    # Ground truth spans [start, end) relative to t0: the fault channel
    # unreadable, a fault reported, the dome hung.
    unreadable: list[list[float]] = []
    reported: list[list[float]] = []
    hangs: list[list[float]] = []

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
        now = time.monotonic()
        opens.append((now, *last_read[0]))
        late = now - t0 - (sp["poll_interval"] + sp["dome_timeout"] + 1)
        if any(s <= late and now - t0 < e for s, e in reported):
            viol.append(f"I9 t={now - t0:.0f}s: OpenShutter while the enclosure reports a fault")
        open_shutter()

    rig.dome.OpenShutter = opened
    light_starts: list[float] = []
    # I17's truth: when each camera's setpoint was last commanded, and since
    # when its CCD has been stuck warm (cam_warm).
    cooler_set = [-1e18 for _ in rig.cams]
    warm_since: list[float | None] = [None for _ in rig.cams]
    for i, cam in enumerate(rig.cams):
        start = cam.StartExposure
        cam_set = cam.set

        async def set_member(member, value, *, timeout=None, _set=cam_set, _i=i):
            await _set(member, value, timeout=timeout)
            if member == "SetCCDTemperature":
                cooler_set[_i] = time.monotonic()

        cam.set = set_member

        def started(duration, light, _start=start, _i=i, _cam=cam) -> None:
            now = time.monotonic()
            if light:
                light_starts.append(now - t0)
                if not rig.dome_open():
                    viol.append(
                        f"I6 t={now - t0:.0f}s: t{_i + 1} light frame, dome {rig.dome.state()}"
                    )
            ws = warm_since[_i]
            if (
                ws is not None
                and now - ws > 5.0
                and now - cooler_set[_i] > COOLING.settle_timeout + 5.0
                and _cam.CoolerOn
                and _cam.CCDTemperature > COOLING.setpoint + COOLING.tolerance
            ):
                viol.append(
                    f"I17 t={now - t0:.0f}s: t{_i + 1} exposing with its CCD at"
                    f" {_cam.CCDTemperature:.1f} C (setpoint {COOLING.setpoint} C), cooler set"
                    f" {now - cooler_set[_i]:.0f}s ago"
                )
            _start(duration, light)

        cam.StartExposure = started
    spec_expose = spec.expose

    async def expose(**frame):
        if not rig.dome_open():
            viol.append(
                f"I6 t={time.monotonic() - t0:.0f}s: spectrum {frame.get('object')},"
                f" dome {rig.dome.state()}"
            )
        return await spec_expose(**frame)

    spec.expose = expose

    faults = schedule(rng)
    refs: dict[tuple[str, int | None], int] = {}
    toos: dict[str, tuple[str, float]] = {}  # group id -> (telescope, when rung)
    taken_in: dict[str, float] = {}  # group id -> when seen in accepted/

    async def apply(f, on: bool) -> None:
        _, _, kind, i = f
        rel = time.monotonic() - t0
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
        key = (kind, i if kind.split("_")[0] in PER_TELESCOPE else None)
        refs[key] = refs.get(key, 0) + (1 if on else -1)
        if refs[key] != (1 if on else 0):
            return  # another fault of this kind on this device still holds
        if kind == "freeze":
            rig.weather.freeze() if on else rig.weather.thaw()
        elif kind == "sm_fail":
            rig.sm.fail("HTTP 500") if on else rig.sm.unfail()
        elif kind == "sm_hang":
            rig.sm.hang() if on else rig.sm.unhang()
        elif kind == "dome_flaky":
            pass  # its bursts are drawn in the loop
        elif kind == "dome_fault":
            chan["fault"] = on
            if on:
                reported.append([rel, NIGHT])
            else:
                reported[-1][1] = rel
        elif kind == "dome_hang":
            if on:
                rig.dome.hang()
                hangs.append([rel, NIGHT])
                unreadable.append(hangs[-1])
            else:
                rig.dome.unhang()
                hangs[-1][1] = rel
                dome_free_since[0] = time.monotonic()
        elif kind.startswith("spec_"):
            hung = refs.get(("spec_hang", None), 0) > 0
            spec.mode = "hang" if hung else "down" if refs.get(("spec_down", None)) else None
        elif kind == "guide_lost":
            guiders[i].set_clouds(on)
        elif kind == "cam_warm":
            # The cooler runs, the CCD stays warm (until the fault ends).
            cam = rig.cams[i]
            cam.coldest = WARM if on else None
            warm_since[i] = time.monotonic() if on else None
        elif kind[:3] in ("cam", "tel"):
            dev = (rig.cams if kind.startswith("cam") else rig.tels)[i]
            if kind.endswith("restart"):
                if on:
                    dev.restart()  # its server restarted: Connected false
            elif kind.endswith("fail"):
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
    dome_free_since = [t0]
    active: set[tuple] = set()
    unsafe_since: float | None = None
    safe_since: float | None = t0
    held_before: float | None = None
    close_since: float | None = None
    late_close = False
    next_burst = 0.0
    #: Spectrograph outages: [start, end, expose requests refused at the start].
    outages: list[list[float]] = []
    #: Seconds of each outage with the spectrograph marked down, the enclosure
    #: open and nothing else wrong: imaging should go on (by outage index).
    imaging_due: dict[int, list[float]] = {}
    unguided: dict[int, float] = {}  # spectrograph job -> since when no feed guides
    failed_since: dict[int, float] = {}  # guider index -> since when failed
    told: set[tuple] = set()  # (invariant, key) reported once
    bound = sp["poll_interval"] + sp["read_timeout"] + sp["dome_timeout"] + TRAVEL + 5
    #: The longest a guider may guide one spectrum: the feed's wait, the
    #: spectrograph lock's, the exposure, its margin, an unanswered abort.
    guide_bound = 2 * SPEC.spectrum_ready_timeout + 600 + SPEC.spectrum_margin + 45 + 120
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
            if "dome_flaky" in kinds and now >= next_burst:
                # One to two polls of failed reads, never as long as the grace.
                length = rng.uniform(1.0, 2 * sp["poll_interval"] + 1)
                chan["fail_until"] = now + length
                unreadable.append([rel, rel + length])
                next_burst = now + length + rng.uniform(20.0, 120.0)
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
                safe_since = None
            else:
                if unsafe_since is not None and now - unsafe_since < sp["poll_interval"]:
                    # An unsafe blip shorter than one poll can go unseen by
                    # design: the hold the supervisor counts goes on.
                    safe_since = held_before
                unsafe_since = None
                safe_since = now if safe_since is None else safe_since
            truth["safe_since"] = safe_since
            # I1, with a fault the enclosure reports, as with bad weather;
            # a hung dome cannot close, so its time counts from the unhang.
            if unsafe or chan["fault"]:
                close_since = now if close_since is None else close_since
                since = max(close_since, dome_free_since[0])
                hung = rig.dome._hung.is_set()
                if not late_close and not hung and not rig.dome_closed() and now - since > bound:
                    why = sorted(kinds) + (["reported fault"] if chan["fault"] else [])
                    viol.append(
                        f"I1 t={rel:.0f}s: must close ({why}) {now - since:.0f}s,"
                        f" dome {rig.dome.state()}"
                    )
                    late_close = True  # one report per episode
            else:
                close_since, late_close = None, False
            # Spectrograph outages (I10, I14, I15).
            if spec.mode is not None and (not outages or outages[-1][1] < NIGHT):
                outages.append([rel, NIGHT, spec.refused])
            elif spec.mode is None and outages and outages[-1][1] == NIGHT:
                outages[-1][1] = rel
                n = spec.refused - int(outages[-1][2])
                if n > SPEC.spectrograph_max_failures:
                    viol.append(
                        f"I10 t={rel:.0f}s: {n} spectra sent to a spectrograph that was out"
                        f" (from {outages[-1][0]:.0f}s; at most"
                        f" {SPEC.spectrograph_max_failures} before it is marked down)"
                    )
            if (
                outages
                and outages[-1][1] == NIGHT
                and seqr.spectrograph_down is not None
                and rig.dome_open()
                and kinds <= {"spec_hang", "spec_down", "too", "dome_flaky"}
            ):
                imaging_due.setdefault(len(outages) - 1, []).append(rel)
            # I11: no spectrum exposes with no telescope guiding it (the
            # spectrograph's abort cannot reach it while it is out).
            job = spec.running()
            settled = not outages or (
                outages[-1][1] < NIGHT and rel - outages[-1][1] > spec.abort_timeout + 15
            )
            if job is not None and settled:
                feed = job["frames"][0].get("telescopes") or []
                if any(guiders[int(t[1:]) - 1].guiding for t in feed):
                    unguided.pop(job["id"], None)
                else:
                    unguided.setdefault(job["id"], now)
                    if now - unguided[job["id"]] > 30 and ("I11", job["id"]) not in told:
                        told.add(("I11", job["id"]))
                        viol.append(
                            f"I11 t={rel:.0f}s: spectrum job {job['id']} ({feed}) exposing"
                            f" {now - unguided[job['id']]:.0f}s with no telescope guiding"
                        )
            for gi, g in enumerate(guiders):
                # I12: a failed guider is stopped (its telescope dropped);
                # I13: no telescope guides one spectrum past its bound.
                if g.state() == "failed":
                    failed_since.setdefault(gi, now)
                    key = ("I12", gi, failed_since[gi])
                    late = now - failed_since[gi] > SPEC.spectrum_ready_timeout + 60
                    if late and key not in told:
                        told.add(key)
                        viol.append(f"I12 t={rel:.0f}s: t{gi + 1} guiding into nothing (failed)")
                else:
                    failed_since.pop(gi, None)
                if g.started_at is not None and now - g.started_at > guide_bound:
                    key = ("I13", gi, g.started_at)
                    if key not in told:
                        told.add(key)
                        viol.append(
                            f"I13 t={rel:.0f}s: t{gi + 1} guiding {now - g.started_at:.0f}s"
                            f" (since {g.started_at - t0:.0f}s)"
                        )
            await asyncio.sleep(1.0)
    finally:
        serve.cancel()
        spec.mode = None
        rig.dome.unhang()
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
    working = {u.name for u in seqr.units.values() if not u.gave_up}
    last_fault = max((f[1] for f in faults if f[2] != "too"), default=0.0)
    started = [
        e.t
        for e in tl.entries()
        if (e.what == "StartExposure" or (e.what == "spectrum_started" and e.source == "seq"))
    ]
    if (
        working
        and last_fault < NIGHT - sp["reopen_hold"] - 1800
        and not [t for t in started if t > t0 + last_fault]
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
        started_too = [
            e for e in tl.events("target_start") if e.detail["target"].startswith(f"{gid} ")
        ]
        # An imaging ToO waits for good on a telescope whose camera is disabled.
        if not started_too and tel in working and seqr.units[tel].camera_disabled is None:
            viol.append(f"I5: {gid} (rung at {rung - t0:.0f}s, {tel}) never started")
    # I7: the lease is revoked for an unreadable fault channel only once it
    # has been unreadable for the grace (never for one failed read).
    for e in tl.events("alarm", "safety"):
        if "enclosure faults unreadable for" not in e.detail.get("msg", ""):
            continue
        t = e.t - t0
        lo, hi = t - grace + 0.5, t - sp["dome_timeout"] - 1
        if not any(s <= lo and end >= hi for s, end in unreadable):
            viol.append(f"I7 t={t:.0f}s: {e.detail['msg'][:80]} (truth: not unreadable that long)")
    # I8: a hung dome is alarmed.
    alarms = [e.t - t0 for e in tl.events("alarm", "safety")]
    for s, end in hangs:
        if end - s > grace + 2 * sp["poll_interval"] + sp["dome_timeout"] + 5:
            if not any(s <= t <= end + 1 for t in alarms):
                viol.append(f"I8: the dome hung {s:.0f}-{end:.0f}s and nothing alarmed")
    # I14: spectroscopy resumes once the spectrograph answers again.
    if outages and working:
        end = outages[-1][1]
        quiet = NIGHT - sp["reopen_hold"] - 3600 - SPEC.spectrograph_recovery_interval
        done = [e.t - t0 for e in tl.events("spectrum_done", "seq")]
        if end < quiet and last_fault < quiet and not [t for t in done if t > end]:
            viol.append(f"I14: no spectrum after the spectrograph came back at {end:.0f}s")
    # I15: imaging goes on while the spectrograph is down.
    disabled_at = {e.detail["telescope"]: e.t - t0 for e in tl.events("camera_disabled", "seq")}
    for k, due in imaging_due.items():
        if len(disabled_at) == len(seqr.units) and max(disabled_at.values()) <= due[-1]:
            continue  # no camera left to image with
        if len(due) >= 1200 and not [t for t in light_starts if due[0] <= t <= due[-1] + 60]:
            viol.append(
                f"I15: spectrograph down {outages[k][0]:.0f}-{outages[k][1]:.0f}s,"
                f" enclosure open {len(due)}s, and no image"
            )
    # I16: clouds (a lost guide star, an acquisition that times out) never
    # put a telescope out for the night: its guider ladder brings it back.
    for e in tl.events("giving_up", "seq"):
        errors = [
            x
            for x in tl.events("telescope_error", "seq")
            if x.detail["telescope"] == e.detail["telescope"] and x.t <= e.t
        ]
        if errors and "GuiderError" in errors[-1].detail["error"]:
            viol.append(
                f"I16 t={e.t - t0:.0f}s: {e.detail['telescope']} gave up over its guider"
                f" ({e.detail.get('reason') or errors[-1].detail['error']})"
            )
    # I17: a disabled camera does not put its telescope out for the night.
    for e in tl.events("giving_up", "seq"):
        name = e.detail["telescope"]
        errors = [
            x
            for x in tl.events("telescope_error", "seq")
            if x.detail["telescope"] == name and x.t <= e.t
        ]
        if errors and "camera cooling fault" in errors[-1].detail["error"]:
            viol.append(f"I17 t={e.t - t0:.0f}s: {name} gave up over its camera's cooling")
    assert not viol, f"seed {seed}: " + "; ".join(viol)


@pytest.mark.parametrize("t2_gave_up", [False, True], ids=["t2_observing", "t2_gave_up"])
async def test_a_device_server_restarts_mid_night(make_rig, t2_gave_up):
    """Night 2026-11-09/10 (robofast e3e9cdc): T2's PWI server was restarted
    mid-night and came back with Connected false, so every AtPark read of T2
    by the supervisor's park-before-open check answered NotConnected (0x407),
    for good. At the next lease the check refused it ("cannot confirm parked:
    t2"), and every lease after: T1 sat idle for the rest of the night with
    nothing wrong with it, and parking T2 by hand did not release it.

    Here, at the shipped timings, with ``park_before_close`` (MINERVA's): T2's
    telescope server restarts 2 h in (``t2_gave_up``: after T2 gave up for
    the night, and T2 is then parked by hand, as on the night), the weather
    closes the enclosure an hour later, and once it reopens T1 (and T2, if
    it still observes) must observe again. Without the reconnect on
    NotConnected (robofast/owner.py) no lease is ever granted again."""
    plan = make(
        [
            group(
                "a", telescopes=["t1"], exptime=300.0, count=3, cadence={"min_interval": 0},
                max_attempts=10**6,
            ),
            group(
                "b", telescopes=["t2"], exptime=300.0, count=3, cadence={"min_interval": 0},
                max_attempts=10**6,
            ),
        ]
    )  # fmt: skip
    rig = make_rig(
        safety={**SHIPPED_SAFETY, "park_before_close": True},
        seq=SHIPPED_SEQ,
        command_timeout=5.0,
        telescopes=2,
        plan=plan,
    )
    rig.dome._travel = TRAVEL
    for tel in rig.tels:
        tel._slew_time, tel._park_time = 30.0, 40.0
    t1cam, t2tel, t2cam = rig.cams[0], rig.tels[1], rig.cams[1]
    t2 = rig.sequencer.units["t2"]
    serve = asyncio.create_task(plan.serve())
    await rig.start()
    t0 = time.monotonic()
    try:
        if t2_gave_up:
            await asyncio.sleep(1800.0)
            t2tel.fail("PWI2 closed")  # T2 errors until it gives up
            async with asyncio.timeout(3600.0):
                while not t2.gave_up:
                    await asyncio.sleep(1.0)
            t2tel.unfail()
        await asyncio.sleep(t0 + 2 * 3600.0 - time.monotonic())
        assert rig.dome_open() and t1cam.exposures_started > 0
        t2tel.restart()  # the server restarted: Connected false
        if t2_gave_up:
            t2tel._parked, t2tel._tracking = True, False  # parked by hand
        await asyncio.sleep(3600.0)
        rig.weather.set_safe(False)
        await asyncio.sleep(600.0)
        assert rig.dome_closed()
        rig.weather.set_safe(True)
        t_safe = time.monotonic()
        n1, n2 = t1cam.exposures_started, t2cam.exposures_started
        await asyncio.sleep(SHIPPED_SAFETY["reopen_hold"] + 3600.0)
    finally:
        serve.cancel()
    after = f"{time.monotonic() - t_safe:.0f} s after the weather cleared"
    assert rig.dome_open(), f"enclosure not reopened {after} (lease refused?)"
    assert t1cam.exposures_started > n1, f"T1 held: no exposure {after}"
    assert t2tel.Connected
    if not t2_gave_up:
        assert not t2.gave_up
        assert t2cam.exposures_started > n2, f"T2: no exposure {after}"
