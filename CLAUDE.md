# Disk Manager

## Mission

Local-first disk space analyzer for macOS (Linux best-effort). It scans folders fast, shows where the space goes (zoomable treemap, folder-scoped list, category breakdown), and reclaims it through a staged, Trash-first deletion flow. A single-user tool on the user's own Mac: FastAPI on `127.0.0.1` + a vanilla-JS UI with no build step. Nothing is deployed.

## Blast radius

This app **deletes real files** and can read anything the user can.
- **Ask first:** executing a staging deletion against anything except a throwaway folder you created; scanning or deleting with elevated rights; loosening `LocalOnlyMiddleware`, the Trash-first rule, or the path checks on `/preview/open`.
- **Free to do without asking:** run the tests, `ruff`, `fallow`, the benchmark (`scripts/bench_scan.py` is read-only), and read-only scans of any folder into a **throwaway** state dir (`DISK_MANAGER_SCANS_DIR=<tmp> DISK_MANAGER_CONFIG=<tmp>/config.json`). Create dummy trees under a temp dir to test deletion end to end.

## Documentation map

| Doc | Open it when |
|---|---|
| [docs/reference/scanning.md](docs/reference/scanning.md) | touching the scanner, the DB schema/columns (`size` vs `apparent_size`, `collapsed`), tree folding, `remove_deleted`, or scan performance |
| [docs/reference/api-and-safety.md](docs/reference/api-and-safety.md) | touching routes, the scan runner/SSE, staging, deletion, the local-only guard, config, or previews |
| [docs/reference/frontend.md](docs/reference/frontend.md) | touching anything in `frontend/`, or testing the UI in a browser |

Plans for future work go in `docs/TODO/`.

## Debugging priors

- **Sizes look too small** → suspect bytes hidden in `collapsed` dirs not being counted (the summary reports them separately), or `apparent_size` being shown where `size` was meant.
- **UI shows stale behaviour after an edit** → the server wasn't restarted (`make run` uses `--reload`; ad-hoc servers don't). Static files are served `no-cache`, so it isn't the browser cache.
- **A preview request hangs** → `qlmanage` was handed a type with no Quick Look thumbnailer (see the allowlist trap in api-and-safety.md).

## Repo layout

- `backend/` — FastAPI app (`main.py`), scan engine (`scanner.py`, `db.py`), `scan_runner.py`, query/staging/deletion/config/preview modules, `categories.py` (the only extension→category map).
- `frontend/` — `index.html`, `styles.css`, `js/` ES modules, `vendor/d3.v7.min.js`.
- `tests/` — pytest. `test_api.py` drives the whole app through `TestClient` on tmp dirs.
- `scripts/bench_scan.py` — scan benchmark (wall time, files/s, peak RSS).
- `scans/`, `config.json` — runtime state, gitignored.

## Load-bearing rules

1. **Never fall back from Trash to permanent delete**, and never compute freed bytes by re-walking the disk; use DB sizes. (api-and-safety.md)
2. **Sizes are never truncated** by `max_depth` or the skip-list: such dirs are walked fully and stored as one `collapsed` row. (scanning.md)
3. **The server owns staging.** The frontend renders whatever state the API returns and keeps no list of its own.
4. **Blocking work stays off the event loop**: SQLite, disk and thumbnailer endpoints are plain `def`, each with its own short-lived connection.
5. **One category map**: `backend/categories.py`, served to the UI by `GET /categories`. Don't add copies.
6. No `alert()`/`confirm()` in the frontend, and no build step.

## Definition of done

- `uv run python -m pytest tests/ -q` green; `uvx ruff check .` clean; `fallow` dead-code clean for frontend changes.
- Scanner changes: benchmark before/after with `scripts/bench_scan.py` on a real tree, and check the total against `du -sk`.
- UI changes: verified in a real browser against a running server.
- Doc pass done (the reference doc for what you touched is still true).

## Cross-cutting invariants

- `size` = allocated bytes (`st_blocks*512`, what deletion frees); `apparent_size` = logical. A hardlinked inode is counted once.
- Subtree filters use the path range `[dir + "/", dir + "0")`, never `LIKE`.
- The local-only guard (Host + Origin checks) is the only thing between a malicious web page and the user's files; keep it in front of every route.

## Commands

```bash
make run                     # uv sync + uvicorn --reload on 127.0.0.1:8000 (PORT=… to change)
make test                    # pytest
uvx ruff check .             # lint
fallow                       # frontend dead code / dupes (config: .fallowrc.json)
uv run python scripts/bench_scan.py ~/Documents --max-depth 10   # scan benchmark
```
