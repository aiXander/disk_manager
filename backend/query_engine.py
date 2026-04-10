"""Filter & query orchestrator for the disk management tool.

Translates frontend filter parameters into optimized parameterized SQL queries
against the ``nodes`` table.
"""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FilterParams(BaseModel):
    extensions: list[str] = []  # lowercase with dot, e.g. [".jpg", ".mp4"]
    exclude_extensions: bool = False
    min_size: int | None = None
    max_size: int | None = None
    mtime_after: float | None = None   # unix timestamp
    mtime_before: float | None = None
    atime_after: float | None = None
    is_dir: bool | None = None         # None = both
    name_contains: str | None = None   # substring match on name
    sort: str = "size_desc"
    page: int = 0
    page_size: int = 100


class PaginatedResult(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Sort mapping (values are never user-interpolated — prevents SQL injection)
# ---------------------------------------------------------------------------

SORT_MAP: dict[str, str] = {
    "size_desc": "size DESC",
    "size_asc": "size ASC",
    "mtime_desc": "mtime DESC",
    "mtime_asc": "mtime ASC",
    "name_asc": "name ASC",
    "name_desc": "name DESC",
}

_SELECT_COLUMNS = (
    "path, name, extension, size, mtime, atime, ctime, birthtime, is_dir, depth"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_where(filters: FilterParams) -> tuple[str, list]:
    """Return a WHERE clause string (without the WHERE keyword) and params."""
    clauses: list[str] = ["1 = 1"]
    params: list = []

    # Extension filter
    if filters.extensions:
        placeholders = ", ".join("?" for _ in filters.extensions)
        op = "NOT IN" if filters.exclude_extensions else "IN"
        clauses.append(f"extension {op} ({placeholders})")
        params.extend(filters.extensions)

    # Size range
    if filters.min_size is not None:
        clauses.append("size >= ?")
        params.append(filters.min_size)
    if filters.max_size is not None:
        clauses.append("size <= ?")
        params.append(filters.max_size)

    # mtime range
    if filters.mtime_after is not None:
        clauses.append("mtime >= ?")
        params.append(filters.mtime_after)
    if filters.mtime_before is not None:
        clauses.append("mtime <= ?")
        params.append(filters.mtime_before)

    # atime range
    if filters.atime_after is not None:
        clauses.append("atime >= ?")
        params.append(filters.atime_after)

    # is_dir
    if filters.is_dir is not None:
        clauses.append("is_dir = ?")
        params.append(int(filters.is_dir))

    # Name substring search
    if filters.name_contains:
        clauses.append("name LIKE ?")
        params.append(f"%{filters.name_contains}%")

    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Public query builders
# ---------------------------------------------------------------------------

def build_filter_query(filters: FilterParams) -> tuple[str, list]:
    """Build a full SELECT query with filtering, sorting, and pagination.

    Returns:
        (sql, params) — a parameterized query string and its bind values.
    """
    where_clause, params = _build_where(filters)

    order_by = SORT_MAP.get(filters.sort, "size DESC")

    offset = filters.page * filters.page_size

    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM nodes"
        f" WHERE {where_clause}"
        f" ORDER BY {order_by}"
        f" LIMIT ? OFFSET ?"
    )
    params.append(filters.page_size)
    params.append(offset)

    return sql, params


def build_count_query(filters: FilterParams) -> tuple[str, list]:
    """Build a COUNT(*) query using the same WHERE clause as *build_filter_query*.

    No ORDER BY, LIMIT, or OFFSET is included.
    """
    where_clause, params = _build_where(filters)

    sql = f"SELECT COUNT(*) FROM nodes WHERE {where_clause}"

    return sql, params


def get_top_n_query(
    n: int,
    mode: str,
    sort: str = "size",
) -> tuple[str, list]:
    """Return a query for the top *n* files or directories.

    Args:
        n: Number of rows to return.
        mode: ``"files"`` or ``"dirs"``.
        sort: ``"size"`` (largest first) or ``"mtime"`` (oldest first).
    """
    is_dir_val = 1 if mode == "dirs" else 0

    if sort == "mtime":
        order_by = "mtime ASC"
    else:
        order_by = "size DESC"

    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM nodes"
        f" WHERE is_dir = ?"
        f" ORDER BY {order_by}"
        f" LIMIT ?"
    )
    params = [is_dir_val, n]

    return sql, params
