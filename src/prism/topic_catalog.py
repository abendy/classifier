"""Curated topic catalog: Pydantic models and YAML loader.

The catalog is the source of truth for topic IDs, names,
descriptions, and exemplars. It feeds the prototype-embedding
job (``topic_loader.embed_catalog``); later Phase 2 slices read
from ``topic_prototypes`` (the embedded form), not from the
YAML directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import yaml
from pydantic import Field

from prism.envelope import _Base

if TYPE_CHECKING:
    from pathlib import Path


_NonEmptyStr = Annotated[str, Field(min_length=1)]


class TopicSpec(_Base):
    id: _NonEmptyStr
    name: _NonEmptyStr
    description: _NonEmptyStr
    exemplars: list[_NonEmptyStr] = Field(default_factory=list)
    parent_topic_id: _NonEmptyStr | None = None
    subtopics: list[TopicSpec] = Field(default_factory=list)


class TopicCatalog(_Base):
    version: _NonEmptyStr
    topics: list[TopicSpec]


def load_catalog(path: Path) -> TopicCatalog:
    """Read and validate a YAML topic catalog."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"{path} is empty or contains no YAML data.")
    return TopicCatalog.model_validate(raw)


def flatten(catalog: TopicCatalog) -> list[TopicSpec]:
    """Flatten subtopics into a single list with parent_topic_id set."""
    flat: list[TopicSpec] = []
    seen: set[str] = set()
    for topic in catalog.topics:
        if topic.parent_topic_id is not None:
            raise ValueError(
                f"top-level topic {topic.id!r} must not set parent_topic_id"
            )
        _add(topic, parent_id=None, flat=flat, seen=seen, depth=0)
    return flat


def _add(
    topic: TopicSpec,
    *,
    parent_id: str | None,
    flat: list[TopicSpec],
    seen: set[str],
    depth: int,
) -> None:
    if depth > 1:
        raise ValueError(
            f"subtopics-of-subtopics are not supported (topic {topic.id!r})"
        )
    if topic.id in seen:
        raise ValueError(f"duplicate topic id: {topic.id!r}")
    seen.add(topic.id)
    flat.append(
        topic.model_copy(update={"parent_topic_id": parent_id, "subtopics": []})
    )
    for subtopic in topic.subtopics:
        _add(subtopic, parent_id=topic.id, flat=flat, seen=seen, depth=depth + 1)
