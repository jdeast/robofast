"""The disk watch (robofast/diskwatch.py, docs/diskwatch.md).

Nothing here touches a real disk but the test's own tmp directory, nor the
network: ``os.statvfs`` is faked, and "ssh" is a small Python script.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from robofast import diskwatch as dw
from robofast.api_auth import TokenStore
from robofast.console.aggregate import Aggregator
from robofast.console.page import render_page
from robofast.console.server import Console, ConsoleConfig
from robofast.notifier.service import build
from robofast.notifier.sources import AggregatorSource
from tests.core.test_notifier import write_config

GB = 10**9

DF_SAMPLE = """Filesystem     1024-blocks      Used Available Capacity Mounted on
udev               8000000         0   8000000       0% /dev
tmpfs              1600000      2000   1598000       1% /run
/dev/sda2        102687672  97000000    429000     100% /
/dev/loop3           56832     56832         0     100% /snap/core20/2318
/dev/sdb1       1921803544 400000000 1424113064      22% /data with space
/dev/sda2        102687672  97000000    429000     100% /var/lib/docker/bind
//nas/share     1000000000 900000000 100000000      90% /mnt/nas
"""

#: wmic's CSV as it comes over ssh from a Windows 7 PC: CR CR LF line ends,
#: a blank line first, and a drive with no media (no FreeSpace, no Size).
WMIC_SAMPLE = (
    "\r\r\n"
    "Node,Caption,FreeSpace,Size\r\r\n"
    "KIWISPEC-PC,C:,3600000000,250056704000\r\r\n"
    "KIWISPEC-PC,D:,0,1000202039296\r\r\n"
    "KIWISPEC-PC,E:,,\r\r\n"
    "\r\r\n"
)


def fake_statvfs(sizes: dict[str, tuple[int, int]]) -> Any:
    """``os.statvfs`` for paths -> (total, free) bytes (4 kB blocks)."""

    def statvfs(path: str) -> SimpleNamespace:
        total, free = sizes[str(path)]
        b = 4096
        return SimpleNamespace(
            f_frsize=b, f_blocks=total // b, f_bfree=free // b, f_bavail=free // b
        )

    return statvfs


def down() -> httpx.AsyncClient:
    """An HTTP client to which every service is down (no network)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))


def config(targets: list[dict[str, Any]], tmp_path: Path, **top: Any) -> dw.Config:
    p = tmp_path / "diskwatch.json"
    p.write_text(json.dumps({"status": "status.json", "targets": targets, **top}))
    return dw.load(p)


# -- grading ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("free_gb", "total_gb", "level"),
    [
        (500, 1000, "ok"),
        (19, 150, "warning"),  # under 20 GB (87 % used)
        (50, 400, "ok"),
        (30, 250, "ok"),  # 88 % used
        (20, 250, "warning"),  # 92 % used
        (4.9, 1000, "alarm"),  # under 5 GB
        (6, 250, "alarm"),  # 97.6 % used
        (1.0, 1000, "critical"),
        (0, 1000, "critical"),
    ],
)
def test_threshold_levels(free_gb: float, total_gb: float, level: str) -> None:
    free, total = int(free_gb * GB), int(total_gb * GB)
    r = dw.Reading("/x", total, free, 100.0 * (total - free) / total)
    got, reason = dw.grade(r, dw.Thresholds(), None)
    assert got == level, reason
    assert f"{free / GB:.1f} GB free" in reason


def test_thresholds_can_be_turned_off_and_set_per_target(tmp_path: Path) -> None:
    c = config(
        [
            {"name": "a", "host": "h", "path": "/a", "warn_pct": None, "warn_free_gb": 2},
            {"name": "b", "host": "h", "path": "/b"},
        ],
        tmp_path,
        alarm_free_gb=1.5,
    )
    a, b = (t.thresholds for t in c.targets)
    assert a.warn_pct is None and a.warn_free_gb == 2 and a.alarm_free_gb == 1.5
    assert b.warn_pct == 90 and b.alarm_free_gb == 1.5
    r = dw.Reading("/a", 1000 * GB, int(1.8 * GB), 99.8)
    assert dw.grade(r, a, None)[0] == "alarm"  # 99.8 % used still alarms (alarm_pct)
    with pytest.raises(ValueError, match="unknown keys"):
        config([{"name": "a", "path": "/a", "bogus": 1}], tmp_path)


def test_fill_rate_forecast() -> None:
    day = 86400.0
    # 50 GB free, losing 10 GB a day, a sample every 5 minutes for a day.
    hist: list[list[float]] = []
    for i in range(289):
        t = i * 300.0
        dw.add_sample(hist, t, int(50 * GB - 10 * GB * t / day), 1000 * GB, day)
    days = dw.days_to_full(hist, day)
    assert days == pytest.approx(4.0, rel=0.01)  # 40 GB left at 10 GB/day
    r = dw.Reading("/x", 1000 * GB, int(hist[-1][1]), 96.0)
    th = dw.Thresholds(warn_pct=None, alarm_pct=None)
    level, reason = dw.grade(r, th, days)
    assert level == "warning" and "full in 4.0 days" in reason
    assert dw.grade(r, th, 8.0)[0] == "ok"
    # Freeing space restarts the trend: no forecast until it has 3 samples over an hour.
    dw.add_sample(hist, day + 300, 200 * GB, 1000 * GB, day)
    assert len(hist) == 1 and dw.days_to_full(hist, day) is None
    # Flat or rising: no forecast.
    flat = [[i * 600.0, 100.0 * GB] for i in range(20)]
    assert dw.days_to_full(flat, day) is None
    # Noisy but falling: the least-squares slope.
    noisy = [[i * 600.0, 100 * GB - i * 1e8 + (3e8 if i % 2 else -3e8)] for i in range(100)]
    assert dw.days_to_full(noisy, day) == pytest.approx(noisy[-1][1] / (1e8 / 600) / day, rel=0.05)


# -- probes ----------------------------------------------------------------------------


def test_a_hung_mount_is_unknown_without_hanging_or_piling_up_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    calls = []

    def hung(path: str) -> Any:
        calls.append(path)
        release.wait(30)
        raise OSError("gone")

    monkeypatch.setattr(os, "statvfs", hung)
    c = config(
        [{"name": "PC data", "host": "192.0.2.22", "path": "/mnt/pc"}], tmp_path, timeout=0.3
    )
    watch = dw.DiskWatch(c)
    try:
        t0 = time.monotonic()
        doc = watch.run_pass(unknown_after=0)
        assert time.monotonic() - t0 < 2.0
        (row,) = doc["rows"]
        assert row["level"] == "unknown" and "timed out after 0.3 s" in row["error"]
        (a,) = doc["alarms"]
        assert a["text"] == "PC data (192.0.2.22): free space unknown (cannot read the disk)"
        # The next pass does not start a second read while the first is stuck.
        t0 = time.monotonic()
        doc = watch.run_pass(unknown_after=0)
        assert time.monotonic() - t0 < 1.0
        assert "still hung" in doc["rows"][0]["error"] and len(calls) == 1
        assert sum(t.name == "statvfs /mnt/pc" for t in threading.enumerate()) == 1
        # Unknown for less than unknown_after: shown, not alarmed.
        assert dw.DiskWatch(c).run_pass()["alarms"] == []
    finally:
        release.set()
    # Once it comes back, a new read is made.
    deadline = time.monotonic() + 5
    while any(t.name == "statvfs /mnt/pc" for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    monkeypatch.setattr(os, "statvfs", fake_statvfs({"/mnt/pc": (100 * GB, 50 * GB)}))
    doc = watch.run_pass(unknown_after=0)
    assert doc["rows"][0]["level"] == "ok" and doc["alarms"] == []


def test_parse_df() -> None:
    rows = dw.parse_df(DF_SAMPLE)
    assert [r.where for r in rows] == ["/", "/data with space"]  # no tmpfs, loop, bind, network
    root = rows[0]
    assert root.total == 102687672 * 1024 and root.free == 429000 * 1024
    assert root.used_pct == pytest.approx(100 * 97000000 / (97000000 + 429000))
    assert [r.where for r in dw.parse_df(DF_SAMPLE, mounts=["/mnt/nas"])] == ["/mnt/nas"]


def test_parse_wmic_windows_output() -> None:
    rows = dw.parse_wmic(WMIC_SAMPLE)
    assert [(r.where, r.free, r.total) for r in rows] == [
        ("C:", 3600000000, 250056704000),
        ("D:", 0, 1000202039296),
    ]  # E: (no media) skipped
    assert rows[1].used_pct == 100.0
    assert [r.where for r in dw.parse_wmic(WMIC_SAMPLE, drives=["D:"])] == ["D:"]
    # wmic redirected writes UTF-16 with a BOM.
    assert dw.parse_wmic(dw.decode(WMIC_SAMPLE.encode("utf-16"))) == rows
    with pytest.raises(dw.ProbeError):
        dw.parse_wmic("'wmic' is not recognized as an internal or external command")


def fake_ssh(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_ssh.py"
    script.write_text("import sys, os, time\n" + body)
    return [sys.executable, str(script)]


def test_ssh_probe_runs_batch_mode_and_reads_every_drive(tmp_path: Path) -> None:
    argv = tmp_path / "argv.json"
    ssh = fake_ssh(
        tmp_path,
        f"open({str(argv)!r}, 'w').write(__import__('json').dumps(sys.argv[1:]))\n"
        f"sys.stdout.buffer.write({WMIC_SAMPLE.encode()!r})\n",
    )
    c = config(
        [{"name": "KIWISPEC-PC", "host": "192.0.2.22", "probe": "ssh", "ssh": "kiwispec",
          "os": "windows"}],
        tmp_path,
        ssh_command=ssh,
    )  # fmt: skip
    doc = dw.DiskWatch(c).run_pass()
    args = json.loads(argv.read_text())
    assert args[:4] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert args[4] == "kiwispec" and args[5].startswith("wmic logicaldisk where DriveType=3")
    levels = {r["name"]: r["level"] for r in doc["rows"]}
    assert levels == {"KIWISPEC-PC C:": "alarm", "KIWISPEC-PC D:": "critical"}


def test_ssh_timeout_kills_it(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    ssh = fake_ssh(
        tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    c = config(
        [{"name": "pc", "host": "192.0.2.5", "probe": "ssh", "ssh": "pc"}],
        tmp_path,
        ssh_command=ssh,
        ssh_timeout=1.0,
    )
    t0 = time.monotonic()
    doc = dw.DiskWatch(c).run_pass(unknown_after=0)
    assert time.monotonic() - t0 < 5
    assert "ssh timed out after 1 s (killed)" in doc["rows"][0]["error"]
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # A failing ssh (no key, host down) is an error, not a crash.
    c.ssh_command = fake_ssh(
        tmp_path, "sys.stderr.write('Permission denied (publickey).\\n')\nsys.exit(255)\n"
    )
    row = dw.DiskWatch(c).run_pass()["rows"][0]
    assert row["error"] == "ssh exit 255: Permission denied (publickey)."


# -- dedupe, the status file ---------------------------------------------------------------


def test_one_volume_seen_twice_alarms_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        os,
        "statvfs",
        fake_statvfs({"/mnt/pc/data": (500 * GB, 2 * GB), "/mnt/pc/log": (500 * GB, 2 * GB),
                      "/mnt/pc/d": (900 * GB, 3 * GB)}),
    )  # fmt: skip
    ssh = fake_ssh(tmp_path, f"sys.stdout.write({WMIC_SAMPLE!r})\n")
    c = config(
        [
            {"name": "PC data", "host": "pc", "path": "/mnt/pc/data"},
            {"name": "PC log", "host": "pc", "path": "/mnt/pc/log"},  # same disk: auto
            {"name": "PC win", "host": "pc", "probe": "ssh", "ssh": "pc", "os": "windows"},
            {"name": "PC D share", "host": "pc", "path": "/mnt/pc/d", "volume": "pc D:"},
        ],
        tmp_path,
        ssh_command=ssh,
    )
    doc = dw.DiskWatch(c).run_pass()
    same = {r["name"]: r.get("same_as") for r in doc["rows"]}
    assert same == {
        "PC data": None,
        "PC log": "PC data",
        "PC win C:": None,
        "PC win D:": None,
        "PC D share": "PC win D:",  # by its volume label
    }
    texts = [a["text"] for a in doc["alarms"]]
    assert texts == [
        "PC data (pc): disk nearly full",
        "PC win C: (pc): disk nearly full",
        "PC win D: (pc): disk full",
    ]


def test_status_file_is_written_atomically(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "s" / "status.json"
    dw.write_status(path, {"rows": [1]})
    assert json.loads(path.read_text()) == {"rows": [1]}

    def broken(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(dw.json, "dump", broken)
    with pytest.raises(OSError):
        dw.write_status(path, {"rows": [2]})
    assert json.loads(path.read_text()) == {"rows": [1]}  # the old one stays whole
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]  # no temp left


def test_history_survives_a_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [1_000_000.0]
    free = [100 * GB]
    monkeypatch.setattr(os, "statvfs", lambda p: fake_statvfs({p: (1000 * GB, free[0])})(p))
    c = config([{"name": "disk", "host": "h", "path": "/d", "warn_pct": None}], tmp_path)
    watch = dw.DiskWatch(c, clock=lambda: now[0])
    for _ in range(12):
        doc = watch.run_pass()
        now[0] += 600
        free[0] -= 2 * GB
    dw.write_status(c.status, doc)
    again = dw.DiskWatch(c, clock=lambda: now[0])
    dw._restore(again, c.status)
    row = again.run_pass()["rows"][0]
    assert row["days_to_full"] == pytest.approx(76 / (2 / 600 * 86400), rel=0.02)
    assert row["level"] == "warning" and "full in" in row["reason"]


# -- the console and the notifier ------------------------------------------------------------


async def site_alarms(status: Path) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as http:
        doc = await Aggregator({}, http, diskwatch=status).collect()
    return doc["site"]["alarms"]


async def test_console_alarm_ids_are_stable_while_the_disk_fills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    free = [3 * GB]
    monkeypatch.setattr(os, "statvfs", lambda p: fake_statvfs({p: (1000 * GB, free[0])})(p))
    c = config([{"name": "T2 camera PC", "host": "192.0.2.12", "path": "/mnt/t2"}], tmp_path)
    watch = dw.DiskWatch(c)
    dw.write_status(c.status, watch.run_pass())
    first = await site_alarms(c.status)
    free[0] -= 123_456_789
    dw.write_status(c.status, watch.run_pass())
    second = await site_alarms(c.status)
    assert [a["id"] for a in first] == [a["id"] for a in second] and len(first) == 1
    assert first[0]["text"] == "T2 camera PC (192.0.2.12): disk nearly full"
    assert first[0]["enclosure"] == "site" and first[0]["source"] == "diskwatch"
    assert first[0]["detail"] != second[0]["detail"]  # the numbers are in the detail
    # A different level is a new alarm (and so a new notification).
    free[0] = GB // 2
    dw.write_status(c.status, watch.run_pass())
    (third,) = await site_alarms(c.status)
    assert third["level"] == "critical" and third["id"] != first[0]["id"]


async def test_a_stale_or_missing_status_file_is_an_alarm(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    (a,) = await site_alarms(status)
    assert a["text"] == "disk watch not running (no status file)" and a["level"] == "alarm"
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    full = {"text": "x (h): disk full", "level": "critical", "detail": "0.0 GB free"}
    status.write_text(json.dumps({"time": old, "max_age": 960, "rows": [], "alarms": [full]}))
    (a,) = await site_alarms(status)  # stale rows raise nothing
    assert a["text"] == "disk watch not running (status file stale)" and a["level"] == "alarm"
    status.write_text("{half a fi")
    (a,) = await site_alarms(status)
    assert a["text"] == "disk watch status unreadable"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as http:
        assert (await Aggregator({}, http).collect())["site"] == {"diskwatch": None, "alarms": []}


async def test_regression_20261124_a_full_disk_reaches_the_notifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-11-24: KiwiSpec PC disk full for two weeks, unnoticed; that
    night's spectroscopy failed. A disk at 0 bytes free is a critical alarm
    in the notifier's alarm list, through the console's Aggregator."""
    monkeypatch.setattr(os, "statvfs", fake_statvfs({"/mnt/kiwispec": (1000 * GB, 0)}))
    c = config(
        [{"name": "KIWISPEC-PC data", "host": "192.0.2.22", "path": "/mnt/kiwispec"}], tmp_path
    )
    dw.write_status(c.status, dw.DiskWatch(c).run_pass())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as http:
        got = await AggregatorSource(Aggregator({}, http, diskwatch=c.status)).alarms()
        # And as the notifier service builds it from its config.
        n = build(write_config(tmp_path, diskwatch=str(c.status)), http=http)
        via_service = await n.sources[0].alarms()
    for alarms in (got, via_service):
        disk = [a for a in alarms if a["source"] == "diskwatch"]
        assert [(a["level"], a["text"]) for a in disk] == [
            ("critical", "KIWISPEC-PC data (192.0.2.22): disk full")
        ]
        assert disk[0]["detail"].startswith("0.0 GB free of 1000.0 GB")


def test_once_prints_a_table_and_exits_by_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sizes = {"/a": (1000 * GB, 500 * GB)}
    monkeypatch.setattr(os, "statvfs", fake_statvfs(sizes))
    cfg = tmp_path / "dw.json"
    cfg.write_text(json.dumps({"targets": [{"name": "gateway /", "host": "gw", "path": "/a"}]}))
    assert dw.main(["--config", str(cfg), "--once"]) == 0
    assert "gateway /" in capsys.readouterr().out
    sizes["/a"] = (1000 * GB, GB // 2)
    assert dw.main(["--config", str(cfg), "--once"]) == 1
    assert "CRITICAL: gateway / (gw): disk full" in capsys.readouterr().out
    assert not (tmp_path / "status.json").exists()  # --once writes nothing


async def test_the_console_shows_the_disks_and_acknowledges_their_alarms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "statvfs", fake_statvfs({"/mnt/t2": (1000 * GB, 3 * GB)}))
    c = config([{"name": "T2 camera PC", "host": "192.0.2.12", "path": "/mnt/t2"}], tmp_path)
    dw.write_status(c.status, dw.DiskWatch(c).run_pass())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as http:
        console = Console(
            {},
            TokenStore(tmp_path / "tokens.json"),
            config=ConsoleConfig(tokens="tokens.json", diskwatch=str(c.status)),
            http=http,
        )
        doc = await console.status(fresh=True)
        (a,) = doc["site"]["alarms"]
        assert (
            a["ack"] is None
            and doc["site"]["diskwatch"]["rows"][0]["free"] == 3 * GB // 4096 * 4096
        )
        await console.act("ack", "jde", {"id": a["id"], "reason": "cleaning it"})
        doc = await console.status(fresh=True)
        assert doc["site"]["alarms"][0]["ack"]["by"] == "jde"
    assert 'id="disks"' in render_page("n")


def test_the_example_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/robofast-diskwatch.service")
    c = dw.load(Path(__file__).parents[2] / "deploy" / "diskwatch.example.json")
    assert c.ssh_command[2] == "/run/credentials/robofast-diskwatch.service/ssh-key"
    assert {t.probe for t in c.targets} == {"path", "ssh"}
    assert all(t.probe == "path" or t.ssh for t in c.targets)
