"""Tests for backend.scanner — filesystem BFS walker."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from backend.scanner import FileRecord, ProgressEvent, _walk_directory


def _default_walk(root: Path, **overrides):
    """Helper that calls _walk_directory with sensible defaults."""
    kwargs = dict(
        root=root,
        max_depth=100,
        adaptive=False,
        adaptive_threshold=5000,
        skip_dir_names=set(),
        cancel_event=threading.Event(),
    )
    kwargs.update(overrides)
    return list(_walk_directory(**kwargs))


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_walk_directory_basic(tmp_path: Path):
    """Create a tmp dir with files and subdirs, verify FileRecords for all."""
    (tmp_path / "a.txt").touch()
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "b.py").touch()

    records = _default_walk(tmp_path)
    paths = {r.path for r in records}

    # Root dir, a.txt, subdir/, subdir/b.py
    assert str(tmp_path) in paths
    assert str(tmp_path / "a.txt") in paths
    assert str(sub) in paths
    assert str(sub / "b.py") in paths
    assert len(records) == 4


def test_walk_directory_skips_symlink_dirs(tmp_path: Path):
    """A symlink pointing to a directory should be yielded as a file (is_dir=0)."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "inside.txt").touch()

    link = tmp_path / "link_to_real"
    link.symlink_to(real_dir)

    records = _default_walk(tmp_path)

    link_records = [r for r in records if r.path == str(link)]
    assert len(link_records) == 1
    assert link_records[0].is_dir == 0


def test_walk_directory_skip_dir_names(tmp_path: Path):
    """Directories in skip_dir_names are yielded as leaf nodes but their
    children are NOT yielded."""
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "package.json").touch()

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").touch()

    records = _default_walk(tmp_path, skip_dir_names={"node_modules"})
    paths = {r.path for r in records}

    # node_modules itself is yielded
    nm_records = [r for r in records if r.path == str(nm)]
    assert len(nm_records) == 1
    assert nm_records[0].is_dir == 1

    # Its children are NOT present
    assert str(nm / "package.json") not in paths

    # Normal sibling is fully walked
    assert str(src / "main.py") in paths


def test_walk_directory_max_depth(tmp_path: Path):
    """Create a 5-level deep tree, set max_depth=2, verify nothing deeper."""
    current = tmp_path
    for i in range(5):
        current = current / f"d{i}"
        current.mkdir()
    (current / "leaf.txt").touch()

    records = _default_walk(tmp_path, max_depth=2)

    max_seen = max(r.depth for r in records)
    assert max_seen <= 2


def test_walk_directory_extensions(tmp_path: Path):
    """Files should get correct lowercase extensions with leading dot."""
    (tmp_path / "readme.MD").touch()
    (tmp_path / "photo.JPG").touch()
    (tmp_path / "Makefile").touch()

    records = _default_walk(tmp_path)

    by_name = {r.name: r for r in records}

    assert by_name["readme.MD"].extension == ".md"
    assert by_name["photo.JPG"].extension == ".jpg"
    assert by_name["Makefile"].extension == ""


def test_file_record_to_dict():
    """FileRecord.to_dict() should return a dict with every slot field."""
    rec = FileRecord(
        path="/tmp/test",
        name="test",
        extension=".txt",
        size=42,
        mtime=1000.0,
        atime=1001.0,
        ctime=1002.0,
        birthtime=999.0,
        is_dir=0,
        depth=1,
        parent_path="/tmp",
    )

    d = rec.to_dict()

    assert isinstance(d, dict)
    expected_keys = {
        "path", "name", "extension", "size", "mtime", "atime",
        "ctime", "birthtime", "is_dir", "depth", "parent_path",
    }
    assert set(d.keys()) == expected_keys
    assert d["path"] == "/tmp/test"
    assert d["size"] == 42
    assert d["is_dir"] == 0


def test_progress_event_fields():
    """ProgressEvent should store all fields correctly, including defaults."""
    evt = ProgressEvent(
        scan_id="scan-abc",
        files_scanned=10,
        dirs_scanned=3,
        current_path="/some/path",
        bytes_seen=4096,
        status="running",
    )

    assert evt.scan_id == "scan-abc"
    assert evt.files_scanned == 10
    assert evt.status == "running"
    assert evt.error is None


def test_walk_directory_cancellation(tmp_path: Path):
    """Setting cancel_event stops the walk early."""
    for i in range(10):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        for j in range(5):
            (d / f"file{j}.txt").touch()

    cancel = threading.Event()
    cancel.set()  # Cancel immediately

    records = _default_walk(tmp_path, cancel_event=cancel)
    # Should have very few records (or none) since we cancelled immediately
    assert len(records) <= 1
