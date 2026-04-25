"""Embed a topic catalog into ``topic_prototypes``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from prism.topic_catalog import flatten
from prism.topic_prototype_repo import TopicPrototypeRepo, TopicPrototypeRow

if TYPE_CHECKING:
    from prism.embedding import Embedder
    from prism.topic_catalog import TopicCatalog


@dataclass(frozen=True)
class EmbedSummary:
    topics_processed: int
    prototypes_written: int
    catalog_version: str
    model_version: str


def embed_catalog(
    catalog: TopicCatalog,
    *,
    embedder: Embedder,
    repo: TopicPrototypeRepo,
    model_version: str,
    now: datetime | None = None,
) -> EmbedSummary:
    """Flatten the catalog, embed each prototype, and persist."""
    flat = flatten(catalog)
    when = now or datetime.now(UTC)
    rows: list[TopicPrototypeRow] = []
    for topic in flat:
        prototypes = [topic.description, *topic.exemplars]
        for idx, text in enumerate(prototypes):
            rows.append(
                TopicPrototypeRow(
                    topic_id=topic.id,
                    exemplar_idx=idx,
                    text=text,
                    model_version=model_version,
                    vector=embedder.embed(text),
                    created_at=when,
                )
            )
    repo.save_many(rows)
    return EmbedSummary(
        topics_processed=len(flat),
        prototypes_written=len(rows),
        catalog_version=catalog.version,
        model_version=model_version,
    )
