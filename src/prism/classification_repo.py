"""Storage for the classifier's current classification per envelope.

`ClassificationRepo.upsert` writes both `classifications`
and `topic_assignments` atomically, fully replacing any
prior topic assignments for the envelope. The repo is the
single source of truth for "which classification is live
right now" per plan §Storage / §Reconcile and emit; the
envelope's `classification` field is composed on read in a
later slice and is not mutated here.

Discriminator: `low_confidence_reason IS NULL` ⟺ confident.
On the confident branch, `confidence` is non-NULL and
`topic_assignments` are populated; on the low-confidence
branch, `confidence` is NULL and `topic_assignments` is an
empty list. The repo enforces the invariant in
``_validate_write``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from prism.envelope import TopicAssignment


LowConfidenceReason = Literal[
    "below-threshold",
    "out-of-band",
    "llm-pick-unsure",
    "llm-output-malformed",
]


@dataclass(frozen=True)
class ClassificationWrite:
    """Caller-facing input shape for ``ClassificationRepo.upsert``.

    The discriminator is implicit:
    - confident:  ``low_confidence_reason is None``,
                  ``confidence is not None``,
                  ``topic_assignments`` non-empty.
    - low-confidence: ``low_confidence_reason is not None``,
                  ``confidence is None``,
                  ``topic_assignments`` empty.
    """

    envelope_id: str
    active_run_id: str
    tags: list[str] | None
    topic_assignments: list[TopicAssignment]
    confidence: float | None
    low_confidence_reason: LowConfidenceReason | None
    classified_by: str
    classified_at: datetime


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
        _validate_write(write)
        tags_json = None if write.tags is None else json.dumps(write.tags, ensure_ascii=False)
        classified_at_iso = write.classified_at.isoformat()
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                _UPSERT_CLASSIFICATION_SQL,
                (
                    write.envelope_id,
                    write.active_run_id,
                    tags_json,
                    write.confidence,
                    write.low_confidence_reason,
                    write.classified_by,
                    classified_at_iso,
                ),
            )
            self._conn.execute(
                _DELETE_TOPIC_ASSIGNMENTS_SQL,
                (write.envelope_id,),
            )
            for assignment in write.topic_assignments:
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
            tags=write.tags,
            topic_assignments=[
                StoredTopicAssignment(
                    topic_id=a.topic_id,
                    subtopic_id=a.subtopic_id,
                    confidence=a.confidence,
                    matched_terms=a.matched_terms,
                )
                for a in write.topic_assignments
            ],
            confidence=write.confidence,
            low_confidence_reason=write.low_confidence_reason,
            classified_by=write.classified_by,
            classified_at=classified_at_iso,
        )


def _validate_write(write: ClassificationWrite) -> None:
    """Enforce the discriminated-union invariant on input.

    Catches caller errors at the seam rather than letting them
    land as malformed rows. Raises ``ValueError`` with a clear
    message when the invariant is violated.
    """
    if write.low_confidence_reason is None:
        if write.confidence is None:
            raise ValueError("confident classification requires non-None confidence")
        if not write.topic_assignments:
            raise ValueError("confident classification requires at least one topic assignment")
    else:
        if write.confidence is not None:
            raise ValueError("low-confidence classification must have confidence=None")
        if write.topic_assignments:
            raise ValueError("low-confidence classification must have empty topic_assignments")
