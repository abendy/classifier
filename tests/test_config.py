"""Tests for the prism config model and loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from prism.config import Config, load_config


def _config_dict() -> dict:
    return yaml.safe_load(Path("config.yaml").read_text())


def test_load_config_happy_path() -> None:
    config = load_config(Path("config.yaml"))
    assert config.service.http_port == 3200


def test_missing_required_section_is_rejected() -> None:
    data = _config_dict()
    del data["pipeline"]
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_unknown_field_at_root_is_rejected() -> None:
    data = _config_dict()
    data["extra"] = "x"
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_unknown_field_in_nested_model_is_rejected() -> None:
    data = _config_dict()
    data["service"]["spurious"] = "x"
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_wrong_type_for_http_port_is_rejected() -> None:
    data = _config_dict()
    data["service"]["http_port"] = "3200"
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_manifest_path_round_trip() -> None:
    config = load_config(Path("config.yaml"))
    assert config.service.manifest_path == Path("./service.json")
