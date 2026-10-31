"""The console's log (docs/console.md, "Log"): the supervisor's and the
sequencer's timelines merged with the changes the console sees, each once,
pushed on the event stream and pollable."""

from __future__ import annotations

import asyncio
import json

from robofast_core.console.logstream import LogStream
from robofast_core.timeline import Timeline

from . import test_console as _console
from .test_console import read_event, snapshot_doc

make_console = _console.make_console


HOLD = {"action": "pause", "operator": "al", "reason": "x"}


def weather(safe: bool, reason: str, ridge: str) -> dict:
    return {"safe": safe, "reason": reason, "bridge": {"sources": {"ridge": {"state": ridge}}}}


def doc(**enc) -> dict:
    base = {
        "timeline": [],
        "_sequencer_timeline": [],
        "alarms": [],
        "weather": {
            "safe": True,
            "reason": "ok",
            "bridge": {"sources": {"ridge": {"state": "safe"}}},
        },
        "enclosure": {"shutter": "OPEN", "holds": {}},
        "telescopes": {"t1": {"state": "observing", "guider": {"state": "guiding"}}},
        "spectrograph": {"job": {"id": 3, "state": "running", "phase": "exposing"}},
    }
    base.update(enc)
    return {"enclosures": {"a1": base}}


def test_timelines_once_in_order_and_changes_after_the_baseline():
    log = LogStream()
    tl = [
        {
            "time": "2026-11-10T03:00:02",
            "kind": "command",
            "source": "sup",
            "device": "dome",
            "what": "CloseShutter",
            "outcome": "ok",
            "detail": {},
        },
        {
            "time": "2026-11-10T03:00:01",
            "kind": "command",
            "source": "sup",
            "device": "dome",
            "what": "CloseShutter",
            "outcome": "sent",
            "detail": {},
        },
        {
            "time": "2026-11-10T03:00:00",
            "kind": "event",
            "source": "sup",
            "device": None,
            "what": "lease_revoked",
            "outcome": "",
            "detail": {"reason": "rain", "level": "warning"},
        },
    ]
    seq = [
        {
            "time": "2026-11-10T03:00:03",
            "kind": "command",
            "source": "seq",
            "device": "t1",
            "what": "Park",
            "outcome": "timeout",
            "detail": {},
        }
    ]
    log.ingest(doc(timeline=tl, _sequencer_timeline=seq))
    texts = [(e["source"], e["level"], e["text"]) for e in log.since(0)]
    assert texts == [
        ("supervisor", "warning", "lease_revoked reason=rain"),
        ("supervisor", "info", "sup → dome CloseShutter: ok"),
        ("sequencer", "warning", "seq → t1 Park: timeout"),
    ]
    # Again: nothing new. Then changes, against the first read.
    log.ingest(doc(timeline=tl, _sequencer_timeline=seq))
    assert len(log.since(0)) == 3
    alarm = {"id": "x1", "source": "t1", "level": "warning", "text": "t1 guider: star_lost"}
    log.ingest(
        doc(
            alarms=[alarm],
            weather={
                "safe": False,
                "reason": "humidity 95",
                "bridge": {"sources": {"ridge": {"state": "unsafe"}}},
            },
            enclosure={
                "shutter": "CLOSING",
                "holds": {"t1": {"action": "pause", "operator": "al", "reason": "x"}},
            },
            telescopes={"t1": {"state": "idle", "guider": {"state": "lost"}}},
            spectrograph={"job": {"id": 3, "state": "running", "phase": "reading out"}},
        )
    )
    new = [(e["source"], e["level"], e["text"]) for e in log.since(3)]
    assert ("t1", "warning", "ALARM: t1 guider: star_lost") in new
    assert ("weather", "warning", "weather UNSAFE: humidity 95") in new
    assert ("weather", "info", "ridge: safe → unsafe") in new
    assert ("enclosure", "info", "shutter OPEN → CLOSING") in new
    assert ("t1", "info", "t1: observing → idle") in new
    assert ("t1-guider", "warning", "t1 guider: guiding → lost") in new
    assert ("spectrograph", "info", "job #3 running: reading out") in new
    assert ("t1", "warning", "t1 held: pause by al (x)") in new
    log.ingest(doc())
    assert ("t1", "info", "cleared: t1 guider: star_lost") in [
        (e["source"], e["level"], e["text"]) for e in log.since(0)
    ]
    ids = [e["id"] for e in log.since(0)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_timeline_recent_has_wall_times():
    tl = Timeline()
    tl.command("seq", "t1", "Park", "sent")
    tl.command("seq", "t1", "Park", "ok")
    tl.event("seq", "observe_start", gen=1)
    rows = tl.recent(10, skip_sent=True)
    assert [r["what"] for r in rows] == ["Park", "observe_start"]
    assert rows[0]["time"].startswith("20") and rows[1]["detail"] == {"gen": 1}


async def test_the_console_streams_and_serves_the_log(make_console):
    rig = await make_console(refresh=60, cache=0)
    snap = snapshot_doc(
        timeline=[
            {
                "time": "2026-11-10T03:00:00+00:00",
                "kind": "event",
                "source": "aqawan1-sequencer",
                "what": "observe_start",
                "outcome": "",
                "detail": {"gen": 4},
            }
        ]
    )
    (rig.tmp / "seq-status.json").write_text(json.dumps(snap))
    await rig.login("alice")
    r = await rig.client.get("/console/api/log?after=0")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert any(e["source"] == "sequencer" and "observe_start gen=4" in e["text"] for e in entries)
    assert any(e["source"] == "supervisor" for e in entries)
    # The sequencer's timeline is for the log only: not in the status document.
    st = (await rig.client.get("/console/api/status")).json()["enclosures"]["aqawan1"]
    assert "_sequencer_timeline" not in st and "timeline" not in st["sequencer"]
    assert (await rig.client.get("/console/api/log?after=x")).status_code == 400
    async with rig.client.stream("GET", "/console/api/events") as s:
        lines = s.aiter_lines()
        first = await asyncio.wait_for(read_event(lines, "log"), 5)
        assert first and first[-1]["id"] >= entries[-1]["id"]
        await rig.act("pause", telescope="t1", reason="clouds coming")
        pushed = await asyncio.wait_for(read_event(lines, "log"), 5)
        assert any(
            e["source"] == "console" and "alice: pause t1: clouds coming" in e["text"]
            for e in pushed
        )


def test_the_page_has_the_log():
    from robofast_core.console.page import render_page

    page = render_page("n")
    assert 'id="log-list"' in page and 'addEventListener("log"' in page and "log-pause" in page
