# HTTP API, deletion and safety

FastAPI app (`backend/main.py`, built by `create_app()`) serving `/api/v1/*` plus the static frontend. Three things here are dangerous or subtle: the scan lifecycle (background task + SSE fan-out), the delete path (it removes real files), and the local-only guard (this API can list and delete anything the user can). Previews live in `backend/preview_engine.py`.

## Where the code lives

- `backend/main.py` — routes, `LocalOnlyMiddleware`, `AppState` (sessions, config, runners), `NoCacheStaticFiles`. `create_app(scans_dir, config_path, frontend_dir)`; env overrides `DISK_MANAGER_SCANS_DIR` / `DISK_MANAGER_CONFIG` (tests and throwaway servers use these).
- `backend/scan_runner.py` — `ScanRun`: one asyncio task per scan; latest-snapshot fan-out to any number of SSE subscribers.
- `backend/query_engine.py` — `/files` + `/summary` SQL (sort whitelist, `under` subtree range, category expansion).
- `backend/staging_manager.py` — staging state lives only in the scan DB's `staged_items` table.
- `backend/deletion_engine.py` — Trash (`send2trash`) or permanent delete, per item.
- `backend/config_manager.py` — pydantic-validated `config.json`.
- `backend/preview_engine.py` — thumbnails (Pillow / PyMuPDF / Quick Look / ffmpeg / SVG icon) and text previews, cached under `~/.cache/disk_manager/<scan_id>/`.
- `tests/test_api.py` — end-to-end through `TestClient` on tmp dirs.

## Why it's built this way

- **POST starts, stream only subscribes.** The old design started the scan inside `GET /stream`, so an EventSource auto-reconnect (or a second tab) restarted it. Subscribers read the newest snapshot rather than a queue, so a slow tab never replays stale events, and a late subscriber gets the terminal event immediately (built from `scan_meta` for scans from an earlier server run).
- **Sync `def` endpoints + one short-lived SQLite connection per request.** Everything that touches SQLite, the disk or a thumbnailer runs in FastAPI's threadpool, so SSE never stalls behind a big query or a 10 GB Trash. No connection is shared across threads.
- **Server is the only staging source of truth.** The frontend never keeps its own list; every endpoint returns the full state.
- `under` equal to the scan's only root is dropped (`_effective_under`): that filter matches everything but steers SQLite onto the path index plus a full sort, about 8x slower.

## Invariants and traps

- **Never fall back from Trash to permanent delete.** A failed `send2trash` is reported as failed. Bytes freed come from DB sizes, never from re-walking the disk. A symlink to a dir removes only the link.
- **A staged path already gone from disk** is reported in `failed` ("no longer exists on disk") *and* removed from staging and the DB, since a retry can't succeed.
- Staging `total_size` skips items inside an already-staged dir; those items carry `covered: true`, and the tray dims them.
- **Local-only guard** (`LocalOnlyMiddleware`, pure ASGI so SSE isn't buffered): the Host header must be localhost / 127.0.0.1 / ::1 on any port (blocks DNS rebinding), and mutations with a foreign or `null` Origin get 403. Requests without an Origin (curl) are allowed.
- Scan ids are `scan_YYYYmmdd_HHMMSS_<ms><4hex>`: sortable by start time, so pruning (`max_scan_sessions`) removes the right one. Pruning never touches a running scan and deletes `-wal`, `-shm` and the thumbnail cache too. At startup, scans left `running` become `interrupted`.
- **Previews.** `qlmanage` **never exits** when macOS has no thumbnailer for a file (zip, unknown extension, `.app`, missing path), so it only runs for the `_QL_EXTS` allowlist, and every subprocess has a timeout. The cache key includes mtime + size, so an edited file regenerates. `scan_id` is validated against `^[A-Za-z0-9_-]+$` before any cache path is built: `clear_cache("..")` once meant `rm -rf ~/.cache`.
- 409 means "not while running": cancelling a scan that isn't running, deleting a running scan, or executing deletion during a scan.
