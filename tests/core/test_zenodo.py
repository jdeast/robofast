"""robofast_core.zenodo: fetch, verify, cache -- against a loopback HTTP
server, never the network."""

from __future__ import annotations

import hashlib
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType

import numpy as np
import pytest

from robofast_core import catalog as cat
from robofast_core import zenodo, zenodo_assets


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class Files:
    """A loopback server for ``/records/<id>/files/<name>`` with faults."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.hits: list[str] = []
        self.fail_5xx = 0
        self.truncate = 0  # serve this many truncated bodies first
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                fake.hits.append(name)
                if fake.fail_5xx:
                    fake.fail_5xx -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                if name not in fake.files:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = fake.files[name]
                if fake.truncate:
                    fake.truncate -= 1
                    body = body[: len(body) // 2]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def pin(self, name: str, record: int = 1) -> dict:
        b = self.files[name]
        return {"url": f"{self.base}/records/{record}/files/{name}", "size": len(b), "md5": _md5(b)}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(zenodo, "RETRY_BACKOFF", 0.0)
    monkeypatch.delenv(zenodo.OFFLINE_ENV, raising=False)
    s = Files({"a.bin": b"a" * 5000, "b.txt": b"hello\n"})
    yield s
    s.close()


def test_fetch_verifies_and_caches(server, tmp_path):
    assets = {n: server.pin(n) for n in ("a.bin", "b.txt")}
    got = zenodo.fetch_assets(assets, tmp_path)
    assert got["a.bin"].read_bytes() == b"a" * 5000
    assert sorted(server.hits) == ["a.bin", "b.txt"]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]
    # Cached at the pinned size: no request at all the second time.
    zenodo.fetch_assets(assets, tmp_path)
    assert len(server.hits) == 2


def test_wrong_size_cache_is_fetched_again(server, tmp_path):
    (tmp_path / "b.txt").write_bytes(b"hel")  # truncated by an old crash
    zenodo.fetch_assets({"b.txt": server.pin("b.txt")}, tmp_path)
    assert (tmp_path / "b.txt").read_bytes() == b"hello\n"


def test_truncated_and_5xx_are_retried(server, tmp_path):
    server.truncate, server.fail_5xx = 1, 1
    zenodo.fetch_assets({"a.bin": server.pin("a.bin")}, tmp_path)
    assert (tmp_path / "a.bin").stat().st_size == 5000
    assert server.hits == ["a.bin"] * 3


def test_md5_mismatch_fails_and_leaves_nothing(server, tmp_path):
    pin = dict(server.pin("b.txt"), md5="0" * 32)
    with pytest.raises(RuntimeError, match="md5"):
        zenodo.fetch_assets({"b.txt": pin}, tmp_path)
    assert len(server.hits) == zenodo.DOWNLOAD_ATTEMPTS
    assert [p.name for p in tmp_path.iterdir() if not p.name.endswith(".lock")] == []


def test_404_is_not_retried(server, tmp_path):
    pin = dict(server.pin("b.txt"), url=f"{server.base}/records/1/files/nope")
    with pytest.raises(urllib.error.HTTPError):
        zenodo.fetch_assets({"nope": pin}, tmp_path)
    assert server.hits == ["nope"]


def test_offline_refuses_to_download(server, tmp_path, monkeypatch):
    monkeypatch.setenv(zenodo.OFFLINE_ENV, "1")
    with pytest.raises(zenodo.AssetUnavailable, match="ROBOFAST_OFFLINE"):
        zenodo.fetch_assets({"b.txt": server.pin("b.txt")}, tmp_path)
    assert server.hits == []
    # A file already there is still used.
    (tmp_path / "b.txt").write_bytes(b"hello\n")
    zenodo.fetch_assets({"b.txt": server.pin("b.txt")}, tmp_path)


def test_trickle_is_abandoned(monkeypatch, tmp_path):
    class Slow:
        def read1(self, n):
            return b"x"

    clock = iter(range(0, 10**6, 50))
    monkeypatch.setattr(zenodo.time, "monotonic", lambda: next(clock))
    with open(tmp_path / "out", "wb") as f, pytest.raises(TimeoutError, match="abandoning"):
        zenodo._copy_with_progress_floor(Slow(), f, "u")


def test_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(zenodo.DATA_DIR_ENV, str(tmp_path / "d"))
    assert zenodo.data_dir() == tmp_path / "d"
    monkeypatch.delenv(zenodo.DATA_DIR_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x"))
    assert zenodo.data_dir() == tmp_path / "x" / "robofast" / "data"


def test_unpinned_record(tmp_path):
    """Before publication: a clear error, unless a local copy is in place."""
    name = zenodo_assets.GAIA_EXTRACTS
    if zenodo_assets.available(name):
        pytest.skip("the record is pinned")
    assert name in zenodo_assets.PENDING
    with pytest.raises(zenodo.AssetUnavailable, match="not pinned"):
        zenodo.fetch_file(name, "bright_stars.npy", root=tmp_path)
    local = tmp_path / name / "bright_stars.npy"
    local.parent.mkdir()
    local.write_bytes(b"x")
    assert zenodo.fetch_file(name, "bright_stars.npy", root=tmp_path) == local


@pytest.fixture
def pinned(server, monkeypatch, tmp_path):
    """The gaia_extracts record pinned to a loopback 'Zenodo' serving a
    small bright-star file."""
    rec = np.zeros(2, cat.TILE_DTYPE)
    rec["source_id"], rec["ra"], rec["dec"], rec["g"] = [1, 2], [10.0, 50.0], [5.0, -5.0], [3, 4]
    p = tmp_path / "src.npy"
    np.save(p, rec, allow_pickle=False)
    server.files["bright_stars.npy"] = p.read_bytes()
    b = server.files["bright_stars.npy"]
    record = zenodo_assets.ZenodoRecord(
        record_id=1,
        concept_record_id=2,
        title="test",
        creators=("Eastman, Jason",),
        files=zenodo_assets._pins({"bright_stars.npy": (len(b), _md5(b))}),
    )
    monkeypatch.setattr(zenodo_assets, "ZENODO", server.base)
    monkeypatch.setattr(
        zenodo_assets, "RECORDS", MappingProxyType({zenodo_assets.GAIA_EXTRACTS: record})
    )
    monkeypatch.setenv(zenodo.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setattr(cat, "_BRIGHT", None)
    return server


def test_bright_stars_downloaded_once(pinned, tmp_path):
    b = cat.bright_stars()
    assert list(b.source_id) == [1, 2] and list(b.source) == ["bright", "bright"]
    assert (tmp_path / "data" / "gaia_extracts" / "bright_stars.npy").is_file()
    assert cat.bright_stars() is b  # memoized; and on disk for the next process
    cat._BRIGHT = None
    cat.bright_stars()
    assert pinned.hits == ["bright_stars.npy"]
    # The pre-fill command fetches everything pinned (here, already there).
    assert zenodo.main(["fetch"]) == 0 and zenodo.main(["status"]) == 0
    assert pinned.hits == ["bright_stars.npy"]


def test_failed_download_is_catalog_unavailable(pinned):
    pinned.files["bright_stars.npy"] = b"different"
    with pytest.raises(cat.CatalogUnavailable, match="bright stars"):
        cat.bright_stars()
