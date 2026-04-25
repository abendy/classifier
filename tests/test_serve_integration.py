"""End-to-end integration test for ``prism serve`` using the real default dense model.

Boots the lifespan against tmp scraper + classifier DBs and a
real ``Embedder`` constructed by ``create_default_embedder()``.
First run downloads ~1.2 GB; subsequent runs are warm.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest
import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from prism.db import connect_sqlite
from prism.embedding import MODEL_VERSION
from prism.embedding_repo import EmbeddingRepo
from prism.serve import lifespan

if TYPE_CHECKING:
    from pathlib import Path


_TS = "2026-04-19T11:00:00+00:00"
_SCRAPER_DDL = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL, available_at TEXT NOT NULL,
    dispatched_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE tweets (id TEXT PRIMARY KEY, text TEXT, author_id TEXT,
    created_at TEXT, lang TEXT, full_json TEXT, unavailable_at TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, username TEXT);
CREATE TABLE media (tweet_id TEXT, type TEXT, url TEXT, preview_image_url TEXT);
"""


def _seed_scraper(scraper_db: Path) -> None:
    conn = sqlite3.connect(scraper_db)
    try:
        conn.executescript(_SCRAPER_DDL)
        conn.execute(
            "INSERT INTO users (id, name, username) VALUES (?, ?, ?)",
            ("u-1", "Ada", "ada"),
        )
        conn.execute(
            "INSERT INTO tweets "
            "(id, text, author_id, created_at, lang, full_json, unavailable_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t-1", "hello world", "u-1", _TS, "en", None, None),
        )
        conn.execute(
            "INSERT INTO outbox (event_type, payload, created_at, available_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "bookmark.created",
                json.dumps({"entityType": "bookmark", "entityId": "t-1:root"}),
                _TS,
                _TS,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _migrate_classifier(classifier_db: Path) -> None:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{classifier_db}")
    command.upgrade(cfg, "head")


def _write_config(
    config_path: Path, scraper_db: Path, classifier_db: Path
) -> None:
    config_path.write_text(
        yaml.safe_dump(
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
                    "tags": {
                        "model": "qwen2.5-7b-instruct",
                        "max_tags": 6,
                    },
                    "sentiment_intent": {"bundled_with": "tags"},
                },
                "ingest": {
                    "source": "x-sync-outbox",
                    "scraper_db_path": str(scraper_db),
                    "poll_interval_ms": 100,
                    "embedding_enabled": True,
                },
                "audit": {
                    "phoenix": {"enabled": False, "local_url": "http://x"},
                    "runs_retention_days": 90,
                },
                "storage": {
                    "sqlite_path": str(classifier_db),
                    "sqlite_vec": True,
                    "duckdb_attach": False,
                },
                "jobs": {
                    "backfill": {"batch_size": 200},
                    "eval": {"holdout_ratio": 0.2},
                    "dspy_compile": {"max_rounds": 5},
                },
            }
        )
    )


def _wait_for_embed_run(classifier_db: Path, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        conn = sqlite3.connect(classifier_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE operation = 'embed' "
                "AND status = 'success'"
            ).fetchone()
            if row and row[0] >= 1:
                return
        finally:
            conn.close()
        time.sleep(0.2)
    pytest.fail("embed run row never reached success")


@pytest.mark.integration
def test_serve_lifespan_runs_real_ingest_and_embed_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scraper_db = tmp_path / "scraper.db"
    classifier_db = tmp_path / "prism.db"
    config_path = tmp_path / "config.yaml"

    _seed_scraper(scraper_db)
    _migrate_classifier(classifier_db)
    _write_config(config_path, scraper_db, classifier_db)

    monkeypatch.setattr("prism.serve.CONFIG_PATH", config_path)

    app = FastAPI()

    async def driver() -> None:
        async with lifespan(app):
            await asyncio.to_thread(
                _wait_for_embed_run, classifier_db, timeout_s=120.0
            )
            assert app.state.prism.task is not None
            assert not app.state.prism.task.done()

    asyncio.run(driver())

    # After lifespan exits, the loop has been shut down and connections
    # closed; verify the run + embedding state from a fresh reader.
    reader = sqlite3.connect(classifier_db)
    try:
        env_row = reader.execute(
            "SELECT id FROM content_envelopes WHERE source_id = ?", ("t-1",)
        ).fetchone()
        assert env_row is not None
        envelope_id = env_row[0]

        ingest_rows = reader.execute(
            "SELECT trace_id FROM runs WHERE operation = 'ingest' "
            "AND status = 'success'"
        ).fetchall()
        assert len(ingest_rows) >= 1
        for (trace_id,) in ingest_rows:
            assert trace_id is not None

        embed_rows = reader.execute(
            "SELECT trace_id, envelope_id FROM runs WHERE operation = 'embed' "
            "AND status = 'success'"
        ).fetchall()
        assert len(embed_rows) >= 1
        for trace_id, embed_envelope_id in embed_rows:
            assert trace_id is not None
            assert embed_envelope_id == envelope_id
    finally:
        reader.close()

    embedding_reader = connect_sqlite(classifier_db, load_vec=True)
    try:
        repo = EmbeddingRepo(embedding_reader)
        assert repo.exists(envelope_id, MODEL_VERSION)
    finally:
        embedding_reader.close()
