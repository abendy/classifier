"""Tests for TopicPrototypeRepo against a real migrated SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest

from prism.embedding import EMBEDDING_DIMENSION
from prism.topic_prototype_repo import TopicPrototypeRepo, TopicPrototypeRow

if TYPE_CHECKING:
    import sqlite3


TS = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


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
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)

    assert repo.save_many([]) == 0
    assert repo.count("model@1") == 0


def test_save_many_round_trips_count_for_one_topic(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)

    written = repo.save_many([_row("history", idx) for idx in range(3)])

    assert written == 3
    assert repo.count("model@1") == 3


def test_save_many_replaces_prior_rows_for_same_topic_and_model(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    repo.save_many([_row("history", idx, text=f"old {idx}") for idx in range(3)])

    written = repo.save_many([_row("history", 0, text="new only")])

    assert written == 1
    assert repo.count("model@1") == 1
    assert _texts(conn_with_vec, "history") == ["new only"]


def test_two_topics_accumulate_and_delete_independently(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    repo.save_many([_row("history", 0), _row("science", 0), _row("science", 1)])

    deleted = repo.delete_for_topic("history", "model@1")

    assert deleted == 1
    assert repo.count("model@1") == 2
    assert _texts(conn_with_vec, "history") == []
    assert _texts(conn_with_vec, "science") == ["text science 0", "text science 1"]


def test_count_filters_by_model_version(conn_with_vec: sqlite3.Connection) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    repo.save_many(
        [
            _row("history", 0, model_version="model@1"),
            _row("history", 0, model_version="model@2"),
            _row("history", 1, model_version="model@2"),
        ]
    )

    assert repo.count("model@1") == 1
    assert repo.count("model@2") == 2


def test_list_vectors_for_model_filters_by_model_version(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    repo.save_many(
        [
            _row("history", 0, model_version="model@1"),
            _row("science", 0, model_version="model@2"),
            _row("science", 1, model_version="model@2"),
        ]
    )

    rows = repo.list_vectors_for_model("model@2")

    assert {(row.topic_id, row.exemplar_idx) for row in rows} == {
        ("science", 0),
        ("science", 1),
    }
    assert {row.model_version for row in rows} == {"model@2"}


def test_list_vectors_for_model_round_trips_vector_bytes(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    vector = np.arange(EMBEDDING_DIMENSION, dtype=np.float32)
    repo.save_many(
        [
            TopicPrototypeRow(
                topic_id="history",
                exemplar_idx=0,
                text="known vector",
                model_version="model@1",
                vector=vector,
                created_at=TS,
            )
        ]
    )

    rows = repo.list_vectors_for_model("model@1")

    assert len(rows) == 1
    assert rows[0].vector.shape == (EMBEDDING_DIMENSION,)
    assert rows[0].vector.tobytes() == vector.tobytes()


def test_get_descriptions_returns_only_exemplar_zero_for_requested_model(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    repo.save_many(
        [
            _row("history", 0, text="History description."),
            _row("history", 1, text="History exemplar."),
            _row("science", 0, text="Science description."),
            _row("arts", 0, text="Arts description."),
            _row("science", 0, model_version="model@2", text="Other model."),
        ]
    )

    descriptions = repo.get_descriptions(["history", "science", "missing"], "model@1")

    assert descriptions == {
        "history": "History description.",
        "science": "Science description.",
    }


def test_save_many_rejects_row_key_delimiter_inputs(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)

    with pytest.raises(ValueError, match="topic_id"):
        repo.save_many([_row("bad\x1ftopic", 0)])
    with pytest.raises(ValueError, match="model_version"):
        repo.save_many([_row("history", 0, model_version="bad\x1fmodel")])
