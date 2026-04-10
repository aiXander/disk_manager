"""Tests for the SQLite data layer (backend.db)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from backend.db import (
    batch_insert,
    compute_directory_sizes,
    count_query,
    delete_records,
    get_tree_data,
    init_db,
    query_files,
    vacuum_db,
)

SCAN_ID = "test_scan"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    path: str,
    name: str,
    *,
    is_dir: bool = False,
    size: int = 0,
    depth: int = 0,
    parent_path: str | None = None,
    extension: str = "",
) -> dict:
    """Return a node dict with all required columns and sensible defaults."""
    now = time.time()
    return {
        "path": path,
        "name": name,
        "extension": extension,
        "size": size,
        "mtime": now,
        "atime": now,
        "ctime": now,
        "birthtime": now,
        "is_dir": int(is_dir),
        "depth": depth,
        "parent_path": parent_path,
    }


def _insert_scan_meta(conn: sqlite3.Connection, scan_id: str = SCAN_ID) -> None:
    """Insert the mandatory scan_meta row so the FK constraint is satisfied."""
    conn.execute(
        "INSERT OR IGNORE INTO scan_meta (scan_id, started_at, status) VALUES (?, ?, ?)",
        (scan_id, time.time(), "running"),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    """Yield a fresh DB connection backed by a file inside tmp_path."""
    db_path = tmp_path / "test.db"
    connection = init_db(db_path)
    _insert_scan_meta(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_db(tmp_path):
    """init_db creates the database with scan_meta and nodes tables."""
    db_path = tmp_path / "init_test.db"
    connection = init_db(db_path)
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scan_meta" in tables
        assert "nodes" in tables
    finally:
        connection.close()


def test_batch_insert(conn):
    """Inserting 10 records stores them all in the nodes table."""
    records = [
        _make_record(f"/tmp/file_{i}.txt", f"file_{i}.txt", extension=".txt", size=100 * i)
        for i in range(10)
    ]
    batch_insert(conn, records)

    row = conn.execute("SELECT COUNT(*) AS cnt FROM nodes").fetchone()
    assert row["cnt"] == 10


def test_batch_insert_empty(conn):
    """Passing an empty list is a no-op and does not raise."""
    batch_insert(conn, [])

    row = conn.execute("SELECT COUNT(*) AS cnt FROM nodes").fetchone()
    assert row["cnt"] == 0


def test_compute_directory_sizes(conn):
    """Parent directory size equals the sum of its children after rollup."""
    records = [
        _make_record("/project", "project", is_dir=True, depth=0, parent_path=None),
        _make_record("/project/a.txt", "a.txt", extension=".txt", size=300, depth=1, parent_path="/project"),
        _make_record("/project/b.txt", "b.txt", extension=".txt", size=700, depth=1, parent_path="/project"),
    ]
    batch_insert(conn, records)

    compute_directory_sizes(conn)

    row = conn.execute("SELECT size FROM nodes WHERE path = '/project'").fetchone()
    assert row["size"] == 1000


def test_get_tree_data(conn):
    """get_tree_data returns a nested structure with correct children."""
    records = [
        _make_record("/root", "root", is_dir=True, depth=0, parent_path=None),
        _make_record("/root/dir", "dir", is_dir=True, depth=1, parent_path="/root"),
        _make_record("/root/dir/f1.py", "f1.py", extension=".py", size=50, depth=2, parent_path="/root/dir"),
        _make_record("/root/dir/f2.py", "f2.py", extension=".py", size=80, depth=2, parent_path="/root/dir"),
    ]
    batch_insert(conn, records)

    tree = get_tree_data(conn, "/root", depth=3)

    assert tree["name"] == "root"
    assert tree["is_dir"] is True
    assert len(tree["children"]) == 1

    child_dir = tree["children"][0]
    assert child_dir["name"] == "dir"
    assert child_dir["is_dir"] is True
    assert len(child_dir["children"]) == 2

    child_names = {c["name"] for c in child_dir["children"]}
    assert child_names == {"f1.py", "f2.py"}

    # Leaf files should have no children key (pruned).
    for leaf in child_dir["children"]:
        assert "children" not in leaf


def test_query_files(conn):
    """query_files executes a SELECT and returns rows as dicts."""
    records = [
        _make_record("/a.txt", "a.txt", extension=".txt", size=10),
        _make_record("/b.log", "b.log", extension=".log", size=20),
        _make_record("/c.txt", "c.txt", extension=".txt", size=30),
    ]
    batch_insert(conn, records)

    results = query_files(
        conn,
        "SELECT path, size FROM nodes WHERE extension = ?",
        [".txt"],
    )
    assert len(results) == 2
    paths = {r["path"] for r in results}
    assert paths == {"/a.txt", "/c.txt"}


def test_count_query(conn):
    """count_query returns the correct number of matching rows."""
    records = [
        _make_record(f"/file_{i}.dat", f"file_{i}.dat", extension=".dat")
        for i in range(5)
    ]
    batch_insert(conn, records)

    count = count_query(
        conn,
        "SELECT * FROM nodes WHERE extension = ?",
        [".dat"],
    )
    assert count == 5


def test_delete_records(conn):
    """delete_records removes the specified paths and leaves the rest."""
    records = [
        _make_record("/keep.txt", "keep.txt", extension=".txt"),
        _make_record("/gone1.txt", "gone1.txt", extension=".txt"),
        _make_record("/gone2.txt", "gone2.txt", extension=".txt"),
    ]
    batch_insert(conn, records)

    delete_records(conn, ["/gone1.txt", "/gone2.txt"])

    remaining = query_files(conn, "SELECT path FROM nodes", [])
    assert len(remaining) == 1
    assert remaining[0]["path"] == "/keep.txt"


def test_vacuum_db(conn):
    """vacuum_db completes without error."""
    records = [_make_record("/v.txt", "v.txt")]
    batch_insert(conn, records)

    # Should not raise.
    vacuum_db(conn)
