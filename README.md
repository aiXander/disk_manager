# Disk Manager

**Local-first disk space analyzer with an interactive web UI.**

Scan your folders, see where the space goes in a zoomable treemap, and reclaim it through a safe, staged deletion flow. Everything runs on your own machine at `localhost:8000`.

- **Fast, accurate scans.** On macOS each directory is read with `getattrlistbulk` across parallel threads, and sizes roll up during the walk. ~1M files take under 10 s on an M2. Sizes are bytes *on disk*, hardlinks count once, and firmlinks aren't double-counted. Results match `du`.
- **Nothing hidden.** `node_modules`, `.git`, `.venv` and folders past the depth limit are measured in full, shown as single blocks, and reported in the category bar.
- **Nested treemap** with folder frames, colour by file type, smooth drill-down, and keyboard navigation.
- **List view** of the current folder: sort, filter by type, size, age and name, and multi-select. Includes a "large and untouched" quick filter.
- **Previews.** Quick Look thumbnails for almost any file on macOS, plus text previews for code and documents.
- **Staged removal.** Mark items, review the reclaimable total, then move them to the Trash (Finder "Put Back" works). Permanent delete needs an explicit, typed confirmation.
- **Local-only by construction.** Requests from other hosts or origins are rejected, which blocks DNS rebinding. The UI has no CDN and no build step.

> Built for macOS (Apple Silicon). Linux works on a best-effort basis: the scanner falls back to `os.scandir`, and previews fall back to Pillow, PyMuPDF and ffmpeg.

![Disk Manager UI — zoomable treemap visualization](thumbnail.jpg)

## Quick start

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
make run              # uv sync + start on http://localhost:8000
make test             # run the test suite
PORT=9000 make run    # custom port
```

Optional: `ffmpeg` for video thumbnails off macOS (`brew install ffmpeg`).

Keyboard: `Return` opens a folder, `Backspace`/`Esc` goes up, `S` marks/unmarks, `R` reveals in Finder, `O` opens, `1`/`2` switch between map and list, `/` filters by name, and `?` shows all shortcuts.

## macOS notes

- **Full Disk Access.** Scanning Desktop, Documents, Downloads or Library may need TCC permission for your terminal. Unreadable folders are counted and skipped.
- **APFS clones and snapshots.** Sizes are allocated bytes, but cloned files share blocks, so the space actually freed can be smaller than shown. Local Time Machine snapshots can also hold on to deleted data until they expire.
- The thumbnail cache lives at `~/.cache/disk_manager/`.

## Development

Architecture, design decisions and invariants: [CLAUDE.md](CLAUDE.md) and [docs/reference/](docs/reference/). Scan benchmark: `uv run python scripts/bench_scan.py <dir>`.
