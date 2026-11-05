"""FastAPI application: routes, SSE progress, local-only security, static frontend.

Blocking work (SQLite queries, deletion, thumbnails) lives in plain ``def``
endpoints, which FastAPI runs in its threadpool, so the event loop (and every
SSE stream) stays responsive. Each request opens its own short-lived SQLite
connection on the WAL database; sqlite3 connections are thread-bound, and
opening one is far cheaper than any query we run.

Paths are overridable for tests and alternative installs via
``create_app(scans_dir=..., config_path=...)`` or the ``DISK_MANAGER_SCANS_DIR``
/ ``DISK_MANAGER_CONFIG`` environment variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Iterator
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from backend import categories, db
from backend.config_manager import ConfigManager, ConfigPatch
from backend.deletion_engine import execute_deletion
from backend.query_engine import SORT_MAP, FilterParams, query_files, row_to_item, summarize
from backend.scan_runner import ScanRun, terminal_frame
from backend.staging_manager import StagingManager, covered_by_staged_dir

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
SCANS_DIR = Path(os.environ.get("DISK_MANAGER_SCANS_DIR", PROJECT_ROOT / "scans"))
CONFIG_PATH = Path(os.environ.get("DISK_MANAGER_CONFIG", PROJECT_ROOT / "config.json"))

API = "/api/v1"

# ---------------------------------------------------------------------------
# Security: only answer requests addressed to localhost (blocks DNS rebinding),
# and refuse cross-origin mutations (blocks CSRF from other sites).
# ---------------------------------------------------------------------------

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _hostname(netloc_or_url: str, *, is_url: bool) -> str | None:
    try:
        return urlsplit(netloc_or_url if is_url else "//" + netloc_or_url).hostname
    except ValueError:
        return None


def is_local_host_header(host: str) -> bool:
    return _hostname(host, is_url=False) in _LOCAL_HOSTNAMES


def is_local_origin(origin: str) -> bool:
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and _hostname(origin, is_url=True) in _LOCAL_HOSTNAMES


class LocalOnlyMiddleware:
    """Pure ASGI middleware (no response buffering, so SSE is unaffected)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            reason = None
            if not is_local_host_header(headers.get("host", "")):
                reason = "Host header must be localhost or 127.0.0.1"
            elif scope["method"] in _MUTATING:
                origin = headers.get("origin")
                if origin is not None and not is_local_origin(origin):
                    reason = "Cross-origin request refused"
            if reason:
                await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ScanSession:
    """A scan DB on disk; hands out one short-lived connection per request."""

    def __init__(self, scan_id: str, db_path: Path) -> None:
        self.scan_id = scan_id
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA mmap_size = 268435456")
        try:
            yield conn
        finally:
            conn.close()


class AppState:
    def __init__(self, scans_dir: Path, config_path: Path) -> None:
        self.scans_dir = scans_dir
        self.config_path = config_path
        self.config: ConfigManager | None = None
        self.sessions: dict[str, ScanSession] = {}
        self.runners: dict[str, ScanRun] = {}

    def db_path(self, scan_id: str) -> Path:
        return self.scans_dir / f"{scan_id}.db"

    def load_sessions(self) -> None:
        """Open (and migrate) every scan DB; scans left 'running' by a dead server become 'interrupted'."""
        self.scans_dir.mkdir(parents=True, exist_ok=True)
        for db_file in sorted(self.scans_dir.glob("*.db")):
            try:
                conn = db.init_db(db_file)
                try:
                    with conn:
                        conn.execute("UPDATE scan_meta SET status = 'interrupted' WHERE status = 'running'")
                finally:
                    conn.close()
                self.sessions[db_file.stem] = ScanSession(db_file.stem, db_file)
            except Exception:
                logger.exception("Failed to load scan DB %s", db_file)

    def is_running(self, scan_id: str) -> bool:
        run = self.runners.get(scan_id)
        return run is not None and not run.finished

    def delete_scan_files(self, scan_id: str) -> None:
        self.sessions.pop(scan_id, None)
        self.runners.pop(scan_id, None)
        for suffix in (".db", ".db-wal", ".db-shm"):
            (self.scans_dir / f"{scan_id}{suffix}").unlink(missing_ok=True)
        try:
            from backend.preview_engine import clear_cache

            clear_cache(scan_id)
        except Exception:
            logger.exception("Failed to clear thumbnail cache for %s", scan_id)

    def prune(self, keep: int) -> None:
        """Delete the oldest finished scans so at most *keep* remain (running scans are never pruned)."""
        ids = sorted(self.sessions)  # scan ids start with a sortable timestamp
        excess = len(ids) - keep
        for scan_id in ids:
            if excess <= 0:
                break
            if not self.is_running(scan_id):
                self.delete_scan_files(scan_id)
                excess -= 1


async def _state(request: Request) -> AppState:
    return request.app.state.dm


State = Annotated[AppState, Depends(_state)]


def _session(state: AppState, scan_id: str) -> ScanSession:
    session = state.sessions.get(scan_id)
    if session is None:
        raise HTTPException(404, f"Scan {scan_id} not found")
    return session


def _parse_roots(raw: str | None) -> list[str]:
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return [str(r) for r in json.loads(raw)]
        except ValueError:
            pass
    return [r.strip() for r in raw.split(",") if r.strip()]


_META_FIELDS = ("started_at", "finished_at", "total_files", "total_dirs", "total_bytes", "error_msg")


def _scan_info(state: AppState, session: ScanSession) -> dict | None:
    """Contract shape for GET /scans; live progress overlaid while a scan runs."""
    run = state.runners.get(session.scan_id)
    row = None
    try:
        with session.connect() as conn:
            row = conn.execute("SELECT * FROM scan_meta WHERE scan_id = ?", (session.scan_id,)).fetchone()
    except sqlite3.Error:
        logger.exception("Failed reading scan_meta for %s", session.scan_id)
    if row is None and run is None:
        return None
    meta = dict(row) if row is not None else {}
    info = {
        "scan_id": session.scan_id,
        "roots": _parse_roots(meta.get("roots")) or (run.roots if run else []),
        "status": meta.get("status") or "running",
        **{k: meta.get(k) for k in _META_FIELDS},
    }
    info["started_at"] = info["started_at"] or (run.started_at if run else None)
    for k in ("total_files", "total_dirs", "total_bytes"):
        info[k] = info[k] or 0
    if run is not None and not run.finished:
        info["status"] = "running"
        ev = run.last_event or {}
        info["total_files"] = ev.get("files_scanned", 0)
        info["total_dirs"] = ev.get("dirs_scanned", 0)
        info["total_bytes"] = ev.get("bytes_seen", 0)
    return info


def _require_node(conn: sqlite3.Connection, path: str, status: int = 404) -> dict:
    node = db.get_node(conn, path)
    if node is None:
        raise HTTPException(status, "Path not found in scan data")
    return node


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    roots: list[str]
    max_depth: int | None = Field(None, ge=1, le=64)


class PathsRequest(BaseModel):
    paths: list[str]


class ExecuteRequest(BaseModel):
    permanent: bool = False


class OpenRequest(BaseModel):
    path: str
    reveal: bool = True


router = APIRouter(prefix=API)

# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def _validate_roots(raw_roots: list[str]) -> list[Path]:
    if not raw_roots or not any(r.strip() for r in raw_roots):
        raise HTTPException(400, "Provide at least one folder to scan")
    roots: list[Path] = []
    for raw in raw_roots:
        if not raw.strip():
            continue
        p = Path(raw.strip()).expanduser()
        if not p.exists():
            raise HTTPException(400, f"Folder does not exist: {raw}")
        if not p.is_dir():
            raise HTTPException(400, f"Not a folder: {raw}")
        roots.append(p.resolve())
    # Scanning /a and /a/b together would count /a/b twice: keep only the outermost.
    unique = {str(r) for r in roots}
    return [Path(r) for r in dict.fromkeys(map(str, roots)) if not covered_by_staged_dir(r, unique)]


@router.post("/scans")
async def create_scan(req: ScanRequest, state: State):
    from backend.scanner import scan_roots

    roots = _validate_roots(req.roots)
    cfg = state.config.config
    max_depth = req.max_depth or cfg["max_depth"]

    await asyncio.to_thread(state.prune, cfg["max_scan_sessions"] - 1)
    state.config.update(ConfigPatch(last_scan_paths=[r.strip() for r in req.roots if r.strip()]))

    now = time.time()
    # Sortable by start time down to the millisecond (prune relies on it).
    scan_id = f"scan_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{int(now * 1000) % 1000:03d}{uuid.uuid4().hex[:4]}"
    db_path = state.db_path(scan_id)
    await asyncio.to_thread(lambda: db.init_db(db_path).close())
    state.sessions[scan_id] = ScanSession(scan_id, db_path)

    run = ScanRun(scan_id, [str(r) for r in roots])
    state.runners[scan_id] = run
    run.start(
        scan_roots(
            scan_id=scan_id,
            roots=roots,
            max_depth=max_depth,
            adaptive=cfg["adaptive_scanning"],
            adaptive_threshold=cfg["adaptive_threshold"],
            db_path=db_path,
            skip_dir_names=cfg["skip_dir_names"],
            cancel_event=run.cancel_event,
        )
    )
    return {"scan_id": scan_id}


@router.get("/scans/{scan_id}/stream")
async def scan_stream(scan_id: str, state: State):
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    run = state.runners.get(scan_id)
    if run is not None:
        return StreamingResponse(run.subscribe(), media_type="text/event-stream", headers=headers)

    session = _session(state, scan_id)
    info = await asyncio.to_thread(_scan_info, state, session)
    if info is None:
        raise HTTPException(404, f"Scan {scan_id} has no metadata")
    if info["status"] in ("running", "interrupted") and not info.get("error_msg"):
        info["error_msg"] = "Scan was interrupted (server stopped before it finished)"

    async def once():
        yield terminal_frame(info)

    return StreamingResponse(once(), media_type="text/event-stream", headers=headers)


@router.post("/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: str, state: State):
    if not state.is_running(scan_id):
        _session(state, scan_id)
        raise HTTPException(409, "Scan is not running")
    state.runners[scan_id].cancel()
    return {"cancelled": scan_id}


@router.get("/scans")
def list_scans(state: State):
    infos = [i for s in list(state.sessions.values()) if (i := _scan_info(state, s))]
    return sorted(infos, key=lambda i: i["started_at"] or 0, reverse=True)


@router.get("/scans/{scan_id}")
def get_scan(scan_id: str, state: State):
    info = _scan_info(state, _session(state, scan_id))
    if info is None:
        raise HTTPException(404, "Scan metadata not found")
    return info


@router.delete("/scans/{scan_id}")
def delete_scan(scan_id: str, state: State):
    _session(state, scan_id)
    if state.is_running(scan_id):
        raise HTTPException(409, "Cancel the scan before deleting it")
    state.delete_scan_files(scan_id)
    return {"deleted": scan_id}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@router.get("/scans/{scan_id}/tree")
def get_tree(
    scan_id: str,
    state: State,
    root: str | None = None,
    depth: int = Query(2, ge=1, le=10),
    max_children: int = Query(60, ge=1, le=500),
):
    session = _session(state, scan_id)
    with session.connect() as conn:
        if root is None:
            row = conn.execute("SELECT roots FROM scan_meta WHERE scan_id = ?", (scan_id,)).fetchone()
            roots = _parse_roots(row["roots"] if row else None)
            if not roots:
                raise HTTPException(404, "Scan has no roots")
            root = roots[0]
        tree = db.get_tree_data(conn, root, depth, max_children)
    if not tree:
        raise HTTPException(404, "Path not found in scan data")
    return tree


def _effective_under(conn: sqlite3.Connection, scan_id: str, under: str | None) -> str | None:
    """Drop an ``under`` that is the scan's only root: the filter would match
    everything but steer SQLite onto the path index plus a full sort (~8x slower)."""
    if not under:
        return None
    row = conn.execute("SELECT roots FROM scan_meta WHERE scan_id = ?", (scan_id,)).fetchone()
    roots = _parse_roots(row["roots"] if row else None)
    return None if roots == [under.rstrip("/") or "/"] else under


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


_SORT_PATTERN = "^(" + "|".join(SORT_MAP) + ")$"


@router.get("/scans/{scan_id}/files")
def list_files(
    scan_id: str,
    state: State,
    under: str | None = None,
    categories: str | None = Query(None, description="Comma-separated, e.g. image,video"),
    extensions: str | None = Query(None, description="Comma-separated, e.g. .jpg,.png"),
    exclude_extensions: bool = False,
    min_size: int | None = None,
    max_size: int | None = None,
    mtime_before: float | None = None,
    mtime_after: float | None = None,
    atime_before: float | None = None,
    atime_after: float | None = None,
    is_dir: bool | None = None,
    name_contains: str | None = None,
    sort: str = Query("size_desc", pattern=_SORT_PATTERN),
    page: int = Query(0, ge=0),
    page_size: int | None = Query(None, ge=1, le=1000),
):
    session = _session(state, scan_id)
    filters = FilterParams(
        under=under or None,
        categories=_csv(categories),
        extensions=_csv(extensions),
        exclude_extensions=exclude_extensions,
        min_size=min_size,
        max_size=max_size,
        mtime_before=mtime_before,
        mtime_after=mtime_after,
        atime_before=atime_before,
        atime_after=atime_after,
        is_dir=is_dir,
        name_contains=name_contains or None,
        sort=sort,
        page=page,
        page_size=page_size or state.config.config["page_size"],
    )
    with session.connect() as conn:
        filters.under = _effective_under(conn, scan_id, filters.under)
        return query_files(conn, filters)


@router.get("/scans/{scan_id}/summary")
def get_summary(scan_id: str, state: State, under: str | None = None):
    session = _session(state, scan_id)
    with session.connect() as conn:
        return summarize(conn, _effective_under(conn, scan_id, under))


@router.get("/categories")
async def get_categories():
    return categories.as_json()


# ---------------------------------------------------------------------------
# Staging & deletion
# ---------------------------------------------------------------------------


@router.get("/scans/{scan_id}/staging")
def get_staging(scan_id: str, state: State):
    with _session(state, scan_id).connect() as conn:
        return StagingManager(conn).state()


@router.post("/scans/{scan_id}/staging/add")
def staging_add(scan_id: str, req: PathsRequest, state: State):
    with _session(state, scan_id).connect() as conn:
        return StagingManager(conn).add(req.paths)


@router.post("/scans/{scan_id}/staging/remove")
def staging_remove(scan_id: str, req: PathsRequest, state: State):
    with _session(state, scan_id).connect() as conn:
        return StagingManager(conn).remove(req.paths)


@router.delete("/scans/{scan_id}/staging")
def staging_clear(scan_id: str, state: State):
    with _session(state, scan_id).connect() as conn:
        return StagingManager(conn).clear()


@router.post("/scans/{scan_id}/staging/execute")
def staging_execute(scan_id: str, req: ExecuteRequest, state: State):
    """Trash (or permanently delete) every staged item.

    Paths that already vanished from disk are reported in ``failed`` with
    "no longer exists on disk", but are still dropped from the scan data and
    from staging (the DB was stale; retrying could never succeed).
    """
    session = _session(state, scan_id)
    if state.is_running(scan_id):
        raise HTTPException(409, "Wait for the scan to finish before deleting")
    with session.connect() as conn:
        staging = StagingManager(conn)
        items = staging.items()
        if not items:
            raise HTTPException(400, "Nothing is staged for deletion")
        sizes = {i["path"]: i["size"] for i in items}
        result = execute_deletion(list(sizes), req.permanent, sizes)
        gone = result.deleted + result.missing
        if gone:
            db.remove_deleted(conn, gone)
            staging.remove_subtrees(gone)
    return result.to_dict()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@router.get("/scans/{scan_id}/preview/thumbnail")
def get_thumbnail(scan_id: str, state: State, path: str):
    from backend.preview_engine import get_cached_or_generate

    with _session(state, scan_id).connect() as conn:
        _require_node(conn, path)
    size = tuple(state.config.config["thumbnail_size"])
    try:
        data, content_type = get_cached_or_generate(scan_id, Path(path), size)
    except ValueError as exc:  # scan id unsafe as a cache dir name
        raise HTTPException(400, str(exc))
    if data is None:
        raise HTTPException(404, "File no longer exists on disk")
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/scans/{scan_id}/preview/text")
def get_text(scan_id: str, state: State, path: str):
    from backend.preview_engine import get_text_preview

    with _session(state, scan_id).connect() as conn:
        node = _require_node(conn, path)
    if node["is_dir"]:
        raise HTTPException(415, "Directories have no text preview")
    if not os.path.isfile(path):
        raise HTTPException(404, "File no longer exists on disk")
    preview = get_text_preview(Path(path))
    if preview is None:
        raise HTTPException(415, "Not a text file")
    return preview


@router.get("/scans/{scan_id}/preview/meta")
def get_meta(scan_id: str, state: State, path: str):
    with _session(state, scan_id).connect() as conn:
        node = _require_node(conn, path)
    item = row_to_item(node)
    item["exists"] = os.path.lexists(path)
    return item


@router.post("/scans/{scan_id}/preview/open")
def open_path(scan_id: str, req: OpenRequest, state: State):
    with _session(state, scan_id).connect() as conn:
        _require_node(conn, req.path, status=403)
    if not os.path.lexists(req.path):
        raise HTTPException(404, "Path no longer exists on disk")
    if platform.system() == "Darwin":
        cmd = ["open", "-R", req.path] if req.reveal else ["open", req.path]
    else:
        cmd = ["xdg-open", str(Path(req.path).parent) if req.reveal else req.path]
    try:
        subprocess.run(cmd, check=True, timeout=10, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise HTTPException(500, f"Failed to open: {exc}")
    return {"success": True}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(state: State):
    return state.config.config


@router.patch("/config")
def update_config(patch: ConfigPatch, state: State):
    return state.config.update(patch)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


class NoCacheStaticFiles(StaticFiles):
    """Frontend files are revalidated on every load, so edited ES modules are never served stale."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    scans_dir: Path | None = None,
    config_path: Path | None = None,
    frontend_dir: Path | None = FRONTEND_DIR,
) -> FastAPI:
    state = AppState(Path(scans_dir or SCANS_DIR), Path(config_path or CONFIG_PATH))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.config = ConfigManager(state.config_path)
        await asyncio.to_thread(state.load_sessions)
        yield
        running = [r for r in state.runners.values() if not r.finished]
        for run in running:
            run.cancel()
        tasks = [r.task for r in running if r.task]
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    app = FastAPI(title="Disk Manager", lifespan=lifespan)
    app.state.dm = state
    app.add_middleware(LocalOnlyMiddleware)
    app.include_router(router)
    if frontend_dir is not None and frontend_dir.exists():
        app.mount("/", NoCacheStaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    return app


app = create_app()
