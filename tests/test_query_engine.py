"""Tests for backend.query_engine query builders."""

from backend.query_engine import (
    FilterParams,
    build_filter_query,
    build_count_query,
    get_top_n_query,
)


def test_build_filter_query_basic():
    """Basic filter with no specific criteria produces a valid query."""
    fp = FilterParams()
    sql, params = build_filter_query(fp)

    assert "WHERE" in sql
    assert "1 = 1" in sql


def test_build_filter_query_extensions():
    """Extension list produces an IN clause with correct placeholders."""
    fp = FilterParams(extensions=[".jpg", ".png"])
    sql, params = build_filter_query(fp)

    assert "extension IN (?, ?)" in sql
    assert ".jpg" in params
    assert ".png" in params


def test_build_filter_query_exclude():
    """exclude_extensions=True flips the clause to NOT IN."""
    fp = FilterParams(extensions=[".tmp"], exclude_extensions=True)
    sql, params = build_filter_query(fp)

    assert "NOT IN" in sql
    assert ".tmp" in params


def test_build_filter_query_size_range():
    """min_size and max_size both appear as clauses."""
    fp = FilterParams(min_size=100, max_size=999)
    sql, params = build_filter_query(fp)

    assert "size >= ?" in sql
    assert "size <= ?" in sql
    assert 100 in params
    assert 999 in params


def test_build_filter_query_name_contains():
    """name_contains produces a LIKE clause."""
    fp = FilterParams(name_contains="DS_Store")
    sql, params = build_filter_query(fp)

    assert "name LIKE ?" in sql
    assert "%DS_Store%" in params


def test_build_count_query():
    """build_count_query produces SELECT COUNT(*) with no LIMIT/OFFSET."""
    fp = FilterParams()
    sql, params = build_count_query(fp)

    assert "COUNT(*)" in sql
    assert "LIMIT" not in sql
    assert "OFFSET" not in sql


def test_get_top_n_query_files():
    """mode='files' sets is_dir = 0."""
    sql, params = get_top_n_query(n=10, mode="files")

    assert "is_dir = ?" in sql
    assert 0 in params


def test_get_top_n_query_dirs():
    """mode='dirs' sets is_dir = 1."""
    sql, params = get_top_n_query(n=5, mode="dirs")

    assert "is_dir = ?" in sql
    assert 1 in params


def test_pagination():
    """page=2, page_size=10 yields LIMIT 10 OFFSET 20."""
    fp = FilterParams(page=2, page_size=10)
    sql, params = build_filter_query(fp)

    assert "LIMIT ? OFFSET ?" in sql
    assert params[-2] == 10
    assert params[-1] == 20
