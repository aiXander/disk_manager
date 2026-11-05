# Frontend

Vanilla ES modules + D3 v7, no build step, served by FastAPI from `frontend/` (with `Cache-Control: no-cache`, so edits show up on reload). Three screens: home (pick folders, previous scans), live scan, and browse (treemap or list of one folder + category bar + details panel + removal tray). The current folder lives in the URL hash, so a reload returns to it. Each module's header comment states its role; start at `frontend/js/app.js` (boot, screen switching, keyboard shortcuts) and `frontend/js/browse.js` (navigation).

## Where the code lives

- `js/api.js` — the only place that calls `/api/v1`; errors carry the server's `detail`.
- `js/state.js` — shared state + tiny event bus. `state.staging` is only the last state the **server** returned.
- `js/treemap.js` — nested, cushion-shaded treemap (see below). `js/list.js` — `/files` with server-side filters, sort and infinite scroll.
- `js/catbar.js` — `/summary` breakdown; clicking a category filters list + dims map. Collapsed-folder bytes are a separate, non-clickable "Bundled folders" segment ([scanning.md](scanning.md) explains why).
- `js/staging.js` — removal tray + confirmation (Trash default; permanent needs a checkbox **and** typing `delete`).
- `js/ui.js` — toasts, modals, context menu, tooltip. **No `alert()`/`confirm()` anywhere**: they block browser automation and look bad.
- `vendor/d3.v7.min.js` — vendored on purpose (local-first, works offline).

## Why it's built this way

- **Treemap: fetch `depth=2`, then refine progressively.** Folder tiles still large on screen fetch their own `tree?depth=1` (cached, ≤90 fetches per view, 6 at a time) and draw inside themselves. A single deep fetch is too big; without refinement the top level was mostly grey blocks. Folded `is_other` nodes ("N smaller items") open the list on double-click; `collapsed` dirs draw as ribbed blocks you can't enter.
- Sizes are **decimal** (1 kB = 1000 B) to match Finder.
- The category→colour map comes from `GET /categories` (`backend/categories.py`); the frontend has no copy.

## Invariants and traps

- `fallow` needs `.fallowrc.json` (repo root) to see `js/app.js` and `styles.css` as entries, because they're loaded from `index.html`. Without it, every module reports as dead. `Treemap.clear/zoomInto/setSelected` are listed as used: they're called on an instance in `browse.js`.
- **Testing in Chrome:** a uvicorn started inside the Claude Code command sandbox is reachable by `curl` but **not** by the user's Chrome (the page shows an error and no request reaches the log). Start the server outside the sandbox, or have the user run it. Use a throwaway state dir (`DISK_MANAGER_SCANS_DIR`, `DISK_MANAGER_CONFIG`) and never execute a deletion against real files; scan a dummy folder for that.
