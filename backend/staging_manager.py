"""Manages the staging queue for files/directories to be deleted.

Backed by a ``staged_items`` table in the scan-session SQLite DB for
crash-safe persistence.  An in-memory dict provides fast access.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


@dataclass
class StagedItem:
    path: str
    name: str
    size: int
    is_dir: bool
    extension: str


@dataclass
class StagingState:
    items: list[StagedItem]
    total_size: int
    count_by_extension: dict[str, int]
    dir_count: int
    file_count: int

    def to_dict(self) -> dict:
        return {
            "items": [asdict(item) for item in self.items],
            "total_size": self.total_size,
            "count_by_extension": self.count_by_extension,
            "dir_count": self.dir_count,
            "file_count": self.file_count,
        }


class StagingManager:
    """Manages a staging queue backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._items: dict[str, StagedItem] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load staged items from the DB into memory."""
        try:
            rows = self._conn.execute("SELECT * FROM staged_items").fetchall()
            for row in rows:
                self._items[row["path"]] = StagedItem(
                    path=row["path"],
                    name=row["name"],
                    size=row["size"],
                    is_dir=bool(row["is_dir"]),
                    extension=row["extension"],
                )
        except sqlite3.OperationalError:
            # Table may not exist yet if schema hasn't been applied.
            pass

    def add_items(self, items: list[dict]) -> StagingState:
        """Add items to the staging queue. Deduplicates by path."""
        for item in items:
            path = item["path"]
            staged = StagedItem(
                path=path,
                name=item["name"],
                size=item["size"],
                is_dir=item["is_dir"],
                extension=item["extension"],
            )
            self._items[path] = staged
            self._conn.execute(
                "INSERT OR REPLACE INTO staged_items (path, name, size, is_dir, extension) "
                "VALUES (?, ?, ?, ?, ?)",
                (path, staged.name, staged.size, int(staged.is_dir), staged.extension),
            )
        self._conn.commit()
        return self.get_state()

    def remove_items(self, paths: list[str]) -> StagingState:
        """Remove items from the staging queue by path."""
        for path in paths:
            self._items.pop(path, None)
        if paths:
            placeholders = ", ".join("?" for _ in paths)
            self._conn.execute(
                f"DELETE FROM staged_items WHERE path IN ({placeholders})", paths
            )
            self._conn.commit()
        return self.get_state()

    def clear(self) -> None:
        """Clear all staged items."""
        self._items.clear()
        self._conn.execute("DELETE FROM staged_items")
        self._conn.commit()

    def get_state(self) -> StagingState:
        """Compute and return the current staging state with aggregates."""
        items = list(self._items.values())
        total_size = sum(item.size for item in items)

        count_by_extension: dict[str, int] = {}
        dir_count = 0
        file_count = 0

        for item in items:
            if item.is_dir:
                dir_count += 1
            else:
                file_count += 1
            count_by_extension[item.extension] = (
                count_by_extension.get(item.extension, 0) + 1
            )

        return StagingState(
            items=items,
            total_size=total_size,
            count_by_extension=count_by_extension,
            dir_count=dir_count,
            file_count=file_count,
        )

    def get_paths(self) -> list[str]:
        """Return all staged paths."""
        return list(self._items.keys())
