"""Tests for the prism HTTP API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from prism.api import _check_health, app
from prism.config import StorageConfig

if TYPE_CHECKING:
    import pytest


def test_health_endpoint_returns_healthy() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_health_response_has_expected_keys() -> None:
    body = TestClient(app).get("/health").json()
    for key in ("status", "message", "timestamp", "details"):
        assert key in body
    for key in ("journal_mode", "sqlite_vec", "duckdb_attach", "migration_head"):
        assert key in body["details"]


def test_health_timestamp_is_iso8601_utc() -> None:
    body = TestClient(app).get("/health").json()
    parsed = datetime.fromisoformat(body["timestamp"])
    assert parsed.tzinfo is not None


def test_check_health_returns_503_for_bad_config() -> None:
    bad = StorageConfig(
        sqlite_path=Path("/dev/null/does-not-exist/x.db"),
        sqlite_vec=True,
        duckdb_attach=True,
    )
    body, http_status = _check_health(bad)
    assert http_status == 503
    assert body["status"] == "unhealthy"


def test_health_returns_503_when_config_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prism import api

    def fake_load_config(_path: Path) -> object:
        raise FileNotFoundError("config.yaml missing")

    monkeypatch.setattr(api, "load_config", fake_load_config)
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert "config" in body["details"]


def test_check_health_marks_disabled_probes_as_ok(tmp_path: Path) -> None:
    storage = StorageConfig(
        sqlite_path=tmp_path / "h.db",
        sqlite_vec=False,
        duckdb_attach=False,
    )
    body, http_status = _check_health(storage)
    assert http_status == 200
    assert body["details"]["sqlite_vec"] == "disabled"
    assert body["details"]["duckdb_attach"] == "disabled"
