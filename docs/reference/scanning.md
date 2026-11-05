# Scanning and the scan database

A scan walks one or more roots and writes one SQLite file per scan (`scans/<scan_id>.db`). Directory sizes are rolled up *during* the walk, so a finished scan needs no post-pass. Every query the UI makes (`/tree`, `/files`, `/summary`, staging) reads that file. The module docstring of `backend/scanner.py` describes the pipeline (bulk listing → worker threads → coordinator rollup → writer thread → indexes built in the "finalizing" phase); this doc holds the meaning of the data and the decisions behind it.

## Where the code lives

- `backend/scanner.py` — `scan_roots()` (async generator of `ProgressEvent`, exactly one terminal event last), `_Walker` (coordinator + worker pool), `_list_*` (macOS `getattrlistbulk` via ctypes, `os.scandir` fallback), `_Writer`.
- `backend/db.py` — schema + `PRAGMA user_version` migration of old scan DBs, `ensure_indexes`/`drop_indexes`, `get_tree_data` (with "N smaller items" folding), `get_node`, `remove_deleted`.
- `backend/categories.py` — the only extension→category map (backend queries, previews and the frontend via `GET /categories` all use it).
- `scripts/bench_scan.py` — wall time, files/s and peak RSS for a real tree. `BENCH_IMPL_DIR=<other backend/>` benchmarks another copy of the code.

## What the columns mean

- `size` is **allocated bytes** (`st_blocks * 512`): what deleting actually frees. `apparent_size` is the logical size. Dirs carry the rolled-up totals of their whole subtree.
- A hard-linked inode is counted once; its other links get `size` 0, because deleting one link frees nothing.
- `collapsed = 1` marks a dir whose descendants have **no rows** but whose totals are complete: skip-list dirs (`node_modules`, `.git`, `.venv`, …), dirs at `max_depth`, unreadable dirs, and other mount points (empty leaf). `max_depth` therefore limits how far you can drill in and how many rows are stored, never the sizes. Collapsed dirs never nest.
- `/summary` sums *file rows*, so collapsed bytes are reported separately as `collapsed: {size, dirs, files}`. The category bar shows them as "Bundled folders"; drop that and the bar silently undercounts — on ~/Documents/GitHub that's 79 of 110 GB. Pinned by `test_summary_reports_collapsed_dir_bytes_separately`.

## Why it's built this way

- **`getattrlistbulk` over `scandir` + `lstat`**: one syscall per batch of entries returns name, type, times, inode, link count and both sizes; about 2x faster on APFS. Workers are threads, not processes: the work is syscalls that release the GIL. 8 workers measured best on an M2.
- **Rollup during the walk, indexes after the load**: the old design inserted with all indexes live and then re-read every row in Python to roll sizes up. That was 4–6x slower, with ~1.3 GB peak RSS on a home-dir scan.
- **Dedup by `(st_dev, st_ino)` from an `fstat` of the opened dir**, not `realpath`: macOS firmlinks (`/Users` ≡ `/System/Volumes/Data/Users`) don't resolve through realpath, and the bulk listing reports placeholder inodes for them. `/System/Volumes/Data` shares `st_dev` with `/` and is walked **last**, so firmlinked dirs keep their familiar `/Users/...` paths.
- Measured (M2, under load): ~/Documents/GitHub, 1.07M files, 110 GB in 6–10 s (old code: 145k files and 30.5 GB, truncated). Home dir: 3.16M files in ~29 s (old code: 162 s), 224 MB RSS. `/`: 4.16M files in 39 s.

## Invariants and traps

- `init_db` builds missing indexes **unless the DB has a scan with status `running`**: opening a DB mid-scan must not start an index build. A cancelled scan's DB gets its indexes on its next open.
- Cancel must end as `cancelled` wherever it lands (mid-walk, final flush, index build); the API runner forwards the scanner's terminal status verbatim.
- `remove_deleted` deletes the subtree and subtracts its totals from every ancestor **and** from `scan_meta`; there is no "recompute from children" path any more (the old one was a silent no-op).
- Paths are compared as strings. Subtree filters use the range `path >= dir + "/" AND path < dir + "0"` (`query_engine.subtree_range`), never `LIKE`, so sibling dirs that share a prefix (`/a/b` vs `/a/bc`) stay apart and the path index is used.
