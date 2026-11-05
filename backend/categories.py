"""Single source of truth for file-type categories.

Extensions are lowercase with a leading dot (as stored in ``nodes.extension``).
The frontend fetches this mapping via ``GET /api/v1/categories`` instead of
keeping its own copy.
"""

from __future__ import annotations

CATEGORY_EXTENSIONS: dict[str, frozenset[str]] = {
    "image": frozenset({
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff",
        ".tif", ".ico", ".heic", ".heif", ".raw", ".cr2", ".cr3", ".nef",
        ".arw", ".dng", ".psd", ".avif",
    }),
    "video": frozenset({
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".prores",
    }),
    "audio": frozenset({
        ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus",
        ".aiff", ".aif", ".alac", ".mid", ".midi",
    }),
    "document": frozenset({
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
        ".rtf", ".csv", ".odt", ".ods", ".odp", ".epub", ".md", ".pages",
        ".numbers", ".key", ".tex",
    }),
    "code": frozenset({
        ".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".java", ".c",
        ".cpp", ".cc", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
        ".swift", ".kt", ".sh", ".bash", ".zsh", ".html", ".css", ".scss",
        ".json", ".xml", ".yaml", ".yml", ".toml", ".sql", ".r", ".ipynb",
        ".lua", ".vue", ".svelte",
    }),
    "archive": frozenset({
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar",
        ".dmg", ".iso", ".pkg", ".deb", ".rpm", ".jar", ".whl",
    }),
    "data": frozenset({
        ".db", ".sqlite", ".sqlite3", ".parquet", ".arrow", ".feather",
        ".h5", ".hdf5", ".npy", ".npz", ".pt", ".pth", ".ckpt",
        ".safetensors", ".onnx", ".gguf", ".bin", ".pkl", ".pickle",
        ".tfrecord", ".lmdb", ".log",
    }),
}

CATEGORY_COLORS: dict[str, str] = {
    "image": "#4e9af1",
    "video": "#e05c5c",
    "audio": "#f1a34e",
    "document": "#6db66d",
    "code": "#b88de8",
    "archive": "#e8d08d",
    "data": "#4ec9c0",
    "other": "#8a8f98",
}

EXT_TO_CATEGORY: dict[str, str] = {
    ext: cat for cat, exts in CATEGORY_EXTENSIONS.items() for ext in exts
}


def category_of(extension: str) -> str:
    """Return the category for an extension like ``".jpg"`` (case-insensitive)."""
    return EXT_TO_CATEGORY.get(extension.lower(), "other")


def extensions_for(categories: list[str]) -> list[str]:
    """Expand category names into a sorted list of their extensions."""
    out: set[str] = set()
    for cat in categories:
        out |= CATEGORY_EXTENSIONS.get(cat, frozenset())
    return sorted(out)


def as_json() -> dict:
    """Serializable mapping for the frontend."""
    return {
        "categories": {
            cat: {"color": CATEGORY_COLORS[cat], "extensions": sorted(exts)}
            for cat, exts in CATEGORY_EXTENSIONS.items()
        },
        "other_color": CATEGORY_COLORS["other"],
    }
