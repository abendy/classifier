"""Tests for ClassificationRepo against a real migrated SQLite DB."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config

from prism.classification_repo import (
    ClassificationRepo,
    ConfidentClassification,
    LowConfidenceClassification,
    StoredClassification,
    StoredTopicAssignment,
)
from prism.db import connect_sqlite
from prism.envelope import TopicAssignment

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from prism.classification_types import LowConfidenceReason


T0 = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = connect_sqlite(db_path, load_vec=False)
    try:
        yield conn
    finally:
        conn.close()


def _assignment(
    topic_id: str = "history",
    *,
    subtopic_id: str | None = None,
    confidence: float | None = 0.91,
    matched_terms: list[str] | None = None,
) -> TopicAssignment:
    return TopicAssignment.model_validate(
        {
            "topicId": topic_id,
            "subtopicId": subtopic_id,
            "confidence": confidence,
            "matchedTerms": matched_terms,
        }
    )


def _confident_write(
    *,
    envelope_id: str = "env-1",
    active_run_id: str = "run-1",
    tags: list[str] | None = None,
    topic_assignments: list[TopicAssignment] | None = None,
    confidence: float = 0.88,
    classified_by: str = "prism@test",
    classified_at: datetime = T0,
) -> ConfidentClassification:
    return ConfidentClassification(
        envelope_id=envelope_id,
        active_run_id=active_run_id,
        tags=tags,
        topic_assignments=topic_assignments if topic_assignments is not None else [_assignment()],
        confidence=confidence,
        classified_by=classified_by,
        classified_at=classified_at,
    )


def _low_confidence_write(
    *,
    envelope_id: str = "env-1",
    active_run_id: str = "run-1",
    low_confidence_reason: LowConfidenceReason = "below-threshold",
    classified_by: str = "prism@test",
    classified_at: datetime = T0,
) -> LowConfidenceClassification:
    return LowConfidenceClassification(
        envelope_id=envelope_id,
        active_run_id=active_run_id,
        reason=low_confidence_reason,
        classified_by=classified_by,
        classified_at=classified_at,
    )


def _fetch_classification(
    conn: sqlite3.Connection,
    envelope_id: str,
) -> sqlite3.Row:
    cursor = conn.execute(
        "SELECT envelope_id, active_run_id, tags, confidence, "
        "low_confidence_reason, classified_by, classified_at "
        "FROM classifications WHERE envelope_id = ?",
        (envelope_id,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return row


def _fetch_topic_assignments(
    conn: sqlite3.Connection,
    envelope_id: str,
) -> list[sqlite3.Row]:
    cursor = conn.execute(
        "SELECT envelope_id, topic_id, subtopic_id, confidence, matched_terms "
        "FROM topic_assignments WHERE envelope_id = ? ORDER BY rowid",
        (envelope_id,),
    )
    try:
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _stored_from_db(conn: sqlite3.Connection, envelope_id: str) -> StoredClassification:
    classification = _fetch_classification(conn, envelope_id)
    assignments = _fetch_topic_assignments(conn, envelope_id)
    return StoredClassification(
        envelope_id=str(classification["envelope_id"]),
        active_run_id=str(classification["active_run_id"]),
        tags=None
        if classification["tags"] is None
        else list(json.loads(str(classification["tags"]))),
        topic_assignments=[
            StoredTopicAssignment(
                topic_id=str(row["topic_id"]),
                subtopic_id=row["subtopic_id"],
                confidence=row["confidence"],
                matched_terms=None
                if row["matched_terms"] is None
                else list(json.loads(str(row["matched_terms"]))),
            )
            for row in assignments
        ],
        confidence=classification["confidence"],
        low_confidence_reason=classification["low_confidence_reason"],
        classified_by=str(classification["classified_by"]),
        classified_at=str(classification["classified_at"]),
    )


def test_confident_write_shape_has_no_low_confidence_reason() -> None:
    write = _confident_write()

    assert not hasattr(write, "low_confidence_reason")


def test_low_confidence_write_shape_has_reason() -> None:
    write = _low_confidence_write(low_confidence_reason="llm-pick-unsure")

    assert write.reason == "llm-pick-unsure"


def test_low_confidence_write_shape_has_no_confident_payload() -> None:
    write = _low_confidence_write()

    assert not hasattr(write, "confidence")
    assert not hasattr(write, "topic_assignments")


def test_upsert_inserts_confident_classification_and_assignments(
    conn: sqlite3.Connection,
) -> None:
    repo = ClassificationRepo(conn)
    write = _confident_write(
        tags=["research", "saved"],
        topic_assignments=[
            _assignment("history", subtopic_id="archives", matched_terms=["archive"]),
            _assignment("science", confidence=None, matched_terms=None),
        ],
    )

    returned = repo.upsert(write)
    stored_row = _fetch_classification(conn, write.envelope_id)
    assignment_rows = _fetch_topic_assignments(conn, write.envelope_id)

    assert stored_row["confidence"] == 0.88
    assert stored_row["low_confidence_reason"] is None
    assert len(assignment_rows) == 2
    assert returned == _stored_from_db(conn, write.envelope_id)


def test_upsert_inserts_low_confidence_classification(
    conn: sqlite3.Connection,
) -> None:
    repo = ClassificationRepo(conn)
    write = _low_confidence_write(low_confidence_reason="below-threshold")

    returned = repo.upsert(write)
    stored_row = _fetch_classification(conn, write.envelope_id)

    assert stored_row["confidence"] is None
    assert stored_row["low_confidence_reason"] == "below-threshold"
    assert _fetch_topic_assignments(conn, write.envelope_id) == []
    assert returned == _stored_from_db(conn, write.envelope_id)


def test_upsert_replaces_prior_topic_assignments_atomically(
    conn: sqlite3.Connection,
) -> None:
    repo = ClassificationRepo(conn)
    repo.upsert(
        _confident_write(
            topic_assignments=[
                _assignment("history"),
                _assignment("science", subtopic_id="physics"),
            ],
        )
    )

    repo.upsert(
        _confident_write(
            active_run_id="run-2",
            topic_assignments=[_assignment("arts", subtopic_id="painting")],
        )
    )

    stored_row = _fetch_classification(conn, "env-1")
    assignment_rows = _fetch_topic_assignments(conn, "env-1")
    assert stored_row["active_run_id"] == "run-2"
    assert len(assignment_rows) == 1
    assert assignment_rows[0]["topic_id"] == "arts"
    assert assignment_rows[0]["subtopic_id"] == "painting"


def test_upsert_replaces_with_low_confidence_clears_assignments(
    conn: sqlite3.Connection,
) -> None:
    repo = ClassificationRepo(conn)
    repo.upsert(
        _confident_write(
            topic_assignments=[
                _assignment("history"),
                _assignment("science", subtopic_id="physics"),
            ],
        )
    )

    repo.upsert(_low_confidence_write(active_run_id="run-2"))

    stored_row = _fetch_classification(conn, "env-1")
    assert _fetch_topic_assignments(conn, "env-1") == []
    assert stored_row["confidence"] is None
    assert stored_row["low_confidence_reason"] == "below-threshold"


def test_upsert_preserves_non_ascii_in_tags_and_matched_terms(
    conn: sqlite3.Connection,
) -> None:
    repo = ClassificationRepo(conn)
    write = _confident_write(
        tags=["📚", "héllo", "“smart”"],
        topic_assignments=[_assignment(matched_terms=["café", "naïve"])],
    )

    repo.upsert(write)

    stored_row = _fetch_classification(conn, write.envelope_id)
    assignment_rows = _fetch_topic_assignments(conn, write.envelope_id)
    assert stored_row["tags"] == '["📚", "héllo", "“smart”"]'
    assert assignment_rows[0]["matched_terms"] == '["café", "naïve"]'
    assert json.loads(stored_row["tags"]) == write.tags
    assert json.loads(assignment_rows[0]["matched_terms"]) == ["café", "naïve"]
