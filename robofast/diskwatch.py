"""Free disk space on every computer, as console alarms (docs/diskwatch.md).

Each target is one disk, or one computer's disks, read one of two ways:

* ``"probe": "path"`` -- ``os.statvfs`` on a local path: the gateway's own
  disks, and the other computers' volumes through the gateway's CIFS or NFS
  mounts (``statvfs`` on a network mount reports the remote volume). A
  network mount can hang, so each read runs in its own daemon thread with a
  ``timeout``; a read that does not come back is reported as unknown, and
  while it is still stuck no second thread is started for that target.
* ``"probe": "ssh"`` -- ``df -Pk`` (Linux) or ``wmic logicaldisk`` (Windows)
  over ``ssh -o BatchMode=yes`` (keys only; a password prompt fails at
  once). Every fixed filesystem or drive is a row of its own, so a full
  ``D:`` that no share exposes is seen too. The whole ``ssh`` process group
  is killed at ``ssh_timeout``.

Each pass (every ``interval`` seconds) grades each row against its
thresholds (``ok`` < ``warning`` < ``alarm`` < ``critical``), forecasts when
it will be full from the last ``window_hours`` of samples, and writes the
**status file** atomically. The observer console reads it (``diskwatch`` in
its config) and shows the alarms, so the notifier sends them. Nothing here
deletes or changes anything.

``python -m robofast.diskwatch --config diskwatch.json`` runs the loop;
``--once`` makes one pass, prints a table and exits 1 if anything alarms.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robofast.config import load_config

log = logging.getLogger(__name__)

#: Gigabyte, as the thresholds count it (10**9 bytes).
GB = 1e9
LEVELS = ("ok", "warning", "alarm", "critical")
#: The alarm text per level. The text is what the console hashes into the
#: alarm's id, so it names the disk and the level only; the numbers that
#: change from pass to pass go in the alarm's ``detail``.
PHRASE = {"warning": "disk space low", "alarm": "disk nearly full", "critical": "disk full"}
UNKNOWN = "free space unknown (cannot read the disk)"
WMIC = "wmic logicaldisk where DriveType=3 get Caption,FreeSpace,Size /format:csv"
DF = "LC_ALL=C df -Pk"


class ProbeError(Exception):
    """A disk could not be read (timeout, ssh failure, unparseable output)."""


@dataclass
class Thresholds:
    """When a disk alarms; ``None`` turns a test off. Defaults in docs/diskwatch.md."""

    warn_free_gb: float | None = 20.0
    alarm_free_gb: float | None = 5.0
    critical_free_gb: float = 1.0
    warn_pct: float | None = 90.0
    alarm_pct: float | None = 97.0
    warn_days: float | None = 7.0
    window_hours: float = 24.0


THRESHOLD_KEYS = {f.name for f in fields(Thresholds)}
TARGET_KEYS = {"name", "host", "probe", "path", "ssh", "os", "drives", "mounts", "volume"}
TOP_KEYS = {"_note", "status", "interval", "timeout", "ssh_timeout", "connect_timeout"}
TOP_KEYS |= {"unknown_after", "ssh_command", "targets"}


@dataclass
class Target:
    name: str
    host: str
    probe: str
    path: str | None = None
    ssh: str | None = None
    os: str = "linux"
    drives: list[str] | None = None
    mounts: list[str] | None = None
    volume: str | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)


@dataclass
class Config:
    targets: list[Target]
    status: Path | None = None
    interval: float = 300.0
    #: One ``statvfs``, and ssh's connection timeout.
    timeout: float = 10.0
    connect_timeout: float = 10.0
    #: The whole ssh command, then it is killed.
    ssh_timeout: float = 30.0
    #: A disk that cannot be read alarms only after this long (a PC rebooting).
    unknown_after: float = 600.0
    ssh_command: list[str] = field(default_factory=lambda: ["ssh"])


def load(path: str | Path) -> Config:
    """The config file (module doc, docs/diskwatch.md)."""
    path = Path(path)
    raw = load_config(path)
    bad = set(raw) - TOP_KEYS - THRESHOLD_KEYS
    if bad:
        raise ValueError(f"{path}: unknown keys {sorted(bad)}")
    defaults = {k: raw[k] for k in THRESHOLD_KEYS if k in raw}
    targets = []
    for i, t in enumerate(raw.get("targets") or []):
        where = f"{path}: targets[{i}]"
        if not isinstance(t, dict) or not t.get("name"):
            raise ValueError(f"{where}: needs a name")
        bad = set(t) - TARGET_KEYS - THRESHOLD_KEYS
        if bad:
            raise ValueError(f"{where}: unknown keys {sorted(bad)}")
        probe = t.get("probe", "path")
        if probe == "path" and not t.get("path"):
            raise ValueError(f"{where}: a path probe needs path")
        if probe == "ssh" and not t.get("ssh"):
            raise ValueError(f"{where}: an ssh probe needs ssh (the host alias)")
        if probe not in ("path", "ssh"):
            raise ValueError(f"{where}: probe is path or ssh")
        if t.get("os", "linux") not in ("linux", "windows"):
            raise ValueError(f"{where}: os is linux or windows")
        th = Thresholds(**{**defaults, **{k: t[k] for k in THRESHOLD_KEYS if k in t}})
        targets.append(
            Target(
                name=str(t["name"]),
                host=str(t.get("host", t.get("ssh", "localhost"))),
                probe=probe,
                path=t.get("path"),
                ssh=t.get("ssh"),
                os=t.get("os", "linux"),
                drives=None if t.get("drives") is None else [str(d).upper() for d in t["drives"]],
                mounts=t.get("mounts"),
                volume=t.get("volume"),
                thresholds=th,
            )
        )
    if not targets:
        raise ValueError(f"{path}: no targets")
    names = [t.name for t in targets]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: target names must differ")
    status = raw.get("status")
    if status is not None:
        status = Path(status) if Path(status).is_absolute() else path.parent / status
    kw = {k: float(raw[k]) for k in ("interval", "timeout", "connect_timeout") if k in raw}
    kw |= {k: float(raw[k]) for k in ("ssh_timeout", "unknown_after") if k in raw}
    if "ssh_command" in raw:
        # $CREDENTIALS_DIRECTORY (systemd LoadCredential=: the ssh key) is expanded.
        kw["ssh_command"] = [os.path.expandvars(str(x)) for x in raw["ssh_command"]]
    return Config(targets=targets, status=status, **kw)  # type: ignore[arg-type]


# -- reading a disk ----------------------------------------------------------------------


@dataclass
class Reading:
    """One filesystem or drive: ``where`` is the path, mount or drive letter."""

    where: str
    total: int
    free: int
    used_pct: float


def _pct(used: int, avail: int) -> float:
    """Used percent as ``df`` counts it (blocks reserved for root left out)."""
    return 100.0 * used / (used + avail) if used + avail > 0 else 0.0


class PathProbe:
    """``os.statvfs`` with a timeout, in a daemon thread (a hung mount
    never blocks the loop nor stops the process from exiting). While a
    read is stuck, no new one is started for the same path."""

    def __init__(self, path: str, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._stuck: threading.Thread | None = None
        self._since = 0.0

    def read(self) -> Reading:
        if self._stuck is not None:
            if self._stuck.is_alive():
                raise ProbeError(
                    f"statvfs {self.path} still hung since "
                    f"{datetime.fromtimestamp(self._since, UTC).isoformat(timespec='seconds')}"
                )
            self._stuck = None
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["st"] = os.statvfs(self.path)
            except OSError as exc:
                box["err"] = exc
            finally:
                done.set()

        t = threading.Thread(target=work, name=f"statvfs {self.path}", daemon=True)
        t.start()
        if not done.wait(self.timeout):
            self._stuck, self._since = t, time.time()
            raise ProbeError(f"statvfs {self.path} timed out after {self.timeout:g} s")
        if "err" in box:
            raise ProbeError(f"statvfs {self.path}: {box['err']}")
        st = box["st"]
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return Reading(self.path, total, free, _pct(used, free))


def run_ssh(cmd: list[str], timeout: float) -> str:
    """Run ``cmd``; its output. On timeout the whole process group is killed."""
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProbeError(f"{cmd[0]}: {exc}") from exc
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGKILL)
        p.kill()
        p.communicate()
        raise ProbeError(f"ssh timed out after {timeout:g} s (killed)") from None
    if p.returncode != 0:
        last = (err.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        raise ProbeError(f"ssh exit {p.returncode}: {last}"[:200])
    return decode(out)


def decode(data: bytes) -> str:
    """Command output; ``wmic`` writes UTF-16 when its output is redirected."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        data and data.count(b"\x00") > len(data) // 4
    ):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def parse_df(text: str, mounts: list[str] | None = None) -> list[Reading]:
    """``df -Pk``: each filesystem on a ``/dev`` device (or each of
    ``mounts``), once per device."""
    out, seen = [], set()
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        fs, mount = f[0], " ".join(f[5:])
        try:
            total, used, avail = (int(x) * 1024 for x in f[1:4])
        except ValueError:
            continue
        if mounts is not None:
            if mount not in mounts:
                continue
        elif not fs.startswith("/dev/") or fs.startswith("/dev/loop") or fs in seen:
            continue
        if total <= 0:
            continue
        seen.add(fs)
        out.append(Reading(mount, total, avail, _pct(used, avail)))
    return out


def parse_wmic(text: str, drives: list[str] | None = None) -> list[Reading]:
    """``wmic logicaldisk ... /format:csv``: one row per drive with media
    (a drive with no ``FreeSpace`` is skipped), or each of ``drives``."""
    header: list[str] | None = None
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        cells = [c.strip() for c in line.split(",")]
        if not line.strip():
            continue
        if header is None:
            if "Caption" in cells:
                header = cells
            continue
        d = dict(zip(header, cells, strict=False))
        cap, free, size = d.get("Caption", ""), d.get("FreeSpace", ""), d.get("Size", "")
        if not cap or (drives is not None and cap.upper() not in drives):
            continue
        try:
            total, avail = int(size), int(free)
        except ValueError:
            continue  # no media
        if total > 0:
            out.append(Reading(cap.upper(), total, avail, 100.0 * (total - avail) / total))
    if header is None:
        raise ProbeError("no wmic output (no Caption header)")
    return out


# -- grading -----------------------------------------------------------------------------


def days_to_full(
    samples: list[list[float]], window_s: float, min_span: float = 3600.0
) -> float | None:
    """Days until free reaches 0 at the least-squares rate over the last
    ``window_s`` of ``[time, free]`` samples; None while it is not falling
    or there are fewer than 3 samples over ``min_span``."""
    if not samples:
        return None
    now = samples[-1][0]
    pts = [(t, f) for t, f in samples if now - t <= window_s]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < min_span:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mf = sum(f for _, f in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    slope = sum((t - mt) * (f - mf) for t, f in pts) / var
    if slope >= 0:
        return None
    return max(0.0, pts[-1][1]) / -slope / 86400.0


def add_sample(hist: list[list[float]], t: float, free: int, total: int, window_s: float) -> None:
    """Append ``[t, free]``; a rise of more than 1 GB or 1 % (space was
    freed) starts the trend again, and samples older than the window go."""
    if hist and free - hist[-1][1] > max(GB, 0.01 * total):
        hist.clear()
    hist.append([t, float(free)])
    while hist and t - hist[0][0] > window_s:
        hist.pop(0)


def grade(r: Reading, th: Thresholds, days: float | None) -> tuple[str, str]:
    """(level, reason): the worst threshold crossed."""
    gb = r.free / GB
    have = f"{gb:.1f} GB free of {r.total / GB:.1f} GB ({r.used_pct:.1f} % used)"
    if r.free <= th.critical_free_gb * GB:
        return "critical", f"{have}; critical at {th.critical_free_gb:g} GB"
    if th.alarm_free_gb is not None and gb < th.alarm_free_gb:
        return "alarm", f"{have}; alarm under {th.alarm_free_gb:g} GB"
    if th.alarm_pct is not None and r.used_pct >= th.alarm_pct:
        return "alarm", f"{have}; alarm over {th.alarm_pct:g} % used"
    if th.warn_free_gb is not None and gb < th.warn_free_gb:
        return "warning", f"{have}; warning under {th.warn_free_gb:g} GB"
    if th.warn_pct is not None and r.used_pct >= th.warn_pct:
        return "warning", f"{have}; warning over {th.warn_pct:g} % used"
    if th.warn_days is not None and days is not None and days < th.warn_days:
        return (
            "warning",
            f"{have}; full in {days:.1f} days at the last {th.window_hours:g} h's rate",
        )
    return "ok", have


def alarm_text(name: str, host: str, level: str) -> str:
    """The alarm's text: stable for one disk at one level (module PHRASE)."""
    return f"{name} ({host}): {UNKNOWN if level == 'unknown' else PHRASE[level]}"


# -- the watcher ---------------------------------------------------------------------------


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


class DiskWatch:
    """Reads every target, grades it and builds the status document."""

    def __init__(self, config: Config, clock: Any = time.time) -> None:
        self.config = config
        self.clock = clock
        self.probes = {
            t.name: PathProbe(str(t.path), config.timeout)
            for t in config.targets
            if t.probe == "path"
        }
        self.history: dict[str, list[list[float]]] = {}
        self.last_ok: dict[str, str] = {}
        self.failing_since: dict[str, float] = {}

    def restore(self, doc: dict[str, Any]) -> None:
        """Pick up the history and last good reads of an earlier status file."""
        hist = doc.get("history")
        if isinstance(hist, dict):
            self.history = {k: [list(map(float, s)) for s in v] for k, v in hist.items()}
        for r in doc.get("rows") or []:
            if isinstance(r, dict) and r.get("last_ok"):
                self.last_ok[str(r.get("name"))] = str(r["last_ok"])

    def _read(self, t: Target) -> list[tuple[str, Reading, str | None]]:
        """(row name, reading, volume) for each of the target's disks."""
        if t.probe == "path":
            return [(t.name, self.probes[t.name].read(), t.volume)]
        c = self.config
        remote = WMIC if t.os == "windows" else DF
        cmd = [*c.ssh_command, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={c.connect_timeout:g}"]
        text = run_ssh([*cmd, str(t.ssh), remote], c.ssh_timeout)
        if t.os == "windows":
            readings = parse_wmic(text, t.drives)
        else:
            readings = parse_df(text, t.mounts)
        if not readings:
            raise ProbeError("no fixed disks in the output")
        return [(f"{t.name} {r.where}", r, f"{t.host} {r.where}") for r in readings]

    def _probe(self, t: Target) -> list[tuple[str, Reading, str | None]] | ProbeError:
        try:
            return self._read(t)
        except ProbeError as exc:
            return exc
        except Exception as exc:  # a parse bug: show it, never stop the loop
            log.exception("diskwatch: %s", t.name)
            return ProbeError(f"{type(exc).__name__}: {exc}"[:200])

    def run_pass(self, unknown_after: float | None = None) -> dict[str, Any]:
        """Read every target (in parallel; each read is bounded) and build
        the status document."""
        c = self.config
        wait = c.unknown_after if unknown_after is None else unknown_after
        with concurrent.futures.ThreadPoolExecutor(min(8, len(c.targets))) as pool:
            results = list(pool.map(self._probe, c.targets))
        now = self.clock()
        rows: list[dict[str, Any]] = []
        alarms: list[dict[str, Any]] = []
        volumes: dict[Any, str] = {}
        for t, res in zip(c.targets, results, strict=True):
            th = t.thresholds
            if isinstance(res, ProbeError):
                since = self.failing_since.setdefault(t.name, now)
                rows.append(
                    {
                        "name": t.name,
                        "host": t.host,
                        "where": t.path or t.ssh,
                        "level": "unknown",
                        "error": str(res),
                        "failing_since": _iso(since),
                        "last_ok": self.last_ok.get(t.name),
                    }
                )
                if now - since >= wait:
                    alarms.append(
                        {
                            "name": t.name,
                            "level": "warning",
                            "text": alarm_text(t.name, t.host, "unknown"),
                            "detail": f"{res}; last read {self.last_ok.get(t.name) or 'never'}",
                        }
                    )
                continue
            self.failing_since.pop(t.name, None)
            self.last_ok[t.name] = _iso(now)
            for name, r, volume in res:
                hist = self.history.setdefault(name, [])
                add_sample(hist, now, r.free, r.total, th.window_hours * 3600)
                days = days_to_full(hist, th.window_hours * 3600)
                level, reason = grade(r, th, days)
                self.last_ok[name] = _iso(now)
                row = {
                    "name": name,
                    "host": t.host,
                    "where": r.where,
                    "total": r.total,
                    "free": r.free,
                    "used_pct": round(r.used_pct, 2),
                    "level": level,
                    "reason": reason,
                    "days_to_full": None if days is None else round(days, 2),
                    "last_ok": self.last_ok[name],
                    "error": None,
                }
                # The same volume seen twice (two shares of one disk, a share
                # and the ssh row of its drive) alarms once: by its volume
                # label, or by an identical size and free space on one host.
                sigs = [("auto", t.host, r.total, r.free)]
                if volume:
                    sigs.append(("volume", volume))
                first = next((volumes[s] for s in sigs if s in volumes), None)
                if first is not None:
                    row["same_as"] = first
                else:
                    volumes.update(dict.fromkeys(sigs, name))
                    if level != "ok":
                        alarms.append(
                            {
                                "name": name,
                                "level": level,
                                "text": alarm_text(name, t.host, level),
                                "detail": reason,
                            }
                        )
                rows.append(row)
        # A disk not read for a week (renamed, removed) is forgotten.
        self.history = {k: v for k, v in self.history.items() if v and now - v[-1][0] < 7 * 86400}
        return {
            "time": _iso(now),
            "interval": c.interval,
            # Older than this, the console says the watch is not running.
            "max_age": 3 * c.interval + 60,
            "rows": rows,
            "alarms": alarms,
            "history": self.history,
        }


def write_status(path: Path, doc: dict[str, Any]) -> None:
    """Write ``doc`` atomically (a temporary file next to it, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def table(doc: dict[str, Any]) -> str:
    """The status document as a plain table (``--once``)."""

    def gb(x: Any) -> str:
        return "-" if x is None else f"{x / GB:.1f}"

    lines = [
        f"{'disk':32} {'host':16} {'free GB':>8} {'of GB':>8} {'used':>6} {'full in':>8}  level"
    ]
    for r in doc["rows"]:
        days = "-" if r.get("days_to_full") is None else f"{r['days_to_full']:.1f} d"
        pct = "-" if r.get("used_pct") is None else f"{r['used_pct']:.1f}%"
        state = r["level"] + (f" (same as {r['same_as']})" if r.get("same_as") else "")
        if r.get("error"):
            state += f": {r['error']}"
        lines.append(
            f"{r['name'][:32]:32} {str(r['host'])[:16]:16} {gb(r.get('free')):>8} "
            f"{gb(r.get('total')):>8} {pct:>6} {days:>8}  {state}"
        )
    for a in doc["alarms"]:
        lines.append(f"{a['level'].upper()}: {a['text']} -- {a['detail']}")
    return "\n".join(lines)


def _restore(watch: DiskWatch, path: Path | None) -> None:
    if path is None:
        return
    try:
        watch.restore(json.loads(path.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        log.warning("diskwatch: earlier status %s unreadable (%s); starting afresh", path, exc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast.diskwatch", description=__doc__)
    ap.add_argument("--config", required=True, help="diskwatch config (JSON)")
    ap.add_argument("--once", action="store_true", help="one pass, print a table, exit 1 on alarms")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load(args.config)
    except (OSError, ValueError, TypeError) as exc:
        ap.error(f"{args.config}: {exc}")
    watch = DiskWatch(config)
    _restore(watch, config.status)
    if args.once:
        # Unknown disks alarm at once; the status file is the service's, not written.
        doc = watch.run_pass(unknown_after=0.0)
        print(table(doc))
        return 1 if doc["alarms"] else 0
    if config.status is None:
        ap.error(f"{args.config}: status (the file the console reads) is required")
    while True:
        start = time.monotonic()
        doc = watch.run_pass()
        try:
            write_status(config.status, doc)
        except OSError:
            log.exception("diskwatch: writing %s", config.status)
        for a in doc["alarms"]:
            log.warning("%s: %s (%s)", a["level"], a["text"], a["detail"])
        time.sleep(max(1.0, config.interval - (time.monotonic() - start)))


if __name__ == "__main__":
    sys.exit(main())
