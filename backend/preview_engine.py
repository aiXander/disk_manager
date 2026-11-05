"""Thumbnails and text previews for the disk manager.

Thumbnail pipeline (first one that yields an image wins, else a tinted SVG icon):

- Plain raster images Pillow decodes natively (JPEG/PNG/GIF/TIFF/...): Pillow,
  with ``draft()`` for JPEG. Measured faster than Quick Look for these
  (~45 ms vs ~80 ms for a 5 MB JPEG, ~80 ms vs ~120 ms for a 6 MB PNG).
- PDF: PyMuPDF (~15 ms vs ~80 ms for Quick Look).
- Everything Quick Look can render (HEIC, RAW, PSD, video, audio artwork,
  Office/iWork, fonts, text): ``qlmanage -t`` on macOS. Video goes to Quick
  Look before ffmpeg because it is steadier on large camera files. Audio
  without embedded artwork (Quick Look's grey note) gets the icon instead.
- Off macOS, or when Quick Look fails: ffmpeg for video, the Pillow/PyMuPDF
  paths above, then the icon.

``qlmanage`` never exits when no thumbnailer exists for a file (unknown
types, archives, ``.app`` bundles, missing files), so it is only invoked for
an allowlist of extensions and always with a timeout.

Cache: ``~/.cache/disk_manager/{scan_id}/{key}.thumb``, where the key hashes
path + mtime + size + requested size, so an edited file regenerates. Writes
go through a temp file + ``os.replace`` so concurrent requests for the same
file never see a half-written thumbnail. Failures are cached too (as the
icon) so a type that times out in Quick Look is not retried on every request.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageOps

from backend.categories import CATEGORY_COLORS, CATEGORY_EXTENSIONS, category_of

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tools & limits
# ---------------------------------------------------------------------------

FFMPEG_PATH: Optional[str] = shutil.which("ffmpeg")
QLMANAGE_PATH: Optional[str] = (
    shutil.which("qlmanage") if sys.platform == "darwin" else None
)

QL_TIMEOUT_S = 8.0
FFMPEG_TIMEOUT_S = 15.0

# Pillow refuses images above 2x this (DecompressionBombError) and warns above
# it. 100 MP covers any camera; bigger files go to Quick Look, which subsamples.
MAX_IMAGE_PIXELS = 100_000_000
# Non-JPEG files above this size skip Pillow (a multi-GB TIFF would be decoded
# in full); JPEGs are fine because draft() decodes at 1/8 scale.
PILLOW_MAX_BYTES = 256 * 1024 * 1024

WEBP_QUALITY = 80

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

_PILLOW_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".ico", ".avif",
})
_PDF_EXTS = frozenset({".pdf"})
_VIDEO_EXTS = CATEGORY_EXTENSIONS["video"]
_AUDIO_EXTS = CATEGORY_EXTENSIONS["audio"]
_FONT_EXTS = frozenset({".ttf", ".otf", ".ttc", ".dfont", ".woff", ".woff2"})
# Types Quick Look has a thumbnailer for. Anything else would hang qlmanage
# until the timeout, so it gets the icon instead.
_QL_EXTS = (
    CATEGORY_EXTENSIONS["image"]
    | CATEGORY_EXTENSIONS["video"]
    | _AUDIO_EXTS  # album artwork; artless files fall back to the icon
    | CATEGORY_EXTENSIONS["document"]
    | _FONT_EXTS
)

SVG_TYPE = "image/svg+xml"
WEBP_TYPE = "image/webp"

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE_ROOT = Path.home() / ".cache" / "disk_manager"
_SAFE_SCAN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _scan_cache_dir(scan_id: str) -> Path:
    # scan_id ends up in a path we rmtree in clear_cache: never let ".." in.
    if not _SAFE_SCAN_ID.match(scan_id):
        raise ValueError(f"invalid scan id: {scan_id!r}")
    return _CACHE_ROOT / scan_id


def _ensure_cache_dir(scan_id: str) -> Path:
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    sentinel = _CACHE_ROOT / ".metadata_never_index"  # keep Spotlight out
    if not sentinel.exists():
        sentinel.touch()
    d = _scan_cache_dir(scan_id)
    d.mkdir(exist_ok=True)
    return d


def _cache_key(path: Path, st: os.stat_result, size: tuple[int, int]) -> str:
    raw = f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0{size[0]}x{size[1]}"
    return hashlib.md5(raw.encode("utf-8", "surrogateescape")).hexdigest()


def _content_type(data: bytes) -> str:
    return SVG_TYPE if data[:4] == b"<svg" else WEBP_TYPE


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def _to_webp(img, size: tuple[int, int]) -> bytes:
    """Downscale a PIL image to fit *size* and encode it as WebP."""
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = img.mode in ("LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        img = img.convert("RGBA" if has_alpha else "RGB")
    img.thumbnail(size)
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Thumbnailers: each returns WebP bytes or None on failure
# ---------------------------------------------------------------------------


def _thumb_pillow(path: Path, size: tuple[int, int], file_size: int) -> Optional[bytes]:
    ext = path.suffix.lower()
    if file_size > PILLOW_MAX_BYTES and ext not in (".jpg", ".jpeg"):
        return None
    try:
        with Image.open(path) as img:
            if img.format == "JPEG":
                img.draft("RGB", size)
            img = ImageOps.exif_transpose(img)
            return _to_webp(img, size)
    except Exception as exc:  # corrupt file, bomb, unsupported variant, ...
        log.debug("pillow thumbnail failed for %s: %s", path, exc)
        return None


def _thumb_pdf(path: Path, size: tuple[int, int]) -> Optional[bytes]:
    try:
        import fitz  # PyMuPDF (imported lazily: slow to load)

        with fitz.open(str(path)) as doc:
            if doc.needs_pass or doc.page_count == 0:
                return None
            page = doc.load_page(0)
            rect = page.rect
            zoom = min(size[0] / rect.width, size[1] / rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            return _to_webp(img, size)
    except Exception as exc:
        log.debug("pdf thumbnail failed for %s: %s", path, exc)
        return None


def _is_grayscale(img) -> bool:
    r, g, b = img.convert("RGB").split()
    return (
        ImageChops.difference(r, g).getextrema()[1] <= 8
        and ImageChops.difference(g, b).getextrema()[1] <= 8
    )


def _thumb_qlmanage(path: Path, size: tuple[int, int]) -> Optional[bytes]:
    if not QLMANAGE_PATH:
        return None
    with tempfile.TemporaryDirectory(prefix="dm-ql-") as out_dir:
        try:
            subprocess.run(
                [QLMANAGE_PATH, "-t", "-s", str(max(size)), "-o", out_dir, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=QL_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("qlmanage failed for %s: %s", path, exc)
            return None
        pngs = list(Path(out_dir).glob("*.png"))
        if not pngs:
            return None
        try:
            with Image.open(pngs[0]) as img:
                img.load()
                if path.suffix.lower() in _AUDIO_EXTS and _is_grayscale(img):
                    return None  # generic music-note placeholder, no artwork
                return _to_webp(img, size)
        except Exception as exc:
            log.debug("qlmanage output unreadable for %s: %s", path, exc)
            return None


def _thumb_ffmpeg(path: Path, size: tuple[int, int]) -> Optional[bytes]:
    if not FFMPEG_PATH:
        return None
    for seek in ("5", "0"):  # 5 s in skips black intros; 0 for short clips
        cmd = [
            FFMPEG_PATH, "-nostdin", "-loglevel", "error",
            "-ss", seek, "-i", str(path),
            "-frames:v", "1", "-an",
            "-vf", f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=FFMPEG_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("ffmpeg failed for %s: %s", path, exc)
            return None
        if result.returncode == 0 and result.stdout:
            try:
                with Image.open(BytesIO(result.stdout)) as img:
                    img.load()
                    return _to_webp(img, size)
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Fallback icons
# ---------------------------------------------------------------------------


def fallback_icon(extension: str, is_dir: bool = False) -> bytes:
    """SVG file icon tinted by category colour, labelled with the extension."""
    if is_dir:
        colour = CATEGORY_COLORS["other"]
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
            'width="256" height="256">'
            '<path d="M28 64a12 12 0 0 1 12-12h58l20 20h98a12 12 0 0 1 12 12v120'
            'a12 12 0 0 1-12 12H40a12 12 0 0 1-12-12z" '
            f'fill="{colour}" fill-opacity="0.25" stroke="{colour}" stroke-width="6"/>'
            "</svg>"
        ).encode("utf-8")

    category = category_of(extension) if extension else "other"
    colour = CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"])
    label = extension.lstrip(".").upper()[:5] if extension else ""
    font_size = {0: 0, 1: 64, 2: 64, 3: 56, 4: 46}.get(len(label), 38)
    text = (
        f'<text x="128" y="{170 + font_size // 3}" font-family="-apple-system, '
        f'Helvetica, Arial, sans-serif" font-size="{font_size}" font-weight="700" '
        f'fill="{colour}" text-anchor="middle">{escape(label)}</text>'
        if label
        else ""
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
        'width="256" height="256">'
        # page with a folded corner
        f'<path d="M56 20h104l56 56v148a12 12 0 0 1-12 12H56a12 12 0 0 1-12-12V32'
        f'a12 12 0 0 1 12-12z" fill="{colour}" fill-opacity="0.16" '
        f'stroke="{colour}" stroke-width="6" stroke-linejoin="round"/>'
        f'<path d="M160 20v44a12 12 0 0 0 12 12h44" fill="none" stroke="{colour}" '
        'stroke-width="6" stroke-linejoin="round"/>'
        f"{text}</svg>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def generate_thumbnail(
    path: Path, size: tuple[int, int] = (256, 256), file_size: int = 0
) -> tuple[bytes, str]:
    """Render a thumbnail for *path* (no caching). Never raises."""
    ext = path.suffix.lower()
    data: Optional[bytes] = None

    if ext in _PILLOW_EXTS:
        data = _thumb_pillow(path, size, file_size)
    elif ext in _PDF_EXTS:
        data = _thumb_pdf(path, size)

    if data is None and ext in _QL_EXTS:
        data = _thumb_qlmanage(path, size)

    if data is None and ext in _VIDEO_EXTS:
        data = _thumb_ffmpeg(path, size)

    if data is None:
        return fallback_icon(ext), SVG_TYPE
    return data, WEBP_TYPE


def get_cached_or_generate(
    scan_id: str,
    path: Path,
    size: tuple[int, int] = (256, 256),
) -> tuple[bytes | None, str]:
    """Return ``(bytes, content_type)`` for a thumbnail of *path*.

    content_type is ``image/webp`` or ``image/svg+xml`` (the category icon).
    Returns ``(None, "")`` if *path* no longer exists. Safe to call from
    several threads at once.
    """
    path = Path(path)
    size = (int(size[0]), int(size[1]))
    try:
        st = path.stat()
    except OSError:
        return None, ""
    if path.is_dir():
        return fallback_icon("", is_dir=True), SVG_TYPE

    cache_dir = _ensure_cache_dir(scan_id)
    cached = cache_dir / f"{_cache_key(path, st, size)}.thumb"
    try:
        data = cached.read_bytes()
        if data:
            return data, _content_type(data)
    except OSError:
        pass

    data, content_type = generate_thumbnail(path, size, st.st_size)
    try:
        _atomic_write(cached, data)
    except OSError as exc:  # non-fatal: serve from memory
        log.debug("could not cache thumbnail for %s: %s", path, exc)
    return data, content_type


def clear_cache(scan_id: str) -> None:
    """Remove every cached thumbnail of *scan_id*."""
    target = _scan_cache_dir(scan_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


# ---------------------------------------------------------------------------
# Text preview
# ---------------------------------------------------------------------------

# Bytes that never appear in real text: C0 controls except \b \t \n \f \r and
# ESC (ANSI colours in logs), plus DEL.
_CONTROL_BYTES = bytes(b for b in range(32) if b not in (8, 9, 10, 12, 13, 27)) + b"\x7f"
_BINARY_CONTROL_RATIO = 0.05
_UTF16_BOMS = ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def _decode_utf8(data: bytes, truncated: bool) -> Optional[str]:
    # A cut at max_bytes may split a multi-byte character: drop up to 3 bytes.
    for trim in range(4 if truncated else 1):
        try:
            return data[: len(data) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def get_text_preview(path: Path, max_bytes: int = 65536) -> dict | None:
    """First *max_bytes* of a text file as ``{text, truncated, encoding}``.

    Tries UTF-8 (BOM stripped), then latin-1; UTF-16 is honoured when it has a
    BOM. Returns None for binary content (NUL bytes or many control
    characters), directories and unreadable files.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError:
        return None
    truncated = len(data) > max_bytes
    data = data[:max_bytes]

    for bom, encoding in _UTF16_BOMS:
        if data.startswith(bom):
            body = data[2 : len(data) - (len(data) % 2)]
            text = body.decode(encoding, errors="replace")
            return {"text": text, "truncated": truncated, "encoding": encoding}
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    if b"\x00" in data:
        return None
    controls = len(data) - len(data.translate(None, _CONTROL_BYTES))
    if data and controls / len(data) > _BINARY_CONTROL_RATIO:
        return None

    text = _decode_utf8(data, truncated)
    if text is not None:
        return {"text": text, "truncated": truncated, "encoding": "utf-8"}
    return {"text": data.decode("latin-1"), "truncated": truncated, "encoding": "latin-1"}
