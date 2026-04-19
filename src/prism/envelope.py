"""Pydantic models for the dada.stream content envelope."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ENVELOPE_VERSION = "1.0.0"

# Lenient datetime: accepts ISO-8601 strings on the dict-validation path
# too, not only through model_validate_json. Strict-mode models otherwise
# refuse string coercion to datetime, which would break any caller that
# loads envelopes from JSON then hands the dict to model_validate.
_DateTime = Annotated[datetime, Field(strict=False)]

# IDs must be non-empty. Spec recommends UUID v7 but doesn't require it;
# any non-empty string is acceptable, empty string is not.
_NonEmptyStr = Annotated[str, Field(min_length=1)]

ContentType = Literal[
    "post",
    "video",
    "article",
    "email",
    "note",
    "bookmark",
    "audio",
    "image",
    "document",
]

Status = Literal["unread", "read", "starred", "archived"]

TaskStatus = Literal["none", "todo", "in_progress", "done"]

Priority = Literal["low", "normal", "high", "urgent"]

RelationType = Literal["reply", "quote", "highlight", "thread", "reference"]

AnnotationType = Literal["note", "highlight", "comment"]


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        strict=True,
    )


class Identity(_Base):
    id: _NonEmptyStr


class Source(_Base):
    service: str
    ingested_at: _DateTime
    source_id: _NonEmptyStr | None = None
    source_url: str | None = None
    source_data: dict[str, Any] | None = None


class MediaRef(_Base):
    url: str
    type: str
    alt: str | None = None


class LinkRef(_Base):
    url: str
    title: str | None = None
    domain: str | None = None


class Author(_Base):
    name: str
    handle: str | None = None
    url: str | None = None
    avatar_url: str | None = None


class Content(_Base):
    type: ContentType
    title: str | None = None
    body: str | None = None
    summary: str | None = None
    media_urls: list[MediaRef] | None = None
    links: list[LinkRef] | None = None
    author: Author | None = None
    published_at: _DateTime | None = None
    language: str | None = None
    duration: float | None = None


class TopicAssignment(_Base):
    topic_id: str
    subtopic_id: str | None = None
    confidence: float | None = None
    matched_terms: list[str] | None = None


class Classification(_Base):
    tags: list[str] | None = None
    topic_assignments: list[TopicAssignment] | None = None
    classified_at: _DateTime | None = None
    classified_by: str | None = None


class Routing(_Base):
    destinations: list[str] | None = None
    routed_at: _DateTime | None = None
    rules: list[str] | None = None


class Annotation(_Base):
    id: _NonEmptyStr
    body: str
    type: AnnotationType
    created_at: _DateTime
    anchor: str | None = None


class State(_Base):
    status: Status = "unread"
    task_status: TaskStatus | None = None
    priority: Priority | None = None
    annotations: list[Annotation] | None = None
    last_interacted_at: _DateTime | None = None


class Relationships(_Base):
    parent_id: _NonEmptyStr | None = None
    type: RelationType | None = None


class System(_Base):
    version: str
    created_at: _DateTime
    updated_at: _DateTime


class ContentEnvelope(_Base):
    identity: Identity
    source: Source
    content: Content
    system: System
    classification: Classification | None = None
    routing: Routing | None = None
    state: State | None = None
    relationships: Relationships | None = None


def export_schema() -> dict[str, Any]:
    return ContentEnvelope.model_json_schema()
