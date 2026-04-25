"""Tests for TopicPrototypeRepo against a real migrated SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.embedding import EMBEDDING_DIMENSION
from prism.topic_prototype_repo import TopicPrototypeRepo, TopicPrototypeRow

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


TS = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = connect_sqlite(db_path, load_vec=True)
    try:
        yield conn
    finally:
        conn.close()


def _row(
    topic_id: str,
    exemplar_idx: int,
    *,
    model_version: str = "model@1",
    text: str | None = None,
) -> TopicPrototypeRow:
    return TopicPrototypeRow(
        topic_id=topic_id,
        exemplar_idx=exemplar_idx,
        text=text or f"text {topic_id} {exemplar_idx}",
        model_version=model_version,
        vector=np.full(EMBEDDING_DIMENSION, exemplar_idx, dtype=np.float32),
        created_at=TS,
    )


def _texts(conn: sqlite3.Connection, topic_id: str) -> list[str]:
    cursor = conn.execute(
        "SELECT text FROM topic_prototypes WHERE topic_id = ? ORDER BY exemplar_idx",
        (topic_id,),
    )
    try:
        return [str(row[0]) for row in cursor.fetchall()]
    finally:
        cursor.close()


def test_save_many_empty_returns_zero_and_count_is_zero(
    conn: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn)

    assert repo.save_many([]) == 0
    assert repo.count("model@1") == 0


def test_save_many_round_trips_count_for_one_topic(conn: sqlite3.Connection) -> None:
    repo = TopicPrototypeRepo(conn)

    written = repo.save_many([_row("history", idx) for idx in range(3)])

    assert written == 3
    assert repo.count("model@1") == 3


def test_save_many_replaces_prior_rows_for_same_topic_and_model(
    conn: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn)
    repo.save_many([_row("history", idx, text=f"old {idx}") for idx in range(3)])

    written = repo.save_many([_row("history", 0, text="new only")])

    assert written == 1
    assert repo.count("model@1") == 1
    assert _texts(conn, "history") == ["new only"]


def test_two_topics_accumulate_and_delete_independently(
    conn: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn)
    repo.save_many([_row("history", 0), _row("science", 0), _row("science", 1)])

    deleted = repo.delete_for_topic("history", "model@1")

    assert deleted == 1
    assert repo.count("model@1") == 2
    assert _texts(conn, "history") == []
    assert _texts(conn, "science") == ["text science 0", "text science 1"]


def test_count_filters_by_model_version(conn: sqlite3.Connection) -> None:
    repo = TopicPrototypeRepo(conn)
    repo.save_many(
        [
            _row("history", 0, model_version="model@1"),
            _row("history", 0, model_version="model@2"),
            _row("history", 1, model_version="model@2"),
        ]
    )

    assert repo.count("model@1") == 1
    assert repo.count("model@2") == 2


def test_save_many_rejects_row_key_delimiter_inputs(
    conn: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn)

    with pytest.raises(ValueError, match="topic_id"):
        repo.save_many([_row("bad\x1ftopic", 0)])
    with pytest.raises(ValueError, match="model_version"):
        repo.save_many([_row("history", 0, model_version="bad\x1fmodel")])
