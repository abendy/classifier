"""Storage for the classifier's current classification per envelope.

`ClassificationRepo.upsert` writes both `classifications`
and `topic_assignments` atomically, fully replacing any
prior topic assignments for the envelope. The repo is the
single source of truth for "which classification is live
right now" per plan §Storage / §Reconcile and emit; the
envelope's `classification` field is composed on read in a
separate read path and is not mutated here.

Discriminator: `low_confidence_reason IS NULL` ⟺ confident.
On the confident branch, `confidence` is non-NULL and
`topic_assignments` are populated; on the low-confidence
branch, `confidence` is NULL and `topic_assignments` is an
empty list. The caller-facing write shape is a union so
confident and low-confidence writes cannot be constructed as
invalid mixed states.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from prism.classification_types import LowConfidenceReason
    from prism.envelope import TopicAssignment


@dataclass(frozen=True)
class ConfidentClassification:
    envelope_id: str
    active_run_id: str
    tags: list[str] | None
    topic_assignments: list[TopicAssignment]
    confidence: float
    classified_by: str
    classified_at: datetime


@dataclass(frozen=True)
class LowConfidenceClassification:
    envelope_id: str
    active_run_id: str
    reason: LowConfidenceReason
    classified_by: str
    classified_at: datetime


ClassificationWrite = ConfidentClassification | LowConfidenceClassification


@dataclass(frozen=True)
class StoredTopicAssignment:
    """Row-shape mirror of ``topic_assignments``.

    ``matched_terms`` is the deserialized list-of-string,
    not the JSON column blob.
    """

    topic_id: str
    subtopic_id: str | None
    confidence: float | None
    matched_terms: list[str] | None


@dataclass(frozen=True)
class StoredClassification:
    """Row-shape mirror of ``classifications`` plus joined assignments.

    ``tags`` is the deserialized list-of-string, not the JSON
    column blob. ``classified_at`` is the stored ISO string.
    """

    envelope_id: str
    active_run_id: str
    tags: list[str] | None
    topic_assignments: list[StoredTopicAssignment]
    confidence: float | None
    low_confidence_reason: LowConfidenceReason | None
    classified_by: str
    classified_at: str


_UPSERT_CLASSIFICATION_SQL = (
    "INSERT INTO classifications "
    "(envelope_id, active_run_id, tags, confidence, "
    "low_confidence_reason, classified_by, classified_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(envelope_id) DO UPDATE SET "
    "active_run_id = excluded.active_run_id, "
    "tags = excluded.tags, "
    "confidence = excluded.confidence, "
    "low_confidence_reason = excluded.low_confidence_reason, "
    "classified_by = excluded.classified_by, "
    "classified_at = excluded.classified_at"
)

_DELETE_TOPIC_ASSIGNMENTS_SQL = "DELETE FROM topic_assignments WHERE envelope_id = ?"

_INSERT_TOPIC_ASSIGNMENT_SQL = (
    "INSERT INTO topic_assignments "
    "(envelope_id, topic_id, subtopic_id, confidence, matched_terms) "
    "VALUES (?, ?, ?, ?, ?)"
)


class ClassificationRepo:
    """CRUD over ``classifications`` + ``topic_assignments`` using raw sqlite3."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, write: ClassificationWrite) -> StoredClassification:
        """Write both tables atomically; return canonical post-mutation state.

        Replaces any prior classification + topic_assignments
        rows for ``write.envelope_id``. The DELETE + INSERTs run
        inside a single auto-managed transaction; rollback on any
        sqlite error leaves both tables unchanged.
        """
        is_confident = isinstance(write, ConfidentClassification)
        tags = write.tags if is_confident else None
        confidence = write.confidence if is_confident else None
        low_confidence_reason = None if is_confident else write.reason
        topic_assignments = write.topic_assignments if is_confident else []
        tags_json = None if tags is None else json.dumps(tags, ensure_ascii=False)
        classified_at_iso = write.classified_at.isoformat()
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                _UPSERT_CLASSIFICATION_SQL,
                (
                    write.envelope_id,
                    write.active_run_id,
                    tags_json,
                    confidence,
                    low_confidence_reason,
                    write.classified_by,
                    classified_at_iso,
                ),
            )
            self._conn.execute(
                _DELETE_TOPIC_ASSIGNMENTS_SQL,
                (write.envelope_id,),
            )
            for assignment in topic_assignments:
                matched_json = (
                    None
                    if assignment.matched_terms is None
                    else json.dumps(assignment.matched_terms, ensure_ascii=False)
                )
                self._conn.execute(
                    _INSERT_TOPIC_ASSIGNMENT_SQL,
                    (
                        write.envelope_id,
                        assignment.topic_id,
                        assignment.subtopic_id,
                        assignment.confidence,
                        matched_json,
                    ),
                )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
        return StoredClassification(
            envelope_id=write.envelope_id,
            active_run_id=write.active_run_id,
            tags=tags,
            topic_assignments=[
                StoredTopicAssignment(
                    topic_id=a.topic_id,
                    subtopic_id=a.subtopic_id,
                    confidence=a.confidence,
                    matched_terms=a.matched_terms,
                )
                for a in topic_assignments
            ],
            confidence=confidence,
            low_confidence_reason=low_confidence_reason,
            classified_by=write.classified_by,
            classified_at=classified_at_iso,
        )
