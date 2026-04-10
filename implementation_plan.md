# Disk Manager — Implementation Plan

## 1. Executive Summary

We are building a local-first disk management utility for macOS (M2 MacBook Pro primary, Linux secondary) with a localhost web UI. The tool scans one or more root directories, persists results to SQLite, and presents interactive treemap/list visualizations with a staged deletion workflow that sends items to macOS Trash rather than permanently deleting them.

### Technology Decisions

**Backend: FastAPI over Flask/Starlette**
FastAPI is chosen because it ships with async-native request handling, automatic OpenAPI docs (useful during development), Pydantic-based request/response validation, and native SSE support via `StreamingResponse`. Flask requires additional extensions for async and SSE. Starlette is FastAPI's underlying framework — using FastAPI gives Starlette's performance with zero additional cost and better ergonomics.

**Progress Streaming: Server-Sent Events (SSE) over WebSocket**
Scanning is a unidirectional push from server to client. WebSocket's bidirectional overhead is unnecessary. SSE is natively supported by all browsers, reconnects automatically, works over HTTP/1.1, and requires no library on the frontend. The only downside (no binary framing) is irrelevant here since we stream JSON progress events.

**Frontend: Vanilla JS + D3.js over React**
This tool has one primary view with a handful of panels. A React build system (Vite or CRA) adds packaging complexity that benefits multi-page SPAs more than single-view utilities. Vanilla JS with D3.js keeps the serving model trivial (FastAPI serves static files directly), avoids a separate build step, and D3's treemap layout is the gold standard for hierarchical visualization. The component complexity does not justify a framework.

**Visualization: D3.js over ECharts/Plotly**
D3's `d3.treemap()` with `d3.hierarchy()` gives precise control over zoomable, interactive hierarchical layouts. ECharts has a treemap but customization depth is limited. Plotly's treemap (using Sunburst/Treemap trace) has poor zoom interaction UX and large bundle size (~3MB). D3 is ~280KB and purpose-built for this.

**Async Scanning: ThreadPoolExecutor via `asyncio.run_in_executor`**
`os.scandir` and `os.stat` are blocking syscalls. `asyncio.run_in_executor` dispatches these to a thread pool without blocking the event loop, keeping FastAPI responsive during long scans. Pure `asyncio` file I/O (via `aiofiles`) does not accelerate `os.stat` calls on macOS — those are kernel-bound, not I/O-wait-bound in the async sense. Subprocess workers add IPC serialization overhead unnecessarily.

**Packaging: Makefile + pip + requirements.txt**
`uvx` is suitable for one-off CLI tools but less ergonomic for a persistent server. A `Makefile` with targets (`make install`, `make run`, `make clean`) is universally understood, requires no additional tooling beyond pip, and is trivially extendable. A `venv` is created automatically by the Makefile.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Browser (localhost:8000)                      │
│                                                                     │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ Scan     │  │ Treemap      │  │ Top-N     │  │ Staging      │  │
│  │ Panel    │  │ Viewer (D3)  │  │ List View │  │ Area         │  │
│  └────┬─────┘  └──────┬───────┘  └─────┬─────┘  └──────┬───────┘  │
│       │               │                │                │           │
│  ┌────▼───────────────▼────────────────▼────────────────▼───────┐  │
│  │                    App State (JS module)                      │  │
│  │   currentScanId, filters, stagedItems, topN, treeData        │  │
│  └────────────────────────────┬──────────────────────────────────┘  │
│                               │ fetch / EventSource                 │
└───────────────────────────────┼─────────────────────────────────────┘
                                │ HTTP + SSE  (localhost:8000)
┌───────────────────────────────▼─────────────────────────────────────┐
│                          FastAPI Application                         │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ /api/scan    │  │ /api/query   │  │ /api/staging             │  │
│  │ (SSE stream) │  │ (filter SQL) │  │ (add/remove/execute)     │  │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬─────────────┘  │
│         │                 │                        │                │
│  ┌──────▼───────┐  ┌──────▼────────┐  ┌──────────▼─────────────┐  │
│  │  Scanner     │  │  QueryEngine  │  │  DeletionEngine         │  │
│  │  Module      │  │  Module       │  │  Module                 │  │
│  └──────┬───────┘  └──────┬────────┘  └──────────┬─────────────┘  │
│         │                 │                        │                │
│  ┌──────▼─────────────────▼────────────────────────▼─────────────┐  │
│  │                     SQLite Layer (per-session DB)              │  │
│  │              files, directories, scan_meta tables              │  │
│  └─────────────────────────────────────────────────────────────-─┘  │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│  │  PreviewEngine   │  │  ConfigManager   │  │  StagingManager   │  │
│  │  (Pillow/ffmpeg/ │  │  (config.json)   │  │  (staging.json)   │  │
│  │   pymupdf)       │  └──────────────────┘  └───────────────────┘  │
│  └──────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────┘
                                │
                         ┌──────▼──────┐
                         │  macOS      │
                         │  Trash /    │
                         │  Finder     │
                         └─────────────┘
```

---

## 3. Module Breakdown

### 3.1 `scanner.py` — Filesystem Scanner

**Responsibility**: Walk one or more root paths, collect file/directory metadata, batch-insert into SQLite, emit progress events via an async generator.

**Key Functions**:

```
scan_roots(
    scan_id: str,
    roots: list[Path],
    max_depth: int,
    adaptive: bool,
    db_path: Path,
    progress_queue: asyncio.Queue
) -> AsyncGenerator[ProgressEvent, None]
```

- Spawns a `ThreadPoolExecutor` worker per root
- Each worker calls `_walk_directory(root, max_depth, adaptive_threshold)`
- Results are batched into lists of 500 records and inserted via `db.batch_insert()`
- After each batch, posts a `ProgressEvent` to the queue
- On completion, triggers `_compute_directory_sizes(db_path)` — a single SQL UPDATE pass that rolls up sizes from leaves to roots

```
_walk_directory(
    root: Path,
    max_depth: int,
    adaptive: bool
) -> Iterator[FileRecord]
```

- Uses `os.scandir()` in a BFS loop (iterative, not recursive — avoids Python stack limits on deep trees)
- For adaptive depth: if a directory contains more than `ADAPTIVE_THRESHOLD` (default: 10,000) immediate children, reduce max_depth for that subtree by 1
- Collects `os.stat()` data: `st_size`, `st_mtime`, `st_atime`, `st_ctime`
- Skips symlinks to directories (records them as files to avoid cycles)
- Handles `PermissionError` gracefully — logs path, continues

```
_compute_directory_sizes(db_path: Path) -> None
```

- Runs entirely in SQL: iterates depth levels from deepest to shallowest
- Uses a recursive CTE or iterative UPDATE to accumulate child sizes into parent `size` column
- This is a post-scan pass, not done during walking (avoids partial reads)

**Inputs**: root paths, max_depth, scan_id, DB path
**Outputs**: SQLite DB populated with file/directory records; async stream of `ProgressEvent` objects

**ProgressEvent schema**:
```json
{
  "scan_id": "string",
  "files_scanned": 1042,
  "dirs_scanned": 88,
  "current_path": "/Users/x/Documents",
  "bytes_seen": 4294967296,
  "status": "running | complete | error",
  "error": null
}
```

---

### 3.2 `db.py` — SQLite Data Layer

**Responsibility**: Schema creation, batch inserts, query helpers, index management, and directory size rollup.

**Key Functions**:

```
init_db(db_path: Path) -> sqlite3.Connection
batch_insert(conn, records: list[FileRecord]) -> None
get_tree_data(conn, root: str, depth: int) -> list[dict]
query_files(conn, filters: FilterParams) -> tuple[list[dict], int]
delete_records(conn, paths: list[str]) -> None
update_parent_sizes(conn, deleted_paths: list[str]) -> None
```

`batch_insert` uses `executemany` with `INSERT OR REPLACE` inside an explicit `BEGIN IMMEDIATE` transaction. Each batch of 500 records commits atomically.

`get_tree_data` returns a nested JSON-serializable structure for D3 hierarchy consumption. Uses a recursive CTE bounded by depth parameter to avoid pulling the full tree for large roots.

`query_files` accepts `FilterParams` (see Section 5) and constructs a parameterized SQL query using the indexed columns. Returns paginated results plus total count via `COUNT(*)` window function.

---

### 3.3 `query_engine.py` — Filter & Query Orchestrator

**Responsibility**: Translate frontend filter parameters into optimized SQL, handle pagination, return results without triggering re-scans.

**Key Functions**:

```
build_filter_query(filters: FilterParams) -> tuple[str, list]
paginate(conn, sql: str, params: list, page: int, page_size: int) -> PaginatedResult
get_top_n(conn, scan_id: str, n: int, mode: Literal["files","dirs"]) -> list[FileRecord]
```

`build_filter_query` builds a parameterized WHERE clause. Critically, extension filtering uses `WHERE extension IN (?, ?, ?)` which hits the index directly. Date range filters use `WHERE mtime >= ?` against the mtime index. The exclude toggle inverts to `WHERE extension NOT IN (?, ?, ?)`.

---

### 3.4 `preview_engine.py` — File Preview Generator

**Responsibility**: Generate thumbnails for images, videos, PDFs, and icon placeholders for other types. Cache thumbnails in a session-specific directory.

**Key Functions**:

```
generate_thumbnail(path: Path, size: tuple[int,int] = (256, 256)) -> bytes | None
_thumb_image(path: Path, size) -> bytes        # Pillow
_thumb_video(path: Path, size) -> bytes        # ffmpeg subprocess, extract frame at 5s
_thumb_pdf(path: Path, size) -> bytes          # pymupdf (fitz) page 0 render
_thumb_fallback(extension: str) -> bytes       # SVG icon map by category
get_cached_or_generate(scan_id: str, path: Path) -> bytes
```

Thumbnails are cached at `~/.cache/disk_manager/{scan_id}/{md5(path)}.webp`. WebP is chosen over PNG for ~30% size reduction. Cache is cleared when a scan session is deleted.

`_thumb_video` calls `ffmpeg` as a subprocess with `-ss 00:00:05 -vframes 1 -f image2pipe -vcodec png` piped to stdout. Falls back to frame 0 if file is shorter than 5 seconds.

---

### 3.5 `deletion_engine.py` — Staged Deletion Executor

**Responsibility**: Execute deletions (Trash or permanent), update SQLite, trigger parent size recalculation.

**Key Functions**:

```
execute_deletion(
    paths: list[str],
    permanent: bool,
    db_path: Path
) -> DeletionResult

_trash_items(paths: list[str]) -> list[str]    # send2trash, returns failed paths
_rm_items(paths: list[str]) -> list[str]       # os.remove / shutil.rmtree, returns failed
```

`execute_deletion` processes paths in two passes: first directories (sorted by depth, deepest first to avoid parent-before-child errors), then files. After deletion, calls `db.delete_records()` and `db.update_parent_sizes()`.

`DeletionResult` contains: `deleted: list[str]`, `failed: list[tuple[str, str]]` (path, error message), `bytes_freed: int`.

---

### 3.6 `staging_manager.py` — Staging Area State

**Responsibility**: Manage the in-memory staging queue, persist to `staging.json`, compute aggregate stats.

**Key Functions**:

```
add_items(paths: list[str], db_path: Path) -> StagingState
remove_items(paths: list[str]) -> StagingState
clear() -> None
get_state() -> StagingState
persist(staging_path: Path) -> None
load(staging_path: Path) -> StagingState
```

`StagingState` contains: `items: list[StagedItem]`, `total_size: int`, `count_by_extension: dict[str, int]`, `dir_count: int`, `file_count: int`.

When `add_items` is called with a directory path, the staging manager queries SQLite for all descendant files to compute the aggregate size — it does NOT recursively expand paths on disk (avoids hitting the filesystem).

---

### 3.7 `config_manager.py` — Configuration Persistence

**Responsibility**: Read/write `config.json`, provide defaults, validate on load.

**Schema**:
```json
{
  "top_n": 25,
  "max_depth": 10,
  "adaptive_scanning": true,
  "adaptive_threshold": 10000,
  "last_scan_paths": ["/Users/x/Downloads"],
  "default_sort": "size_desc",
  "thumbnail_size": [256, 256],
  "page_size": 100,
  "permanent_delete_default": false
}
```

---

### 3.8 `main.py` — FastAPI Application Entry Point

**Responsibility**: Route definitions, static file serving, lifespan management (DB connection pool per scan session), CORS configuration.

Uses FastAPI's `lifespan` context manager to initialize config on startup and clean up open DB connections on shutdown. Maintains a dict `scan_sessions: dict[str, ScanSession]` in application state, where `ScanSession` holds `db_path`, `conn`, `staging_manager` instance.

---

## 4. Data Model (SQLite Schema DDL)

```sql
-- One database file per scan session, e.g. scans/scan_20260410_143022.db

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 536870912;  -- 512MB memory-mapped I/O

-- ─────────────────────────────────────────
-- Scan metadata
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scan_meta (
    scan_id     TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,          -- Unix timestamp
    finished_at REAL,
    roots       TEXT NOT NULL,          -- JSON array of root paths
    max_depth   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',  -- running|complete|error
    total_files INTEGER DEFAULT 0,
    total_dirs  INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    error_msg   TEXT
);

-- ─────────────────────────────────────────
-- File and directory records
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,    -- rowid alias, implicit in SQLite
    path        TEXT NOT NULL UNIQUE,   -- absolute path, UTF-8
    name        TEXT NOT NULL,          -- basename only
    extension   TEXT NOT NULL DEFAULT '',  -- lowercase, includes dot: ".jpg"
    size        INTEGER NOT NULL DEFAULT 0,  -- bytes; for dirs: recursive sum
    mtime       REAL NOT NULL,          -- st_mtime (Unix float)
    atime       REAL NOT NULL,          -- st_atime
    ctime       REAL NOT NULL,          -- st_ctime (metadata change on macOS/Linux)
    birthtime   REAL,                   -- st_birthtime: macOS only, NULL on Linux
    is_dir      INTEGER NOT NULL,       -- 0 or 1 (SQLite has no BOOLEAN)
    depth       INTEGER NOT NULL,       -- depth from scan root (root itself = 0)
    parent_path TEXT,                   -- NULL for root nodes
    scan_id     TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scan_meta(scan_id)
);

-- ─────────────────────────────────────────
-- Indexes (critical for 1M+ row performance)
-- ─────────────────────────────────────────

-- Extension filtering (most common filter operation)
CREATE INDEX IF NOT EXISTS idx_extension ON nodes(extension) WHERE is_dir = 0;

-- Size sorting / top-N queries
CREATE INDEX IF NOT EXISTS idx_size_desc ON nodes(size DESC);

-- Date-range filters (mtime is most queried)
CREATE INDEX IF NOT EXISTS idx_mtime ON nodes(mtime);
CREATE INDEX IF NOT EXISTS idx_atime ON nodes(atime);

-- Directory tree traversal (parent lookups)
CREATE INDEX IF NOT EXISTS idx_parent ON nodes(parent_path);

-- Scan session scoping (all queries include scan_id)
CREATE INDEX IF NOT EXISTS idx_scan ON nodes(scan_id);

-- Composite: scan + is_dir + size (covers top-N dir queries)
CREATE INDEX IF NOT EXISTS idx_scan_dir_size ON nodes(scan_id, is_dir, size DESC);

-- Composite: scan + extension + mtime (covers filtered list queries)
CREATE INDEX IF NOT EXISTS idx_scan_ext_mtime ON nodes(scan_id, extension, mtime);
```

**Schema design notes**:
1. `path` is UNIQUE, not a composite key with `scan_id`, because within one session a path appears exactly once. Cross-session comparison uses separate DB files.
2. `extension` stored with leading dot (`.jpg`) for direct IN-list matching.
3. `size` for directories is populated in a post-scan rollup pass, not during walk.
4. `ctime` on macOS is "time of last metadata change", not "creation time". True creation time (`birthtime`) requires `os.stat().st_birthtime` (macOS-only). `birthtime` column is NULL on Linux.
5. WAL mode allows concurrent readers during the scan write phase.

**Estimated storage**: At ~500 bytes per row (including indexes), 1M files ≈ 500MB DB. With WAL and mmap this remains fast. Run `VACUUM` after scan completion to reclaim WAL overhead.

**Directory size rollup SQL** (post-scan, run per depth level from deepest to shallowest):

```sql
UPDATE nodes
SET size = (
    SELECT COALESCE(SUM(child.size), 0)
    FROM nodes child
    WHERE child.parent_path = nodes.path
      AND child.scan_id = nodes.scan_id
)
WHERE nodes.is_dir = 1
  AND nodes.scan_id = ?
  AND nodes.depth = ?;
```

---

## 5. API Endpoints (REST + SSE)

All endpoints prefixed with `/api/v1`.

### 5.1 Scan Management

```
POST   /api/v1/scans
  Body: { roots: [string], max_depth: int, adaptive: bool }
  Returns: { scan_id: string, db_path: string }
  Creates a new scan session. Actual scan starts in background thread pool.

GET    /api/v1/scans/{scan_id}/stream
  Returns: text/event-stream (SSE)
  Streams ProgressEvent objects until status=complete or status=error.

GET    /api/v1/scans
  Returns: list of { scan_id, started_at, status, roots, total_files, total_bytes }

GET    /api/v1/scans/{scan_id}
  Returns: ScanMeta object.

DELETE /api/v1/scans/{scan_id}
  Deletes the scan DB file and thumbnail cache for this session.
```

### 5.2 Tree & Visualization Data

```
GET    /api/v1/scans/{scan_id}/tree
  Query params: root=<path>, depth=<int default 3>
  Returns: D3-compatible hierarchy JSON { name, path, size, children: [...] }
  Bounded by depth param; deeper nodes loaded lazily on zoom.

GET    /api/v1/scans/{scan_id}/top
  Query params: n=<int>, mode=<files|dirs>, sort=<size|mtime>
  Returns: list of FileRecord (top N by size)
```

### 5.3 File Queries (Filtered List View)

```
GET    /api/v1/scans/{scan_id}/files
  Query params:
    extensions=.jpg,.mp4,...     (comma-separated, empty = all)
    exclude_extensions=<bool>    (invert extension filter)
    min_size=<bytes>
    max_size=<bytes>
    mtime_after=<unix_ts>
    mtime_before=<unix_ts>
    atime_after=<unix_ts>
    is_dir=<bool>                (omit = both)
    sort=<size_desc|size_asc|mtime_desc|name_asc>
    page=<int default 0>
    page_size=<int default 100>
  Returns: { items: [...], total: int, page: int, page_size: int }
```

### 5.4 Staging & Deletion

```
GET    /api/v1/scans/{scan_id}/staging
  Returns: StagingState { items, total_size, count_by_extension, ... }

POST   /api/v1/scans/{scan_id}/staging/add
  Body: { paths: [string] }
  Returns: updated StagingState

POST   /api/v1/scans/{scan_id}/staging/remove
  Body: { paths: [string] }
  Returns: updated StagingState

DELETE /api/v1/scans/{scan_id}/staging
  Clears all staged items.

POST   /api/v1/scans/{scan_id}/staging/execute
  Body: { permanent: bool }
  Returns: DeletionResult { deleted, failed, bytes_freed }
  Sends to Trash by default (permanent=false). Destructive — requires explicit flag.
```

### 5.5 Preview

```
GET    /api/v1/scans/{scan_id}/preview/thumbnail
  Query params: path=<url-encoded absolute path>
  Returns: image/webp (or image/svg+xml for fallback icons)
  Generates and caches thumbnail on first request.

GET    /api/v1/scans/{scan_id}/preview/meta
  Query params: path=<url-encoded absolute path>
  Returns: FileRecord with all metadata fields.

POST   /api/v1/scans/{scan_id}/preview/open
  Body: { path: string }
  Executes: open -R <path> (macOS) or xdg-open <dirname> (Linux)
  Returns: { success: bool }
```

### 5.6 Configuration

```
GET    /api/v1/config
  Returns: current config.json contents.

PATCH  /api/v1/config
  Body: partial config object
  Merges with existing config, persists, returns updated config.
```

---

## 6. Frontend Component Tree

```
App (index.html + app.js)
│
├── ConfigPanel (config-panel.js)
│   ├── TopNSlider
│   └── MaxDepthInput
│
├── ScanPanel (scan-panel.js)
│   ├── PathSelector (multi-input with add/remove)
│   ├── ScanButton
│   ├── ProgressBar
│   └── SessionSelector (dropdown of past scans)
│
├── FilterBar (filter-bar.js)
│   ├── ExtensionFilter
│   │   ├── ExtensionInput (tokenized tag input)
│   │   └── ExcludeToggle
│   ├── DateRangeFilter
│   │   ├── MtimeRange (last N days slider)
│   │   └── AtimeRange (last N days slider)
│   ├── SizeRangeFilter
│   │   ├── MinSizeInput
│   │   └── MaxSizeInput
│   └── ViewToggle (treemap | top-n list)
│
├── MainView (main-view.js)
│   ├── TreemapView (treemap.js)
│   │   ├── D3TreemapCanvas (SVG element)
│   │   ├── BreadcrumbNav (zoom path display)
│   │   └── ZoomControls
│   └── TopNListView (topn-list.js)
│       ├── ModeToggle (files | dirs)
│       ├── SortControl
│       └── FileTable (virtualized rows)
│
├── PreviewPanel (preview-panel.js)
│   ├── ThumbnailDisplay
│   ├── MetadataTable
│   ├── OpenInFinderButton
│   └── StageForDeletionButton
│
└── StagingPanel (staging-panel.js)
    ├── StagingSummary (total size, counts by type)
    ├── StagingList (removable items)
    ├── ClearStagingButton
    └── ExecuteDeletionButton
        └── ConfirmationModal
            ├── TrashToggle (Trash vs permanent)
            ├── SummaryText
            └── ConfirmButton / CancelButton
```

### Frontend State Management

A single `AppState` ES module (no library) holds:

```javascript
const AppState = {
  currentScanId: null,
  filters: { extensions: [], excludeExtensions: false, mtimeAfterDays: null, ... },
  topN: 25,
  viewMode: 'treemap',  // 'treemap' | 'topn'
  stagedItems: [],
  treeZoomPath: null,
  previewPath: null,
  lastFilterHash: null,  // debounce: avoid redundant fetches
};
```

Filter changes debounce 300ms before triggering API calls to avoid flooding SQLite during slider drags.

---

## 7. Key Algorithms

### 7.1 Scanning with Adaptive Depth

```
BFS scan algorithm:

queue = deque([(root, depth=0)])
while queue:
    (path, depth) = queue.popleft()

    if depth > effective_max_depth(path):
        continue

    entries = os.scandir(path)  -- O(1) per entry via getdents syscall

    batch = []
    child_count = 0
    for entry in entries:
        stat = entry.stat(follow_symlinks=False)
        record = build_record(entry, stat, depth)
        batch.append(record)
        child_count += 1

        if entry.is_dir(follow_symlinks=False):
            child_max_depth = max_depth
            if adaptive and child_count > ADAPTIVE_THRESHOLD:
                child_max_depth = max(0, max_depth - 1)
            queue.append((entry.path, depth + 1, child_max_depth))

    db.batch_insert(batch)
    yield ProgressEvent(files_scanned=..., current_path=path)
```

**Key insight**: `os.scandir()` returns `DirEntry` objects that cache `stat` results from the underlying `readdir`/`getdirentries` syscall — calling `entry.stat()` on a `DirEntry` does NOT make an additional syscall on many filesystems. Significantly faster than `os.listdir()` + `os.stat()` in a loop.

**APFS note**: On macOS (APFS), directory sizes are not exposed via `dirent` — you must sum children. The post-scan rollup pass handles this correctly.

### 7.2 Fast Filter Queries (No Re-scan)

```python
def build_filter_query(filters: FilterParams) -> tuple[str, list]:
    clauses = ["scan_id = ?"]
    params = [filters.scan_id]

    if filters.extensions:
        placeholders = ",".join("?" * len(filters.extensions))
        op = "NOT IN" if filters.exclude_extensions else "IN"
        clauses.append(f"extension {op} ({placeholders})")
        params.extend(filters.extensions)  # lowercase with dot, e.g. ".jpg"

    if filters.mtime_after_days is not None:
        cutoff = time.time() - (filters.mtime_after_days * 86400)
        clauses.append("mtime >= ?")
        params.append(cutoff)

    if filters.min_size is not None:
        clauses.append("size >= ?")
        params.append(filters.min_size)

    where = " AND ".join(clauses)
    sort_col = SORT_MAP[filters.sort]  # hardcoded whitelist — never interpolate user input

    sql = f"""
        SELECT path, name, extension, size, mtime, atime, ctime, is_dir, depth
        FROM nodes
        WHERE {where}
        ORDER BY {sort_col}
        LIMIT ? OFFSET ?
    """
    params.extend([filters.page_size, filters.page * filters.page_size])
    return sql, params
```

`SORT_MAP` is a hardcoded dict — sort columns are never interpolated from user input, preventing SQL injection.

### 7.3 D3 Treemap Lazy Loading

```javascript
async function zoomTo(path) {
    AppState.treeZoomPath = path;
    const data = await fetchTree(currentScanId, path, depth=3);
    const root = d3.hierarchy(data)
        .sum(d => d.size)
        .sort((a, b) => b.value - a.value);

    d3.treemap()
        .size([width, height])
        .paddingOuter(3)
        .paddingInner(1)
        .round(true)
        (root);

    renderCells(root.leaves());
    renderBreadcrumb(path);
}
```

Never loads the full 1M-node tree into the browser. `GET /tree` returns only the subtree rooted at `path` with `depth=3`, using a bounded recursive CTE.

### 7.4 Treemap Color Encoding

Files/directories colored by extension category:

```javascript
const CATEGORY_COLORS = {
    image:    '#4e9af1',  // .jpg .png .gif .webp .heic .raw
    video:    '#e05c5c',  // .mp4 .mov .mkv .avi
    audio:    '#f1a34e',  // .mp3 .flac .aac .wav
    document: '#6db66d',  // .pdf .docx .xlsx .pptx
    code:     '#b88de8',  // .py .js .ts .go .rs
    archive:  '#e8d08d',  // .zip .tar .gz .dmg
    other:    '#aaaaaa',
};
```

Color intensity scales with size within category (D3 sequential scale). Instant visual identification without palette overwhelm.

---

## 8. Performance Considerations

### 8.1 SQLite Configuration

```sql
PRAGMA journal_mode = WAL;         -- concurrent readers during write
PRAGMA synchronous = NORMAL;       -- safe on power events, faster than FULL
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 536870912;      -- 512MB mmap
PRAGMA cache_size = -65536;        -- 64MB page cache
```

Call `PRAGMA optimize` after scan completion to update internal query planner statistics.

### 8.2 Batch Insert Performance

| Method | ~1M rows |
|--------|----------|
| Single row INSERT | ~120s |
| executemany, autocommit | ~45s |
| executemany, explicit transaction | ~4-6s |
| executemany, WAL, `BEGIN IMMEDIATE` | ~3-4s |

Use `BEGIN IMMEDIATE` to avoid write-lock upgrades mid-transaction.

### 8.3 Worker Threads

`ThreadPoolExecutor` with `max_workers=min(4, os.cpu_count())`. More workers give diminishing returns on M2 — scan time is syscall-bound, not compute-bound.

### 8.4 Frontend Virtualization

Top-N list and filtered file list can return 10K+ rows. Render only visible rows via Intersection Observer virtual scroll. Fixed row height (40px) enables O(1) scroll position calculation.

For the treemap: prune cells smaller than `totalSize * 0.0001`, and never render cells smaller than 2px × 2px.

### 8.5 Thumbnail Executor

Use a dedicated `ThreadPoolExecutor` (max_workers=2) separate from the scan executor. Prevents thumbnail generation from starving scan progress SSE events.

### 8.6 Memory Usage

At 1M files × ~200 bytes per `FileRecord` = ~200MB peak. Mitigate by:
- Batching 500 records at a time (cap in-memory accumulation)
- Using `__slots__` on `FileRecord` dataclass
- Releasing batch list explicitly after `db.batch_insert()`

---

## 9. macOS-Specific Integrations

### 9.1 Trash via send2trash

```python
from send2trash import send2trash

# Uses FSMoveObjectToTrashSync Carbon API via ctypes
# Items appear in Finder's Trash with "Put Back" support
send2trash(path)
```

**Critical**: `send2trash` fails on volumes without a `.Trashes` directory (some external drives, network mounts). Catch `TrashPermissionError` per-item, surface failures explicitly in UI — never silently fall back to permanent deletion.

### 9.2 Open in Finder

```python
import subprocess, platform

def open_in_finder(path: str) -> None:
    if platform.system() == "Darwin":
        subprocess.run(["open", "-R", path], check=True)  # reveals & selects in Finder
    else:
        subprocess.run(["xdg-open", str(Path(path).parent)], check=True)
```

`open -R` is the correct macOS invocation. `open` without `-R` would open the file with its default application.

### 9.3 birthtime (True Creation Time) on macOS

```python
stat = os.stat(path)
birthtime = getattr(stat, 'st_birthtime', None)  # macOS-only; None on Linux
```

Stored in the `birthtime` column (NULL on Linux). `ctime` on macOS is metadata change time, not creation time.

### 9.4 APFS vs HFS+ — Logical vs Physical Size

APFS uses sparse files and file clones. `st_size` reports logical size; physical blocks is `st_blocks * 512`. For disk cleaning, **logical size** is the correct metric ("what do I recover if I delete this?"), since file clones make physical reclamation unpredictable. Document this in the UI.

### 9.5 macOS TCC Permissions

Accessing Desktop, Documents, Downloads, or iCloud Drive on macOS Ventura+ requires Full Disk Access in System Settings. On `PermissionError`:
1. Show a banner: "Grant Full Disk Access to scan this location"
2. Provide a button: `open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"`
3. Log all permission-denied paths to `scan_meta.error_msg` / a separate `permission_errors` list

### 9.6 Spotlight Exclusion

The thumbnail cache at `~/.cache/disk_manager/` should contain a `.metadata_never_index` file to prevent Spotlight from indexing thumbnails.

### 9.7 iCloud Drive Stubs

Files in `~/Library/Mobile Documents/` may be 0-byte iCloud stubs (not downloaded). Default behavior: detect iCloud paths at scan start, warn the user, and skip 0-byte stubs with `st_size == 0` in iCloud directories. Make this configurable.

---

## 10. Open Questions for the User

### Q1: Multi-scan comparison — is it needed?

Should the tool support diffing two scan sessions ("what changed since last month")?

- **Option A** *(recommended for v1)*: Single active scan only. The schema (separate DB files) does not preclude adding diff via `ATTACH DATABASE` later.

---

### Q2: iCloud Drive / network paths

Files in iCloud Drive may appear as 0-byte stubs unless downloaded.

- **Option A** *(recommended)*: Skip iCloud stubs silently with a configurable toggle; show warning banner when iCloud paths are detected.

---

### Q3: Deletion of directories with staged children

If the user stages `/foo/` and separately stages `/foo/bar.jpg`, executing will try to delete both.

- **Option B** *(recommended)*: Backend deduplication — `execute_deletion` skips paths whose parent is also in the delete list. Silent and correct.

---

### Q4: Authentication / access control

The server runs on `localhost:8000` with no authentication. A rogue local process or malicious webpage (CSRF) could trigger deletions.

- **Option A**: No auth. Accept the risk for a personal developer tool.
- **Option B** *(recommended)*: Bind to `127.0.0.1` only (prevents LAN access). CORS explicitly allows only `http://localhost:8000`. Add a CSRF token for mutation endpoints.

---

### Q5: Top-N slider persistence

When the user changes the slider from 25 to 50, should it persist:

- **Option A**: Only for the current browser session (`sessionStorage`).
- **Option B**: Immediately to `config.json`.
- **Option C** *(recommended)*: Debounced PATCH after 1 second of slider inactivity. Instant feel, batched writes.

---

### Q6: Preview panel for directories

When the user clicks a directory:

- **Option A** *(recommended, easy start)*: Show metadata only (size, file count, mtime). No thumbnail.
- **Option C** *(implement this upgrade if its easy to do)*: Embed a mini D3 treemap of the directory's contents inline. Compelling for v2 — tree data is already available.

---

### Q7: Linux support level

- **Option A** *(recommended)*: Linux works if dependencies installed, platform-specific features (Trash, file manager) are best-effort with clear docs. `send2trash` on Linux depends on `gio`/`kioclient5` which may not exist on minimal installs. Focus on making this work on macbook first!

---

### Q8: Default excluded directories

Should certain high-noise directories be skipped by default?

Suggested default skip list: `node_modules`, `.git`, `__pycache__`, `.venv`, `venv`, `target` (Rust), `.stack-work` (Haskell).

- **Option A** *(recommended)*: Yes, configurable via `config.json` → `skip_dir_names: [...]`. For these high-noise directories: note their full size, but don't go inside them, these become leaf nodes.

---

### Q9: Scan result retention

How many past scan sessions should be kept?

- **Option B**: Auto-prune: keep last N sessions (configurable, default 5).

---

## 11. Implementation Phases

### Phase 1: Core Scanning + Storage (Foundation)
1. `db.py`: schema creation, `batch_insert`, basic `query_files`
2. `scanner.py`: BFS walk, `os.scandir` loop, batch inserts, directory size rollup
3. `config_manager.py`: load/save `config.json`
4. `main.py`: minimal FastAPI app, `POST /scans`, `GET /scans/{id}/stream` (SSE)
5. Manual verification: scan `~/Downloads`, inspect DB with DB Browser for SQLite

**Success criteria**: SQLite populated correctly; SSE events visible in `curl`; 100K files scan completes in < 30s.

---

### Phase 2: Query API + Basic Frontend
1. `query_engine.py`: `build_filter_query`, pagination
2. API endpoints: `GET /files`, `GET /top`
3. `index.html` + `app.js`: skeleton layout, scan panel, progress bar via EventSource
4. `filter-bar.js`: extension filter, date sliders, size inputs
5. `topn-list.js`: virtualized table, toggle files/dirs
6. Wire filter changes → API calls → table updates

**Success criteria**: Filter by `.jpg` returns correct results in < 200ms on 500K row DB.

---

### Phase 3: Treemap Visualization
1. `treemap.js`: D3 hierarchy, initial render at depth=3
2. `GET /tree` endpoint with bounded recursive CTE
3. Zoom interaction: click cell → fetch subtree → re-render
4. Breadcrumb navigation
5. Color encoding by file category
6. Right-click treemap cell → stage for deletion

**Success criteria**: Treemap renders for 10GB directory structure; zoom responsive < 300ms per level.

---

### Phase 4: Preview Panel
1. `preview_engine.py`: Pillow images, ffmpeg videos, pymupdf PDFs, SVG fallbacks
2. `GET /preview/thumbnail` with thumbnail cache
3. `GET /preview/meta`
4. `POST /preview/open` (`open -R`)
5. `preview-panel.js`: slide-in panel, thumbnail, metadata, open button

**Success criteria**: Image thumbnail < 500ms cached, < 2s uncached. Video frame extraction works for .mp4.

---

### Phase 5: Staging & Deletion
1. `staging_manager.py`: in-memory state, `staging.json` persistence
2. `deletion_engine.py`: `send2trash` integration, permanent delete path
3. Staging API endpoints
4. `staging-panel.js`: summary stats, item list with remove buttons
5. `ConfirmationModal`: Trash vs permanent toggle, size summary
6. Post-deletion: SQLite cleanup, parent size update, UI refresh

**Success criteria**: Stage 10 files + 1 directory, execute sends to Trash, Finder Trash count increases, SQLite updated.

---

### Phase 6: Polish + Config + Packaging
1. `config-panel.js`: top-N slider, max depth, debounced persistence
2. Error states: permission denied banners, TCC guidance, scan errors
3. Loading skeletons during API calls
4. `Makefile`: `make install`, `make run`, `make clean`
5. `requirements.txt` with pinned versions
6. Spotlight exclusion file for thumbnail cache
7. Default skip-dirs list in config

---

## 12. Risk Areas and Mitigations

### Risk 1: SQLite write contention during concurrent scan + queries

**Problem**: Bulk inserts during scan block read queries briefly.

**Mitigation**: WAL mode allows concurrent readers. Use per-session `threading.RLock` around writes. Separate `sqlite3.Connection` objects for reads vs writes (WAL supports multiple connections).

---

### Risk 2: Very large directories (node_modules) blowing memory

**Problem**: A `node_modules` tree can contain 200K+ small files.

**Mitigation**: Batch size of 500 records caps in-memory accumulation. BFS processes one directory at a time. Add `node_modules`, `.git`, `__pycache__` to default skip list.

---

### Risk 3: ffmpeg not available

**Problem**: Video thumbnail extraction requires `ffmpeg` as a system binary.

**Mitigation**: Check `shutil.which("ffmpeg")` at startup. If absent, show warning and fall back to category icon. Do NOT bundle ffmpeg. Suggest `brew install ffmpeg` in startup logs.

---

### Risk 4: Symlink cycles

**Problem**: Symlinks to ancestor directories create infinite BFS loops.

**Mitigation**: Track visited real paths in a `set[str]` using `os.path.realpath()`. Skip `is_dir(follow_symlinks=True)` entries where `follow_symlinks=False` differs. Record symlinks to directories as files in the DB.

---

### Risk 5: D3 treemap rendering 1M nodes causing browser freeze

**Problem**: Naive full-tree return from backend would freeze the browser tab.

**Mitigation**: `GET /tree` bounded by depth=3. Backend prunes nodes below `totalSize * 0.0001`. Frontend additionally prunes cells < 2px × 2px. Lazy subtree loading on zoom.

---

### Risk 6: send2trash failures on external/network volumes

**Problem**: `send2trash` fails on volumes without `.Trashes`.

**Mitigation**: Catch `TrashPermissionError` and `OSError` per-file. Add failed items to `DeletionResult.failed`. Show in UI with option to delete permanently — requiring explicit user confirmation. Never silently fall back.

---

### Risk 7: Port 8000 already in use

**Mitigation**: Catch `OSError: Address already in use` on startup, print clear message. Support `--port` CLI argument and `PORT` in `config.json`. Makefile passes `PORT` env var to uvicorn.

---

### Risk 8: Scanning system directories

**Problem**: Scanning `/` or `/System` causes thousands of `PermissionError` exceptions and noise.

**Mitigation**: Default `skip_paths` list for well-known system directories. Display count of permission-denied paths in scan summary with "View skipped paths" expandable. Never silently succeed.

---

### Risk 9: APFS file clones — misleading "freed space" after deletion

**Problem**: APFS file clones share underlying blocks. Deleting a clone may free 0 physical bytes.

**Mitigation**: Clearly label sizes in UI as "logical size". Add tooltip: "Actual freed disk space may differ due to APFS file cloning." Do not attempt to compute physical size (unreliable via user-space APIs).

---

## Project Directory Structure

```
disk_manager/
├── Makefile
├── requirements.txt
├── config.json                    # auto-created on first run
├── scans/                         # scan DB files: scan_20260410_143022.db
├── backend/
│   ├── main.py                    # FastAPI app, routes, lifespan
│   ├── scanner.py                 # BFS walker, SSE progress, adaptive depth
│   ├── db.py                      # SQLite schema, batch insert, queries
│   ├── query_engine.py            # Filter query builder, pagination
│   ├── preview_engine.py          # Thumbnail generation, caching
│   ├── deletion_engine.py         # send2trash, permanent delete, DB cleanup
│   ├── staging_manager.py         # Staging state, staging.json persistence
│   └── config_manager.py          # config.json read/write
└── frontend/
    ├── index.html
    ├── app.js                     # AppState, boot, routing
    ├── scan-panel.js
    ├── filter-bar.js
    ├── main-view.js
    ├── treemap.js                 # D3 treemap, zoom, breadcrumb
    ├── topn-list.js               # Virtualized top-N list
    ├── preview-panel.js           # Thumbnail, metadata, open-in-Finder
    ├── staging-panel.js           # Staging list, execute modal
    ├── config-panel.js
    └── styles.css
```
