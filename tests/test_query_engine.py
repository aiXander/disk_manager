"""Tests for backend.query_engine (SQL builders + query execution)."""

import sqlite3

import pytest

from backend.query_engine import (
    FilterParams,
    build_count_query,
    build_filter_query,
    query_files,
    subtree_range,
    summarize,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE nodes (path TEXT PRIMARY KEY, name TEXT, extension TEXT, size INTEGER,"
        " apparent_size INTEGER, file_count INTEGER, mtime REAL, atime REAL, ctime REAL,"
        " birthtime REAL, is_dir INTEGER, depth INTEGER, parent_path TEXT, collapsed INTEGER DEFAULT 0)"
    )
    rows = [
        ("/r", "r", "", 1600, 1, None, 0),
        ("/r/a", "a", "", 1100, 1, "/r", 1),
        ("/r/a/x.jpg", "x.jpg", ".jpg", 1000, 0, "/r/a", 2),
        ("/r/a/y.py", "y.py", ".py", 100, 0, "/r/a", 2),
        ("/r/ab.mp4", "ab.mp4", ".mp4", 400, 0, "/r", 1),
        ("/r/z.weird", "z.weird", ".weird", 100, 0, "/r", 1),
    ]
    c.executemany(
        "INSERT INTO nodes (path, name, extension, size, apparent_size, file_count, mtime, atime,"
        " ctime, birthtime, is_dir, depth, parent_path) VALUES (?, ?, ?, ?, ?, 1, 10, 10, 10, 10, ?, ?, ?)",
        [(p, n, e, s, s, d, par, dep) for p, n, e, s, d, par, dep in rows],
    )
    yield c
    c.close()


def _paths(result):
    return [i["path"] for i in result["items"]]


def test_subtree_range_excludes_sibling_with_shared_prefix():
    lo, hi = subtree_range("/r/a")
    assert lo <= "/r/a/x.jpg" < hi
    assert not (lo <= "/r/ab.mp4" < hi)


def test_under_filters_subtree_only(conn):
    res = query_files(conn, FilterParams(under="/r/a", is_dir=False))
    assert _paths(res) == ["/r/a/x.jpg", "/r/a/y.py"]
    assert res["total"] == 2
    assert res["total_size"] == 1100


def test_categories_filter_and_other(conn):
    res = query_files(conn, FilterParams(categories=["image", "video"]))
    assert set(_paths(res)) == {"/r/a/x.jpg", "/r/ab.mp4"}
    res = query_files(conn, FilterParams(categories=["other"]))
    assert _paths(res) == ["/r/z.weird"]


def test_extensions_normalized_and_excluded(conn):
    res = query_files(conn, FilterParams(extensions=["JPG"]))
    assert _paths(res) == ["/r/a/x.jpg"]
    res = query_files(conn, FilterParams(extensions=[".jpg"], exclude_extensions=True, is_dir=False))
    assert "/r/a/x.jpg" not in _paths(res)


def test_rows_have_category_and_bools(conn):
    item = query_files(conn, FilterParams(sort="size_desc"))["items"][0]
    assert item["path"] == "/r" and item["is_dir"] is True and item["category"] is None
    files = query_files(conn, FilterParams(is_dir=False))["items"]
    assert {i["category"] for i in files} == {"image", "code", "video", "other"}


def test_name_contains_escapes_wildcards(conn):
    assert query_files(conn, FilterParams(name_contains="%"))["total"] == 0
    assert _paths(query_files(conn, FilterParams(name_contains="ab"))) == ["/r/ab.mp4"]


def test_sort_whitelist_and_pagination():
    sql, params = build_filter_query(FilterParams(sort="size_desc; DROP TABLE nodes", page=2, page_size=10))
    assert "DROP" not in sql and "ORDER BY size DESC" in sql
    assert params[-2:] == [10, 20]
    count_sql, _ = build_count_query(FilterParams())
    assert "LIMIT" not in count_sql and "SUM(size)" in count_sql


def test_summary(conn):
    s = summarize(conn)
    assert s["total_files"] == 4 and s["total_size"] == 1600
    assert s["by_category"][0] == {"category": "image", "size": 1000, "count": 1}
    assert [e["extension"] for e in s["top_extensions"][:2]] == [".jpg", ".mp4"]
    sub = summarize(conn, under="/r/a")
    assert sub["total_files"] == 2 and sub["total_size"] == 1100


def test_summary_reports_collapsed_dir_bytes_separately(conn):
    # A collapsed dir (e.g. node_modules) has no file rows; its bytes must still
    # reach the UI, otherwise the category bar undercounts the folder.
    conn.execute(
        "INSERT INTO nodes (path, name, extension, size, apparent_size, file_count, mtime, atime,"
        " ctime, birthtime, is_dir, depth, parent_path, collapsed)"
        " VALUES ('/r/a/node_modules', 'node_modules', '', 5000, 4000, 42, 10, 10, 10, 10, 1, 2, '/r/a', 1)"
    )
    s = summarize(conn)
    assert s["collapsed"] == {"size": 5000, "dirs": 1, "files": 42}
    assert s["total_size"] == 1600  # files only, unchanged
    assert summarize(conn, under="/r/a")["collapsed"]["size"] == 5000
    assert summarize(conn, under="/r/ab")["collapsed"]["size"] == 0
