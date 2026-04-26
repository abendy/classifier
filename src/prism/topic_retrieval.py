"""Cosine-similarity retrieval over topic prototypes.

Given a query vector and the topic prototype repo, return the
top-k topics by max-similarity across each topic's prototype rows.
The LLM pick step consumes these candidates downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from prism.embedding_repo import EmbeddingRepo
    from prism.topic_prototype_repo import TopicPrototypeRepo


@dataclass(frozen=True)
class TopicCandidate:
    topic_id: str
    score: float
    best_exemplar_idx: int
    best_exemplar_text: str


def retrieve_top_k(
    query_vector: np.ndarray,
    *,
    repo: TopicPrototypeRepo,
    model_version: str,
    k: int,
) -> list[TopicCandidate]:
    """Return the top-k topics by max cosine similarity to ``query_vector``."""
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")

    prototypes = repo.list_vectors_for_model(model_version)
    if not prototypes:
        return []

    query_norm = float(np.linalg.norm(query_vector))
    if query_norm == 0.0:
        return []

    best: dict[str, tuple[float, int, str]] = {}
    for proto in prototypes:
        proto_norm = float(np.linalg.norm(proto.vector))
        if proto_norm == 0.0:
            continue
        score = float(np.dot(query_vector, proto.vector) / (query_norm * proto_norm))
        prior = best.get(proto.topic_id)
        if prior is None or score > prior[0]:
            best[proto.topic_id] = (score, proto.exemplar_idx, proto.text)

    ranked = sorted(
        (
            TopicCandidate(
                topic_id=topic_id,
                score=score,
                best_exemplar_idx=idx,
                best_exemplar_text=text,
            )
            for topic_id, (score, idx, text) in best.items()
        ),
        key=lambda c: c.score,
        reverse=True,
    )
    return ranked[:k]


def retrieve_top_k_for_envelope(
    envelope_id: str,
    *,
    embedding_repo: EmbeddingRepo,
    topic_repo: TopicPrototypeRepo,
    model_version: str,
    k: int,
) -> list[TopicCandidate]:
    """Look up the envelope's stored embedding, then retrieve."""
    stored = embedding_repo.get(envelope_id, model_version)
    if stored is None:
        raise LookupError(
            f"no embedding found for envelope_id={envelope_id!r} "
            f"under model_version={model_version!r}"
        )
    return retrieve_top_k(
        stored.vector,
        repo=topic_repo,
        model_version=model_version,
        k=k,
    )
