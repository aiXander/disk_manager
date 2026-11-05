"""Tests for backend.staging_manager (stateless, DB-backed)."""

import sqlite3

import pytest

from backend.staging_manager import StagingManager, covered_by_staged_dir


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE nodes (path TEXT PRIMARY KEY, name TEXT, extension TEXT, size INTEGER, is_dir INTEGER)")
    c.execute(
        "CREATE TABLE staged_items (path TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,"
        " is_dir INTEGER NOT NULL, extension TEXT NOT NULL DEFAULT '')"
    )
    c.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
        [
            ("/r/d", "d", "", 300, 1),
            ("/r/d/in.jpg", "in.jpg", ".jpg", 200, 0),
            ("/r/d2.png", "d2.png", ".png", 50, 0),
        ],
    )
    yield c
    c.close()


def test_covered_by_staged_dir():
    assert covered_by_staged_dir("/r/d/in.jpg", {"/r/d"})
    assert not covered_by_staged_dir("/r/d2.png", {"/r/d"})
    assert covered_by_staged_dir("/a", {"/"})
    assert not covered_by_staged_dir("/r/d", {"/r/d"})


def test_add_ignores_unknown_paths_and_dedups(conn):
    state = StagingManager(conn).add(["/r/d2.png", "/r/d2.png", "/not/scanned"])
    assert [i["path"] for i in state["items"]] == ["/r/d2.png"]
    assert state["item_count"] == 1 and state["file_count"] == 1 and state["total_size"] == 50


def test_no_double_count_under_staged_dir(conn):
    sm = StagingManager(conn)
    state = sm.add(["/r/d/in.jpg", "/r/d", "/r/d2.png"])
    assert state["total_size"] == 300 + 50
    assert state["item_count"] == 3 and state["dir_count"] == 1 and state["file_count"] == 2
    covered = {i["path"]: i["covered"] for i in state["items"]}
    assert covered == {"/r/d": False, "/r/d/in.jpg": True, "/r/d2.png": False}
    state = sm.remove(["/r/d"])
    assert state["total_size"] == 250


def test_state_shared_across_instances_and_clear(conn):
    StagingManager(conn).add(["/r/d"])
    assert StagingManager(conn).state()["total_size"] == 300
    assert StagingManager(conn).clear()["item_count"] == 0


def test_sizes_follow_nodes_table(conn):
    sm = StagingManager(conn)
    sm.add(["/r/d"])
    conn.execute("UPDATE nodes SET size = 100 WHERE path = '/r/d'")
    assert sm.state()["total_size"] == 100


def test_remove_subtrees(conn):
    sm = StagingManager(conn)
    sm.add(["/r/d", "/r/d/in.jpg", "/r/d2.png"])
    sm.remove_subtrees(["/r/d"])
    assert [i["path"] for i in sm.state()["items"]] == ["/r/d2.png"]
