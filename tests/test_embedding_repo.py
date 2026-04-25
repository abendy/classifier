"""Tests for EmbeddingRepo against a real migrated SQLite DB."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.embedding import EMBEDDING_DIMENSION
from prism.embedding_repo import EmbeddingRepo, StoredEmbedding

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


TS = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Run alembic upgrade head against a tmp sqlite DB and yield
    a live connection with sqlite-vec loaded."""
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = connect_sqlite(db_path, load_vec=True)
    try:
        yield conn
    finally:
        conn.close()


def test_save_and_get_round_trips_the_vector(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    vector = np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
    repo.save(
        envelope_id="env-1",
        model_version="test@1",
        vector=vector,
        now=TS,
    )
    stored = repo.get("env-1", "test@1")
    assert stored is not None
    assert stored.envelope_id == "env-1"
    assert stored.model_version == "test@1"
    assert np.array_equal(stored.vector, vector)


def test_get_missing_pair_returns_none(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    assert repo.get("never-saved", "test@1") is None


def test_exists_toggles_false_to_true_across_a_save(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    assert repo.exists("env-1", "test@1") is False
    repo.save(
        envelope_id="env-1",
        model_version="test@1",
        vector=np.zeros(EMBEDDING_DIMENSION, dtype=np.float32),
        now=TS,
    )
    assert repo.exists("env-1", "test@1") is True


def test_save_upserts_when_called_twice_for_same_pair(
    conn: sqlite3.Connection,
) -> None:
    repo = EmbeddingRepo(conn)
    zeros = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    ones = np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
    repo.save(envelope_id="env-1", model_version="test@1", vector=zeros, now=TS)
    repo.save(envelope_id="env-1", model_version="test@1", vector=ones, now=TS)

    stored = repo.get("env-1", "test@1")
    assert stored is not None
    assert np.array_equal(stored.vector, ones)

    cursor = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE envelope_id = ?", ("env-1",)
    )
    try:
        (count,) = cursor.fetchone()
    finally:
        cursor.close()
    assert count == 1


def test_save_different_model_versions_coexist_for_same_envelope(
    conn: sqlite3.Connection,
) -> None:
    repo = EmbeddingRepo(conn)
    zeros = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    ones = np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
    repo.save(envelope_id="env-1", model_version="m1", vector=zeros, now=TS)
    repo.save(envelope_id="env-1", model_version="m2", vector=ones, now=TS)

    stored_m1 = repo.get("env-1", "m1")
    stored_m2 = repo.get("env-1", "m2")
    assert stored_m1 is not None
    assert stored_m2 is not None
    assert np.array_equal(stored_m1.vector, zeros)
    assert np.array_equal(stored_m2.vector, ones)

    cursor = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE envelope_id = ?", ("env-1",)
    )
    try:
        (count,) = cursor.fetchone()
    finally:
        cursor.close()
    assert count == 2


def test_save_rejects_wrong_dtype(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    bad = np.ones(EMBEDDING_DIMENSION, dtype=np.float64)
    with pytest.raises(ValueError, match="float32"):
        repo.save(envelope_id="env-1", model_version="test@1", vector=bad, now=TS)
    assert repo.exists("env-1", "test@1") is False


def test_save_rejects_wrong_shape(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    bad = np.zeros(512, dtype=np.float32)
    with pytest.raises(ValueError, match="shape"):
        repo.save(envelope_id="env-1", model_version="test@1", vector=bad, now=TS)
    assert repo.exists("env-1", "test@1") is False


def test_stored_embedding_is_frozen() -> None:
    stored = StoredEmbedding(
        envelope_id="env-1",
        model_version="test@1",
        created_at=TS.isoformat(),
        vector=np.zeros(EMBEDDING_DIMENSION, dtype=np.float32),
    )
    attr_name = "vector"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(stored, attr_name, np.ones(EMBEDDING_DIMENSION, dtype=np.float32))


def test_model_version_and_created_at_round_trip(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    repo.save(
        envelope_id="env-1",
        model_version="bge-large-en@1.5",
        vector=np.ones(EMBEDDING_DIMENSION, dtype=np.float32),
        now=TS,
    )
    stored = repo.get("env-1", "bge-large-en@1.5")
    assert stored is not None
    assert stored.model_version == "bge-large-en@1.5"
    assert stored.created_at == TS.isoformat()


def test_save_rejects_row_key_delimiter_in_inputs(conn: sqlite3.Connection) -> None:
    repo = EmbeddingRepo(conn)
    vector = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    with pytest.raises(ValueError, match="envelope_id"):
        repo.save(
            envelope_id="env\x1f-1",
            model_version="bge-large-en@1.5",
            vector=vector,
        )
    with pytest.raises(ValueError, match="model_version"):
        repo.save(
            envelope_id="env-1",
            model_version="bge\x1flarge-en@1.5",
            vector=vector,
        )
