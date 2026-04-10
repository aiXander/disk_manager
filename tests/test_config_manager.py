"""Tests for backend.config_manager.ConfigManager."""

import json

from backend.config_manager import ConfigManager, DEFAULT_CONFIG


def test_load_creates_default(tmp_path):
    """When no config file exists, load() creates one with all defaults."""
    cfg_path = tmp_path / "config.json"
    cm = ConfigManager(cfg_path)

    assert cfg_path.exists()
    on_disk = json.loads(cfg_path.read_text())
    for key, value in DEFAULT_CONFIG.items():
        assert on_disk[key] == value


def test_load_merges_defaults(tmp_path):
    """A partial config file gets missing keys filled from DEFAULT_CONFIG."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"top_n": 50}))

    cm = ConfigManager(cfg_path)
    cfg = cm.config

    # Explicitly set key is preserved.
    assert cfg["top_n"] == 50
    # Missing keys fall back to defaults.
    assert cfg["max_depth"] == DEFAULT_CONFIG["max_depth"]
    assert cfg["adaptive_scanning"] == DEFAULT_CONFIG["adaptive_scanning"]
    assert cfg["skip_dir_names"] == DEFAULT_CONFIG["skip_dir_names"]


def test_update(tmp_path):
    """update() merges new keys, saves, and returns the updated config."""
    cfg_path = tmp_path / "config.json"
    cm = ConfigManager(cfg_path)

    result = cm.update({"top_n": 10, "page_size": 50})

    assert result["top_n"] == 10
    assert result["page_size"] == 50

    # Verify it was persisted to disk.
    on_disk = json.loads(cfg_path.read_text())
    assert on_disk["top_n"] == 10
    assert on_disk["page_size"] == 50


def test_validation_clamps(tmp_path):
    """Negative positive-int values are clamped back to their defaults."""
    cfg_path = tmp_path / "config.json"
    cm = ConfigManager(cfg_path)

    result = cm.update({"top_n": -1})
    assert result["top_n"] == DEFAULT_CONFIG["top_n"]  # 25


def test_get(tmp_path):
    """get() returns the correct value for an existing key and default for missing."""
    cfg_path = tmp_path / "config.json"
    cm = ConfigManager(cfg_path)

    assert cm.get("top_n") == DEFAULT_CONFIG["top_n"]
    assert cm.get("nonexistent_key") is None
    assert cm.get("nonexistent_key", 42) == 42
