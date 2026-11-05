"""Executes staged deletions: move to Trash (default) or permanent delete.

Blocking; call it off the event loop. Byte counts come from the scan DB sizes
passed in by the caller, never from re-walking the disk. Trash failures are
reported as failures; there is no silent fallback to permanent deletion.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field

from backend.staging_manager import covered_by_staged_dir

logger = logging.getLogger(__name__)

try:
    import send2trash as _send2trash
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    _send2trash = None

MISSING_ERROR = "no longer exists on disk"


@dataclass
class DeletionResult:
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # also listed in `failed`
    bytes_freed: int = 0

    def to_dict(self) -> dict:
        return {
            "deleted": self.deleted,
            "failed": [{"path": p, "error": e} for p, e in self.failed],
            "bytes_freed": self.bytes_freed,
        }


def _deduplicate_paths(paths: list[str]) -> list[str]:
    """Drop paths that sit under another path in the list (deleting the ancestor covers them).

    Pure path logic, no disk access: only a directory can have descendants, so
    any listed ancestor is necessarily a directory.
    """
    unique = list(dict.fromkeys(p.rstrip("/") or "/" for p in paths))
    listed = set(unique)
    return [p for p in unique if not covered_by_staged_dir(p, listed)]


def _trash(path: str) -> None:
    if _send2trash is None:
        raise RuntimeError("send2trash is not installed; cannot move items to Trash")
    _send2trash.send2trash(path)


def _remove_permanently(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def execute_deletion(
    paths: list[str],
    permanent: bool,
    sizes: dict[str, int] | None = None,
) -> DeletionResult:
    """Delete *paths* (trash unless *permanent*).

    *sizes* maps path -> bytes from the scan DB; ``bytes_freed`` sums it over
    the paths actually deleted. Paths already gone from disk are reported in
    ``failed`` (error ``MISSING_ERROR``) and in ``missing``.
    """
    sizes = sizes or {}
    result = DeletionResult()
    for path in _deduplicate_paths(paths):
        if not os.path.lexists(path):
            result.failed.append((path, MISSING_ERROR))
            result.missing.append(path)
            continue
        try:
            if permanent:
                _remove_permanently(path)
            else:
                _trash(path)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            result.failed.append((path, msg))
            logger.error("Failed to delete %s: %s", path, msg)
            continue
        result.deleted.append(path)
        result.bytes_freed += sizes.get(path, 0)
        logger.info("%s %s (%d bytes)", "Deleted" if permanent else "Trashed", path, sizes.get(path, 0))
    return result
