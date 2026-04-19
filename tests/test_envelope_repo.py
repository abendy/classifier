"""Tests for EnvelopeRepo against a real migrated SQLite DB."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.envelope import ContentEnvelope
from prism.envelope_repo import EnvelopeRepo

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

TS = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


def _minimal_dict(envelope_id: str = "env-1") -> dict[str, Any]:
    return {
        "identity": {"id": envelope_id},
        "source": {"service": "x-sync", "ingestedAt": TS},
        "content": {"type": "post"},
        "system": {"version": "1.0.0", "createdAt": TS, "updatedAt": TS},
    }


def _full_dict(envelope_id: str = "env-2") -> dict[str, Any]:
    return {
        "identity": {"id": envelope_id},
        "source": {
            "service": "x-sync",
            "sourceId": "tweet-42",
            "sourceUrl": "https://x.com/u/status/42",
            "ingestedAt": TS,
            "sourceData": {"retweets": 7, "raw": {"nested": True}},
        },
        "content": {
            "type": "video",
            "title": "Demo clip",
            "body": "Long description here.",
            "summary": "Demo.",
            "mediaUrls": [
                {"url": "https://cdn/m.mp4", "type": "video/mp4", "alt": "clip"},
            ],
            "links": [
                {"url": "https://ref", "title": "Ref", "domain": "ref"},
            ],
            "author": {
                "name": "Ada",
                "handle": "ada",
                "url": "https://x.com/ada",
                "avatarUrl": "https://cdn/a.png",
            },
            "publishedAt": TS,
            "language": "en",
            "duration": 42.5,
        },
        "classification": {
            "tags": ["ml", "demo"],
            "topicAssignments": [
                {
                    "topicId": "t-1",
                    "subtopicId": "st-1",
                    "confidence": 0.87,
                    "matchedTerms": ["ml"],
                },
            ],
            "classifiedAt": TS,
            "classifiedBy": "rules@1.0",
        },
        "routing": {
            "destinations": ["inbox-a"],
            "routedAt": TS,
            "rules": ["r-1"],
        },
        "state": {
            "status": "starred",
            "taskStatus": "todo",
            "priority": "high",
            "annotations": [
                {
                    "id": "a-1",
                    "body": "interesting",
                    "type": "note",
                    "createdAt": TS,
                    "anchor": "00:12",
                },
            ],
            "lastInteractedAt": TS,
        },
        "relationships": {"parentId": "env-0", "type": "reply"},
        "system": {"version": "1.0.0", "createdAt": TS, "updatedAt": TS},
    }


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Run alembic upgrade head against a tmp sqlite DB and yield
    a live connection positioned at head."""
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = connect_sqlite(db_path, load_vec=False)
    try:
        yield conn
    finally:
        conn.close()


def test_save_and_get_minimal_envelope(conn: sqlite3.Connection) -> None:
    envelope = ContentEnvelope.model_validate(_minimal_dict())
    repo = EnvelopeRepo(conn)
    repo.save(envelope)
    fetched = repo.get("env-1")
    assert fetched == envelope


def test_save_and_get_fully_populated_envelope(conn: sqlite3.Connection) -> None:
    envelope = ContentEnvelope.model_validate(_full_dict())
    repo = EnvelopeRepo(conn)
    repo.save(envelope)
    fetched = repo.get("env-2")
    assert fetched == envelope
    assert fetched is not None
    assert fetched.classification is not None
    assert fetched.routing is not None
    assert fetched.state is not None
    assert fetched.relationships is not None


def test_get_missing_id_returns_none(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    assert repo.get("never-saved") is None


def test_exists_reflects_save(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    assert repo.exists("env-1") is False
    repo.save(ContentEnvelope.model_validate(_minimal_dict()))
    assert repo.exists("env-1") is True


def test_duplicate_save_raises_integrity_error(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    envelope = ContentEnvelope.model_validate(_minimal_dict())
    repo.save(envelope)
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(envelope)


def test_failed_save_does_not_leave_connection_mid_transaction(
    conn: sqlite3.Connection,
) -> None:
    repo = EnvelopeRepo(conn)
    envelope = ContentEnvelope.model_validate(_minimal_dict())
    repo.save(envelope)
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(envelope)
    assert conn.in_transaction is False
    repo.save(ContentEnvelope.model_validate(_minimal_dict("env-9")))
    assert repo.exists("env-9") is True


class _CommitFailConn(sqlite3.Connection):
    def commit(self) -> None:
        raise sqlite3.OperationalError("simulated commit failure")


def test_failed_commit_leaves_connection_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_path, factory=_CommitFailConn)
    try:
        repo = EnvelopeRepo(conn)
        envelope = ContentEnvelope.model_validate(_minimal_dict())
        with pytest.raises(sqlite3.OperationalError):
            repo.save(envelope)
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_content_envelopes_table_has_expected_columns(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info('content_envelopes')")
    try:
        names = [row[1] for row in cursor.fetchall()]
    finally:
        cursor.close()
    assert names == ["id", "envelope_json", "ingested_at", "created_at"]
