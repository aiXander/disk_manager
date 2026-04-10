"""Filesystem scanner — BFS directory walker with async progress reporting.

Walks directory trees using iterative BFS with ``os.scandir()``, collects
per-entry metadata, and inserts results into SQLite via batch operations
exposed by :mod:`backend.db`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Iterator

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
_SENTINEL = object()  # signals the producer thread is done


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProgressEvent:
    """Lightweight snapshot of scan progress, yielded to the caller."""

    scan_id: str
    files_scanned: int
    dirs_scanned: int
    current_path: str
    bytes_seen: int
    status: str  # "running" | "complete" | "error"
    error: str | None = None


@dataclass
class FileRecord:
    """One filesystem node (file *or* directory) ready for DB insertion."""

    __slots__ = (
        "path",
        "name",
        "extension",
        "size",
        "mtime",
        "atime",
        "ctime",
        "birthtime",
        "is_dir",
        "depth",
        "parent_path",
    )

    path: str
    name: str
    extension: str
    size: int
    mtime: float
    atime: float
    ctime: float
    birthtime: float | None
    is_dir: int  # 0 or 1
    depth: int
    parent_path: str | None

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


# ---------------------------------------------------------------------------
# BFS walker (blocking — runs in a thread)
# ---------------------------------------------------------------------------


def _walk_directory(
    root: Path,
    max_depth: int,
    adaptive: bool,
    adaptive_threshold: int,
    skip_dir_names: set[str],
    cancel_event: threading.Event,
) -> Iterator[FileRecord]:
    """Iterative BFS over *root* using :func:`os.scandir`.

    Yields a :class:`FileRecord` for every node encountered.  Symlinks that
    point to directories are recorded as regular file entries (not followed)
    to avoid cycles.  Real-path tracking additionally guards against any
    remaining symlink loops.
    """

    root = root.resolve()
    root_str = str(root)

    # Track visited real-paths to avoid symlink cycles.
    visited: set[str] = set()

    # BFS queue: (path, depth, effective_max_depth)
    queue: deque[tuple[Path, int, int]] = deque()
    queue.append((root, 0, max_depth))

    while queue:
        if cancel_event.is_set():
            return

        current_path, depth, eff_max_depth = queue.popleft()

        real = os.path.realpath(current_path)
        if real in visited:
            continue
        visited.add(real)

        # Stat the directory itself.
        try:
            st = current_path.stat()
        except OSError as exc:
            logger.debug("Skipping inaccessible directory %s: %s", current_path, exc)
            continue

        parent_str: str | None = (
            str(current_path.parent) if depth > 0 else None
        )

        yield FileRecord(
            path=str(current_path),
            name=current_path.name or root_str,
            extension="",
            size=st.st_size,
            mtime=st.st_mtime,
            atime=st.st_atime,
            ctime=st.st_ctime,
            birthtime=getattr(st, "st_birthtime", None),
            is_dir=1,
            depth=depth,
            parent_path=parent_str,
        )

        # If we've reached the effective depth limit, don't descend.
        if depth >= eff_max_depth:
            continue

        # Scan children.
        try:
            scandir_it = os.scandir(current_path)
        except OSError as exc:
            logger.debug("Skipping inaccessible directory %s: %s", current_path, exc)
            continue

        entries: list = []
        try:
            for _entry in scandir_it:
                entries.append(_entry)
        except OSError as exc:
            logger.debug("Partial read of %s: %s", current_path, exc)
        finally:
            scandir_it.close()

        # Adaptive depth: decide child effective max-depth uniformly for all
        # children based on total entry count, BEFORE iterating entries.
        child_eff_max = eff_max_depth
        if adaptive and len(entries) > adaptive_threshold:
            child_eff_max = max(depth + 1, eff_max_depth - 1)

        for entry in entries:
            entry_path = Path(entry.path)
            try:
                entry_is_dir = entry.is_dir(follow_symlinks=False)
                entry_is_symlink = entry.is_symlink()
                e_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                logger.debug("Skipping inaccessible entry %s: %s", entry.path, exc)
                continue

            if entry_is_dir and not entry_is_symlink:
                if entry.name in skip_dir_names:
                    # Yield the skipped directory as a leaf node …
                    yield FileRecord(
                        path=str(entry_path),
                        name=entry.name,
                        extension="",
                        size=e_stat.st_size,
                        mtime=e_stat.st_mtime,
                        atime=e_stat.st_atime,
                        ctime=e_stat.st_ctime,
                        birthtime=getattr(e_stat, "st_birthtime", None),
                        is_dir=1,
                        depth=depth + 1,
                        parent_path=str(current_path),
                    )
                    # … but do NOT enqueue it for traversal.
                else:
                    queue.append((entry_path, depth + 1, child_eff_max))
            else:
                # Regular file or symlink-to-directory (treated as file).
                ext = entry_path.suffix.lower() if entry_path.suffix else ""
                yield FileRecord(
                    path=str(entry_path),
                    name=entry.name,
                    extension=ext,
                    size=e_stat.st_size,
                    mtime=e_stat.st_mtime,
                    atime=e_stat.st_atime,
                    ctime=e_stat.st_ctime,
                    birthtime=getattr(e_stat, "st_birthtime", None),
                    is_dir=0,
                    depth=depth + 1,
                    parent_path=str(current_path),
                )


# ---------------------------------------------------------------------------
# Async scan orchestrator
# ---------------------------------------------------------------------------


def _blocking_producer(
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    roots: list[Path],
    max_depth: int,
    adaptive: bool,
    adaptive_threshold: int,
    scan_id: str,
    skip_dir_names: set[str],
    cancel_event: threading.Event,
) -> None:
    """Run in a worker thread — walks all *roots* and pushes record batches
    onto an :class:`asyncio.Queue` so the async generator can consume them.
    """
    batch: list[dict] = []
    files_scanned = 0
    dirs_scanned = 0
    bytes_seen = 0
    current_path = ""

    try:
        for root in roots:
            for record in _walk_directory(
                root, max_depth, adaptive, adaptive_threshold,
                skip_dir_names, cancel_event,
            ):
                if record.is_dir:
                    dirs_scanned += 1
                else:
                    files_scanned += 1
                bytes_seen += record.size
                current_path = record.path

                batch.append(record.to_dict())

                if len(batch) >= BATCH_SIZE:
                    progress = ProgressEvent(
                        scan_id=scan_id,
                        files_scanned=files_scanned,
                        dirs_scanned=dirs_scanned,
                        current_path=current_path,
                        bytes_seen=bytes_seen,
                        status="running",
                    )
                    loop.call_soon_threadsafe(queue.put_nowait, (batch, progress))
                    batch = []

                    # Check cancellation after each batch.
                    if cancel_event.is_set():
                        cancelled = ProgressEvent(
                            scan_id=scan_id,
                            files_scanned=files_scanned,
                            dirs_scanned=dirs_scanned,
                            current_path=current_path,
                            bytes_seen=bytes_seen,
                            status="cancelled",
                        )
                        loop.call_soon_threadsafe(
                            queue.put_nowait, (None, cancelled)
                        )
                        return

        # Flush remaining records.
        if batch:
            progress = ProgressEvent(
                scan_id=scan_id,
                files_scanned=files_scanned,
                dirs_scanned=dirs_scanned,
                current_path=current_path,
                bytes_seen=bytes_seen,
                status="running",
            )
            loop.call_soon_threadsafe(queue.put_nowait, (batch, progress))

        # Signal completion with totals.
        final = ProgressEvent(
            scan_id=scan_id,
            files_scanned=files_scanned,
            dirs_scanned=dirs_scanned,
            current_path="",
            bytes_seen=bytes_seen,
            status="complete",
        )
        loop.call_soon_threadsafe(queue.put_nowait, (None, final))

    except Exception as exc:
        error_event = ProgressEvent(
            scan_id=scan_id,
            files_scanned=files_scanned,
            dirs_scanned=dirs_scanned,
            current_path=current_path,
            bytes_seen=bytes_seen,
            status="error",
            error=str(exc),
        )
        loop.call_soon_threadsafe(queue.put_nowait, (None, error_event))


async def scan_roots(
    scan_id: str,
    roots: list[Path],
    max_depth: int,
    adaptive: bool,
    adaptive_threshold: int,
    db_path: Path,
    skip_dir_names: list[str],
    cancel_event: threading.Event | None = None,
) -> AsyncGenerator[ProgressEvent, None]:
    """Scan *roots* in background threads and yield progress events.

    Records are inserted into the SQLite database at *db_path* in batches.
    After all roots have been scanned, directory sizes are rolled up and the
    ``scan_meta`` row is finalised.

    Pass a :class:`threading.Event` as *cancel_event* to allow external
    cancellation.  The event is also stored as an attribute on this coroutine
    so the API layer can retrieve and set it.
    """
    from backend.db import batch_insert, compute_directory_sizes, init_db

    if cancel_event is None:
        cancel_event = threading.Event()

    # Store on the function for external access.
    scan_roots.cancel_event = cancel_event  # type: ignore[attr-defined]

    conn = init_db(db_path)
    started_at = time.time()

    # Create the scan_meta row.
    conn.execute(
        "INSERT INTO scan_meta (scan_id, started_at, roots, max_depth, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (scan_id, started_at, ",".join(str(r) for r in roots), max_depth),
    )
    conn.commit()

    loop = asyncio.get_event_loop()
    record_queue: asyncio.Queue = asyncio.Queue()
    skip_set = set(skip_dir_names)

    pool = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1))
    loop.run_in_executor(
        pool,
        _blocking_producer,
        record_queue,
        loop,
        roots,
        max_depth,
        adaptive,
        adaptive_threshold,
        scan_id,
        skip_set,
        cancel_event,
    )

    final_event: ProgressEvent | None = None

    try:
        while True:
            batch, progress = await record_queue.get()

            if batch is not None:
                batch_insert(conn, batch)
                yield progress
            else:
                # No more batches — this is the terminal event.
                final_event = progress
                break

        # Post-scan work.
        if final_event and final_event.status == "complete":
            compute_directory_sizes(conn)
            finished_at = time.time()
            conn.execute(
                "UPDATE scan_meta SET status='complete', finished_at=?, "
                "total_files=?, total_dirs=?, total_bytes=? WHERE scan_id=?",
                (
                    finished_at,
                    final_event.files_scanned,
                    final_event.dirs_scanned,
                    final_event.bytes_seen,
                    scan_id,
                ),
            )
            conn.commit()
            yield final_event
        elif final_event and final_event.status == "cancelled":
            conn.execute(
                "UPDATE scan_meta SET status='cancelled', finished_at=? "
                "WHERE scan_id=?",
                (time.time(), scan_id),
            )
            conn.commit()
            yield final_event
        elif final_event:
            # Error path.
            conn.execute(
                "UPDATE scan_meta SET status='error', finished_at=?, error_msg=? "
                "WHERE scan_id=?",
                (time.time(), final_event.error, scan_id),
            )
            conn.commit()
            yield final_event

    except Exception as exc:
        logger.exception("Unhandled error during scan %s", scan_id)
        conn.execute(
            "UPDATE scan_meta SET status='error', finished_at=?, error_msg=? "
            "WHERE scan_id=?",
            (time.time(), str(exc), scan_id),
        )
        conn.commit()
        yield ProgressEvent(
            scan_id=scan_id,
            files_scanned=0,
            dirs_scanned=0,
            current_path="",
            bytes_seen=0,
            status="error",
            error=str(exc),
        )
    finally:
        pool.shutdown(wait=False)
        conn.close()
