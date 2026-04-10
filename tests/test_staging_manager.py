"""Tests for backend.staging_manager.StagingManager (SQLite-backed)."""

import pytest

from backend.db import init_db
from backend.staging_manager import StagingManager


def _make_item(path, name, size, is_dir=False, extension=""):
    return {
        "path": path,
        "name": name,
        "size": size,
        "is_dir": is_dir,
        "extension": extension,
    }


ITEMS = [
    _make_item("/tmp/a.jpg", "a.jpg", 100, extension=".jpg"),
    _make_item("/tmp/b.png", "b.png", 200, extension=".png"),
    _make_item("/tmp/c", "c", 300, is_dir=True, extension=""),
]


@pytest.fixture()
def sm(tmp_path):
    """Yield a StagingManager backed by a temp SQLite DB."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    manager = StagingManager(conn)
    yield manager
    conn.close()


def test_add_items(sm):
    """Adding 3 items gives correct counts and total_size."""
    state = sm.add_items(ITEMS)

    assert len(state.items) == 3
    assert state.total_size == 600
    assert state.file_count == 2
    assert state.dir_count == 1


def test_add_items_dedup(sm):
    """Adding the same path twice keeps only one entry."""
    sm.add_items([ITEMS[0]])
    state = sm.add_items([ITEMS[0]])

    assert len(state.items) == 1


def test_remove_items(sm):
    """Removing an item by path removes it from the state."""
    sm.add_items(ITEMS)
    state = sm.remove_items(["/tmp/b.png"])

    paths = [item.path for item in state.items]
    assert "/tmp/b.png" not in paths
    assert len(state.items) == 2
    assert state.total_size == 400


def test_clear(sm):
    """clear() empties all staged items."""
    sm.add_items(ITEMS)
    sm.clear()

    state = sm.get_state()
    assert len(state.items) == 0
    assert state.total_size == 0


def test_persistence_via_db(tmp_path):
    """Items persist in SQLite — a new StagingManager on the same DB sees them."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    sm1 = StagingManager(conn)
    sm1.add_items(ITEMS)

    # Create a new StagingManager on the same connection — should load from DB.
    sm2 = StagingManager(conn)
    state = sm2.get_state()

    assert len(state.items) == 3
    assert state.total_size == 600
    conn.close()


def test_get_state_aggregates(sm):
    """count_by_extension, dir_count, and file_count are computed correctly."""
    sm.add_items(ITEMS)
    state = sm.get_state()

    assert state.count_by_extension[".jpg"] == 1
    assert state.count_by_extension[".png"] == 1
    assert state.count_by_extension[""] == 1
    assert state.dir_count == 1
    assert state.file_count == 2
