"""Tests for backend.config_manager."""

import json

import pytest
from pydantic import ValidationError

from backend.config_manager import DEFAULT_CONFIG, ConfigManager


def test_load_creates_default(tmp_path):
    cfg_path = tmp_path / "config.json"
    ConfigManager(cfg_path)
    assert json.loads(cfg_path.read_text()) == DEFAULT_CONFIG


def test_load_merges_defaults_and_drops_unknown_keys(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"top_n": 50, "stale_key": 1}))
    cfg = ConfigManager(cfg_path).config
    assert cfg["top_n"] == 50
    assert cfg["max_depth"] == DEFAULT_CONFIG["max_depth"]
    assert "stale_key" not in cfg


def test_invalid_values_on_disk_reset_to_default(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"top_n": -1, "page_size": 50}))
    cfg = ConfigManager(cfg_path).config
    assert cfg["top_n"] == DEFAULT_CONFIG["top_n"]
    assert cfg["page_size"] == 50


def test_update_persists(tmp_path):
    cfg_path = tmp_path / "config.json"
    result = ConfigManager(cfg_path).update({"top_n": 10, "page_size": 50})
    assert result["top_n"] == 10
    assert json.loads(cfg_path.read_text())["page_size"] == 50


@pytest.mark.parametrize("patch", [{"bogus": 1}, {"page_size": 0}, {"default_sort": "random"}])
def test_update_rejects_unknown_or_invalid(tmp_path, patch):
    cm = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ValidationError):
        cm.update(patch)
    assert cm.config == DEFAULT_CONFIG
