"""The recovery ladder's last step (robofast/escalation.py, docs/recovery.md,
"Escalation"): an agent hook that fixes the device (it rejoins) or fails or
hangs (dropped for the night, the others observe on), no agent configured,
the rate limits, robofast's own check after the agent, the one summary per
incident through the status snapshot and the console's alarms -- with fake
devices and small Python agents run as real subprocesses."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from robofast.console.aggregate import Aggregator
from robofast.escalation import EscalationPolicy, Incident, fault_signature, run_agent
from robofast.owner import DeviceOwner
from robofast.recovery import DevicePolicy, RecoveryLadder, RecoveryResult
from robofast.sequencer_service import snapshot
from robofast.testing.checks import wait_until
from robofast.testing.fakes import FakeCamera, FakeDeviceError


async def no_sleep(s: float) -> None:
    await asyncio.sleep(0)


def agent(tmp_path: Path, body: str, name: str = "agent.py") -> list[str]:
    """A Python agent hook: ``body`` runs with ``ctx`` (the context read
    from stdin), ``summary`` (the summary file's path) and ``work`` (the
    test's directory)."""
    script = tmp_path / name
    script.write_text(
        "import json, os, sys, time, pathlib\n"
        "ctx = json.load(sys.stdin)\n"
        "summary = pathlib.Path(os.environ['ROBOFAST_ESCALATION_SUMMARY'])\n"
        f"work = pathlib.Path({str(tmp_path)!r})\n" + textwrap.dedent(body)
    )
    return [sys.executable, str(script)]


class MendableCamera(FakeCamera):
    """Fails every call while ``broken``, or until ``mended_by`` exists (a
    file an agent hook creates: the fix it makes "by its own means")."""

    broken = False
    mended_by: Path | None = None

    async def _gate(self) -> None:
        await super()._gate()
        if self.broken and not (self.mended_by is not None and self.mended_by.exists()):
            raise FakeDeviceError("camera driver crashed")


def rig_with_escalation(make_rig, tmp_path: Path, escalate: dict | None):
    rig = make_rig(exptime=0.05, count=200, telescopes=2, seq={"error_backoff": 0.1})
    cam = MendableCamera()
    cam.mended_by = tmp_path / "mended"
    unit = rig.sequencer.units["t1"]
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    entry: dict = {"camera": {"steps": ["reconnect"], "verify_timeout": 0.1}}
    if escalate is not None:
        entry["escalate"] = escalate
    unit.recovery = RecoveryLadder.from_config("t1", entry, sleep=no_sleep)
    return rig, cam, unit


async def break_it(rig, cam: MendableCamera) -> None:
    await rig.start()
    await wait_until(lambda: cam.exposures_started >= 1, timeout=10)
    cam.broken = True


def incident_events(rig) -> list:
    return rig.timeline.events("incident")


async def test_an_agent_that_fixes_it_the_telescope_rejoins(make_rig, tmp_path):
    cmd = agent(
        tmp_path,
        """
        (work / "context.json").write_text(json.dumps(ctx))
        time.sleep(1.0)  # the other telescope observes meanwhile
        (work / "mended").touch()
        summary.write_text(
            "Diagnosis: the camera driver crashed.\\nDid: restarted it.\\nResult: fixed."
        )
        """,
    )
    rig, cam, unit = rig_with_escalation(
        make_rig, tmp_path, {"agent": {"command": cmd, "timeout": 30}, "safe": ["site line"]}
    )
    await break_it(rig, cam)
    await wait_until(lambda: rig.timeline.events("escalation_start"), timeout=20)
    assert snapshot(rig.sequencer)["telescopes"]["t1"]["state"] == "escalated"
    t2 = rig.cams[1].exposures_started
    await wait_until(lambda: incident_events(rig), timeout=30)
    # The other telescope was not held up while the agent worked.
    assert rig.cams[1].exposures_started > t2
    [inc] = incident_events(rig)
    assert inc.detail["outcome"] == "recovered" and inc.detail["level"] == "alarm"
    assert "Diagnosis: the camera driver crashed." in inc.detail["summary"]
    assert "RECOVERED" in inc.detail["summary"] and "reconnect: failed" in inc.detail["summary"]
    # It rejoins the night.
    n = cam.exposures_started
    await wait_until(lambda: cam.exposures_started > n + 2, timeout=20)
    assert not unit.gave_up and unit.escalation is None
    assert not rig.timeline.events("giving_up")
    # What the agent was given.
    ctx = json.loads((tmp_path / "context.json").read_text())
    assert ctx["telescope"] == "t1" and ctx["device"] == "camera"
    assert "camera driver crashed" in ctx["error"]
    assert ctx["tried"][0]["rung"] == "reconnect" and ctx["tried"][0]["outcome"].startswith(
        "failed"
    )
    assert ctx["other_telescopes"] == ["t2"]
    assert "site line" in ctx["safe"] and len(ctx["safe"]) > 1
    assert ctx["timeline"] and all(
        e["source"].startswith("t1.") or e["detail"].get("telescope") == "t1"
        for e in ctx["timeline"]
    )
    assert ctx["status"]["telescopes"]["t1"]["state"] == "escalated"
    # One summary for a person, in the snapshot and so on the console.
    [snap] = snapshot(rig.sequencer)["telescopes"]["t1"]["incidents"]
    assert snap["id"] == "t1-camera-1" and snap["outcome"] == "recovered"


@pytest.mark.parametrize(
    ("body", "fixed", "why"),
    [
        (
            "print('Diagnosis: the camera has no power. Did nothing.')\nsys.exit(3)",
            False,
            "exited 3",
        ),
        # Exits 0 without fixing it: robofast checks for itself.
        ("print('I think it is fine now.')\n", False, "robofast's own check"),
    ],
    ids=["agent-fails", "agent-is-wrong"],
)
async def test_an_agent_that_does_not_fix_it_drops_the_telescope(
    make_rig, tmp_path, body, fixed, why
):
    cmd = agent(tmp_path, body)
    rig, cam, unit = rig_with_escalation(
        make_rig, tmp_path, {"agent": {"command": cmd, "timeout": 30}}
    )
    await break_it(rig, cam)
    await wait_until(lambda: unit.gave_up, timeout=30)
    [inc] = incident_events(rig)
    assert inc.detail["outcome"] == "dropped" and inc.detail["level"] == "critical"
    s = inc.detail["summary"]
    assert why in s and "DROPPED" in s and "the other telescopes observe on" in s
    assert ("no power" in s) or ("fine now" in s)  # the agent's own summary, from stdout
    [give] = rig.timeline.events("giving_up")
    assert "escalation" in give.detail["reason"]
    t2 = rig.cams[1].exposures_started
    await wait_until(lambda: rig.cams[1].exposures_started > t2 + 1, timeout=20)


async def test_an_agent_that_hangs_is_killed_at_its_timeout(make_rig, tmp_path):
    cmd = agent(
        tmp_path,
        """
        (work / "agent.pid").write_text(str(os.getpid()))
        time.sleep(600)
        """,
    )
    rig, cam, unit = rig_with_escalation(
        make_rig, tmp_path, {"agent": {"command": cmd, "timeout": 2}}
    )
    await break_it(rig, cam)
    await wait_until(lambda: unit.gave_up, timeout=40)
    [inc] = incident_events(rig)
    assert "timed out after 2 s" in inc.detail["summary"]
    pid = int((tmp_path / "agent.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_no_agent_configured_the_person_is_told_at_once(make_rig, tmp_path):
    rig, cam, unit = rig_with_escalation(make_rig, tmp_path, None)
    await break_it(rig, cam)
    await wait_until(lambda: unit.gave_up, timeout=20)
    assert not rig.timeline.events("escalation_start")
    [inc] = incident_events(rig)
    assert "Agent: no agent configured" in inc.detail["summary"]
    assert inc.detail["outcome"] == "dropped"
    [snap] = snapshot(rig.sequencer)["telescopes"]["t1"]["incidents"]
    assert snap["level"] == "critical" and "dropped for the night" in snap["text"]


async def test_the_same_fault_again_is_not_escalated_again(make_rig, tmp_path):
    """The agent fixed it; it fails the same way again: no second agent
    run (and no second page for that run), the telescope is dropped."""
    cmd = agent(
        tmp_path,
        """
        with open(work / "runs", "a") as f:
            f.write("x")
        (work / "mended").touch()
        print("restarted the driver")
        """,
    )
    rig, cam, unit = rig_with_escalation(
        make_rig, tmp_path, {"agent": {"command": cmd, "timeout": 30}, "max_per_night": 5}
    )
    await break_it(rig, cam)
    await wait_until(lambda: incident_events(rig), timeout=30)
    n = cam.exposures_started
    await wait_until(lambda: cam.exposures_started > n, timeout=20)
    (tmp_path / "mended").unlink()  # the same crash again
    await wait_until(lambda: unit.gave_up, timeout=30)
    assert (tmp_path / "runs").read_text() == "x"
    first, second = incident_events(rig)
    assert first.detail["outcome"] == "recovered" and second.detail["outcome"] == "dropped"
    assert "the same fault was escalated" in second.detail["summary"]
    ids = [i["id"] for i in snapshot(rig.sequencer)["telescopes"]["t1"]["incidents"]]
    assert ids == ["t1-camera-1", "t1-camera-2"]


async def test_at_most_max_per_night_agent_runs_per_device(make_rig, tmp_path):
    cmd = agent(tmp_path, "time.sleep(600)\n")
    rig = make_rig(telescopes=1)
    seq = rig.sequencer
    unit = seq.units["t1"]
    unit.recovery = RecoveryLadder.from_config(
        "t1",
        {
            "camera": {"steps": ["reconnect"]},
            "escalate": {"agent": {"command": cmd, "timeout": 600}, "max_per_night": 1},
        },
    )
    tried = [("reconnect", "failed: no answer")]
    assert seq._escalate(unit, RecoveryResult("camera", tried, False, True), "fault A") is None
    assert unit.escalation is not None and len(seq._escalations) == 1
    why = seq._escalate(unit, RecoveryResult("camera", tried, False, True), "fault B 2")
    assert why is not None and "max_per_night 1" in why
    # The running agent is stopped (and killed) with the sequencer.
    await seq._stop_escalations()
    assert not seq._escalations
    first, second = unit.incidents
    assert first.outcome == "dropped" and "sequencer stopped" in first.agent
    assert second.outcome == "dropped"


def test_fault_signature_ignores_numbers():
    assert fault_signature("camera", "timeout after 3.5 s (0x1A)") == fault_signature(
        "camera", "timeout after 12 s (0x2B)"
    )
    assert fault_signature("camera", "a") != fault_signature("focuser", "a")


# -- the hook runner ---------------------------------------------------------------------------


async def test_run_agent_contract(tmp_path):
    cmd = agent(
        tmp_path,
        """
        given = pathlib.Path(os.environ['ROBOFAST_ESCALATION_CONTEXT'])
        assert ctx == json.loads(given.read_text())
        assert os.environ['ROBOFAST_ESCALATION_TELESCOPE'] == 't1'
        assert os.environ['EXTRA'] == 'yes'
        print("this is stdout, not the summary")
        summary.write_text("from the file")
        """,
    )
    pol = EscalationPolicy(agent={"command": cmd, "timeout": 30, "env": {"EXTRA": "yes"}})
    res = await run_agent(pol, {"telescope": "t1", "device": "camera"})
    assert res.exit == 0 and res.summary == "from the file" and not res.timed_out


async def test_run_agent_summary_from_stdout_and_truncated(tmp_path):
    cmd = agent(tmp_path, "print('x' * 10000)\nsys.exit(1)\n")
    res = await run_agent(EscalationPolicy(agent={"command": cmd}, summary_max=100), {})
    assert res.exit == 1 and len(res.summary) == 100


async def test_run_agent_kills_the_whole_process_group(tmp_path):
    cmd = agent(
        tmp_path,
        """
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        (work / "child.pid").write_text(str(child.pid))
        time.sleep(600)
        """,
    )
    res = await run_agent(EscalationPolicy(agent={"command": cmd, "timeout": 1.5}), {})
    assert res.timed_out and res.exit is None
    child = int((tmp_path / "child.pid").read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_run_agent_that_cannot_start(tmp_path):
    res = await run_agent(EscalationPolicy(agent={"command": [str(tmp_path / "nope")]}), {})
    assert res.error is not None and res.exit is None


# -- configuration ------------------------------------------------------------------------------


def test_escalate_per_telescope_merged_with_per_device():
    ladder = RecoveryLadder.from_config(
        "t1",
        {
            "escalate": {"agent": {"command": ["/usr/bin/true"]}, "max_per_night": 3},
            "camera": {"steps": ["reconnect"], "escalate": {"max_per_night": 1}},
            "telescope": {"steps": ["reconnect"], "escalate": {"agent": None}},
            "focuser": {"steps": ["reconnect"]},
        },
    )
    assert ladder.escalation("camera").max_per_night == 1
    assert ladder.escalation("camera").agent == {"command": ["/usr/bin/true"]}
    assert ladder.escalation("telescope").agent is None
    assert ladder.escalation("focuser").max_per_night == 3
    assert DevicePolicy().escalation.agent is None


@pytest.mark.parametrize(
    "bad",
    [
        {"agent": {"command": ["relative/agent"]}},
        {"agent": {"command": "/usr/bin/agent --shell"}},
        {"agent": {"command": ["/usr/bin/true"], "timeout": 0}},
        {"agent": {"command": ["/usr/bin/true"], "shell": True}},
        {"max_per_night": -1},
        {"pages": 3},
    ],
)
def test_escalate_validation(bad):
    with pytest.raises(ValueError):
        RecoveryLadder.from_config("t1", {"escalate": bad, "camera": {}})


# -- the summary, on the console and so to the notifier -------------------------------------------


def test_an_incident_is_one_console_alarm_with_its_summary():
    inc = Incident("t1", "camera", 1, "t1.camera.StartExposure: boom", [("reconnect", "failed")])
    inc.agent, inc.agent_summary = "exited 1 (could not fix it)", "Diagnosis: no power."
    inc.finish("dropped", "the agent could not fix it")
    alarms: list[tuple] = []
    seq = {"telescopes": {"t1": {"state": "gave_up", "incidents": [inc.as_dict()]}}}
    Aggregator._telescope_view("t1", seq, {}, None, None, lambda *a: alarms.append(a))
    texts = [a[1] for a in alarms]
    assert "t1 gave up for the night" in texts
    [mine] = [a for a in alarms if a[1] == inc.text]
    assert mine[2] == "critical" and "Diagnosis: no power." in mine[3]
    assert "Outcome: DROPPED" in mine[3]


async def test_after_an_agent_the_guider_is_checked_through_its_service(make_rig):
    class Guider:
        state = "failed"

        async def status(self):
            return {"state": self.state, "reason": "camera gone"}

        async def reconnect(self):
            pass

    rig = make_rig(telescopes=1)
    unit = rig.sequencer.units["t1"]
    unit.guider = Guider()
    unit.recovery = RecoveryLadder.from_config(
        "t1", {"guider": {"steps": ["reconnect"], "verify_timeout": 0.1}}, sleep=no_sleep
    )
    with pytest.raises(Exception, match="the guider is failed"):
        await rig.sequencer._verify_device(unit, "guider")
    unit.guider.state = "idle"
    await rig.sequencer._verify_device(unit, "guider")


@pytest.mark.parametrize("own", [{"agent": False}, False], ids=["agent-false", "escalate-false"])
async def test_the_agent_step_can_be_skipped_by_config(make_rig, tmp_path, own):
    """A telescope with an agent, its camera's skipped: straight from the
    exhausted ladder to the person, and the telescope is dropped."""
    cmd = agent(tmp_path, "(work / 'ran').touch()\n")
    rig = make_rig(exptime=0.05, count=200, telescopes=2, seq={"error_backoff": 0.1})
    cam = MendableCamera()
    unit = rig.sequencer.units["t1"]
    unit.camera = DeviceOwner("t1.camera", lambda: cam, rig.timeline, default_timeout=0.5)
    unit.recovery = RecoveryLadder.from_config(
        "t1",
        {
            "escalate": {"agent": {"command": cmd, "timeout": 30}},
            "camera": {"steps": ["reconnect"], "verify_timeout": 0.1, "escalate": own},
        },
        sleep=no_sleep,
    )
    assert unit.recovery.escalation("camera").skipped
    await break_it(rig, cam)
    await wait_until(lambda: unit.gave_up, timeout=20)
    assert not rig.timeline.events("escalation_start") and not (tmp_path / "ran").exists()
    [inc] = incident_events(rig)
    assert inc.detail["outcome"] == "dropped"
    assert "Agent: skipped by the config (escalate.agent: false)" in inc.detail["summary"]
    [give] = rig.timeline.events("giving_up")
    assert "skipped by the config" in give.detail["reason"]


def test_skipping_the_agent_in_config():
    hook = {"command": ["/usr/bin/true"]}
    ladder = RecoveryLadder.from_config(
        "t1",
        {
            "escalate": {"agent": hook},
            "camera": {"escalate": {"agent": False}},
            "focuser": {"escalate": False},
            "telescope": {},
        },
    )
    assert ladder.escalation("camera").skipped and ladder.escalation("camera").agent is None
    assert ladder.escalation("focuser").skipped
    assert ladder.escalation("telescope").agent == hook
    whole = RecoveryLadder.from_config("t1", {"escalate": False, "camera": {}})
    assert whole.escalation("camera").skipped
    assert not RecoveryLadder.from_config("t1", {"camera": {}}).escalation("camera").skipped
    with pytest.raises(ValueError):
        RecoveryLadder.from_config("t1", {"escalate": {"agent": True}, "camera": {}})
