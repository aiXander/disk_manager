"""Executes staged deletions using send2trash (Trash) or permanent delete."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import send2trash as _send2trash
except ImportError:
    _send2trash = None
    logger.warning(
        "send2trash is not installed. Trash-based deletion will not be available. "
        "Install it with: pip install send2trash"
    )


@dataclass
class DeletionResult:
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes_freed: int = 0

    def to_dict(self) -> dict:
        return {
            "deleted": self.deleted,
            "failed": [{"path": p, "error": e} for p, e in self.failed],
            "bytes_freed": self.bytes_freed,
        }


def _deduplicate_paths(paths: list[str]) -> list[str]:
    """Remove child paths whose parent directory is already in the list.

    If a parent directory is staged for deletion, its children will be deleted
    along with it, so we skip them to avoid errors or double-counting.
    """
    # Sort by depth (shallowest first) so parents come before children.
    sorted_paths = sorted(paths, key=lambda p: p.count(os.sep))

    kept: list[str] = []
    kept_dirs: set[str] = set()

    for path in sorted_paths:
        # Check if any already-kept directory is a parent of this path.
        parent = Path(path).parent
        skip = False
        while str(parent) != parent.anchor:
            if str(parent) in kept_dirs:
                skip = True
                break
            parent = parent.parent

        if not skip:
            kept.append(path)
            if Path(path).is_dir():
                kept_dirs.add(path)

    return kept


def _get_size(path: str) -> int:
    """Get size of a file or directory. Returns 0 on error."""
    try:
        p = Path(path)
        if p.is_file():
            return p.stat().st_size
        elif p.is_dir():
            total = 0
            for entry in p.rglob("*"):
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
            return total
    except OSError:
        pass
    return 0


def _trash_item(path: str) -> None:
    """Move a single item to the system Trash via send2trash."""
    if _send2trash is None:
        raise RuntimeError(
            "send2trash is not installed. Cannot move items to Trash. "
            "Install it with: pip install send2trash"
        )
    _send2trash.send2trash(path)


def execute_deletion(paths: list[str], permanent: bool) -> DeletionResult:
    """Execute deletion for the given paths.

    Args:
        paths: List of absolute file/directory paths to delete.
        permanent: If True, permanently delete. If False, move to Trash.

    Returns:
        DeletionResult with lists of deleted/failed paths and bytes freed.
    """
    result = DeletionResult()
    deduped = _deduplicate_paths(paths)

    # Separate into directories and files.
    dirs: list[str] = []
    files: list[str] = []
    for p in deduped:
        if Path(p).is_dir():
            dirs.append(p)
        else:
            files.append(p)

    # Sort directories deepest-first so children are removed before parents.
    dirs.sort(key=lambda p: p.count(os.sep), reverse=True)

    # Process files first, then directories (deepest first).
    for path in files + dirs:
        size = _get_size(path)
        try:
            if permanent:
                if Path(path).is_file():
                    os.remove(path)
                else:
                    shutil.rmtree(path)
            else:
                _trash_item(path)

            result.deleted.append(path)
            result.bytes_freed += size
            logger.info("Deleted: %s (%d bytes)", path, size)

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            result.failed.append((path, error_msg))
            logger.error("Failed to delete %s: %s", path, error_msg)

    return result
