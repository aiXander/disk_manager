"""Filter & query builder for the ``nodes`` table.

Translates ``/files`` and ``/summary`` parameters into parameterized SQL. Sort
keys map through a fixed whitelist and every value is bound, so nothing the
client sends is interpolated into SQL.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from backend.categories import CATEGORY_EXTENSIONS, EXT_TO_CATEGORY, category_of, extensions_for


class FilterParams(BaseModel):
    under: str | None = None             # restrict to the subtree below this path
    categories: list[str] = []           # category names from backend.categories ("other" = unknown ext)
    extensions: list[str] = []           # ".jpg" / "jpg", case-insensitive
    exclude_extensions: bool = False     # negates `extensions` (not `categories`)
    min_size: int | None = None
    max_size: int | None = None
    mtime_after: float | None = None     # unix seconds
    mtime_before: float | None = None
    atime_after: float | None = None
    atime_before: float | None = None
    is_dir: bool | None = None           # None = both
    name_contains: str | None = None
    sort: str = "size_desc"
    page: int = 0
    page_size: int = 100


SORT_MAP: dict[str, str] = {
    "size_desc": "size DESC",
    "size_asc": "size ASC",
    "mtime_desc": "mtime DESC",
    "mtime_asc": "mtime ASC",
    "name_asc": "name ASC",
    "name_desc": "name DESC",
    "atime_asc": "atime ASC",
}

_SELECT_COLUMNS = (
    "path, name, extension, size, apparent_size, file_count, mtime, atime, "
    "birthtime, is_dir, collapsed"
)

_ALL_KNOWN_EXTENSIONS = sorted(EXT_TO_CATEGORY)


def normalize_extension(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if not ext or ext.startswith(".") else "." + ext


def subtree_range(under: str) -> tuple[str, str]:
    """Half-open [lo, hi) path range covering every strict descendant of *under*.

    Uses the path index: '0' is the byte right after '/', so ``prefix/...`` sorts
    inside [``prefix/``, ``prefix0``).
    """
    base = under.rstrip("/")
    return base + "/", base + "0"


def _placeholders(values: list) -> str:
    return ", ".join("?" for _ in values)


def _build_where(f: FilterParams) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    if f.under:
        lo, hi = subtree_range(f.under)
        clauses.append("path >= ? AND path < ?")
        params += [lo, hi]

    cats = [c for c in f.categories if c]
    if cats:
        cat_exts = extensions_for([c for c in cats if c != "other"])
        parts: list[str] = []
        if cat_exts:
            parts.append(f"extension IN ({_placeholders(cat_exts)})")
            params += cat_exts
        if "other" in cats:
            parts.append(f"extension NOT IN ({_placeholders(_ALL_KNOWN_EXTENSIONS)})")
            params += _ALL_KNOWN_EXTENSIONS
        clauses.append("is_dir = 0")
        clauses.append("(" + " OR ".join(parts) + ")" if parts else "0")

    exts = [e for e in (normalize_extension(x) for x in f.extensions) if e]
    if exts:
        op = "NOT IN" if f.exclude_extensions else "IN"
        clauses.append(f"extension {op} ({_placeholders(exts)})")
        params += exts

    for column, op, value in (
        ("size", ">=", f.min_size),
        ("size", "<=", f.max_size),
        ("mtime", ">=", f.mtime_after),
        ("mtime", "<=", f.mtime_before),
        ("atime", ">=", f.atime_after),
        ("atime", "<=", f.atime_before),
    ):
        if value is not None:
            clauses.append(f"{column} {op} ?")
            params.append(value)

    if f.is_dir is not None:
        clauses.append("is_dir = ?")
        params.append(int(f.is_dir))

    if f.name_contains:
        escaped = f.name_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    return (" AND ".join(clauses) or "1 = 1"), params


def build_filter_query(f: FilterParams) -> tuple[str, list]:
    """SELECT with filtering, sorting and pagination."""
    where, params = _build_where(f)
    order_by = SORT_MAP.get(f.sort, "size DESC")
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM nodes WHERE {where}"
        f" ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    return sql, params + [f.page_size, f.page * f.page_size]


def build_count_query(f: FilterParams) -> tuple[str, list]:
    """COUNT(*) and SUM(size) over the same filtered set (no paging)."""
    where, params = _build_where(f)
    return f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM nodes WHERE {where}", params


def row_to_item(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item["is_dir"] = bool(item["is_dir"])
    item["collapsed"] = bool(item.get("collapsed") or 0)
    item["category"] = None if item["is_dir"] else category_of(item.get("extension") or "")
    return item


def query_files(conn: sqlite3.Connection, f: FilterParams) -> dict:
    """Run the paged query; returns the ``/files`` response body."""
    sql, params = build_filter_query(f)
    items = [row_to_item(r) for r in conn.execute(sql, params).fetchall()]
    count_sql, count_params = build_count_query(f)
    total, total_size = conn.execute(count_sql, count_params).fetchone()
    return {
        "items": items,
        "total": total,
        "page": f.page,
        "page_size": f.page_size,
        "total_size": total_size,
    }


def summarize(conn: sqlite3.Connection, under: str | None = None, top_n_extensions: int = 15) -> dict:
    """Per-category and per-extension totals over *files* (optionally a subtree)."""
    sql = "SELECT extension, COALESCE(SUM(size), 0), COUNT(*) FROM nodes WHERE is_dir = 0"
    params: list = []
    if under:
        lo, hi = subtree_range(under)
        sql += " AND path >= ? AND path < ?"
        params += [lo, hi]
    sql += " GROUP BY extension"

    by_ext: list[dict] = []
    by_cat: dict[str, dict] = {}
    total_size = total_files = 0
    for ext, size, count in conn.execute(sql, params):
        ext = ext or ""
        by_ext.append({"extension": ext, "size": size, "count": count})
        cat = category_of(ext)
        bucket = by_cat.setdefault(cat, {"category": cat, "size": 0, "count": 0})
        bucket["size"] += size
        bucket["count"] += count
        total_size += size
        total_files += count

    # Collapsed dirs (skip-list / depth limit) hold bytes that no file row
    # carries; report them separately so the UI total matches the folder size.
    # They never nest (a collapsed dir has no stored descendants).
    csql = (
        "SELECT COALESCE(SUM(size), 0), COUNT(*), COALESCE(SUM(file_count), 0) "
        "FROM nodes WHERE is_dir = 1 AND collapsed = 1"
    )
    if under:
        csql += " AND path >= ? AND path < ?"
    c_size, c_dirs, c_files = conn.execute(csql, params).fetchone()

    by_ext.sort(key=lambda e: e["size"], reverse=True)
    order = list(CATEGORY_EXTENSIONS) + ["other"]
    return {
        "total_size": total_size,
        "total_files": total_files,
        "collapsed": {"size": c_size, "dirs": c_dirs, "files": c_files},
        "by_category": sorted(by_cat.values(), key=lambda c: (-c["size"], order.index(c["category"]))),
        "top_extensions": by_ext[:top_n_extensions],
    }
