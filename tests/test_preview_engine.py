"""Tests for backend.preview_engine (thumbnails, cache, text preview)."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import backend.preview_engine as pe
from backend.categories import CATEGORY_COLORS

SIZE = (128, 128)


@pytest.fixture(autouse=True)
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setattr(pe, "_CACHE_ROOT", root)
    return root


@pytest.fixture
def no_quicklook(monkeypatch):
    monkeypatch.setattr(pe, "QLMANAGE_PATH", None)


def _png(path: Path, size=(400, 300), color=(200, 30, 30)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _count_generations(monkeypatch) -> list[Path]:
    calls: list[Path] = []
    real = pe.generate_thumbnail

    def spy(path, size=(256, 256), file_size=0):
        calls.append(path)
        return real(path, size, file_size)

    monkeypatch.setattr(pe, "generate_thumbnail", spy)
    return calls


# ---------------------------------------------------------------------------
# Thumbnails & cache
# ---------------------------------------------------------------------------


def test_image_thumbnail_is_webp_within_requested_size(tmp_path, no_quicklook):
    img = tmp_path / "photo.jpg"
    Image.new("RGB", (1200, 800), (10, 120, 200)).save(img, quality=90)

    data, ctype = pe.get_cached_or_generate("s1", img, SIZE)

    assert ctype == "image/webp"
    thumb = Image.open(BytesIO(data))
    assert thumb.format == "WEBP"
    assert max(thumb.size) <= max(SIZE)


def test_second_request_is_served_from_cache(tmp_path, monkeypatch, no_quicklook):
    img = _png(tmp_path / "a.png")
    calls = _count_generations(monkeypatch)

    first = pe.get_cached_or_generate("s1", img, SIZE)
    second = pe.get_cached_or_generate("s1", img, SIZE)

    assert first == second
    assert len(calls) == 1


def test_changed_file_regenerates(tmp_path, monkeypatch, no_quicklook):
    img = _png(tmp_path / "a.png", color=(255, 0, 0))
    calls = _count_generations(monkeypatch)
    pe.get_cached_or_generate("s1", img, SIZE)

    _png(img, color=(0, 0, 255))
    st = img.stat()
    os.utime(img, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    data, _ = pe.get_cached_or_generate("s1", img, SIZE)

    assert len(calls) == 2
    r, g, b = Image.open(BytesIO(data)).convert("RGB").getpixel((5, 5))
    assert b > 200 and r < 50


def test_unknown_type_gets_extension_icon_without_quicklook(tmp_path, monkeypatch):
    # qlmanage hangs on types it has no thumbnailer for: it must not be called.
    def boom(*a, **k):
        raise AssertionError("subprocess must not run for unknown types")

    monkeypatch.setattr(pe.subprocess, "run", boom)
    f = tmp_path / "blob.zzz"
    f.write_bytes(b"\x00\x01\x02")

    data, ctype = pe.get_cached_or_generate("s1", f, SIZE)

    assert ctype == "image/svg+xml"
    assert b"ZZZ" in data
    assert CATEGORY_COLORS["other"].encode() in data


def test_icon_is_tinted_by_category():
    assert CATEGORY_COLORS["archive"].encode() in pe.fallback_icon(".zip")
    assert CATEGORY_COLORS["code"].encode() in pe.fallback_icon(".py")


def test_icon_escapes_extension_label():
    svg = pe.fallback_icon(".a<b").decode()
    assert "A&lt;B" in svg and "A<B" not in svg


def test_corrupt_image_falls_back_to_icon(tmp_path, no_quicklook):
    f = tmp_path / "broken.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")

    data, ctype = pe.get_cached_or_generate("s1", f, SIZE)

    assert ctype == "image/svg+xml"
    assert b"PNG" in data


def test_decompression_bomb_falls_back_to_icon(tmp_path, monkeypatch, no_quicklook):
    img = _png(tmp_path / "huge.png", size=(200, 200))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 200x200 > 2x limit

    _, ctype = pe.get_cached_or_generate("s1", img, SIZE)

    assert ctype == "image/svg+xml"


def test_missing_file_returns_none(tmp_path):
    assert pe.get_cached_or_generate("s1", tmp_path / "gone.jpg", SIZE) == (None, "")


def test_directory_gets_folder_icon(tmp_path):
    data, ctype = pe.get_cached_or_generate("s1", tmp_path, SIZE)
    assert ctype == "image/svg+xml" and data.startswith(b"<svg")


def test_concurrent_requests_for_same_file(tmp_path, cache_root, no_quicklook):
    img = _png(tmp_path / "a.png")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: pe.get_cached_or_generate("s1", img, SIZE), range(16)))

    assert len({r[0] for r in results}) == 1
    files = list((cache_root / "s1").iterdir())
    assert len(files) == 1 and files[0].suffix == ".thumb"


def test_clear_cache_removes_scan_dir(tmp_path, cache_root, no_quicklook):
    pe.get_cached_or_generate("s1", _png(tmp_path / "a.png"), SIZE)
    assert (cache_root / "s1").is_dir()

    pe.clear_cache("s1")

    assert not (cache_root / "s1").exists()


def test_clear_cache_rejects_path_traversal():
    with pytest.raises(ValueError):
        pe.clear_cache("..")


@pytest.mark.skipif(sys.platform != "darwin" or not pe.QLMANAGE_PATH, reason="macOS Quick Look only")
def test_quicklook_renders_thumbnail(tmp_path):
    img = _png(tmp_path / "ql.png", size=(600, 400))

    data = pe._thumb_qlmanage(img, SIZE)

    assert data is not None
    thumb = Image.open(BytesIO(data))
    assert thumb.format == "WEBP" and max(thumb.size) <= max(SIZE)


# ---------------------------------------------------------------------------
# Text preview
# ---------------------------------------------------------------------------


def test_text_preview_utf8(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("héllo wörld\n", encoding="utf-8")

    assert pe.get_text_preview(f) == {
        "text": "héllo wörld\n", "truncated": False, "encoding": "utf-8",
    }


def test_text_preview_latin1_fallback(tmp_path):
    f = tmp_path / "old.txt"
    f.write_bytes("café crème".encode("latin-1"))

    result = pe.get_text_preview(f)

    assert result["encoding"] == "latin-1"
    assert result["text"] == "café crème"


def test_text_preview_binary_returns_none(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"ELF\x00\x01\x02binary")
    assert pe.get_text_preview(f) is None


def test_text_preview_truncates_without_splitting_characters(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text("é" * 100, encoding="utf-8")  # 200 bytes, 2 per char

    result = pe.get_text_preview(f, max_bytes=51)  # cut inside a character

    assert result["truncated"] is True
    assert result["encoding"] == "utf-8"
    assert result["text"] == "é" * 25


def test_text_preview_utf16_with_bom(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes("hi there".encode("utf-16"))  # BOM + NUL-heavy body

    result = pe.get_text_preview(f)

    assert result["text"] == "hi there"
    assert result["encoding"].startswith("utf-16")


def test_text_preview_directory_returns_none(tmp_path):
    assert pe.get_text_preview(tmp_path) is None
