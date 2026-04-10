"""Tests for deletion_engine — CRITICAL: never delete real files."""

import os
from pathlib import Path

import pytest

from backend.deletion_engine import DeletionResult, _deduplicate_paths, execute_deletion


def test_deletion_result_to_dict():
    r = DeletionResult(
        deleted=["/a/b.txt"],
        failed=[("/a/c.txt", "PermissionError")],
        bytes_freed=1024,
    )
    d = r.to_dict()
    assert d["deleted"] == ["/a/b.txt"]
    assert d["failed"] == [{"path": "/a/c.txt", "error": "PermissionError"}]
    assert d["bytes_freed"] == 1024


def test_deduplicate_paths_removes_children():
    paths = ["/a/b/c.txt", "/a/b", "/a/b/d.txt", "/x/y.txt"]
    # /a/b is a directory (need it to exist for is_dir check).
    # Since _deduplicate_paths calls Path.is_dir(), we test with real tmp paths.
    pass  # Tested via test_deduplicate_with_real_dirs below


def test_deduplicate_with_real_dirs(tmp_path):
    # Create structure: parent_dir/child.txt
    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "child.txt"
    child.write_text("hello")
    other = tmp_path / "other.txt"
    other.write_text("world")

    paths = [str(child), str(parent), str(other)]
    result = _deduplicate_paths(paths)

    # parent is a dir, child is inside it — child should be removed
    assert str(parent) in result
    assert str(other) in result
    assert str(child) not in result


def test_execute_deletion_permanent(tmp_path):
    """Permanently delete files in a temp directory."""
    f1 = tmp_path / "file1.txt"
    f1.write_text("data1")
    f2 = tmp_path / "file2.txt"
    f2.write_text("data2")

    result = execute_deletion([str(f1), str(f2)], permanent=True)

    assert str(f1) in result.deleted
    assert str(f2) in result.deleted
    assert not f1.exists()
    assert not f2.exists()
    assert result.bytes_freed > 0
    assert len(result.failed) == 0


def test_execute_deletion_nonexistent(tmp_path):
    """Deleting a nonexistent file should fail gracefully."""
    fake = str(tmp_path / "nonexistent.txt")
    result = execute_deletion([fake], permanent=True)

    assert len(result.deleted) == 0
    assert len(result.failed) == 1
    assert result.failed[0][0] == fake


def test_execute_deletion_directory(tmp_path):
    """Permanently delete a directory."""
    d = tmp_path / "mydir"
    d.mkdir()
    (d / "inside.txt").write_text("inner")

    result = execute_deletion([str(d)], permanent=True)

    assert str(d) in result.deleted
    assert not d.exists()


def test_execute_deletion_empty_list():
    """Empty path list should return empty result."""
    result = execute_deletion([], permanent=True)
    assert result.deleted == []
    assert result.failed == []
    assert result.bytes_freed == 0
