"""Tests for the SQLite data layer (backend.db)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from backend.db import (
    SCHEMA_VERSION,
    batch_insert,
    count_query,
    get_node,
    get_tree_data,
    init_db,
    query_files,
    remove_deleted,
    vacuum_db,
)

SCAN_ID = "test_scan"


def _rec(path, *, is_dir=False, size=0, apparent=None, count=None, depth=0,
         parent=None, collapsed=0, extension="") -> dict:
    now = time.time()
    return {
        "path": path,
        "name": path.rsplit("/", 1)[-1] or path,
        "extension": extension,
        "size": size,
        "apparent_size": size if apparent is None else apparent,
        "file_count": (0 if is_dir else 1) if count is None else count,
        "mtime": now,
        "atime": now,
        "ctime": now,
        "birthtime": now,
        "is_dir": int(is_dir),
        "depth": depth,
        "parent_path": parent,
        "collapsed": collapsed,
    }


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "test.db")
    connection.execute(
        "INSERT INTO scan_meta (scan_id, started_at, status, total_files, total_bytes) "
        "VALUES (?, ?, 'complete', 3, 180)",
        (SCAN_ID, time.time()),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def tree(conn):
    """/r (180) -> a/ (150: x 100, y 50), ab.txt 30."""
    batch_insert(conn, [
        _rec("/r", is_dir=True, size=180, count=3),
        _rec("/r/a", is_dir=True, size=150, count=2, depth=1, parent="/r"),
        _rec("/r/a/x", size=100, depth=2, parent="/r/a"),
        _rec("/r/a/y", size=50, depth=2, parent="/r/a"),
        _rec("/r/ab.txt", size=30, depth=1, parent="/r", extension=".txt"),
    ])
    return conn


# ---------------------------------------------------------------------------
# Schema & migration
# ---------------------------------------------------------------------------

def test_init_db_schema(tmp_path):
    c = init_db(tmp_path / "fresh.db")
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"scan_meta", "nodes", "staged_items"} <= tables
        cols = {r[1] for r in c.execute("PRAGMA table_info(nodes)")}
        assert {"apparent_size", "file_count", "collapsed"} <= cols
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_nodes_path" in idx
    finally:
        c.close()


def test_init_db_skips_indexes_while_scan_running(tmp_path):
    path = tmp_path / "running.db"
    c = init_db(path)
    c.execute("INSERT INTO scan_meta (scan_id, status) VALUES ('s', 'running')")
    c.execute("DROP INDEX idx_nodes_path")
    c.commit()
    c.close()
    c = init_db(path)
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    c.close()
    assert "idx_nodes_path" not in idx


_OLD_SCHEMA = """
CREATE TABLE scan_meta (scan_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL,
    roots TEXT, max_depth INTEGER, status TEXT NOT NULL DEFAULT 'running',
    total_files INTEGER NOT NULL DEFAULT 0, total_dirs INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0, error_msg TEXT);
CREATE TABLE nodes (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    extension TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL,
    atime REAL NOT NULL, ctime REAL NOT NULL, birthtime REAL, is_dir INTEGER NOT NULL,
    depth INTEGER NOT NULL, parent_path TEXT);
CREATE INDEX idx_parent ON nodes (parent_path);
CREATE INDEX idx_size_desc ON nodes (size DESC);
CREATE TABLE staged_items (path TEXT PRIMARY KEY, name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, is_dir INTEGER NOT NULL, extension TEXT NOT NULL DEFAULT '');
INSERT INTO scan_meta (scan_id, status) VALUES ('old', 'complete');
INSERT INTO nodes (path, name, size, mtime, atime, ctime, is_dir, depth, parent_path) VALUES
    ('/r', 'r', 60, 0, 0, 0, 1, 0, NULL),
    ('/r/d', 'd', 40, 0, 0, 0, 1, 1, '/r'),
    ('/r/d/f1', 'f1', 10, 0, 0, 0, 0, 2, '/r/d'),
    ('/r/d/f2', 'f2', 30, 0, 0, 0, 0, 2, '/r/d'),
    ('/r/node_modules', 'node_modules', 20, 0, 0, 0, 1, 1, '/r');
"""


def test_old_db_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(_OLD_SCHEMA)
    raw.close()

    c = init_db(path)
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = {r["path"]: r for r in c.execute("SELECT * FROM nodes")}
        assert rows["/r/d/f2"]["apparent_size"] == 30
        assert (rows["/r/d/f1"]["file_count"], rows["/r/d"]["file_count"]) == (1, 2)
        assert rows["/r"]["file_count"] == 2
        assert rows["/r/node_modules"]["collapsed"] == 1
        assert rows["/r/d"]["collapsed"] == 0
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_nodes_parent" in idx and "idx_size_desc" not in idx
        t = get_tree_data(c, "/r", depth=2)
        assert [k["name"] for k in t["children"]] == ["d", "node_modules"]
        assert t["children"][0]["apparent_size"] == 40
    finally:
        c.close()
    # Re-opening a migrated DB is a no-op.
    init_db(path).close()


# ---------------------------------------------------------------------------
# remove_deleted
# ---------------------------------------------------------------------------

def _sizes(conn):
    return {r["path"]: (r["size"], r["apparent_size"], r["file_count"])
            for r in conn.execute("SELECT * FROM nodes")}


def test_remove_deleted_dir_updates_ancestors(tree):
    remove_deleted(tree, ["/r/a"])
    assert _sizes(tree) == {"/r": (30, 30, 1), "/r/ab.txt": (30, 30, 1)}
    meta = tree.execute("SELECT total_files, total_bytes FROM scan_meta").fetchone()
    assert tuple(meta) == (1, 30)


def test_remove_deleted_does_not_touch_prefix_siblings(tree):
    """Deleting /r/a must not delete /r/ab.txt (shares the string prefix)."""
    remove_deleted(tree, ["/r/a/x"])
    s = _sizes(tree)
    assert "/r/ab.txt" in s and "/r/a/y" in s and "/r/a/x" not in s
    assert s["/r/a"] == (50, 50, 1)
    assert s["/r"] == (80, 80, 2)


def test_remove_deleted_nested_paths_subtract_once(tree):
    remove_deleted(tree, ["/r/a/x", "/r/a", "/r/a/"])
    assert _sizes(tree)["/r"] == (30, 30, 1)


def test_remove_deleted_unknown_and_empty(tree):
    remove_deleted(tree, [])
    remove_deleted(tree, ["/nope"])
    assert _sizes(tree)["/r"] == (180, 180, 3)


# ---------------------------------------------------------------------------
# get_tree_data / get_node
# ---------------------------------------------------------------------------

def test_get_tree_data_nested(tree):
    t = get_tree_data(tree, "/r", depth=3)
    assert (t["name"], t["is_dir"], t["size"], t["file_count"]) == ("r", True, 180, 3)
    assert [c["name"] for c in t["children"]] == ["a", "ab.txt"]  # size desc
    a = t["children"][0]
    assert [c["name"] for c in a["children"]] == ["x", "y"]
    assert all("children" not in leaf for leaf in a["children"])
    assert set(a) >= {"name", "path", "size", "apparent_size", "file_count", "is_dir", "collapsed"}


def test_get_tree_data_depth_limit(tree):
    t = get_tree_data(tree, "/r", depth=1)
    assert "children" not in t["children"][0]
    assert "children" not in get_tree_data(tree, "/r", depth=0)


def test_get_tree_data_unknown_root(tree):
    assert get_tree_data(tree, "/missing", depth=2) == {}


def test_get_tree_data_folds_small_children(conn):
    recs = [_rec("/big", is_dir=True, size=55, count=10)]
    recs += [_rec(f"/big/f{i}", size=i, depth=1, parent="/big") for i in range(1, 11)]
    batch_insert(conn, recs)

    t = get_tree_data(conn, "/big", depth=1, max_children=3)
    kids = t["children"]
    assert [k["size"] for k in kids[:3]] == [10, 9, 8]
    other = kids[3]
    assert len(kids) == 4
    assert other["is_other"] is True and other["path"] is None
    assert other["count"] == 7 and other["name"] == "7 smaller items"
    assert other["size"] == sum(range(1, 8)) and other["file_count"] == 7

    assert len(get_tree_data(conn, "/big", depth=1, max_children=10)["children"]) == 10


def test_get_tree_data_does_not_expand_collapsed(conn):
    batch_insert(conn, [
        _rec("/c", is_dir=True, size=5, count=1),
        _rec("/c/node_modules", is_dir=True, size=5, count=1, depth=1, parent="/c", collapsed=1),
    ])
    nm = get_tree_data(conn, "/c", depth=3)["children"][0]
    assert nm["collapsed"] is True and "children" not in nm


def test_get_node(tree):
    node = get_node(tree, "/r/a/x")
    assert node["size"] == 100 and node["parent_path"] == "/r/a"
    assert get_node(tree, "/nope") is None


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def test_batch_insert_defaults(conn):
    batch_insert(conn, [{
        "path": "/f", "name": "f", "size": 7, "mtime": 0, "atime": 0, "ctime": 0,
        "is_dir": 0, "depth": 0,
    }])
    n = get_node(conn, "/f")
    assert (n["apparent_size"], n["file_count"], n["collapsed"]) == (7, 1, 0)


def test_query_and_count(tree):
    rows = query_files(tree, "SELECT path FROM nodes WHERE extension = ?", [".txt"])
    assert [r["path"] for r in rows] == ["/r/ab.txt"]
    assert count_query(tree, "SELECT * FROM nodes WHERE is_dir = ?", [0]) == 3


def test_vacuum_db(tree):
    vacuum_db(tree)
