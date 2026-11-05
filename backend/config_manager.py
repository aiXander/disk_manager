"""Configuration manager for the disk management tool.

Reads and writes ``config.json`` (path supplied by the caller). The schema is a
pydantic model: values loaded from disk that fail validation fall back to their
defaults, unknown keys on disk are dropped, and ``ConfigPatch`` (used by
``PATCH /config``) rejects unknown keys outright.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

_DEFAULT_SKIP_DIRS = [
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    ".stack-work",
]

SORT_KEYS = ("size_desc", "size_asc", "mtime_desc", "mtime_asc", "name_asc", "name_desc", "atime_asc")


class Config(BaseModel):
    """Full, validated configuration."""

    model_config = ConfigDict(extra="ignore")

    top_n: int = Field(25, ge=1, le=1000)
    max_depth: int = Field(10, ge=1, le=64)
    adaptive_scanning: bool = True
    adaptive_threshold: int = Field(10000, ge=1)
    last_scan_paths: list[str] = []
    default_sort: str = Field("size_desc", pattern="^(" + "|".join(SORT_KEYS) + ")$")
    thumbnail_size: tuple[int, int] = (256, 256)
    page_size: int = Field(100, ge=1, le=1000)
    permanent_delete_default: bool = False
    skip_dir_names: list[str] = Field(default_factory=lambda: list(_DEFAULT_SKIP_DIRS))
    max_scan_sessions: int = Field(5, ge=1, le=100)


class ConfigPatch(BaseModel):
    """Partial update for ``PATCH /config``; unknown keys are a 422."""

    model_config = ConfigDict(extra="forbid")

    top_n: int | None = Field(None, ge=1, le=1000)
    max_depth: int | None = Field(None, ge=1, le=64)
    adaptive_scanning: bool | None = None
    adaptive_threshold: int | None = Field(None, ge=1)
    last_scan_paths: list[str] | None = None
    default_sort: str | None = Field(None, pattern="^(" + "|".join(SORT_KEYS) + ")$")
    thumbnail_size: tuple[int, int] | None = None
    page_size: int | None = Field(None, ge=1, le=1000)
    permanent_delete_default: bool | None = None
    skip_dir_names: list[str] | None = None
    max_scan_sessions: int | None = Field(None, ge=1, le=100)


DEFAULT_CONFIG: dict = Config().model_dump(mode="json")


def _coerce(data: dict) -> Config:
    """Build a Config from raw data, resetting each invalid field to its default."""
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        bad = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
        logger.warning("Invalid config values reset to defaults: %s", sorted(bad))
        return Config.model_validate({k: v for k, v in data.items() if k not in bad})


class ConfigManager:
    """Manages reading, writing, and validating application configuration."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = Path(config_path)
        self._config = Config()
        self.load()

    def load(self) -> dict:
        """Load config from disk (defaults fill gaps), persist the validated result."""
        data: dict = {}
        if self._config_path.exists():
            try:
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Unreadable %s; using defaults", self._config_path)
            if not isinstance(data, dict):
                data = {}
        self._config = _coerce(data)
        self.save()
        return self.config

    def save(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def update(self, partial: dict | ConfigPatch) -> dict:
        """Apply a validated partial update, save, and return the new config.

        Raises ``pydantic.ValidationError`` on unknown keys or invalid values.
        """
        patch = partial if isinstance(partial, ConfigPatch) else ConfigPatch.model_validate(partial)
        changes = patch.model_dump(exclude_unset=True, exclude_none=True)
        self._config = Config.model_validate({**self._config.model_dump(), **changes})
        self.save()
        return self.config

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    @property
    def config(self) -> dict:
        """A JSON-ready copy of the current configuration."""
        return self._config.model_dump(mode="json")
