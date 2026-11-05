"""Tests for deletion_engine. Only ever touches files under tmp_path, permanent mode only."""

from backend.deletion_engine import MISSING_ERROR, DeletionResult, _deduplicate_paths, execute_deletion


def test_deletion_result_to_dict():
    d = DeletionResult(deleted=["/a/b"], failed=[("/a/c", "PermissionError")], bytes_freed=1).to_dict()
    assert d == {"deleted": ["/a/b"], "failed": [{"path": "/a/c", "error": "PermissionError"}], "bytes_freed": 1}


def test_deduplicate_drops_descendants_without_disk_access():
    paths = ["/x/a/b/c.txt", "/x/a", "/x/ab.txt", "/x/a"]
    assert _deduplicate_paths(paths) == ["/x/a", "/x/ab.txt"]


def test_permanent_delete_counts_db_sizes(tmp_path):
    f1, d = tmp_path / "f1.txt", tmp_path / "dir"
    f1.write_text("data")
    d.mkdir()
    (d / "inner.txt").write_text("inner")
    paths = [str(f1), str(d), str(d / "inner.txt")]
    result = execute_deletion(paths, permanent=True, sizes={str(f1): 4096, str(d): 8192})
    assert sorted(result.deleted) == sorted([str(f1), str(d)])
    assert result.bytes_freed == 4096 + 8192
    assert not f1.exists() and not d.exists()
    assert result.failed == []


def test_missing_path_reported(tmp_path):
    fake = str(tmp_path / "gone.txt")
    result = execute_deletion([fake], permanent=True, sizes={fake: 100})
    assert result.deleted == [] and result.bytes_freed == 0
    assert result.failed == [(fake, MISSING_ERROR)]
    assert result.missing == [fake]


def test_symlink_to_dir_removes_link_only(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target)
    result = execute_deletion([str(link)], permanent=True)
    assert result.deleted == [str(link)]
    assert (target / "keep.txt").exists()


def test_trash_failure_never_falls_back_to_permanent(tmp_path, monkeypatch):
    import backend.deletion_engine as de

    def boom(path):
        raise OSError("trash unavailable")

    monkeypatch.setattr(de, "_trash", boom)
    f = tmp_path / "f.txt"
    f.write_text("x")
    result = execute_deletion([str(f)], permanent=False)
    assert f.exists()
    assert result.deleted == [] and "trash unavailable" in result.failed[0][1]


def test_empty_list():
    assert execute_deletion([], permanent=True).to_dict() == {"deleted": [], "failed": [], "bytes_freed": 0}
