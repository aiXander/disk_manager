"""FastAPI application — routes, SSE streaming, static files, lifespan."""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from backend import db
from backend.config_manager import ConfigManager
from backend.deletion_engine import execute_deletion
from backend.query_engine import (
    FilterParams,
    PaginatedResult,
    build_count_query,
    build_filter_query,
    get_top_n_query,
)
from backend.scanner import scan_roots
from backend.staging_manager import StagingManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCANS_DIR = PROJECT_ROOT / "scans"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# ---------------------------------------------------------------------------
# CSRF Origin-check middleware (appendix #7)
# ---------------------------------------------------------------------------

ALLOWED_ORIGINS = {"http://localhost", "http://127.0.0.1"}


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Reject mutation requests whose Origin header doesn't match localhost."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                # Strip port for comparison.
                origin_base = origin.rsplit(":", 1)[0] if ":" in origin else origin
                if origin_base not in ALLOWED_ORIGINS:
                    from starlette.responses import JSONResponse
                    return JSONResponse(
                        {"detail": "CSRF check failed: origin not allowed"},
                        status_code=403,
                    )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Application state helpers
# ---------------------------------------------------------------------------


class ScanSession:
    """Holds per-scan-session resources."""

    def __init__(self, scan_id: str, db_path: Path):
        self.scan_id = scan_id
        self.db_path = db_path
        self.conn = db.init_db(db_path)
        self.staging = StagingManager(self.conn)
        self.cancel_event: threading.Event | None = None


# Global state (populated during lifespan).
scan_sessions: dict[str, ScanSession] = {}
config_manager: ConfigManager | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config_manager
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    config_manager = ConfigManager(CONFIG_PATH)

    # Reload any existing scan DBs from disk.
    for db_file in sorted(SCANS_DIR.glob("*.db")):
        scan_id = db_file.stem
        try:
            session = ScanSession(scan_id, db_file)
            scan_sessions[scan_id] = session
        except Exception:
            logger.exception("Failed to load scan DB %s", db_file)

    yield

    for session in scan_sessions.values():
        try:
            session.conn.close()
        except Exception:
            pass
    scan_sessions.clear()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Disk Manager", lifespan=lifespan)

app.add_middleware(CSRFOriginMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_session(scan_id: str) -> ScanSession:
    session = scan_sessions.get(scan_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Scan session {scan_id} not found")
    return session


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    roots: list[str]
    max_depth: int | None = None
    adaptive: bool | None = None


class StagingAddRequest(BaseModel):
    paths: list[str]


class StagingRemoveRequest(BaseModel):
    paths: list[str]


class StagingExecuteRequest(BaseModel):
    permanent: bool = False


class OpenRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# 5.1 Scan Management
# ---------------------------------------------------------------------------


@app.post("/api/v1/scans")
async def create_scan(req: ScanRequest):
    cfg = config_manager.config
    max_depth = req.max_depth if req.max_depth is not None else cfg["max_depth"]
    adaptive = req.adaptive if req.adaptive is not None else cfg["adaptive_scanning"]

    scan_id = f"scan_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db_path = SCANS_DIR / f"{scan_id}.db"

    config_manager.update({"last_scan_paths": req.roots})

    # Prune old scans if over limit.
    max_sessions = cfg.get("max_scan_sessions", 5)
    existing = sorted(SCANS_DIR.glob("*.db"))
    while len(existing) >= max_sessions:
        oldest = existing.pop(0)
        old_id = oldest.stem
        if old_id in scan_sessions:
            scan_sessions[old_id].conn.close()
            del scan_sessions[old_id]
        oldest.unlink(missing_ok=True)

    return {
        "scan_id": scan_id,
        "db_path": str(db_path),
        "max_depth": max_depth,
        "adaptive": adaptive,
    }


@app.get("/api/v1/scans/{scan_id}/stream")
async def scan_stream(scan_id: str):
    """SSE endpoint — starts scan and streams ProgressEvent objects."""
    cfg = config_manager.config
    db_path = SCANS_DIR / f"{scan_id}.db"

    roots = [Path(p) for p in cfg.get("last_scan_paths", [])]
    if not roots:
        raise HTTPException(400, "No scan roots configured")

    max_depth = cfg["max_depth"]
    adaptive = cfg["adaptive_scanning"]
    adaptive_threshold = cfg["adaptive_threshold"]
    skip_dir_names = cfg.get("skip_dir_names", [])

    cancel_event = threading.Event()

    async def event_generator():
        async for event in scan_roots(
            scan_id=scan_id,
            roots=roots,
            max_depth=max_depth,
            adaptive=adaptive,
            adaptive_threshold=adaptive_threshold,
            db_path=db_path,
            skip_dir_names=skip_dir_names,
            cancel_event=cancel_event,
        ):
            data = json.dumps(asdict(event))
            event_type = "progress" if event.status == "running" else event.status
            yield f"event: {event_type}\ndata: {data}\n\n"

            if event.status in ("complete", "error", "cancelled"):
                if scan_id not in scan_sessions:
                    try:
                        session = ScanSession(scan_id, db_path)
                        scan_sessions[scan_id] = session
                    except Exception:
                        logger.exception("Failed to open session after scan")

    # Store cancel_event so the cancel endpoint can access it.
    # Create a temporary session placeholder for tracking.
    _pending_cancel_events[scan_id] = cancel_event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Track cancel events for in-progress scans.
_pending_cancel_events: dict[str, threading.Event] = {}


@app.post("/api/v1/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: str):
    """Cancel a running scan."""
    cancel_event = _pending_cancel_events.get(scan_id)
    if cancel_event is None:
        raise HTTPException(404, "No running scan found for this scan_id")
    cancel_event.set()
    _pending_cancel_events.pop(scan_id, None)
    return {"cancelled": scan_id}


@app.get("/api/v1/scans")
async def list_scans():
    results = []
    for sid, session in scan_sessions.items():
        try:
            row = session.conn.execute(
                "SELECT * FROM scan_meta WHERE scan_id = ?", (sid,)
            ).fetchone()
            if row:
                results.append(dict(row))
        except Exception:
            pass
    return results


@app.get("/api/v1/scans/{scan_id}")
async def get_scan(scan_id: str):
    session = _get_session(scan_id)
    row = session.conn.execute(
        "SELECT * FROM scan_meta WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Scan metadata not found")
    return dict(row)


@app.delete("/api/v1/scans/{scan_id}")
async def delete_scan(scan_id: str):
    session = scan_sessions.pop(scan_id, None)
    if session:
        session.conn.close()
    db_file = SCANS_DIR / f"{scan_id}.db"
    db_file.unlink(missing_ok=True)
    for suffix in (".db-wal", ".db-shm"):
        (SCANS_DIR / f"{scan_id}{suffix}").unlink(missing_ok=True)
    try:
        from backend.preview_engine import clear_cache
        clear_cache(scan_id)
    except Exception:
        pass
    return {"deleted": scan_id}


# ---------------------------------------------------------------------------
# 5.2 Tree & Visualization (scan_id no longer in nodes table)
# ---------------------------------------------------------------------------


@app.get("/api/v1/scans/{scan_id}/tree")
async def get_tree(
    scan_id: str,
    root: str | None = Query(None, description="Root path for the subtree"),
    depth: int = Query(3, ge=1, le=20),
):
    session = _get_session(scan_id)
    if root is None:
        row = session.conn.execute(
            "SELECT roots FROM scan_meta WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        if not row or not row["roots"]:
            raise HTTPException(404, "No roots found in scan metadata")
        roots = [r.strip() for r in row["roots"].split(",") if r.strip()]
        root = roots[0]
    tree = db.get_tree_data(session.conn, root, depth)
    if not tree:
        raise HTTPException(404, "Path not found in scan data")
    return tree


@app.get("/api/v1/scans/{scan_id}/top")
async def get_top_n(
    scan_id: str,
    n: int = Query(25, ge=1, le=1000),
    mode: str = Query("files", pattern="^(files|dirs)$"),
    sort: str = Query("size", pattern="^(size|mtime)$"),
):
    session = _get_session(scan_id)
    sql, params = get_top_n_query(n, mode, sort)
    rows = db.query_files(session.conn, sql, params)
    return rows


# ---------------------------------------------------------------------------
# 5.3 File Queries
# ---------------------------------------------------------------------------


@app.get("/api/v1/scans/{scan_id}/files")
async def query_files(
    scan_id: str,
    extensions: str = Query("", description="Comma-separated, e.g. .jpg,.mp4"),
    exclude_extensions: bool = Query(False),
    min_size: int | None = Query(None),
    max_size: int | None = Query(None),
    mtime_after: float | None = Query(None),
    mtime_before: float | None = Query(None),
    atime_after: float | None = Query(None),
    is_dir: bool | None = Query(None),
    name_contains: str | None = Query(None, description="Substring match on file name"),
    sort: str = Query("size_desc"),
    page: int = Query(0, ge=0),
    page_size: int | None = Query(None, ge=1, le=1000),
):
    session = _get_session(scan_id)
    cfg = config_manager.config

    ext_list = [e.strip() for e in extensions.split(",") if e.strip()] if extensions else []

    filters = FilterParams(
        extensions=ext_list,
        exclude_extensions=exclude_extensions,
        min_size=min_size,
        max_size=max_size,
        mtime_after=mtime_after,
        mtime_before=mtime_before,
        atime_after=atime_after,
        is_dir=is_dir,
        name_contains=name_contains,
        sort=sort,
        page=page,
        page_size=page_size or cfg["page_size"],
    )

    sql, params = build_filter_query(filters)
    items = db.query_files(session.conn, sql, params)

    count_sql, count_params = build_count_query(filters)
    row = session.conn.execute(count_sql, count_params).fetchone()
    total = row[0] if row else 0

    return PaginatedResult(
        items=items,
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    ).model_dump()


# ---------------------------------------------------------------------------
# 5.4 Staging & Deletion
# ---------------------------------------------------------------------------


@app.get("/api/v1/scans/{scan_id}/staging")
async def get_staging(scan_id: str):
    session = _get_session(scan_id)
    return session.staging.get_state().to_dict()


@app.post("/api/v1/scans/{scan_id}/staging/add")
async def staging_add(scan_id: str, req: StagingAddRequest):
    session = _get_session(scan_id)

    items = []
    for path in req.paths:
        row = session.conn.execute(
            "SELECT path, name, size, is_dir, extension FROM nodes WHERE path = ?",
            (path,),
        ).fetchone()
        if row:
            items.append({
                "path": row["path"],
                "name": row["name"],
                "size": row["size"],
                "is_dir": bool(row["is_dir"]),
                "extension": row["extension"],
            })

    state = session.staging.add_items(items)
    return state.to_dict()


@app.post("/api/v1/scans/{scan_id}/staging/remove")
async def staging_remove(scan_id: str, req: StagingRemoveRequest):
    session = _get_session(scan_id)
    state = session.staging.remove_items(req.paths)
    return state.to_dict()


@app.delete("/api/v1/scans/{scan_id}/staging")
async def staging_clear(scan_id: str):
    session = _get_session(scan_id)
    session.staging.clear()
    return session.staging.get_state().to_dict()


@app.post("/api/v1/scans/{scan_id}/staging/execute")
async def staging_execute(scan_id: str, req: StagingExecuteRequest):
    session = _get_session(scan_id)
    paths = session.staging.get_paths()
    if not paths:
        raise HTTPException(400, "No items staged for deletion")

    result = execute_deletion(paths, req.permanent)

    if result.deleted:
        db.delete_records(session.conn, result.deleted)
        db.update_parent_sizes(session.conn, result.deleted)

    session.staging.remove_items(result.deleted)
    return result.to_dict()


# ---------------------------------------------------------------------------
# 5.5 Preview (with path validation — appendix #6)
# ---------------------------------------------------------------------------


@app.get("/api/v1/scans/{scan_id}/preview/thumbnail")
async def get_thumbnail(
    scan_id: str,
    path: str = Query(..., description="Absolute path to the file"),
):
    try:
        from backend.preview_engine import get_cached_or_generate
    except ImportError:
        raise HTTPException(501, "Preview engine dependencies not installed")

    session = _get_session(scan_id)

    # Validate path exists in scan data.
    row = session.conn.execute(
        "SELECT path FROM nodes WHERE path = ?", (path,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Path not found in scan data")

    cfg = config_manager.config
    size = tuple(cfg.get("thumbnail_size", [256, 256]))
    data, content_type = get_cached_or_generate(scan_id, Path(path), size)
    if data is None:
        raise HTTPException(404, "Could not generate thumbnail")
    return Response(content=data, media_type=content_type)


@app.get("/api/v1/scans/{scan_id}/preview/meta")
async def get_preview_meta(
    scan_id: str,
    path: str = Query(..., description="Absolute path"),
):
    session = _get_session(scan_id)
    row = session.conn.execute(
        "SELECT * FROM nodes WHERE path = ?", (path,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Path not found in scan data")
    return dict(row)


@app.post("/api/v1/scans/{scan_id}/preview/open")
async def open_in_finder(scan_id: str, req: OpenRequest):
    session = _get_session(scan_id)

    # Validate path exists in scan data (appendix #6).
    row = session.conn.execute(
        "SELECT path FROM nodes WHERE path = ?", (req.path,)
    ).fetchone()
    if not row:
        raise HTTPException(403, "Path not in scan data — refusing to open arbitrary locations")

    if not Path(req.path).exists():
        raise HTTPException(404, "Path does not exist on disk")
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", "-R", req.path], check=True)
        else:
            subprocess.run(["xdg-open", str(Path(req.path).parent)], check=True)
        return {"success": True}
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"Failed to open: {exc}")


# ---------------------------------------------------------------------------
# 5.6 Configuration
# ---------------------------------------------------------------------------


@app.get("/api/v1/config")
async def get_config():
    return config_manager.config


@app.patch("/api/v1/config")
async def update_config(request: Request):
    body = await request.json()
    updated = config_manager.update(body)
    return updated


# ---------------------------------------------------------------------------
# Static files (frontend) — must be mounted last
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
