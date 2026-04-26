"""Tests for embedding topic catalogs into topic prototypes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from prism.embedding import EMBEDDING_DIMENSION, Embedder
from prism.topic_catalog import TopicCatalog
from prism.topic_loader import embed_catalog
from prism.topic_prototype_repo import TopicPrototypeRepo

if TYPE_CHECKING:
    import sqlite3


TS = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


class FakeBackend:
    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [
            np.full(EMBEDDING_DIMENSION, len(text), dtype=np.float32)
            for text in texts
        ]


def _catalog(exemplars: list[str] | None = None) -> TopicCatalog:
    return TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                    "exemplars": exemplars or ["Roman history.", "World War II."],
                }
            ],
        }
    )


def _topic_ids(conn: sqlite3.Connection) -> list[str]:
    cursor = conn.execute(
        "SELECT topic_id FROM topic_prototypes ORDER BY topic_id, exemplar_idx"
    )
    try:
        return [str(row[0]) for row in cursor.fetchall()]
    finally:
        cursor.close()


def test_embed_catalog_writes_description_plus_exemplars(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)

    summary = embed_catalog(
        _catalog(),
        embedder=Embedder(FakeBackend()),
        repo=repo,
        model_version="model@1",
        now=TS,
    )

    assert summary.prototypes_written == 3
    assert repo.count("model@1") == 3


def test_embed_catalog_flattens_and_embeds_subtopics_as_siblings(
    conn_with_vec: sqlite3.Connection,
) -> None:
    catalog = TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                    "exemplars": ["General history."],
                    "subtopics": [
                        {
                            "id": "history-rome",
                            "name": "Rome",
                            "description": "Roman history.",
                            "exemplars": ["The Principate."],
                        }
                    ],
                }
            ],
        }
    )
    repo = TopicPrototypeRepo(conn_with_vec)

    summary = embed_catalog(
        catalog,
        embedder=Embedder(FakeBackend()),
        repo=repo,
        model_version="model@1",
        now=TS,
    )

    assert summary.topics_processed == 2
    assert summary.prototypes_written == 4
    assert _topic_ids(conn_with_vec) == [
        "history",
        "history",
        "history-rome",
        "history-rome",
    ]


def test_embed_summary_reflects_catalog_model_and_counts(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)

    summary = embed_catalog(
        _catalog(["one"]),
        embedder=Embedder(FakeBackend()),
        repo=repo,
        model_version="model@2",
        now=TS,
    )

    assert summary.catalog_version == "topics@test"
    assert summary.model_version == "model@2"
    assert summary.topics_processed == 1
    assert summary.prototypes_written == 2


def test_embed_catalog_reembed_does_not_accumulate_stale_rows(
    conn_with_vec: sqlite3.Connection,
) -> None:
    repo = TopicPrototypeRepo(conn_with_vec)
    embedder = Embedder(FakeBackend())
    embed_catalog(
        _catalog(["first", "second"]),
        embedder=embedder,
        repo=repo,
        model_version="model@1",
        now=TS,
    )

    summary = embed_catalog(
        _catalog(["replacement"]),
        embedder=embedder,
        repo=repo,
        model_version="model@1",
        now=TS,
    )

    assert summary.prototypes_written == 2
    assert repo.count("model@1") == 2
