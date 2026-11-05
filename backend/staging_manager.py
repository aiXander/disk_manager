"""Staging queue for files/directories to be deleted.

The ``staged_items`` table in the scan DB is the single source of truth (no
in-memory copy), so any connection/thread sees the same state. Sizes in the
returned state come from the live ``nodes`` rows, so they stay correct after
partial deletions shrink a staged directory.
"""

from __future__ import annotations

import os
import sqlite3

_CHUNK = 500  # stay well under SQLite's bound-parameter limit


def _chunks(values: list[str]):
    for i in range(0, len(values), _CHUNK):
        yield values[i:i + _CHUNK]


def covered_by_staged_dir(path: str, staged_dirs: set[str]) -> bool:
    """True if some strict ancestor of *path* is in *staged_dirs*."""
    parent = os.path.dirname(path)
    while parent and parent not in staged_dirs:
        nxt = os.path.dirname(parent)
        if nxt == parent:
            return False
        parent = nxt
    return bool(parent)


class StagingManager:
    """Thin, stateless wrapper around ``staged_items`` on one connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, paths: list[str]) -> dict:
        """Stage paths that exist in the scan data; unknown paths are ignored."""
        with self._conn:
            for chunk in _chunks(list(dict.fromkeys(paths))):
                self._conn.execute(
                    "INSERT OR REPLACE INTO staged_items (path, name, size, is_dir, extension) "
                    "SELECT path, name, size, is_dir, extension FROM nodes "
                    f"WHERE path IN ({', '.join('?' for _ in chunk)})",
                    chunk,
                )
        return self.state()

    def remove(self, paths: list[str]) -> dict:
        with self._conn:
            for chunk in _chunks(list(paths)):
                self._conn.execute(
                    f"DELETE FROM staged_items WHERE path IN ({', '.join('?' for _ in chunk)})",
                    chunk,
                )
        return self.state()

    def remove_subtrees(self, roots: list[str]) -> None:
        """Unstage each root and everything staged beneath it."""
        with self._conn:
            for root in roots:
                base = root.rstrip("/")
                self._conn.execute(
                    "DELETE FROM staged_items WHERE path = ? OR (path >= ? AND path < ?)",
                    (root, base + "/", base + "0"),
                )

    def clear(self) -> dict:
        with self._conn:
            self._conn.execute("DELETE FROM staged_items")
        return self.state()

    def items(self) -> list[dict]:
        """Staged items, with name/size refreshed from ``nodes`` when present."""
        rows = self._conn.execute(
            "SELECT s.path, COALESCE(n.name, s.name) AS name, COALESCE(n.size, s.size) AS size, "
            "COALESCE(n.is_dir, s.is_dir) AS is_dir, COALESCE(n.extension, s.extension) AS extension "
            "FROM staged_items s LEFT JOIN nodes n ON n.path = s.path ORDER BY size DESC"
        ).fetchall()
        return [
            {
                "path": r["path"],
                "name": r["name"],
                "size": r["size"],
                "is_dir": bool(r["is_dir"]),
                "extension": r["extension"] or "",
            }
            for r in rows
        ]

    def state(self) -> dict:
        """``{items, total_size, item_count, dir_count, file_count}``.

        An item staged inside an already-staged directory is listed (with
        ``covered: true``) but not added to ``total_size`` again.
        """
        items = self.items()
        staged_dirs = {i["path"] for i in items if i["is_dir"]}
        total_size = dir_count = file_count = 0
        for item in items:
            item["covered"] = covered_by_staged_dir(item["path"], staged_dirs)
            if not item["covered"]:
                total_size += item["size"]
            if item["is_dir"]:
                dir_count += 1
            else:
                file_count += 1
        return {
            "items": items,
            "total_size": total_size,
            "item_count": len(items),
            "dir_count": dir_count,
            "file_count": file_count,
        }
