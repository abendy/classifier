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


def _minimal_dict(
    envelope_id: str = "env-1", *, source_id: str | None = None
) -> dict[str, Any]:
    return {
        "identity": {"id": envelope_id},
        "source": {
            "service": "x-sync",
            "sourceId": source_id or f"src-{envelope_id}",
            "ingestedAt": TS,
        },
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
    assert names == ["id", "envelope_json", "ingested_at", "created_at", "source_id"]


def test_duplicate_source_id_raises_integrity_error(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    repo.save(ContentEnvelope.model_validate(_minimal_dict("env-1", source_id="shared")))
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(ContentEnvelope.model_validate(_minimal_dict("env-2", source_id="shared")))


def test_exists_by_source_id_reflects_save(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    assert repo.exists_by_source_id("src-env-1") is False
    repo.save(ContentEnvelope.model_validate(_minimal_dict()))
    assert repo.exists_by_source_id("src-env-1") is True


def test_refresh_preserves_identity_id_and_created_at(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    original = ContentEnvelope.model_validate(_minimal_dict("env-orig", source_id="src-1"))
    repo.save(original)

    later = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    incoming_dict = {
        "identity": {"id": "env-new"},
        "source": {"service": "x-sync", "sourceId": "src-1", "ingestedAt": later},
        "content": {"type": "post", "body": "full body now"},
        "system": {"version": "1.0.0", "createdAt": later, "updatedAt": later},
    }
    repo.refresh_by_source_id(ContentEnvelope.model_validate(incoming_dict))

    refreshed = repo.get("env-orig")
    assert refreshed is not None
    assert refreshed.identity.id == "env-orig"
    assert refreshed.system.created_at == TS
    assert refreshed.system.updated_at == later
    assert refreshed.source.ingested_at == later
    assert refreshed.content.body == "full body now"


def test_refresh_by_source_id_raises_key_error_when_missing(
    conn: sqlite3.Connection,
) -> None:
    repo = EnvelopeRepo(conn)
    envelope = ContentEnvelope.model_validate(_minimal_dict())
    with pytest.raises(KeyError):
        repo.refresh_by_source_id(envelope)


def test_refresh_returns_merged_envelope_with_preserved_identity(
    conn: sqlite3.Connection,
) -> None:
    """Return value carries the preserved stored identity.id plus the
    mapper-owned sections from the input — the shape callers need when
    they key downstream state (embeddings, runs rows) by the stable
    envelope id instead of the per-observation UUID the mapper mints.
    """
    repo = EnvelopeRepo(conn)
    repo.save(ContentEnvelope.model_validate(_minimal_dict("env-orig", source_id="src-1")))

    later = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    incoming = ContentEnvelope.model_validate({
        "identity": {"id": "env-transient"},
        "source": {"service": "x-sync", "sourceId": "src-1", "ingestedAt": later},
        "content": {"type": "post", "body": "updated body"},
        "system": {"version": "1.0.0", "createdAt": later, "updatedAt": later},
    })
    merged = repo.refresh_by_source_id(incoming)

    assert merged.identity.id == "env-orig"
    assert merged.system.created_at == TS
    assert merged.system.updated_at == later
    assert merged.source.ingested_at == later
    assert merged.content.body == "updated body"
    # The returned envelope matches what was actually persisted.
    stored = repo.get("env-orig")
    assert stored is not None
    assert stored.model_dump(mode="json") == merged.model_dump(mode="json")


def test_refresh_updates_ingested_at_column(conn: sqlite3.Connection) -> None:
    repo = EnvelopeRepo(conn)
    repo.save(ContentEnvelope.model_validate(_minimal_dict("env-1", source_id="src-1")))

    later = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    incoming = {
        "identity": {"id": "does-not-matter"},
        "source": {"service": "x-sync", "sourceId": "src-1", "ingestedAt": later},
        "content": {"type": "post"},
        "system": {"version": "1.0.0", "createdAt": later, "updatedAt": later},
    }
    repo.refresh_by_source_id(ContentEnvelope.model_validate(incoming))

    cursor = conn.execute(
        "SELECT ingested_at, created_at FROM content_envelopes WHERE source_id = 'src-1'"
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row[0] == later.isoformat()
    assert row[1] == TS.isoformat()


def test_refresh_preserves_non_mapper_sections(conn: sqlite3.Connection) -> None:
    """Refresh keeps classification/routing/state/relationships — those belong
    to downstream pipelines and the mapper has no right to erase them."""
    repo = EnvelopeRepo(conn)

    seed_dict = _minimal_dict("env-1", source_id="src-1")
    seed_dict["classification"] = {
        "tags": ["ml"],
        "classifiedAt": TS,
        "classifiedBy": "rules@1.0",
    }
    seed_dict["routing"] = {"destinations": ["inbox-a"], "routedAt": TS}
    seed_dict["state"] = {
        "status": "starred",
        "annotations": [
            {"id": "a-1", "body": "note", "type": "note", "createdAt": TS},
        ],
    }
    seed_dict["relationships"] = {"parentId": "env-0", "type": "reply"}
    repo.save(ContentEnvelope.model_validate(seed_dict))

    later = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # Mapper-only payload — no classification/routing/state/relationships.
    incoming = {
        "identity": {"id": "ignored"},
        "source": {"service": "x-sync", "sourceId": "src-1", "ingestedAt": later},
        "content": {"type": "post", "body": "new body"},
        "system": {"version": "1.0.0", "createdAt": later, "updatedAt": later},
    }
    repo.refresh_by_source_id(ContentEnvelope.model_validate(incoming))

    refreshed = repo.get("env-1")
    assert refreshed is not None
    assert refreshed.content.body == "new body"
    assert refreshed.classification is not None
    assert refreshed.classification.tags == ["ml"]
    assert refreshed.routing is not None
    assert refreshed.routing.destinations == ["inbox-a"]
    assert refreshed.state is not None
    assert refreshed.state.status == "starred"
    assert refreshed.state.annotations is not None
    assert [a.id for a in refreshed.state.annotations] == ["a-1"]
    assert refreshed.relationships is not None
    assert refreshed.relationships.parent_id == "env-0"
