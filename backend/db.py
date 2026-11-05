"""SQLite data layer for per-scan-session databases.

One database file per scan. ``nodes`` holds one row per stored filesystem
object; directory rows carry rolled-up totals of their whole subtree
(``size`` = allocated bytes, ``apparent_size`` = logical bytes,
``file_count`` = descendant files), so sizes are never truncated by the
depth limit or skip-list: a ``collapsed`` directory is fully counted, its
descendants just aren't stored as rows.

Secondary indexes are built *after* the scanner's bulk load
(:func:`drop_indexes` / :func:`ensure_indexes`); :func:`init_db` builds them
lazily for any DB that has no scan running in it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

_PRAGMAS = """\
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 536870912;
PRAGMA cache_size = -65536;
"""

_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS scan_meta (
    scan_id     TEXT PRIMARY KEY,
    started_at  REAL,
    finished_at REAL,
    roots       TEXT,
    max_depth   INTEGER,
    status      TEXT    NOT NULL DEFAULT 'running',
    total_files INTEGER NOT NULL DEFAULT 0,
    total_dirs  INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    error_msg   TEXT
);

-- No inline UNIQUE on path: the unique index is built after the bulk load.
CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    extension     TEXT    NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    apparent_size INTEGER NOT NULL DEFAULT 0,
    file_count    INTEGER NOT NULL DEFAULT 0,
    mtime         REAL    NOT NULL,
    atime         REAL    NOT NULL,
    ctime         REAL    NOT NULL,
    birthtime     REAL,
    is_dir        INTEGER NOT NULL,
    depth         INTEGER NOT NULL,
    parent_path   TEXT,
    collapsed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS staged_items (
    path      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    is_dir    INTEGER NOT NULL,
    extension TEXT NOT NULL DEFAULT ''
);
"""

# name -> definition. Built after the bulk load, never during it.
_INDEXES = {
    "idx_nodes_path": "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_path ON nodes (path)",
    "idx_nodes_parent": "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes (parent_path, size DESC)",
    "idx_nodes_size": "CREATE INDEX IF NOT EXISTS idx_nodes_size ON nodes (size DESC)",
    "idx_nodes_ext": "CREATE INDEX IF NOT EXISTS idx_nodes_ext ON nodes (extension, size DESC)",
    "idx_nodes_mtime": "CREATE INDEX IF NOT EXISTS idx_nodes_mtime ON nodes (mtime)",
    "idx_nodes_atime": "CREATE INDEX IF NOT EXISTS idx_nodes_atime ON nodes (atime)",
}

# Indexes of the pre-overhaul schema (superseded by _INDEXES).
_LEGACY_INDEXES = (
    "idx_extension", "idx_size_desc", "idx_mtime", "idx_atime",
    "idx_parent", "idx_dir_size", "idx_ext_mtime",
)

NODE_COLUMNS = (
    "path", "name", "extension", "size", "apparent_size", "file_count",
    "mtime", "atime", "ctime", "birthtime", "is_dir", "depth", "parent_path",
    "collapsed",
)

INSERT_SQL = (
    f"INSERT INTO nodes ({', '.join(NODE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in NODE_COLUMNS)})"
)

_TREE_COLUMNS = "path, name, size, apparent_size, file_count, is_dir, collapsed"


# ---------------------------------------------------------------------------
# Open / migrate / indexes
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open and migrate) a scan database.

    Secondary indexes are ensured unless a scan is currently running in
    this DB (the scanner builds them itself once its bulk load is done).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRAGMAS)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _migrate(conn)
    running = conn.execute(
        "SELECT 1 FROM scan_meta WHERE status = 'running' LIMIT 1"
    ).fetchone()
    if not running:
        ensure_indexes(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a fresh or pre-overhaul DB up to SCHEMA_VERSION."""
    conn.executescript(_TABLES_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    with conn:
        if "apparent_size" not in cols:
            # Old `size` was the logical st_size; keep it as apparent size.
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN apparent_size INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE nodes SET apparent_size = size")
        if "file_count" not in cols:
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN file_count INTEGER NOT NULL DEFAULT 0"
            )
            _rollup_file_counts(conn)
        if "collapsed" not in cols:
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN collapsed INTEGER NOT NULL DEFAULT 0"
            )
            # Old skip-dirs / depth-limited dirs: directories without child rows.
            conn.execute(
                "UPDATE nodes SET collapsed = 1 WHERE is_dir = 1 AND NOT EXISTS "
                "(SELECT 1 FROM nodes c WHERE c.parent_path = nodes.path)"
            )
        for name in _LEGACY_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _rollup_file_counts(conn: sqlite3.Connection) -> None:
    """Fill ``file_count`` from the stored rows (old DBs only)."""
    counts: dict[str, int] = {}
    parent_of: dict[str, str | None] = {}
    files: list[str | None] = []
    for path, parent, is_dir in conn.execute(
        "SELECT path, parent_path, is_dir FROM nodes"
    ):
        if is_dir:
            counts.setdefault(path, 0)
            parent_of[path] = parent
        else:
            files.append(parent)
    for parent in files:
        if parent in counts:
            counts[parent] += 1
    for path in sorted(counts, key=lambda p: p.count("/"), reverse=True):
        parent = parent_of.get(path)
        if parent in counts:
            counts[parent] += counts[path]
    conn.execute("UPDATE nodes SET file_count = 1 WHERE is_dir = 0")
    conn.executemany(
        "UPDATE nodes SET file_count = ? WHERE path = ?",
        ((c, p) for p, c in counts.items()),
    )


def ensure_indexes(conn: sqlite3.Connection) -> None:
    """Create any missing secondary index (no-op when all exist)."""
    present = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    missing = [sql for name, sql in _INDEXES.items() if name not in present]
    if not missing:
        return
    # Measured on 1.2M rows: a small page cache + file-backed sorter with
    # worker threads builds all indexes ~35% faster than the defaults, at
    # ~40 MB instead of ~900 MB peak RSS.
    conn.executescript(
        f"PRAGMA threads = {min(4, os.cpu_count() or 1)};"
        "PRAGMA cache_size = -4096; PRAGMA temp_store = FILE; PRAGMA mmap_size = 0;"
    )
    try:
        with conn:
            for sql in missing:
                conn.execute(sql)
        conn.execute("PRAGMA optimize")
    finally:
        conn.executescript(_PRAGMAS)


def drop_indexes(conn: sqlite3.Connection) -> None:
    """Drop secondary indexes ahead of a bulk load."""
    with conn:
        for name in (*_INDEXES, *_LEGACY_INDEXES):
            conn.execute(f"DROP INDEX IF EXISTS {name}")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Bulk-insert node tuples ordered as :data:`NODE_COLUMNS` (no commit)."""
    conn.executemany(INSERT_SQL, rows)


def batch_insert(conn: sqlite3.Connection, records: list[dict]) -> None:
    """Insert node dicts (convenience for tests/tools; the scanner uses tuples).

    Missing new-style columns default sensibly: ``apparent_size`` = ``size``,
    ``file_count`` = 1 for files / 0 for dirs, ``collapsed`` = 0.
    """
    if not records:
        return
    rows = []
    for r in records:
        r = {
            "apparent_size": r.get("size", 0),
            "file_count": 0 if r.get("is_dir") else 1,
            "collapsed": 0,
            "birthtime": None,
            "parent_path": None,
            "extension": "",
            **r,
        }
        rows.append(tuple(r[c] for c in NODE_COLUMNS))
    sql = INSERT_SQL.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
    with conn:
        conn.executemany(sql, rows)


def remove_deleted(conn: sqlite3.Connection, paths: list[str]) -> None:
    """Delete *paths* and all their descendants; subtract their totals from
    every ancestor row (and from the scan_meta totals)."""
    wanted = {_norm(p) for p in paths if p}
    # Drop paths already covered by another deleted path (no double subtraction).
    top = [p for p in wanted if not any(a in wanted for a in _ancestors(p))]
    if not top:
        return
    deltas: dict[str, list[int]] = {}
    freed_bytes = freed_files = 0
    with conn:
        for p in top:
            row = conn.execute(
                "SELECT size, apparent_size, file_count FROM nodes WHERE path = ?", (p,)
            ).fetchone()
            if row is None:
                continue
            size, apparent, count = row[0], row[1], row[2]
            freed_bytes += size
            freed_files += count
            for a in _ancestors(p):
                d = deltas.setdefault(a, [0, 0, 0])
                d[0] += size
                d[1] += apparent
                d[2] += count
            lo, hi = _subtree_range(p)
            conn.execute(
                "DELETE FROM nodes WHERE path = ? OR (path >= ? AND path < ?)",
                (p, lo, hi),
            )
        conn.executemany(
            "UPDATE nodes SET size = MAX(0, size - ?), "
            "apparent_size = MAX(0, apparent_size - ?), "
            "file_count = MAX(0, file_count - ?) WHERE path = ?",
            ((d[0], d[1], d[2], a) for a, d in deltas.items()),
        )
        conn.execute(
            "UPDATE scan_meta SET total_bytes = MAX(0, total_bytes - ?), "
            "total_files = MAX(0, total_files - ?)",
            (freed_bytes, freed_files),
        )


def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


def _ancestors(path: str):
    """Yield every proper ancestor of an absolute *path*, nearest first."""
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return
        yield parent
        path = parent


def _subtree_range(path: str) -> tuple[str, str]:
    """Half-open [lo, hi) range of strict descendants ('0' follows '/')."""
    base = path.rstrip("/")
    return base + "/", base + "0"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_node(conn: sqlite3.Connection, path: str) -> dict | None:
    """Return the full row for *path*, or ``None``."""
    row = conn.execute("SELECT * FROM nodes WHERE path = ?", (_norm(path),)).fetchone()
    return dict(row) if row is not None else None


def _tree_node(row) -> dict:
    return {
        "name": row[1],
        "path": row[0],
        "size": row[2],
        "apparent_size": row[3],
        "file_count": row[4],
        "is_dir": bool(row[5]),
        "collapsed": bool(row[6]),
    }


def get_tree_data(
    conn: sqlite3.Connection,
    root: str,
    depth: int,
    max_children: int = 60,
) -> dict:
    """Nested dict of *root* and up to *depth* levels below it, for D3.

    Per directory only the ``max_children`` largest children are returned;
    the rest are folded into one synthetic ``{"name": "<N> smaller items",
    "path": None, "is_other": True, ...}`` child. Files and directories at
    the depth boundary carry no ``children`` key. ``{}`` if *root* is unknown.
    """
    row = conn.execute(
        f"SELECT {_TREE_COLUMNS} FROM nodes WHERE path = ?", (_norm(root),)
    ).fetchone()
    if row is None:
        return {}
    tree = _tree_node(row)
    limit = max_children if max_children and max_children > 0 else -1
    frontier = [tree]
    for _ in range(max(0, depth)):
        next_frontier = []
        for node in frontier:
            if not node["is_dir"] or node["collapsed"]:
                continue
            kids = [
                _tree_node(r)
                for r in conn.execute(
                    f"SELECT {_TREE_COLUMNS} FROM nodes WHERE parent_path = ? "
                    "ORDER BY size DESC LIMIT ?",
                    (node["path"], limit + 1 if limit > 0 else -1),
                )
            ]
            if not kids:
                continue
            if limit > 0 and len(kids) > limit:
                kids = kids[:limit]
                n, size, apparent, files = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0), "
                    "COALESCE(SUM(apparent_size), 0), COALESCE(SUM(file_count), 0) "
                    "FROM nodes WHERE parent_path = ?",
                    (node["path"],),
                ).fetchone()
                n -= limit
                kids.append({
                    "name": f"{n} smaller items",
                    "path": None,
                    "is_other": True,
                    "is_dir": False,
                    "size": size - sum(k["size"] for k in kids),
                    "apparent_size": apparent - sum(k["apparent_size"] for k in kids),
                    "file_count": files - sum(k["file_count"] for k in kids),
                    "count": n,
                })
            node["children"] = kids
            next_frontier.extend(k for k in kids if k["is_dir"])
        frontier = next_frontier
    return tree


def query_files(
    conn: sqlite3.Connection,
    sql: str,
    params: list,
) -> list[dict]:
    """Execute a parameterised query and return rows as dicts."""
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def count_query(
    conn: sqlite3.Connection,
    sql: str,
    params: list,
) -> int:
    """Wrap *sql* in a counting sub-query and return the total."""
    wrapped = f"SELECT COUNT(*) AS cnt FROM ({sql})"
    row = conn.execute(wrapped, params).fetchone()
    return row["cnt"]


# ---------------------------------------------------------------------------
# Staged-items helpers
# ---------------------------------------------------------------------------

def add_staged_items(conn: sqlite3.Connection, items: list[dict]) -> None:
    """INSERT OR REPLACE items into the staged_items table."""
    if not items:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO staged_items (path, name, size, is_dir, extension) "
        "VALUES (:path, :name, :size, :is_dir, :extension)",
        items,
    )
    conn.commit()


def remove_staged_items(conn: sqlite3.Connection, paths: list[str]) -> None:
    """Delete staged items by path."""
    if not paths:
        return
    placeholders = ", ".join("?" for _ in paths)
    conn.execute(f"DELETE FROM staged_items WHERE path IN ({placeholders})", paths)
    conn.commit()


def clear_staged_items(conn: sqlite3.Connection) -> None:
    """Remove all staged items."""
    conn.execute("DELETE FROM staged_items")
    conn.commit()


def get_staged_items(conn: sqlite3.Connection) -> list[dict]:
    """Return all staged items as a list of dicts."""
    rows = conn.execute("SELECT path, name, size, is_dir, extension FROM staged_items").fetchall()
    return [dict(row) for row in rows]


def vacuum_db(conn: sqlite3.Connection) -> None:
    """Reclaim space and optimise query plans."""
    conn.execute("VACUUM")
    conn.execute("PRAGMA optimize")
