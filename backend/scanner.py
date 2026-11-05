"""Filesystem scanner: parallel directory walk with on-the-fly size rollup.

Design (measured on an M2 / APFS, see scripts/bench_scan.py):

* **Listing.** On macOS each directory is read with ``getattrlistbulk(2)``
  (via ctypes): one syscall returns name, type, times, inode, link count,
  logical and allocated size for a whole batch of entries, ~2x faster than
  ``scandir`` + one ``lstat`` per entry. Elsewhere (or if the call is not
  supported) it falls back to ``os.scandir``.
* **Parallelism.** The work is syscall-bound and those calls release the
  GIL, so a pool of worker threads lists directories concurrently; one
  coordinator thread hands out directories depth-first (bounded memory),
  rolls sizes up and feeds a writer thread that bulk-inserts tuples.
* **Rollup during the walk.** A directory row is emitted once its whole
  subtree is done, with final ``size``/``apparent_size``/``file_count``;
  there is no post-pass over all rows. Secondary indexes are built after
  the load ("finalizing" phase).
* **Correctness.** ``size`` is allocated bytes (``st_blocks * 512``),
  ``apparent_size`` the logical size; a hard-linked inode is counted once.
  Skip-dirs and dirs at the depth limit are walked fully but stored as one
  ``collapsed`` row. Directories are de-duplicated by ``(st_dev, st_ino)``
  (catches macOS firmlinks such as /Users vs /System/Volumes/Data/Users);
  other devices / mount points are recorded as an empty collapsed leaf.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import json
import logging
import os
import queue
import sqlite3
import stat
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Callable

from backend import db

logger = logging.getLogger(__name__)

WALK_THREADS = max(2, min(8, os.cpu_count() or 4))
FLUSH_ROWS = 20_000          # rows per writer batch
PROGRESS_INTERVAL = 0.1      # seconds between "running" events
_INFLIGHT_PER_THREAD = 4     # directories queued per worker

# Visited last when scanning "/", so firmlinked dirs (/Users, /Applications,
# ...) are attributed to their familiar path, not to the Data volume path;
# what is only reachable through the Data volume is still counted there.
_DEFERRED_DIRS = frozenset({"/System/Volumes/Data"})


@dataclass
class ProgressEvent:
    """Snapshot of scan progress, yielded to the caller."""

    scan_id: str
    files_scanned: int
    dirs_scanned: int
    current_path: str
    bytes_seen: int
    status: str  # "running" | "complete" | "cancelled" | "error"
    error: str | None = None
    phase: str = "walking"  # "walking" | "finalizing"
    elapsed_s: float = 0.0
    errors: int = 0  # unreadable directories / entries


# ---------------------------------------------------------------------------
# Directory listing backends (run in worker threads)
# ---------------------------------------------------------------------------
#
# Both backends share one signature:
#   list_dir(fd_or_none, path, prefix, child_depth, rows, subdirs, dedupe)
#     -> (alloc_sum, apparent_sum, n_files, n_entries, n_errors)
# appending file row tuples to *rows* (unless None) and
# (name, mtime, atime, ctime, birthtime, is_mount) to *subdirs*.
# *dedupe(ino, alloc)* returns the bytes to count for a hard-linked file.


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if 0 < dot < len(name) - 1 else ""


def _list_scandir(fd, path, prefix, child_depth, rows, subdirs, dedupe):
    alloc_sum = apparent_sum = n_files = n_entries = n_errors = 0
    parent_dev = os.stat(path).st_dev
    try:
        with os.scandir(path) as it:
            for entry in it:
                n_entries += 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    n_errors += 1
                    continue
                name = entry.name
                birth = getattr(st, "st_birthtime", None)
                if stat.S_ISDIR(st.st_mode):
                    subdirs.append((name, st.st_mtime, st.st_atime, st.st_ctime,
                                    birth, st.st_dev != parent_dev))
                    continue
                alloc = st.st_blocks * 512
                if st.st_nlink > 1:
                    alloc = dedupe(st.st_ino, alloc)
                alloc_sum += alloc
                apparent_sum += st.st_size
                n_files += 1
                if rows is not None:
                    rows.append((prefix + name, name, _ext(name), alloc, st.st_size, 1,
                                 st.st_mtime, st.st_atime, st.st_ctime, birth,
                                 0, child_depth, path, 0))
    except OSError:
        n_errors += 1
    return alloc_sum, apparent_sum, n_files, n_entries, n_errors


_list_bulk = None  # set below on macOS

if sys.platform == "darwin":  # pragma: no branch
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _getattrlistbulk = _libc.getattrlistbulk
    except (OSError, AttributeError):  # pragma: no cover
        _getattrlistbulk = None

    if _getattrlistbulk is not None:
        class _AttrList(ctypes.Structure):
            _fields_ = [
                ("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32),
            ]

        _A_NAME, _A_OBJTYPE = 0x1, 0x8
        _A_CRTIME, _A_MODTIME, _A_CHGTIME, _A_ACCTIME = 0x200, 0x400, 0x800, 0x1000
        _A_FILEID, _A_ERROR, _A_RETURNED = 0x02000000, 0x20000000, 0x80000000
        _A_DIR_MOUNTSTATUS = 0x4
        _A_FILE_LINKCOUNT, _A_FILE_TOTALSIZE, _A_FILE_ALLOCSIZE = 0x1, 0x2, 0x4
        _VDIR = 2
        _MNT_POINT = 0x1

        _CMN = (_A_RETURNED | _A_NAME | _A_OBJTYPE | _A_CRTIME | _A_MODTIME
                | _A_CHGTIME | _A_ACCTIME | _A_FILEID | _A_ERROR)
        _FILEATTRS = _A_FILE_LINKCOUNT | _A_FILE_TOTALSIZE | _A_FILE_ALLOCSIZE
        _ATTRLIST = _AttrList(5, 0, _CMN, 0, _A_DIR_MOUNTSTATUS, _FILEATTRS, 0)
        _ATTRLIST_REF = ctypes.byref(_ATTRLIST)
        _getattrlistbulk.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_uint64]
        _getattrlistbulk.restype = ctypes.c_int

        _BUFSIZE = 64 * 1024
        # Entry layout: u32 length, attribute_set_t (5 x u32) returned, then
        # the attributes in bit order, packed. ERROR (when returned) comes
        # right after the returned set; the fast paths below cover entries
        # without error, i.e. the common file and directory layouts.
        _CMN_OK = _CMN & ~_A_ERROR
        _RET = struct.Struct("<5I")
        _FILE_RET = (_CMN_OK, 0, 0, _FILEATTRS, 0)
        _DIR_RET = (_CMN_OK, 0, _A_DIR_MOUNTSTATUS, 0, 0)
        # length, ret x5, name(off,len), objtype, 4 x timespec, fileid, ...
        _FILE = struct.Struct("<I5IiIIqqqqqqqqQIqq")   # + linkcount, size, alloc
        _DIR = struct.Struct("<I5IiIIqqqqqqqqQI")      # + mountstatus
        _U32 = struct.Struct("<I")
        _tls = threading.local()

        def _bulk_entries(fd, path):
            """Yield (memoryview, offset) per entry, refilling the buffer."""
            buf = getattr(_tls, "buf", None)
            if buf is None:
                buf = _tls.buf = ctypes.create_string_buffer(_BUFSIZE)
                _tls.mv = memoryview(buf).cast("B")
            mv = _tls.mv
            while True:
                n = _getattrlistbulk(fd, _ATTRLIST_REF, buf, _BUFSIZE, 0)
                if n < 0:
                    e = ctypes.get_errno()
                    raise OSError(e, os.strerror(e), path)
                if n == 0:
                    return
                off = 0
                for _ in range(n):
                    yield mv, off
                    off += _U32.unpack_from(mv, off)[0]

        def _list_bulk(fd, path, prefix, child_depth, rows, subdirs, dedupe):  # noqa: F811
            alloc_sum = apparent_sum = n_files = n_entries = n_errors = 0
            try:
                for mv, off in _bulk_entries(fd, path):
                    n_entries += 1
                    ret = _RET.unpack_from(mv, off + 4)
                    if ret == _FILE_RET:
                        (_, _, _, _, _, _, noff, nlen, _typ, cs, cn, ms, mn, chs, chn,
                         as_, an, ino, nlink, size, alloc) = _FILE.unpack_from(mv, off)
                    elif ret == _DIR_RET:
                        (_, _, _, _, _, _, noff, nlen, _typ, cs, cn, ms, mn, chs, chn,
                         as_, an, _ino, mnt) = _DIR.unpack_from(mv, off)
                        start = off + 24 + noff
                        name = str(mv[start:start + nlen - 1], "utf-8", "surrogateescape")
                        subdirs.append((name, ms + mn * 1e-9, as_ + an * 1e-9,
                                        chs + chn * 1e-9, cs + cn * 1e-9,
                                        bool(mnt & _MNT_POINT)))
                        continue
                    else:
                        parsed = _parse_generic(mv, off, ret)
                        if parsed is None:
                            n_errors += 1
                            continue
                        (noff, nlen, typ, cs, cn, ms, mn, chs, chn, as_, an, ino,
                         mnt, nlink, size, alloc) = parsed
                        if typ == _VDIR:
                            start = off + 24 + noff
                            name = str(mv[start:start + nlen - 1], "utf-8", "surrogateescape")
                            subdirs.append((name, ms + mn * 1e-9, as_ + an * 1e-9,
                                            chs + chn * 1e-9, cs + cn * 1e-9,
                                            bool(mnt & _MNT_POINT)))
                            continue
                    if nlink > 1:
                        alloc = dedupe(ino, alloc)
                    alloc_sum += alloc
                    apparent_sum += size
                    n_files += 1
                    if rows is not None:
                        start = off + 24 + noff
                        name = str(mv[start:start + nlen - 1], "utf-8", "surrogateescape")
                        rows.append((prefix + name, name, _ext(name), alloc, size, 1,
                                     ms + mn * 1e-9, as_ + an * 1e-9, chs + chn * 1e-9,
                                     cs + cn * 1e-9, 0, child_depth, path, 0))
            except OSError as exc:
                if exc.errno in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS) and not n_entries:
                    return _list_scandir(None, path, prefix, child_depth, rows,
                                         subdirs, dedupe)
                n_errors += 1
            return alloc_sum, apparent_sum, n_files, n_entries, n_errors

        def _parse_generic(mv, off, ret):
            """Slow path for any other attribute layout; None on entry error."""
            cmn, _vol, dira, filea, _fork = ret
            p = off + 24
            if cmn & _A_ERROR:
                if _U32.unpack_from(mv, p)[0]:
                    return None
                p += 4
            noff = nlen = 0
            if cmn & _A_NAME:
                noff, nlen = struct.unpack_from("<iI", mv, p)
                noff += p - (off + 24)  # make relative to off + 24 like the fast paths
                p += 8
            else:
                return None
            typ = 0
            if cmn & _A_OBJTYPE:
                typ = _U32.unpack_from(mv, p)[0]
                p += 4
            times = []
            for bit in (_A_CRTIME, _A_MODTIME, _A_CHGTIME, _A_ACCTIME):
                if cmn & bit:
                    times.extend(struct.unpack_from("<qq", mv, p))
                    p += 16
                else:
                    times.extend((0, 0))
            ino = 0
            if cmn & _A_FILEID:
                ino = struct.unpack_from("<Q", mv, p)[0]
                p += 8
            mnt = 0
            if dira & _A_DIR_MOUNTSTATUS:
                mnt = _U32.unpack_from(mv, p)[0]
                p += 4
            nlink, size, alloc = 1, 0, 0
            if filea & _A_FILE_LINKCOUNT:
                nlink = _U32.unpack_from(mv, p)[0]
                p += 4
            if filea & _A_FILE_TOTALSIZE:
                size = struct.unpack_from("<q", mv, p)[0]
                p += 8
            if filea & _A_FILE_ALLOCSIZE:
                alloc = struct.unpack_from("<q", mv, p)[0]
                p += 8
            return (noff, nlen, typ, *times, ino, mnt, nlink, size, alloc)


# ---------------------------------------------------------------------------
# Walk state
# ---------------------------------------------------------------------------


class _Dir:
    """An in-flight directory: lives until its whole subtree is rolled up."""

    __slots__ = (
        "path", "name", "parent", "depth", "eff_max", "store", "expand",
        "times", "dev", "pending", "size", "apparent", "count", "collapsed",
    )

    def __init__(self, path, name, parent, depth, eff_max, store, expand, times, dev):
        self.path = path
        self.name = name
        self.parent = parent
        self.depth = depth
        self.eff_max = eff_max
        self.store = store      # this dir gets a row
        self.expand = expand    # its children get rows
        self.times = times      # (mtime, atime, ctime, birthtime)
        self.dev = dev
        self.pending = 1        # own listing + outstanding child dirs
        self.size = 0
        self.apparent = 0
        self.count = 0
        self.collapsed = int(store and not expand)

    def row(self) -> tuple:
        m, a, c, b = self.times
        parent_path = self.parent.path if self.parent is not None else None
        return (self.path, self.name, "", self.size, self.apparent, self.count,
                m, a, c, b, 1, self.depth, parent_path, self.collapsed)


_OK, _DUP, _MOUNT, _UNREADABLE = range(4)


class _Walker:
    """Coordinates worker threads, the rollup and the writer for one scan."""

    def __init__(self, roots, max_depth, adaptive, adaptive_threshold, skip,
                 cancel_event, write_rows, on_progress, threads=WALK_THREADS):
        self.roots = roots
        self.max_depth = max_depth
        self.adaptive = adaptive
        self.adaptive_threshold = adaptive_threshold
        self.skip = skip
        self.cancel_event = cancel_event
        self.write_rows = write_rows
        self.on_progress = on_progress
        self.threads = threads
        self.lister = _list_bulk or _list_scandir
        self.visited: set[tuple[int, int]] = set()
        self.links: set[tuple[int, int]] = set()
        self.lock = threading.Lock()
        self.files = self.dirs = self.bytes = self.errors = 0
        self.current_path = ""
        self.root_dirs: list[_Dir] = []
        self.rows: list[tuple] = []

    # -- worker side ------------------------------------------------------

    def _dedupe_for(self, dev):
        links, lock = self.links, self.lock

        def dedupe(ino, alloc):
            key = (dev, ino)
            with lock:
                if key in links:
                    return 0
                links.add(key)
            return alloc
        return dedupe

    def _scan(self, d: _Dir):
        try:
            fd = os.open(d.path, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return d, _UNREADABLE, None
        try:
            st = os.fstat(fd)
            if st.st_dev != d.dev:
                return d, _MOUNT, None
            key = (st.st_dev, st.st_ino)
            with self.lock:
                if key in self.visited:
                    return d, _DUP, None
                self.visited.add(key)
            rows = [] if d.expand else None
            subdirs: list[tuple] = []
            prefix = d.path + "/" if d.path != "/" else "/"
            totals = self.lister(fd, d.path, prefix, d.depth + 1, rows, subdirs,
                                 self._dedupe_for(st.st_dev))
            return d, _OK, (st.st_blocks * 512, rows, subdirs, totals)
        except OSError:
            return d, _UNREADABLE, None
        finally:
            os.close(fd)

    def _worker(self, tasks: queue.SimpleQueue, results: queue.SimpleQueue):
        while True:
            d = tasks.get()
            if d is None:
                return
            try:
                results.put(self._scan(d))
            except BaseException as exc:  # pragma: no cover - surfaced to coordinator
                results.put((d, exc, None))

    # -- coordinator side --------------------------------------------------

    def _finish(self, d: _Dir) -> None:
        """*d*'s subtree is complete: emit its row and roll up into parents."""
        while True:
            if d.store:
                self.rows.append(d.row())
            p = d.parent
            if p is None:
                self.root_dirs.append(d)
                return
            p.size += d.size
            p.apparent += d.apparent
            p.count += d.count
            p.pending -= 1
            if p.pending:
                return
            d = p

    def _flush(self, force=False) -> None:
        if self.rows and (force or len(self.rows) >= FLUSH_ROWS):
            self.write_rows(self.rows)
            self.rows = []

    def _handle(self, d: _Dir, status, payload, stack: deque) -> None:
        self.current_path = d.path
        if status == _DUP:
            d.store = False
        elif status == _MOUNT:
            d.collapsed = 1
        elif status == _UNREADABLE:
            self.errors += 1
            d.collapsed = 1
        elif status == _OK:
            own_alloc, rows, subdirs, (alloc, apparent, nfiles, nentries, nerr) = payload
            self.dirs += 1
            self.files += nfiles
            self.bytes += alloc + own_alloc
            self.errors += nerr
            d.size += alloc + own_alloc
            d.apparent += apparent
            d.count += nfiles
            if rows:
                self.rows.extend(rows)
            child_max = d.eff_max
            if self.adaptive and nentries > self.adaptive_threshold:
                child_max = max(d.depth + 1, d.eff_max - 1)
            depth = d.depth + 1
            prefix = d.path + "/" if d.path != "/" else "/"
            for name, m, a, c, b, is_mount in subdirs:
                expand = d.expand and depth < child_max and name not in self.skip
                child = _Dir(prefix + name, name, d, depth, child_max,
                             d.expand, expand, (m, a, c, b), d.dev)
                d.pending += 1
                if child.path in _DEFERRED_DIRS:
                    # The Data volume is flagged as a mount point but shares
                    # st_dev with "/"; walk it, firmlinked dirs dedupe by inode.
                    stack.appendleft(child)
                elif is_mount:
                    child.collapsed = int(child.store)
                    self._finish(child)
                else:
                    stack.append(child)
        else:  # an exception escaped a worker
            raise status
        d.pending -= 1
        if not d.pending:
            self._finish(d)

    def _progress(self, phase="walking") -> ProgressEvent:
        return ProgressEvent(
            scan_id="", files_scanned=self.files, dirs_scanned=self.dirs,
            current_path=self.current_path, bytes_seen=self.bytes,
            status="running", phase=phase, errors=self.errors,
        )

    def run(self) -> bool:
        """Walk every root. Returns False if cancelled."""
        stack: deque[_Dir] = deque()
        for root in self.roots:
            try:
                st = os.stat(root)
            except OSError:
                self.errors += 1
                continue
            name = os.path.basename(root) or root
            times = (st.st_mtime, st.st_atime, st.st_ctime,
                     getattr(st, "st_birthtime", None))
            stack.append(_Dir(root, name, None, 0, self.max_depth, True,
                              self.max_depth > 0, times, st.st_dev))

        tasks: queue.SimpleQueue = queue.SimpleQueue()
        results: queue.SimpleQueue = queue.SimpleQueue()
        workers = [
            threading.Thread(target=self._worker, args=(tasks, results),
                             name=f"scan-walk-{i}", daemon=True)
            for i in range(self.threads)
        ]
        for w in workers:
            w.start()
        limit = self.threads * _INFLIGHT_PER_THREAD
        outstanding = 0
        last_progress = 0.0
        cancelled = False
        try:
            while stack or outstanding:
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                while stack and outstanding < limit:
                    tasks.put(stack.pop())
                    outstanding += 1
                d, status, payload = results.get()
                outstanding -= 1
                self._handle(d, status, payload, stack)
                self._flush()
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL:
                    last_progress = now
                    self.on_progress(self._progress())
        finally:
            # On cancel/error: drop queued work, outstanding results are ignored.
            while True:
                try:
                    tasks.get_nowait()
                except queue.Empty:
                    break
            for _ in workers:
                tasks.put(None)
            for w in workers:
                w.join()
        if not cancelled:
            self._flush(force=True)
        return not cancelled


# ---------------------------------------------------------------------------
# Scan driver (runs in its own thread)
# ---------------------------------------------------------------------------


class _Writer(threading.Thread):
    """Inserts row batches on its own thread so the walk never waits on SQLite."""

    def __init__(self, db_path: Path):
        super().__init__(name="scan-writer", daemon=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.execute("PRAGMA cache_size = -8192")  # no indexes during the load
        self.q: queue.Queue = queue.Queue(maxsize=4)
        self.error: BaseException | None = None

    def put(self, rows):
        if self.error is not None:
            raise self.error
        self.q.put(rows)

    def run(self):
        while True:
            rows = self.q.get()
            if rows is None:
                return
            if self.error is not None:
                continue
            try:
                db.insert_rows(self.conn, rows)
                self.conn.commit()
            except BaseException as exc:
                self.error = exc

    def close(self):
        self.q.put(None)
        self.join()
        self.conn.close()
        if self.error is not None:
            raise self.error


def _normalize_roots(roots) -> list[str]:
    """Absolute, symlink-free roots with duplicates and nested roots dropped."""
    resolved = list(dict.fromkeys(os.path.realpath(os.path.expanduser(str(r))) for r in roots))
    keep = []
    for r in resolved:
        nested = any(
            o != r and (r.startswith(o.rstrip("/") + "/")) for o in resolved
        )
        if not nested:
            keep.append(r)
    return keep


def _run_scan(scan_id, roots, max_depth, adaptive, adaptive_threshold, db_path,
              skip_dir_names, cancel_event, emit: Callable[[ProgressEvent], None]):
    started = time.time()
    t0 = time.monotonic()

    def stamp(ev: ProgressEvent) -> ProgressEvent:
        ev.scan_id = scan_id
        ev.elapsed_s = round(time.monotonic() - t0, 3)
        return ev

    walker = None
    conn = None
    try:
        root_list = _normalize_roots(roots)
        conn = db.init_db(Path(db_path))
        conn.execute("PRAGMA synchronous = OFF")
        with conn:
            conn.execute(
                "INSERT INTO scan_meta (scan_id, started_at, roots, max_depth, status) "
                "VALUES (?, ?, ?, ?, 'running') ON CONFLICT(scan_id) DO UPDATE SET "
                "started_at = excluded.started_at, roots = excluded.roots, "
                "max_depth = excluded.max_depth, status = 'running', "
                "finished_at = NULL, error_msg = NULL",
                (scan_id, started, json.dumps(root_list), max_depth),
            )
            conn.execute("DELETE FROM nodes")
        db.drop_indexes(conn)

        writer = _Writer(Path(db_path))
        writer.start()
        walker = _Walker(root_list, max_depth, adaptive, adaptive_threshold,
                         set(skip_dir_names or ()), cancel_event, writer.put,
                         lambda ev: emit(stamp(ev)))
        try:
            completed = walker.run()
        finally:
            writer.close()

        def finish_cancelled():
            with conn:
                conn.execute(
                    "UPDATE scan_meta SET status = 'cancelled', finished_at = ? "
                    "WHERE scan_id = ?", (time.time(), scan_id))
            final = walker._progress()
            final.status = "cancelled"
            emit(stamp(final))

        # A cancel that lands after the last directory (during the final
        # flush) still counts: the user asked for it.
        if not completed or cancel_event.is_set():
            finish_cancelled()
            return

        emit(stamp(walker._progress("finalizing")))
        db.ensure_indexes(conn)
        if cancel_event.is_set():
            finish_cancelled()
            return
        total_bytes = sum(d.size for d in walker.root_dirs)
        total_files = sum(d.count for d in walker.root_dirs)
        with conn:
            conn.execute(
                "UPDATE scan_meta SET status = 'complete', finished_at = ?, "
                "total_files = ?, total_dirs = ?, total_bytes = ? WHERE scan_id = ?",
                (time.time(), total_files, walker.dirs, total_bytes, scan_id))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        final = walker._progress("finalizing")
        final.status = "complete"
        final.current_path = ""
        final.files_scanned = total_files
        final.bytes_seen = total_bytes
        emit(stamp(final))
    except Exception as exc:
        logger.exception("Scan %s failed", scan_id)
        if conn is not None:
            try:
                with conn:
                    conn.execute(
                        "UPDATE scan_meta SET status = 'error', finished_at = ?, "
                        "error_msg = ? WHERE scan_id = ?", (time.time(), str(exc), scan_id))
            except Exception:  # pragma: no cover
                logger.exception("Could not record scan error")
        final = walker._progress() if walker else ProgressEvent(scan_id, 0, 0, "", 0, "running")
        final.status = "error"
        final.error = str(exc)
        emit(stamp(final))
    finally:
        if conn is not None:
            conn.close()


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
    """Scan *roots* into the DB at *db_path*, yielding :class:`ProgressEvent`.

    ``running`` events arrive at most every ``PROGRESS_INTERVAL`` seconds
    (phase ``walking``, then one ``finalizing`` while indexes are built);
    the last event is exactly one of ``complete`` / ``cancelled`` /
    ``error``. Set *cancel_event* to stop early. If the consumer stops
    iterating, the scan is cancelled.
    """
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    if cancel_event is None:
        cancel_event = threading.Event()

    def emit(ev: ProgressEvent) -> None:
        try:
            loop.call_soon_threadsafe(events.put_nowait, ev)
        except RuntimeError:  # loop closed: nobody is listening any more
            cancel_event.set()

    thread = threading.Thread(
        target=_run_scan,
        args=(scan_id, list(roots), max_depth, adaptive, adaptive_threshold,
              db_path, skip_dir_names, cancel_event, emit),
        name=f"scan-{scan_id}", daemon=True,
    )
    thread.start()
    try:
        while True:
            ev = await events.get()
            yield ev
            if ev.status != "running":
                return
    finally:
        if thread.is_alive():
            cancel_event.set()
