"""Unit tests for the developer smoke CLI."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from typer.testing import CliRunner

from prism.config import Config
from prism.dev_cli import dev_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


runner = CliRunner()


def _make_config(scraper_db: Path, classifier_db: Path) -> Config:
    return Config.model_validate(
        {
            "service": {
                "http_port": 3200,
                "manifest_path": "service.json",
            },
            "pipeline": {
                "embedding": {"model": "bge-large-en", "version": "1.5"},
                "topic": {
                    "model": "qwen2.5-7b-instruct",
                    "quantization": "q4_k_m",
                    "confidence_threshold": 0.55,
                    "retrieval_top_k": 5,
                },
                "tags": {"model": "qwen2.5-7b-instruct", "max_tags": 6},
                "sentiment_intent": {"bundled_with": "tags"},
            },
            "ingest": {
                "source": "x-sync-outbox",
                "scraper_db_path": scraper_db,
                "poll_interval_ms": 1000,
                "embedding_enabled": True,
            },
            "audit": {
                "phoenix": {"enabled": False, "local_url": "http://x"},
                "runs_retention_days": 90,
            },
            "storage": {
                "sqlite_path": classifier_db,
                "sqlite_vec": False,
                "duckdb_attach": False,
            },
            "jobs": {
                "backfill": {"batch_size": 200},
                "eval": {"holdout_ratio": 0.2},
                "dspy_compile": {"max_rounds": 5},
            },
        }
    )


@pytest.fixture
def classifier_db(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "prism.db"
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    yield db_path


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def test_seed_scraper_writes_four_tables(tmp_path: Path) -> None:
    scraper_db = tmp_path / "scraper.db"

    result = runner.invoke(
        dev_app, ["seed-scraper", "--path", str(scraper_db), "--rows", "2"]
    )

    assert result.exit_code == 0
    assert {"outbox", "tweets", "users", "media"} <= _table_names(scraper_db)
    assert _count(scraper_db, "outbox") == 2
    assert _count(scraper_db, "tweets") == 2


def test_seed_scraper_refuses_to_clobber_by_default(tmp_path: Path) -> None:
    scraper_db = tmp_path / "scraper.db"
    scraper_db.write_text("already here")

    result = runner.invoke(dev_app, ["seed-scraper", "--path", str(scraper_db)])

    assert result.exit_code == 1
    assert "--force" in result.stderr


def test_seed_scraper_force_overwrites(tmp_path: Path) -> None:
    scraper_db = tmp_path / "scraper.db"
    conn = sqlite3.connect(scraper_db)
    try:
        conn.execute("CREATE TABLE old_shape (id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        dev_app,
        ["seed-scraper", "--path", str(scraper_db), "--force", "--rows", "1"],
    )

    assert result.exit_code == 0
    assert "old_shape" not in _table_names(scraper_db)
    assert _count(scraper_db, "outbox") == 1


def test_ingest_once_runs_against_seeded_db_and_prints_stats(
    tmp_path: Path,
    classifier_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scraper_db = tmp_path / "scraper.db"
    seed = runner.invoke(
        dev_app, ["seed-scraper", "--path", str(scraper_db), "--rows", "1"]
    )
    assert seed.exit_code == 0
    monkeypatch.setattr(
        "prism.dev_cli.load_config",
        lambda _path: _make_config(scraper_db, classifier_db),
    )

    result = runner.invoke(dev_app, ["ingest-once", "--no-embed"])

    assert result.exit_code == 0
    assert "observed" in result.output
    assert "processed" in result.output
    assert "embedded" in result.output
    assert "run_id" in result.output
    assert _count(classifier_db, "content_envelopes") == 1


def test_ingest_once_fails_fast_on_bad_scraper_schema(
    tmp_path: Path,
    classifier_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scraper_db = tmp_path / "bad-scraper.db"
    conn = sqlite3.connect(scraper_db)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        "prism.dev_cli.load_config",
        lambda _path: _make_config(scraper_db, classifier_db),
    )

    result = runner.invoke(dev_app, ["ingest-once", "--no-embed"])

    assert result.exit_code != 0
    combined = result.output + result.stderr + repr(result.exception)
    assert "missing required table 'outbox'" in combined


def test_embed_prints_model_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEmbedder:
        def embed(self, text: str) -> np.ndarray[Any, np.dtype[np.float32]]:
            assert text == "hello world"
            return np.arange(1024, dtype=np.float32)

    monkeypatch.setattr(
        "prism.embedding.create_default_embedder", lambda: FakeEmbedder()
    )

    result = runner.invoke(dev_app, ["embed", "hello world"])

    assert result.exit_code == 0
    assert "bge-large-en@1.5" in result.output
    assert "1024" in result.output
    assert "First 8 components" in result.output


def test_embed_empty_text_exits_with_error() -> None:
    result = runner.invoke(dev_app, ["embed", ""])

    assert result.exit_code == 1
    assert "empty input" in result.stderr
