"""Fetch, verify and cache the data files robofast keeps on Zenodo.

Data robofast needs but may not ship under its BSD licence -- the Gaia
DR3 bright-star list and the real-Gaia test fixture, which are under ESA's
CC BY-NC 3.0 IGO licence (docs/sky.md, "Licences") -- live in a Zenodo
record and are downloaded on first use. Every file is pinned by size and
md5 in ONE place, :mod:`robofast_core.zenodo_assets`; this module owns the
mechanics. Uploading a new version is ``scripts/zenodo_publish.py``
(docs/zenodo.md).

Adapted from EXOZIPPy (``exozippy.utilities.zenodo``, BSD 3-Clause, by the
same author), trimmed to what robofast needs: the destination here is
already a per-user cache directory, so EXOZIPPy's second, machine-level
cache behind it is left out.

Integrity, because the failure without it is silent and lasting (a
truncated body written in place, and every later run reading half a file):

* a cached file is size-checked on every use (cheap; truncation always
  changes the size);
* a download lands on a temporary ``.part`` name in the destination
  directory, is checked for size AND md5, and only then atomically renamed
  into place, so an interrupted fetch is never mistaken for a cached one;
* a corrupt cached file is fetched again rather than raising;
* a transfer is bounded: every socket read times out after
  ``DOWNLOAD_TIMEOUT`` s of silence, and one that trickles below
  ``PROGRESS_FLOOR`` bytes per ``PROGRESS_WINDOW`` is abandoned and retried;
* concurrent fetches of one file (two services starting at once) serialize
  on an ``flock`` next to it.

Where the files go: :func:`data_dir`, which is ``$ROBOFAST_DATA_DIR`` if
set, else ``$XDG_CACHE_HOME/robofast/data``, else
``~/.cache/robofast/data``; each record in its own subdirectory. Set
``ROBOFAST_OFFLINE=1`` to forbid downloads (a missing file then raises
:class:`AssetUnavailable` at once). To pre-fill a control host that will
run offline::

    ROBOFAST_DATA_DIR=/var/lib/robofast/data python -m robofast_core.zenodo fetch
    python -m robofast_core.zenodo status

or copy the files from another machine into the same layout: a file of
the pinned size is used as it is.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

try:  # POSIX only; correct without it, just not serialized.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

__all__ = [
    "DATA_DIR_ENV",
    "OFFLINE_ENV",
    "AssetUnavailable",
    "data_dir",
    "fetch_assets",
    "fetch_every_record",
    "fetch_file",
    "offline",
]

DATA_DIR_ENV = "ROBOFAST_DATA_DIR"
OFFLINE_ENV = "ROBOFAST_OFFLINE"
_FALSE = frozenset({"", "0", "no", "false", "off"})

#: Zenodo is a free academic host and answers 5xx under load: four attempts
#: with doubling backoff (~35 s) ride out a transient gateway error without
#: making a dead URL take minutes to report.
DOWNLOAD_ATTEMPTS = 4
RETRY_BACKOFF = 5.0  # s; doubles each attempt
#: Socket timeout for the connect and for every single read (not a budget
#: for the whole transfer): only a peer gone quiet trips it.
DOWNLOAD_TIMEOUT = 60.0
DOWNLOAD_CHUNK = 1 << 20
#: A peer that trickles never goes quiet; an attempt moving fewer than
#: PROGRESS_FLOOR bytes in any PROGRESS_WINDOW is abandoned and retried.
PROGRESS_WINDOW = 120.0
PROGRESS_FLOOR = 1 << 20
#: How long to wait for another process fetching the same file.
LOCK_TIMEOUT = 600.0
LOCK_POLL = 0.5


class AssetUnavailable(RuntimeError):
    """A data file is not on disk and cannot be fetched (not pinned yet,
    offline, or the download failed). The message says how to provide it."""


def data_dir() -> Path:
    """Where downloaded data files live (module doc). Pure: creates nothing."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "robofast" / "data"


def offline() -> bool:
    """True when ``$ROBOFAST_OFFLINE`` forbids downloads."""
    return os.environ.get(OFFLINE_ENV, "").strip().lower() not in _FALSE


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    """An exclusive ``flock`` on ``<path>.lock``, polled so a stuck holder
    cannot wedge the caller; proceeding without it is safe (the publish is
    an atomic rename of a verified file), only wasteful."""
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    fd = None
    try:
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    break  # this filesystem does not lock: go on unlocked
                if time.monotonic() >= deadline:
                    log.warning("waited %.0f s for another fetch of %s", LOCK_TIMEOUT, path.name)
                    break
                time.sleep(LOCK_POLL)
    except OSError as e:
        log.debug("no lock for %s (%s)", path, e)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _copy_with_progress_floor(response, handle, url: str) -> None:
    # read1, not read: read(n) loops until it has n bytes, so at a trickle
    # one call could outlast the window before the check ever ran.
    start, moved = time.monotonic(), 0
    while True:
        block = response.read1(DOWNLOAD_CHUNK)
        if not block:
            return
        handle.write(block)
        moved += len(block)
        elapsed = time.monotonic() - start
        if elapsed >= PROGRESS_WINDOW:
            if moved < PROGRESS_FLOOR:
                raise TimeoutError(
                    f"{url} delivered only {moved} bytes in {elapsed:.0f} s; abandoning "
                    "this attempt"
                )
            start, moved = time.monotonic(), 0


def _urlretrieve(url: str, dest: Path) -> None:
    """``urlretrieve`` with a socket timeout on every read (which
    urlretrieve cannot be given), so a stalled peer raises and is retried."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
        with open(dest, "wb") as handle:
            _copy_with_progress_floor(response, handle, url)


def _download_verified(meta: Mapping[str, object], filename: str, part: Path) -> None:
    """Download ``meta["url"]`` onto ``part``, size- and md5-checked, with
    retries. On return ``part`` holds the pinned bytes; on failure it does
    not exist. A 4xx (wrong record or name) is raised at once."""
    last: BaseException | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            _urlretrieve(str(meta["url"]), part)
            size = part.stat().st_size
            if size != meta["size"]:
                raise RuntimeError(f"{filename}: got {size} bytes, expected {meta['size']}")
            digest = _md5(part)
            if digest != meta["md5"]:
                raise RuntimeError(f"{filename}: md5 {digest}, expected {meta['md5']}")
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                part.unlink(missing_ok=True)
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as e:
            last = e
        part.unlink(missing_ok=True)
        if attempt < DOWNLOAD_ATTEMPTS:
            delay = RETRY_BACKOFF * 2 ** (attempt - 1)
            log.warning(
                "download of %s failed (attempt %d/%d): %s; retrying in %.0f s",
                filename,
                attempt,
                DOWNLOAD_ATTEMPTS,
                last,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"could not download {filename} after {DOWNLOAD_ATTEMPTS} attempts: {last}"
    ) from last


def fetch_assets(
    assets: Mapping[str, Mapping[str, object]],
    dest_dir: str | Path,
    on_fetch: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """Make every ``{filename: {"url", "size", "md5"}}`` of ``assets``
    present and intact in ``dest_dir``; returns ``{filename: path}``.

    A file already there at the pinned size is used as it is; one of the
    wrong size is fetched again. ``on_fetch(filename)`` is called just
    before each real download. Raises :class:`AssetUnavailable` when a file
    is missing and downloads are forbidden (``$ROBOFAST_OFFLINE``),
    ``RuntimeError`` when every attempt failed, and
    ``urllib.error.HTTPError`` at once for a 4xx."""
    dest_dir = Path(dest_dir)
    out: dict[str, Path] = {}
    for filename, meta in assets.items():
        dest = dest_dir / filename
        out[filename] = dest
        if dest.is_file():
            size = dest.stat().st_size
            if size == meta["size"]:
                continue
            log.warning("%s is %d bytes, expected %d: fetching it again", dest, size, meta["size"])
        if offline():
            raise AssetUnavailable(
                f"{dest} is missing and {OFFLINE_ENV} forbids downloads; copy it there "
                f"(docs/sky.md, 'Pre-filling a host')"
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        with _lock(dest):
            if dest.is_file() and dest.stat().st_size == meta["size"]:
                continue  # whoever held the lock fetched it
            if on_fetch is not None:
                on_fetch(filename)
            log.info("downloading %s from %s", filename, meta["url"])
            fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=f".{filename}.", suffix=".part")
            os.close(fd)
            part = Path(tmp)
            part.unlink()  # reserve the name only; the download creates it
            try:
                _download_verified(meta, filename, part)
                os.replace(part, dest)
            finally:
                part.unlink(missing_ok=True)
        log.info("saved %s", dest)
    return out


def record_dir(name: str, root: str | Path | None = None) -> Path:
    """The directory record ``name``'s files are cached in."""
    return (Path(root) if root is not None else data_dir()) / name


def fetch_file(name: str, filename: str, root: str | Path | None = None) -> Path:
    """The local path of ``filename`` of the registered record ``name``,
    fetched first if need be. Raises :class:`AssetUnavailable` (with what
    to do about it) when the record is not pinned yet or the download
    fails."""
    from robofast_core import zenodo_assets

    dest = record_dir(name, root)
    if not zenodo_assets.available(name):
        local = dest / filename
        if local.is_file():
            # Placed by hand (or built by scripts/build_catalog.py) before
            # the record was pinned: nothing to check it against.
            return local
        raise AssetUnavailable(
            f"{filename}: Zenodo record {name!r} is not pinned yet "
            f"(robofast_core/zenodo_assets.py); build it with scripts/build_catalog.py "
            f"or copy it to {local}"
        )
    try:
        return fetch_assets(zenodo_assets.assets(name, [filename]), dest)[filename]
    except AssetUnavailable:
        raise
    except (RuntimeError, OSError, urllib.error.URLError) as e:
        raise AssetUnavailable(f"{filename} (Zenodo record {name!r}): {e}") from e


def fetch_every_record(root: str | Path | None = None) -> list[Path]:
    """Every file of every pinned record into ``root`` (default
    :func:`data_dir`): the one-call pre-fill for an offline host and for
    CI's download cache. Returns the paths."""
    from robofast_core import zenodo_assets

    paths: list[Path] = []
    for name in sorted(zenodo_assets.RECORDS):
        got = fetch_assets(zenodo_assets.assets(name), record_dir(name, root))
        paths += [got[k] for k in sorted(got)]
    return paths


def main(argv: list[str] | None = None) -> int:
    from robofast_core import zenodo_assets

    ap = argparse.ArgumentParser(
        prog="python -m robofast_core.zenodo",
        description="Fetch (or list) the data files robofast keeps on Zenodo (docs/sky.md).",
    )
    ap.add_argument("action", choices=("fetch", "status"))
    ap.add_argument("--dir", help=f"data directory (default ${DATA_DIR_ENV} or the user cache)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = Path(args.dir) if args.dir else data_dir()
    if args.action == "fetch":
        if not zenodo_assets.RECORDS:
            print("no Zenodo record is pinned yet: nothing to fetch")
            return 0
        for p in fetch_every_record(root):
            print(p)
        return 0
    print(f"data directory: {root}")
    for name in sorted(set(zenodo_assets.RECORDS) | set(zenodo_assets.PENDING)):
        if name not in zenodo_assets.RECORDS:
            print(f"{name}: not pinned yet ({zenodo_assets.PENDING[name]})")
            continue
        rec = zenodo_assets.RECORDS[name]
        print(f"{name}: record {rec.record_id} ({rec.doi})")
        for filename, pin in sorted(rec.files.items()):
            p = record_dir(name, root) / filename
            ok = p.is_file() and p.stat().st_size == pin.size
            print(f"  {'ok     ' if ok else 'MISSING'} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
