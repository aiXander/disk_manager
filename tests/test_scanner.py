"""Tests for backend.scanner: walk a tmp tree into a tmp DB, check the rows."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from backend import scanner
from backend.scanner import ProgressEvent, scan_roots


@pytest.fixture(params=["bulk", "scandir"])
def lister(request, monkeypatch):
    """Run every test against both listing backends."""
    if request.param == "bulk":
        if scanner._list_bulk is None:
            pytest.skip("getattrlistbulk only on macOS")
    else:
        monkeypatch.setattr(scanner, "_list_bulk", None)
    return request.param


def _scan(tmp_path: Path, roots, cancel_event=None, **overrides):
    kwargs = dict(
        scan_id="t",
        roots=roots if isinstance(roots, list) else [roots],
        max_depth=100,
        adaptive=False,
        adaptive_threshold=5000,
        db_path=tmp_path / "scan.db",
        skip_dir_names=[],
        cancel_event=cancel_event,
    )
    kwargs.update(overrides)

    async def collect():
        return [ev async for ev in scan_roots(**kwargs)]

    events = asyncio.run(collect())
    conn = sqlite3.connect(str(kwargs["db_path"]))
    conn.row_factory = sqlite3.Row
    return events, conn


def _rows(conn) -> dict[str, sqlite3.Row]:
    return {r["path"]: r for r in conn.execute("SELECT * FROM nodes")}


def _alloc(p: Path) -> int:
    return os.lstat(p).st_blocks * 512


def _write(p: Path, nbytes: int) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * nbytes)
    return p


@pytest.fixture()
def data(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root.resolve()


# ------------------------------------------------------------------
# Rows, sizes, rollup
# ------------------------------------------------------------------


def test_basic_rows_and_rollup(tmp_path, data, lister):
    a = _write(data / "a.txt", 10_000)
    b = _write(data / "sub" / "b.PY", 50_000)
    events, conn = _scan(tmp_path, data)
    rows = _rows(conn)

    assert set(rows) == {str(data), str(a), str(data / "sub"), str(b)}
    assert rows[str(a)]["size"] == _alloc(a)
    assert rows[str(a)]["apparent_size"] == 10_000
    assert rows[str(b)]["extension"] == ".py"
    assert rows[str(b)]["parent_path"] == str(data / "sub")
    assert rows[str(b)]["depth"] == 2

    root = rows[str(data)]
    assert root["parent_path"] is None and root["depth"] == 0
    assert root["file_count"] == 2
    assert root["apparent_size"] == 60_000
    assert root["size"] >= _alloc(a) + _alloc(b)
    assert rows[str(data / "sub")]["size"] >= _alloc(b)
    assert all(r["collapsed"] == 0 for r in rows.values())

    final = events[-1]
    assert final.status == "complete"
    assert final.files_scanned == 2
    assert final.bytes_seen == root["size"]
    meta = conn.execute("SELECT * FROM scan_meta").fetchone()
    assert meta["status"] == "complete"
    assert (meta["total_files"], meta["total_bytes"]) == (2, root["size"])
    assert json.loads(meta["roots"]) == [str(data)]


def test_skip_dir_fully_counted_but_collapsed(tmp_path, data, lister):
    """node_modules is one collapsed row whose size includes everything below it."""
    big = _write(data / "node_modules" / "pkg" / "lib" / "big.bin", 300_000)
    _write(data / "src" / "main.py", 100)
    _, conn = _scan(tmp_path, data, skip_dir_names=["node_modules"])
    rows = _rows(conn)

    nm = rows[str(data / "node_modules")]
    assert nm["collapsed"] == 1
    assert nm["size"] >= _alloc(big)
    assert nm["apparent_size"] == 300_000
    assert nm["file_count"] == 1
    assert not [p for p in rows if p.startswith(str(data / "node_modules") + "/")]
    assert str(data / "src" / "main.py") in rows
    assert rows[str(data)]["apparent_size"] == 300_100


def test_depth_limit_does_not_truncate_sizes(tmp_path, data, lister):
    deep = data / "d0" / "d1" / "d2" / "d3" / "d4"
    leaf = _write(deep / "leaf.bin", 123_456)
    _, conn = _scan(tmp_path, data, max_depth=2)
    rows = _rows(conn)

    assert max(r["depth"] for r in rows.values()) == 2
    boundary = rows[str(data / "d0" / "d1")]
    assert boundary["collapsed"] == 1
    assert boundary["apparent_size"] == 123_456
    assert boundary["file_count"] == 1
    assert rows[str(data)]["size"] >= _alloc(leaf)
    assert rows[str(data)]["file_count"] == 1


def test_adaptive_depth_reduces_depth_below_big_dirs(tmp_path, data, lister):
    for i in range(6):
        _write(data / "wide" / f"f{i}", 1)
    _write(data / "wide" / "sub" / "deeper" / "x", 7)
    _, conn = _scan(tmp_path, data, max_depth=3, adaptive=True, adaptive_threshold=5)
    rows = _rows(conn)
    assert rows[str(data / "wide" / "sub")]["collapsed"] == 1
    assert rows[str(data)]["apparent_size"] == 13


def test_hardlinks_counted_once(tmp_path, data, lister):
    a = _write(data / "a.bin", 200_000)
    os.link(a, data / "b.bin")
    _, conn = _scan(tmp_path, data)
    rows = _rows(conn)

    sizes = sorted(rows[str(data / n)]["size"] for n in ("a.bin", "b.bin"))
    assert sizes == [0, _alloc(a)]  # one link carries the bytes, the other is free
    assert rows[str(data)]["size"] < 2 * _alloc(a)
    assert rows[str(data)]["file_count"] == 2


def test_symlink_to_dir_is_not_followed(tmp_path, data, lister):
    _write(data / "real" / "inside.bin", 50_000)
    (data / "link").symlink_to(data / "real")
    _, conn = _scan(tmp_path, data)
    rows = _rows(conn)

    assert rows[str(data / "link")]["is_dir"] == 0
    assert str(data / "link" / "inside.bin") not in rows
    assert rows[str(data)]["apparent_size"] == 50_000 + rows[str(data / "link")]["apparent_size"]


def test_extensions(tmp_path, data, lister):
    for name in ("readme.MD", "photo.JPG", "Makefile", ".bashrc", "a.tar.gz"):
        _write(data / name, 1)
    _, conn = _scan(tmp_path, data)
    ext = {r["name"]: r["extension"] for r in _rows(conn).values() if not r["is_dir"]}
    assert ext == {"readme.MD": ".md", "photo.JPG": ".jpg", "Makefile": "",
                   ".bashrc": "", "a.tar.gz": ".gz"}


def test_unreadable_dir_is_counted_as_error(tmp_path, data, lister):
    locked = data / "locked"
    _write(locked / "secret.bin", 10)
    locked.chmod(0)
    try:
        events, conn = _scan(tmp_path, data)
    finally:
        locked.chmod(0o755)
    rows = _rows(conn)
    assert events[-1].status == "complete"
    assert events[-1].errors >= 1
    assert rows[str(locked)]["collapsed"] == 1
    assert str(locked / "secret.bin") not in rows


def test_nested_and_duplicate_roots_are_scanned_once(tmp_path, data, lister):
    f = _write(data / "sub" / "f.bin", 1000)
    _, conn = _scan(tmp_path, [data / "sub", data, data])
    rows = _rows(conn)
    assert rows[str(f)]["depth"] == 2
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE depth = 0").fetchone()[0] == 1


def test_rescan_into_same_db_replaces_rows(tmp_path, data, lister):
    _write(data / "old.bin", 1)
    _scan(tmp_path, data)
    (data / "old.bin").unlink()
    _write(data / "new.bin", 1)
    _, conn = _scan(tmp_path, data)
    assert {Path(p).name for p in _rows(conn)} == {"data", "new.bin"}


def test_indexes_built_after_scan(tmp_path, data):
    _write(data / "a", 1)
    _, conn = _scan(tmp_path, data)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_nodes_path", "idx_nodes_parent", "idx_nodes_size"} <= names


# ------------------------------------------------------------------
# Events and cancellation
# ------------------------------------------------------------------


def test_event_sequence(tmp_path, data, monkeypatch):
    monkeypatch.setattr(scanner, "PROGRESS_INTERVAL", 0)
    for i in range(20):
        _write(data / f"d{i}" / "f.txt", 10)
    events, _ = _scan(tmp_path, data)

    statuses = [e.status for e in events]
    assert statuses[-1] == "complete"
    assert statuses.count("complete") == 1 and all(s == "running" for s in statuses[:-1])
    phases = [e.phase for e in events[:-1]]
    assert "walking" in phases and phases[-1] == "finalizing"
    assert all(e.scan_id == "t" for e in events)
    assert events[-1].elapsed_s >= events[0].elapsed_s >= 0


def test_cancel_before_start(tmp_path, data, lister):
    for i in range(10):
        _write(data / f"dir{i}" / "f.txt", 10)
    cancel = threading.Event()
    cancel.set()
    events, conn = _scan(tmp_path, data, cancel_event=cancel)

    assert [e.status for e in events][-1] == "cancelled"
    assert sum(e.status != "running" for e in events) == 1
    assert conn.execute("SELECT status FROM scan_meta").fetchone()[0] == "cancelled"


def test_cancel_during_final_flush_is_still_cancelled(tmp_path, data, monkeypatch):
    """A cancel arriving after the last directory (while the last rows are
    written) must not end as 'complete'."""
    _write(data / "f.txt", 10)
    cancel = threading.Event()
    real_close = scanner._Writer.close

    def close_and_cancel(self):
        cancel.set()
        real_close(self)

    monkeypatch.setattr(scanner._Writer, "close", close_and_cancel)
    events, conn = _scan(tmp_path, data, cancel_event=cancel)

    assert events[-1].status == "cancelled"
    assert conn.execute("SELECT status FROM scan_meta").fetchone()[0] == "cancelled"


def test_missing_root_reports_error_count(tmp_path):
    events, _ = _scan(tmp_path, tmp_path / "does-not-exist")
    assert events[-1].status == "complete"
    assert events[-1].errors == 1


def test_progress_event_defaults():
    evt = ProgressEvent("s", 1, 2, "/p", 3, "running")
    assert (evt.phase, evt.elapsed_s, evt.errors, evt.error) == ("walking", 0.0, 0, None)
