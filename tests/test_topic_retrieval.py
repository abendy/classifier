"""Unit tests for topic candidate retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from prism.embedding_repo import StoredEmbedding
from prism.topic_prototype_repo import StoredTopicPrototype
from prism.topic_retrieval import retrieve_top_k, retrieve_top_k_for_envelope


def _proto(
    topic_id: str,
    exemplar_idx: int,
    vector: list[float],
    *,
    text: str | None = None,
    model_version: str = "model@1",
) -> StoredTopicPrototype:
    return StoredTopicPrototype(
        topic_id=topic_id,
        exemplar_idx=exemplar_idx,
        text=text or f"{topic_id} exemplar {exemplar_idx}",
        model_version=model_version,
        vector=np.array(vector, dtype=np.float32),
    )


class FakeTopicRepo:
    def __init__(self, prototypes: list[StoredTopicPrototype]) -> None:
        self._prototypes = prototypes
        self.requested_model_version: str | None = None

    def list_vectors_for_model(
        self, model_version: str
    ) -> list[StoredTopicPrototype]:
        self.requested_model_version = model_version
        return [
            proto
            for proto in self._prototypes
            if proto.model_version == model_version
        ]


class FakeEmbeddingRepo:
    def __init__(self, stored: StoredEmbedding | None) -> None:
        self._stored = stored

    def get(self, _envelope_id: str, _model_version: str) -> StoredEmbedding | None:
        return self._stored


def test_single_topic_single_prototype_returns_one_candidate() -> None:
    repo: Any = FakeTopicRepo([_proto("history", 0, [1.0, 0.0])])

    candidates = retrieve_top_k(
        np.array([1.0, 0.0], dtype=np.float32),
        repo=repo,
        model_version="model@1",
        k=5,
    )

    assert len(candidates) == 1
    assert candidates[0].topic_id == "history"
    assert candidates[0].score == pytest.approx(1.0)
    assert candidates[0].best_exemplar_idx == 0


def test_max_aggregation_selects_best_exemplar_for_topic() -> None:
    repo: Any = FakeTopicRepo(
        [
            _proto("history", 0, [0.9, 0.4358899], text="best"),
            _proto("history", 1, [0.5, 0.8660254], text="middle"),
            _proto("history", 2, [0.3, 0.9539392], text="low"),
        ]
    )

    candidates = retrieve_top_k(
        np.array([1.0, 0.0], dtype=np.float32),
        repo=repo,
        model_version="model@1",
        k=5,
    )

    assert len(candidates) == 1
    assert candidates[0].score == pytest.approx(0.9)
    assert candidates[0].best_exemplar_idx == 0
    assert candidates[0].best_exemplar_text == "best"


def test_top_k_truncates_in_score_descending_order() -> None:
    repo: Any = FakeTopicRepo(
        [
            _proto("topic-0", 0, [0.9, 0.4358899]),
            _proto("topic-1", 0, [0.8, 0.6]),
            _proto("topic-2", 0, [0.7, 0.7141429]),
            _proto("topic-3", 0, [0.6, 0.8]),
        ]
    )

    candidates = retrieve_top_k(
        np.array([1.0, 0.0], dtype=np.float32),
        repo=repo,
        model_version="model@1",
        k=2,
    )

    assert [candidate.topic_id for candidate in candidates] == [
        "topic-0",
        "topic-1",
    ]


def test_k_larger_than_topic_count_returns_all_candidates() -> None:
    repo: Any = FakeTopicRepo(
        [
            _proto("history", 0, [1.0, 0.0]),
            _proto("science", 0, [0.0, 1.0]),
        ]
    )

    candidates = retrieve_top_k(
        np.array([1.0, 0.0], dtype=np.float32),
        repo=repo,
        model_version="model@1",
        k=5,
    )

    assert [candidate.topic_id for candidate in candidates] == [
        "history",
        "science",
    ]


def test_empty_prototype_table_returns_empty_list() -> None:
    repo: Any = FakeTopicRepo([])

    assert (
        retrieve_top_k(
            np.array([1.0, 0.0], dtype=np.float32),
            repo=repo,
            model_version="model@1",
            k=5,
        )
        == []
    )


def test_zero_norm_query_vector_returns_empty_list() -> None:
    repo: Any = FakeTopicRepo([_proto("history", 0, [1.0, 0.0])])

    assert (
        retrieve_top_k(
            np.array([0.0, 0.0], dtype=np.float32),
            repo=repo,
            model_version="model@1",
            k=5,
        )
        == []
    )


def test_retrieve_for_envelope_raises_when_embedding_is_missing() -> None:
    embedding_repo: Any = FakeEmbeddingRepo(None)
    topic_repo: Any = FakeTopicRepo([])

    with pytest.raises(LookupError, match="no embedding found"):
        retrieve_top_k_for_envelope(
            "missing-envelope",
            embedding_repo=embedding_repo,
            topic_repo=topic_repo,
            model_version="model@1",
            k=5,
        )


def test_retrieve_for_envelope_uses_stored_embedding() -> None:
    stored = StoredEmbedding(
        envelope_id="env-1",
        model_version="model@1",
        created_at=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).isoformat(),
        vector=np.array([1.0, 0.0], dtype=np.float32),
    )
    embedding_repo: Any = FakeEmbeddingRepo(stored)
    topic_repo: Any = FakeTopicRepo([_proto("history", 0, [1.0, 0.0])])

    candidates = retrieve_top_k_for_envelope(
        "env-1",
        embedding_repo=embedding_repo,
        topic_repo=topic_repo,
        model_version="model@1",
        k=5,
    )

    assert [candidate.topic_id for candidate in candidates] == ["history"]
