# Implementation Plan — Appendix: Review Feedback & Improvements

## Bugs & Structural Issues

### 1. Adaptive depth logic is applied mid-iteration
The BFS pseudocode checks `child_count > ADAPTIVE_THRESHOLD` inside the per-entry loop, so early children get full depth and late children get reduced depth. Fix: count entries first (or use parent's child count from a prior level), then decide depth uniformly for all children of that directory.

### 2. `scan_id` column is redundant if using per-session DB files
The schema creates one SQLite file per scan, but `scan_id` appears on every `nodes` row, in the foreign key, and in multiple composite indexes. This bloats storage and indexes on 1M+ row tables for no benefit. Either drop `scan_id` from `nodes` (and all its indexes) since the DB file already scopes the session, or switch to a single shared DB. Recommended: drop it from `nodes`.

### 3. Directory size rollup SQL is slow
The correlated subquery (`SELECT SUM(child.size) FROM nodes child WHERE child.parent_path = nodes.path`) runs per directory per depth level — O(depth × num_dirs). Better approach: accumulate sizes in a `dict[parent_path, size]` during the BFS walk itself (you already visit every node), then batch-update directory rows once at the end. Avoids expensive SQL entirely.

### 4. No scan cancellation
Add `POST /api/v1/scans/{scan_id}/cancel`. Use a `threading.Event` that the BFS loop checks after each batch. Without this, an accidental deep scan of `/Users` is unstoppable.

### 5. Move staging state from `staging.json` to SQLite
`staging.json` as a persistence mechanism is crash-unsafe and introduces a second serialization format. Add a `staged_items` table to the scan DB instead — it's atomic, crash-safe, and queryable. The in-memory representation can stay, just back it with SQL instead of JSON.

### 6. Path validation on `POST /preview/open`
The `open -R` endpoint accepts any path string. Validate that the requested path exists in the `nodes` table for the given `scan_id` before invoking `subprocess`. Prevents a rogue local request from revealing arbitrary Finder locations.

### 7. CSRF protection: use Origin header check
The plan recommends CSRF tokens but never specs them. Simpler and sufficient for localhost: add middleware that rejects mutation requests (POST/PATCH/DELETE) where the `Origin` header is present and doesn't match `http://localhost:{port}` or `http://127.0.0.1:{port}`.

## Missing Functionality

### 8. Add a name/path search filter
Users need to find files by name ("where are all my `.DS_Store` files?"). Add `name_contains` (LIKE-based) and/or `path_glob` parameters to `GET /files`. No new index needed — SQLite's existing `path` UNIQUE index covers prefix searches, and substring searches on 1M rows are still fast with LIKE.

### 9. Frontend error and loading states
`AppState` has no concept of error or loading. Add `loading` and `error` fields (per-panel or global) so the UI can show spinners, retry prompts, or error banners instead of silently failing on network errors or server crashes.

## Feature Ideas (post-MVP)

### 10. Duplicate file detection
High-value for a disk cleanup tool. Approach: group files by size, then compare via partial hash (first 4KB + last 4KB) to avoid full-file reads. Surface as a "Duplicates" tab alongside the treemap and top-N views.

### 11. Age-based heatmap toggle on treemap
Add a toggle to re-color treemap cells by last access time (red = untouched for years, green = recent) instead of file type. Directly answers "what can I safely delete?"

### 12. Drag-and-drop to staging panel
Support dragging items from both the treemap and list views into the staging panel. More natural for bulk selection than right-click context menus alone.
