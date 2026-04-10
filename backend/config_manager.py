"""Configuration manager for the disk management tool.

Reads and writes config.json located in the project root (parent of backend/).
"""

import json
from pathlib import Path

DEFAULT_CONFIG = {
    "top_n": 25,
    "max_depth": 10,
    "adaptive_scanning": True,
    "adaptive_threshold": 10000,
    "last_scan_paths": [],
    "default_sort": "size_desc",
    "thumbnail_size": [256, 256],
    "page_size": 100,
    "permanent_delete_default": False,
    "skip_dir_names": [
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        ".stack-work",
    ],
    "max_scan_sessions": 5,
}

# Keys that must be positive integers; mapped to their default values.
_POSITIVE_INT_KEYS = {
    "top_n": DEFAULT_CONFIG["top_n"],
    "max_depth": DEFAULT_CONFIG["max_depth"],
    "page_size": DEFAULT_CONFIG["page_size"],
    "adaptive_threshold": DEFAULT_CONFIG["adaptive_threshold"],
}


class ConfigManager:
    """Manages reading, writing, and validating application configuration."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = Path(config_path)
        self._config: dict = {}
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """Load config from disk, merge with defaults, validate, and return it.

        If the config file does not exist, write the defaults and return them.
        """
        if self._config_path.exists():
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Defaults fill in any missing keys.
            merged = {**DEFAULT_CONFIG, **data}
        else:
            merged = dict(DEFAULT_CONFIG)

        self._config = self._validate(merged)

        # Persist so the file always reflects the validated state.
        self.save()
        return dict(self._config)

    def save(self) -> None:
        """Write the current config to *config.json* with ``indent=2``."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(self._config, f, indent=2)

    def update(self, partial: dict) -> dict:
        """Merge *partial* into the current config, save, and return it."""
        self._config.update(partial)
        self._config = self._validate(self._config)
        self.save()
        return dict(self._config)

    def get(self, key: str, default=None):
        """Return the value for *key*, or *default* if it is not present."""
        return self._config.get(key, default)

    @property
    def config(self) -> dict:
        """Return a copy of the current configuration dictionary."""
        return dict(self._config)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(cfg: dict) -> dict:
        """Clamp invalid positive-int fields back to their defaults."""
        for key, default_value in _POSITIVE_INT_KEYS.items():
            try:
                if int(cfg[key]) <= 0:
                    cfg[key] = default_value
            except (TypeError, ValueError, KeyError):
                cfg[key] = default_value
        return cfg
