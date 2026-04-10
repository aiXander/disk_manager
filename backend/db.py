"""SQLite data layer for per-scan-session databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 536870912;
PRAGMA cache_size = -65536;

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

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    extension   TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL,
    atime       REAL    NOT NULL,
    ctime       REAL    NOT NULL,
    birthtime   REAL,
    is_dir      INTEGER NOT NULL,
    depth       INTEGER NOT NULL,
    parent_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_extension  ON nodes (extension) WHERE is_dir = 0;
CREATE INDEX IF NOT EXISTS idx_size_desc  ON nodes (size DESC);
CREATE INDEX IF NOT EXISTS idx_mtime      ON nodes (mtime);
CREATE INDEX IF NOT EXISTS idx_atime      ON nodes (atime);
CREATE INDEX IF NOT EXISTS idx_parent     ON nodes (parent_path);
CREATE INDEX IF NOT EXISTS idx_dir_size   ON nodes (is_dir, size DESC);
CREATE INDEX IF NOT EXISTS idx_ext_mtime  ON nodes (extension, mtime);

CREATE TABLE IF NOT EXISTS staged_items (
    path      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    is_dir    INTEGER NOT NULL,
    extension TEXT NOT NULL DEFAULT ''
);
"""

_NODE_COLUMNS = (
    "path", "name", "extension", "size", "mtime", "atime", "ctime",
    "birthtime", "is_dir", "depth", "parent_path",
)

_INSERT_SQL = (
    f"INSERT OR REPLACE INTO nodes ({', '.join(_NODE_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _NODE_COLUMNS)})"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) a scan database and ensure the schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    return conn


def batch_insert(conn: sqlite3.Connection, records: list[dict]) -> None:
    """Bulk-insert node records inside a BEGIN IMMEDIATE transaction."""
    if not records:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(_INSERT_SQL, records)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_tree_data(
    conn: sqlite3.Connection,
    root: str,
    depth: int,
) -> dict:
    """Return a nested dict suitable for D3 hierarchy visualisation.

    Uses a recursive CTE to fetch descendants up to *depth* levels below
    the given *root* path, then assembles them into a tree in Python.
    """
    sql = """\
        WITH RECURSIVE subtree AS (
            SELECT path, name, size, is_dir, depth, parent_path
            FROM   nodes
            WHERE  path = ?
          UNION ALL
            SELECT n.path, n.name, n.size, n.is_dir, n.depth, n.parent_path
            FROM   nodes n
            JOIN   subtree s ON n.parent_path = s.path
            WHERE  n.depth <= (
                SELECT depth FROM nodes WHERE path = ?
            ) + ?
        )
        SELECT path, name, size, is_dir, parent_path
        FROM   subtree
    """
    rows = conn.execute(sql, (root, root, depth)).fetchall()

    if not rows:
        return {}

    node_map: dict[str, dict] = {}
    for row in rows:
        node_map[row["path"]] = {
            "name": row["name"],
            "path": row["path"],
            "size": row["size"],
            "is_dir": bool(row["is_dir"]),
            "children": [],
        }

    # Link children to parents.
    for row in rows:
        parent = row["parent_path"]
        if parent and parent in node_map:
            node_map[parent]["children"].append(node_map[row["path"]])

    # The root of the requested subtree.
    root_node = node_map.get(root, {})

    # Strip empty children lists from leaf files for cleaner output.
    def _prune(node: dict) -> dict:
        if not node.get("is_dir") or not node.get("children"):
            node.pop("children", None)
        else:
            node["children"] = [_prune(c) for c in node["children"]]
        return node

    return _prune(root_node)


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


def delete_records(
    conn: sqlite3.Connection,
    paths: list[str],
) -> None:
    """Delete nodes whose paths are in *paths*."""
    if not paths:
        return
    placeholders = ", ".join("?" for _ in paths)
    conn.execute(f"DELETE FROM nodes WHERE path IN ({placeholders})", paths)
    conn.commit()


def update_parent_sizes(
    conn: sqlite3.Connection,
    deleted_paths: list[str],
) -> None:
    """Recalculate directory sizes for parents affected by deletions.

    Walks up from each deleted path's parent to the root, recalculating
    sizes bottom-up.
    """
    if not deleted_paths:
        return

    # Collect all unique parent paths that need recalculation.
    affected: set[str] = set()
    for p in deleted_paths:
        parent = str(Path(p).parent)
        while parent and parent != parent:  # guard against infinite loop
            affected.add(parent)
            next_parent = str(Path(parent).parent)
            if next_parent == parent:
                break
            parent = next_parent

    if not affected:
        return

    # Recalculate from deepest to shallowest.
    sorted_parents = sorted(affected, key=lambda p: p.count("/"), reverse=True)

    for parent_path in sorted_parents:
        conn.execute(
            """\
            UPDATE nodes
            SET    size = (
                SELECT COALESCE(SUM(child.size), 0)
                FROM   nodes child
                WHERE  child.parent_path = nodes.path
            )
            WHERE is_dir = 1 AND path = ?
            """,
            (parent_path,),
        )
    conn.commit()


def compute_directory_sizes(conn: sqlite3.Connection) -> None:
    """Post-scan rollup: set each directory's size to the sum of its children.

    Fetches all nodes once and accumulates sizes in Python (O(n)),
    then batch-updates all directories.
    """
    rows = conn.execute(
        "SELECT path, parent_path, size, is_dir FROM nodes"
    ).fetchall()

    if not rows:
        return

    # Build a lookup of parent_path for each directory so we can
    # propagate totals upward without a second scan of all rows.
    dir_parent: dict[str, str | None] = {}
    dir_sizes: dict[str, int] = {}

    for row in rows:
        if row["is_dir"]:
            dir_sizes[row["path"]] = 0
            dir_parent[row["path"]] = row["parent_path"]

    # First pass: add every node's size to its immediate parent dir.
    for row in rows:
        parent = row["parent_path"]
        if parent is not None and parent in dir_sizes:
            dir_sizes[parent] += row["size"]

    # Second pass: propagate directory subtotals upward (deepest first).
    sorted_dirs = sorted(dir_sizes.keys(), key=lambda p: p.count("/"), reverse=True)
    for dir_path in sorted_dirs:
        parent = dir_parent.get(dir_path)
        if parent is not None and parent in dir_sizes:
            dir_sizes[parent] += dir_sizes[dir_path]

    # Batch update.
    updates = [(size, path) for path, size in dir_sizes.items()]
    conn.executemany("UPDATE nodes SET size = ? WHERE path = ? AND is_dir = 1", updates)
    conn.commit()


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
