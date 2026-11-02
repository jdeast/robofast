"""Lamp hours (robofast_core/devices/lamp_hours.py, docs/pulizzi.md): the
tracker on a fake clock (accumulation, flushes, restarts, crashes,
replacements, a corrupt file, the rated-life warnings), then in the lamp
server against the simulated unit (lamps switched outside robofast, the
Status and LampReplaced actions, a clean stop, failures that must not get
in the way of switching).

Fast tier: no serial port, no sleeps beyond a few poll intervals.
"""

from __future__ import annotations

import json
import logging
import shutil
import time

import pytest

from robofast_core.devices.lamp_hours import LampHours
from robofast_core.devices.pulizzi_server import PulizziConfig, PulizziServer, hours_path
from tests.core.test_pulizzi import LAMPS, Client, lamp_config, serve, wait_until_sync  # noqa: F401


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def tracker(path, clock, **kw) -> LampHours:
    rated = kw.pop("rated", {"ThAr1": None, "Incand": None})
    return LampHours(rated, path, clock=clock, wall=lambda: 1.8e9, **kw)


def saved(path) -> dict:
    return json.loads(path.read_text())["lamps"]


def burn(h: LampHours, clock: Clock, name: str, seconds: float, step: float = 2.0) -> None:
    """The lamp seen on every ``step`` s for ``seconds`` (as the monitor polls)."""
    end = clock.t + seconds
    while clock.t < end:
        clock.t = min(end, clock.t + step)
        h.observe(name, True)


# -- the tracker -----------------------------------------------------------------------


def test_hours_and_starts_accumulate_across_on_and_off(tmp_path):
    clock, path = Clock(), tmp_path / "hours.json"
    h = tracker(path, clock)
    assert not path.exists()  # nothing to write yet
    h.observe("ThAr1", False)
    h.observe("ThAr1", True)
    assert saved(path)["ThAr1"]["on"] is True  # a start is written at once
    burn(h, clock, "ThAr1", 20)
    h.observe("ThAr1", False)
    assert saved(path)["ThAr1"]["seconds"] == 20.0 and saved(path)["ThAr1"]["on"] is False
    clock.t += 100  # off: not counted
    h.observe("ThAr1", False)
    h.observe("ThAr1", True)
    burn(h, clock, "ThAr1", 5)
    clock.t += 1
    h.observe("ThAr1", False)  # counted to the first sight of it off
    r = h.report("ThAr1")
    assert r["hours"] == round(26 / 3600, 4) and r["cycles"] == 2
    assert r["since_replaced"] == {"hours": r["hours"], "cycles": 2, "at": None, "by": None}
    assert r["rated_hours"] is None and r["life_used"] is None and r["life_warning"] is None
    assert h.report("Incand")["cycles"] == 0 and h.error is None


def test_the_hours_are_written_every_flush_interval_while_a_lamp_burns(tmp_path):
    clock, path = Clock(), tmp_path / "hours.json"
    h = tracker(path, clock, flush_interval=60)
    h.observe("ThAr1", True)
    burn(h, clock, "ThAr1", 58)
    assert saved(path)["ThAr1"]["seconds"] == 0.0  # not yet
    burn(h, clock, "ThAr1", 4)
    assert saved(path)["ThAr1"]["seconds"] == 60.0
    burn(h, clock, "ThAr1", 60)
    assert saved(path)["ThAr1"]["seconds"] == 120.0


def test_the_hours_survive_a_restart(tmp_path):
    clock, path = Clock(), tmp_path / "sub" / "hours.json"
    h = tracker(path, clock)
    h.observe("Incand", True)
    burn(h, clock, "Incand", 30)
    h.observe("Incand", False)
    h.close()
    h2 = tracker(path, clock)
    assert h2.report("Incand")["cycles"] == 1 and h2.lamps["Incand"].seconds == 30.0
    h2.observe("Incand", True)
    burn(h2, clock, "Incand", 10)
    h2.observe("Incand", False)
    assert saved(path)["Incand"]["seconds"] == 40.0 and saved(path)["Incand"]["cycles"] == 2


def test_a_crash_loses_at_most_the_flush_interval_and_is_no_new_start(tmp_path):
    clock, path = Clock(), tmp_path / "hours.json"
    h = tracker(path, clock, flush_interval=60)
    h.observe("ThAr1", True)
    burn(h, clock, "ThAr1", 100)  # then the process dies: no close()
    on_disk = saved(path)["ThAr1"]
    assert 100 - 60 <= on_disk["seconds"] <= 100 and on_disk["on"] is True
    clock.t += 500  # down, the lamp still on: nobody saw it, not counted
    h2 = tracker(path, clock, flush_interval=60)
    h2.observe("ThAr1", True)
    burn(h2, clock, "ThAr1", 10)
    h2.observe("ThAr1", False)
    after = saved(path)["ThAr1"]
    assert after["cycles"] == 1  # the same burn, not a new start
    assert after["seconds"] == on_disk["seconds"] + 10


def test_a_replacement_restarts_the_lamps_own_count_and_keeps_the_totals(tmp_path, caplog):
    clock, path = Clock(), tmp_path / "hours.json"
    h = tracker(path, clock, rated={"ThAr1": 1.0, "Incand": None})
    h.observe("ThAr1", True)
    burn(h, clock, "ThAr1", 1800)
    h.observe("ThAr1", False)
    with caplog.at_level(logging.WARNING):
        old = h.replaced("ThAr1", "jde", "new Photron lamp", hours=0.25)
    assert old["hours"] == 0.5 and old["cycles"] == 1 and old["by"] == "jde"
    assert "replaced" in caplog.text and "jde" in caplog.text
    r = h.report("ThAr1")
    assert r["hours"] == 0.5 and r["cycles"] == 1  # lifetime: kept
    s = r["since_replaced"]
    assert s["hours"] == 0.25 and s["cycles"] == 0 and s["by"] == "jde" and s["at"]
    assert r["life_used"] == 0.25
    h2 = tracker(path, clock, rated={"ThAr1": 1.0, "Incand": None})
    assert h2.report("ThAr1") == r
    assert saved(path)["ThAr1"]["replacements"] == [old]


def test_the_rated_life_warns_at_90_and_100_percent_and_never_refuses(tmp_path, caplog):
    clock = Clock()
    h = tracker(tmp_path / "hours.json", clock, rated={"ThAr1": 0.01, "Incand": None})  # 36 s
    caplog.set_level(logging.WARNING)
    caplog.clear()  # the missing file
    h.observe("ThAr1", True)
    burn(h, clock, "ThAr1", 30, step=1)
    assert h.report("ThAr1")["life_warning"] is None and not caplog.records
    burn(h, clock, "ThAr1", 3, step=1)
    assert h.report("ThAr1")["life_warning"] == "near"
    (near,) = caplog.records
    assert near.levelno == logging.WARNING and "92 %" in near.getMessage()
    burn(h, clock, "ThAr1", 4, step=1)
    r = h.report("ThAr1")
    assert r["life_warning"] == "past" and r["life_used"] > 1
    assert [x.levelno for x in caplog.records] == [logging.WARNING, logging.ERROR]
    burn(h, clock, "ThAr1", 10, step=1)
    assert len(caplog.records) == 2  # once each
    # A restart past the rating says so again, once.
    h.close()
    caplog.clear()
    h2 = tracker(tmp_path / "hours.json", clock, rated={"ThAr1": 0.01, "Incand": None})
    assert [x.levelno for x in caplog.records] == [logging.ERROR]
    assert h2.report("ThAr1")["life_warning"] == "past"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "lamps": {}}',
        '{"version": 1, "lamps": {"ThAr1": {"seconds": -5, "cycles": 1, "since_replaced": {}}}}',
    ],
)
def test_a_corrupt_file_is_kept_aside_and_the_hours_start_afresh(tmp_path, caplog, content):
    clock, path = Clock(), tmp_path / "hours.json"
    path.write_text(content)
    with caplog.at_level(logging.ERROR):
        h = tracker(path, clock)
    assert "LAMP HOURS" in caplog.text and "corrupt" in h.error
    (aside,) = tmp_path.glob("hours.json.corrupt-*")
    assert aside.read_text() == content and not path.exists()
    h.observe("ThAr1", True)
    assert saved(path)["ThAr1"]["cycles"] == 1  # a fresh file
    assert h.error is not None  # still reported: hours were lost


def test_a_missing_file_starts_at_zero_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        h = tracker(tmp_path / "none.json", Clock())
    assert "not found" in caplog.text and h.error is None
    assert h.report("ThAr1")["hours"] == 0


def test_lamps_in_the_file_but_not_configured_are_kept(tmp_path):
    clock, path = Clock(), tmp_path / "hours.json"
    h = tracker(path, clock, rated={"ThAr1": None, "Old": None})
    h.observe("Old", True)
    h.observe("Old", False)
    h2 = tracker(path, clock, rated={"ThAr1": None})
    h2.observe("ThAr1", True)
    assert saved(path)["Old"]["cycles"] == 1


def test_a_failed_write_is_reported_and_retried(tmp_path, caplog):
    clock = Clock()
    blocker = tmp_path / "file"
    blocker.write_text("")
    h = tracker(blocker / "hours.json", clock)  # its directory is a file
    with caplog.at_level(logging.ERROR):
        h.observe("ThAr1", True)
    assert "could not write" in h.error and "could not write" in caplog.text
    burn(h, clock, "ThAr1", 10)
    assert h.lamps["ThAr1"].seconds == 10.0  # counted in memory
    blocker.unlink()
    h.observe("ThAr1", False)
    assert h.error is None and saved(blocker / "hours.json")["ThAr1"]["seconds"] == 10.0


# -- in the lamp server ----------------------------------------------------------------


def test_the_hours_file_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOFAST_DATA_DIR", str(tmp_path))
    cfg = lamp_config()
    assert hours_path(cfg) is None  # the simulator: in memory
    cfg.port.type = "serial"
    assert hours_path(cfg) == tmp_path / "lamp-hours" / "lamps.json"
    cfg.hours_file = "x/h.json"
    assert hours_path(cfg) == tmp_path / "x" / "h.json"
    cfg.hours_file = str(tmp_path / "abs.json")
    assert hours_path(cfg) == tmp_path / "abs.json"


def test_status_counts_a_lamp_switched_on_outside_robofast(serve, tmp_path):  # noqa: F811
    path = tmp_path / "hours.json"
    s = serve(hours_file=str(path), outlets=[{**LAMPS[0], "rated_hours": 500}, *LAMPS[1:]])
    c = Client(s).connect()
    s.sim.panel(4, True)  # by hand (or the legacy software)
    wait_until_sync(lambda: c.status()["outlets"][1]["cycles"] == 1, 3.0)
    time.sleep(0.5)
    s.sim.panel(4, False)
    wait_until_sync(lambda: saved(path)["Incand"]["on"] is False, 3.0)
    assert 0.5 <= saved(path)["Incand"]["seconds"] < 2.0
    st = c.status()
    lamp, incand, other = st["outlets"]
    assert incand["cycles"] == 1 and incand["hours"] > 0 and incand["rated_hours"] is None
    assert lamp["rated_hours"] == 500 and lamp["life_used"] == 0 and lamp["cycles"] == 0
    assert "hours" not in other  # not a lamp
    assert st["hours_file"] == str(path) and st["hours_error"] is None


def test_a_clean_stop_writes_the_hours(serve, tmp_path):  # noqa: F811
    path = tmp_path / "hours.json"
    s = serve(hours_file=str(path), hours_flush=600)
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    time.sleep(0.3)
    s.stop()  # switches it off: an off transition, written
    d = saved(path)["ThAr1"]
    assert d["cycles"] == 1 and d["on"] is False and 0.3 <= d["seconds"] < 2.0


def test_a_crashed_server_loses_at_most_its_flush_interval(serve, tmp_path):  # noqa: F811
    path, copy = tmp_path / "hours.json", tmp_path / "after-crash.json"
    s = serve(hours_file=str(path), hours_flush=0.2)
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    t0 = time.monotonic()
    time.sleep(0.8)
    shutil.copy(path, copy)  # the file as a crash right now would leave it
    burned = time.monotonic() - t0
    d = json.loads(copy.read_text())["lamps"]["ThAr1"]
    # At most a flush interval, plus slack for a loaded test machine (the
    # exact bound is checked on a fake clock above).
    assert d["on"] is True and burned - 0.2 - 0.5 <= d["seconds"] <= burned
    # A new server on that file, the lamp still on: the same burn goes on.
    s2 = serve(hours_file=str(copy), sim={"on": [2]})
    c2 = Client(s2).connect()
    time.sleep(0.3)
    assert c2.status()["outlets"][0]["cycles"] == 1
    s2.stop()
    after = json.loads(copy.read_text())["lamps"]["ThAr1"]
    assert after["cycles"] == 1 and after["seconds"] >= d["seconds"] + 0.2


def test_the_lamp_replaced_action(serve, tmp_path):  # noqa: F811
    path = tmp_path / "hours.json"
    s = serve(hours_file=str(path))
    c = Client(s).connect()
    assert "LampReplaced" in c.get("supportedactions")
    c.put("setswitch", Id=0, State=True)
    time.sleep(0.4)
    c.put("setswitch", Id=0, State=False)

    def act(params) -> object:
        p = params if isinstance(params, str) else json.dumps(params)
        r = c.put("action", Action="LampReplaced", Parameters=p)
        return r if isinstance(r, tuple) else json.loads(r)

    for bad in (
        "",
        "ThAr1",
        {"lamp": "ThAr1"},
        {"lamp": "other", "by": "x"},
        {"lamp": "ThAr1", "by": "x", "hours": -1},
        {"lamp": "ThAr1", "by": " "},
    ):
        err = act(bad)
        assert err[0] == "error" and err[1] == 0x401, bad
    out = act({"lamp": "ThAr1", "by": "jde", "note": "new lamp"})
    assert out["lamp"] == "ThAr1" and out["old"]["cycles"] == 1 and out["old"]["hours"] > 0
    assert out["now"]["since_replaced"]["cycles"] == 0
    assert out["now"]["since_replaced"]["by"] == "jde (client 7)"
    assert out["now"]["cycles"] == 1  # lifetime
    assert saved(path)["ThAr1"]["replacements"][0]["note"] == "new lamp"
    assert s.sim.lit() == [3, 5]  # switched nothing


def test_tracker_failures_never_stop_a_lamp_switching(serve, tmp_path, monkeypatch):  # noqa: F811
    blocker = tmp_path / "file"
    blocker.write_text("")
    s = serve(hours_file=str(blocker / "hours.json"))  # unwritable
    c = Client(s).connect()
    assert c.put("setswitch", Id=0, State=True) is None and 2 in s.sim.lit()
    assert "could not write" in c.status()["hours_error"]

    def boom(*a, **kw):
        raise RuntimeError("tracker bug")

    monkeypatch.setattr(s.lamps.hours, "observe", boom)
    assert c.put("setswitch", Id=0, State=False) is None and s.sim.lit() == [3, 5]
    c.put("setswitch", Id=1, State=True)
    off = json.loads(c.put("action", Action="LampsOff", Parameters=""))["off"]
    assert off == ["Incand"] and s.sim.lit() == [3, 5]


def test_a_corrupt_file_does_not_stop_the_server(serve, tmp_path):  # noqa: F811
    path = tmp_path / "hours.json"
    path.write_text("garbage")
    s = serve(hours_file=str(path))
    c = Client(s).connect()
    assert c.put("setswitch", Id=0, State=True) is None
    st = c.status()
    assert "unreadable" in st["hours_error"] and st["outlets"][0]["cycles"] == 1
    assert list(tmp_path.glob("hours.json.corrupt-*"))


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"outlets": [{"outlet": 3, "name": "a", "rated_hours": 10}]}, "for lamps"),
        ({"outlets": [{"outlet": 2, "name": "a", "lamp": True, "rated_hours": 0}]}, "rated_hours"),
        ({"outlets": [{"outlet": 2, "name": "a", "lamp": True, "rated_hours": "1"}]}, "rated"),
        ({"hours_flush": 0}, "hours_flush"),
        ({"hours_file": ""}, "hours_file"),
    ],
)
def test_hours_config_validation(change, error):
    with pytest.raises(ValueError, match=error):
        PulizziConfig.from_dict({"outlets": LAMPS, "port": {"type": "sim"}, **change})


def test_the_simulator_keeps_no_file_by_default(serve, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ROBOFAST_DATA_DIR", str(tmp_path))
    s: PulizziServer = serve()
    c = Client(s).connect()
    c.put("setswitch", Id=0, State=True)
    assert c.status()["hours_file"] is None
    s.stop()
    assert not any(tmp_path.rglob("*.json"))
