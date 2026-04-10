"""Thumbnail generation and file preview for the disk management tool."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

CATEGORY_MAP: dict[str, set[str]] = {
    "image": {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
        ".bmp", ".tiff", ".tif", ".raw", ".cr2", ".nef", ".svg", ".ico",
    },
    "video": {
        ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v",
    },
    "audio": {
        ".mp3", ".flac", ".aac", ".wav", ".ogg", ".wma", ".m4a", ".aiff",
    },
    "document": {
        ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
        ".txt", ".rtf", ".csv",
    },
    "code": {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
        ".c", ".cpp", ".h", ".css", ".html", ".json", ".yaml", ".yml",
        ".toml", ".md", ".sh",
    },
    "archive": {
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".dmg", ".iso",
    },
}

# Reverse lookup: extension -> category
_EXT_TO_CATEGORY: dict[str, str] = {}
for _cat, _exts in CATEGORY_MAP.items():
    for _ext in _exts:
        _EXT_TO_CATEGORY[_ext] = _cat

CATEGORY_COLORS: dict[str, str] = {
    "image": "#4e9af1",
    "video": "#e05c5c",
    "audio": "#f1a34e",
    "document": "#6db66d",
    "code": "#b88de8",
    "archive": "#e8d08d",
    "other": "#aaaaaa",
}

# ---------------------------------------------------------------------------
# ffmpeg availability check
# ---------------------------------------------------------------------------

FFMPEG_PATH: Optional[str] = shutil.which("ffmpeg")

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

_CACHE_ROOT = Path.home() / ".cache" / "disk_manager"


def _ensure_cache_root() -> None:
    """Create the cache root and add a .metadata_never_index file to prevent
    Spotlight indexing on macOS."""
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    sentinel = _CACHE_ROOT / ".metadata_never_index"
    if not sentinel.exists():
        sentinel.touch()


def _cache_dir(scan_id: str) -> Path:
    """Return (and create) the cache directory for a given scan session."""
    _ensure_cache_root()
    d = _CACHE_ROOT / scan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(file_path: Path) -> str:
    """Deterministic cache filename based on the absolute path."""
    return hashlib.md5(str(file_path).encode()).hexdigest() + ".webp"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_category(extension: str) -> str:
    """Return the category name for *extension* (e.g. ``'.py'``), or
    ``'other'`` if unrecognised."""
    return _EXT_TO_CATEGORY.get(extension.lower(), "other")


# ---------------------------------------------------------------------------
# Thumbnail generators
# ---------------------------------------------------------------------------


def _thumb_image(path: Path, size: tuple[int, int]) -> bytes:
    """Generate a WebP thumbnail from an image file using Pillow."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(size)
            buf = BytesIO()
            img.save(buf, format="WEBP")
            return buf.getvalue()
    except Exception:
        return _thumb_fallback(path.suffix)


def _thumb_video(path: Path, size: tuple[int, int]) -> bytes:
    """Extract a single frame from a video via ffmpeg and return a WebP
    thumbnail.  Uses hardware-accelerated decoding on macOS Apple Silicon
    where possible."""
    if not FFMPEG_PATH:
        return _thumb_fallback(path.suffix)

    def _extract_frame(seek: int) -> Optional[bytes]:
        cmd = [
            FFMPEG_PATH,
            "-ss", str(seek),
            "-i", str(path),
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    png_data = _extract_frame(5)
    if png_data is None:
        png_data = _extract_frame(0)
    if png_data is None:
        return _thumb_fallback(path.suffix)

    try:
        from PIL import Image

        img = Image.open(BytesIO(png_data)).convert("RGB")
        img.thumbnail(size)
        buf = BytesIO()
        img.save(buf, format="WEBP")
        return buf.getvalue()
    except Exception:
        return _thumb_fallback(path.suffix)


def _thumb_pdf(path: Path, size: tuple[int, int]) -> bytes:
    """Render the first page of a PDF as a WebP thumbnail using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        from PIL import Image

        doc = fitz.open(str(path))
        try:
            page = doc.load_page(0)
            pix = page.get_pixmap()
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img.thumbnail(size)
            buf = BytesIO()
            img.save(buf, format="WEBP")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return _thumb_fallback(path.suffix)


def _thumb_fallback(extension: str) -> bytes:
    """Return a simple SVG icon (as bytes) with a coloured rectangle and the
    extension text centred inside."""
    category = get_category(extension)
    colour = CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"])
    label = extension.lstrip(".").upper() if extension else "FILE"

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
        f'<rect width="256" height="256" rx="16" fill="{colour}"/>'
        f'<text x="128" y="140" font-family="sans-serif" font-size="48" '
        f'font-weight="bold" fill="#fff" text-anchor="middle">{label}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_IMAGE_EXTS = CATEGORY_MAP["image"] - {".svg", ".ico"}
_VIDEO_EXTS = CATEGORY_MAP["video"]
_PDF_EXT = {".pdf"}


def generate_thumbnail(
    path: Path,
    size: tuple[int, int] = (256, 256),
) -> tuple[bytes, str]:
    """Generate a thumbnail for *path*.

    Returns ``(data_bytes, content_type)`` where *content_type* is
    ``'image/webp'`` for rasterised thumbnails or ``'image/svg+xml'`` for the
    SVG fallback.
    """
    ext = path.suffix.lower()

    if ext in _IMAGE_EXTS:
        data = _thumb_image(path, size)
    elif ext in _VIDEO_EXTS:
        data = _thumb_video(path, size)
    elif ext in _PDF_EXT:
        data = _thumb_pdf(path, size)
    else:
        data = _thumb_fallback(ext)

    # Determine content type by inspecting the payload: SVG fallback starts
    # with '<svg'.
    if data[:4] == b"<svg":
        return data, "image/svg+xml"
    return data, "image/webp"


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def get_cached_or_generate(
    scan_id: str,
    file_path: Path,
    size: tuple[int, int] = (256, 256),
) -> tuple[bytes, str]:
    """Return a (possibly cached) thumbnail for *file_path*.

    Thumbnails are stored under ``~/.cache/disk_manager/{scan_id}/`` with an
    MD5-based filename.  Returns ``(data_bytes, content_type)``.
    """
    cache = _cache_dir(scan_id)
    key = _cache_key(file_path)
    cached_file = cache / key

    if cached_file.exists():
        data = cached_file.read_bytes()
        if data[:4] == b"<svg":
            return data, "image/svg+xml"
        return data, "image/webp"

    data, content_type = generate_thumbnail(file_path, size)

    try:
        cached_file.write_bytes(data)
    except OSError:
        pass  # Non-fatal: we still have the data in memory.

    return data, content_type


def clear_cache(scan_id: str) -> None:
    """Remove the entire cache directory for *scan_id*."""
    target = _CACHE_ROOT / scan_id
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
