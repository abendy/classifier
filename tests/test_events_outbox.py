"""Tests for EventOutboxRepo against a real migrated SQLite DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.events_outbox import EmittedEvent, EmittedEventRow, EventOutboxRepo

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


def _event(
    *,
    id: str = "evt-1",
    event_type: str = "content-classified",
    source: str = "prism",
    correlation_id: str = "env-1",
    causation_id: str | None = "run-1",
    payload: dict[str, object] | None = None,
    envelope_version: str = "1.0.0",
    created_at: str | None = None,
) -> EmittedEvent:
    return EmittedEvent(
        id=id,
        event_type=event_type,
        source=source,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload or {"k": "v"},
        envelope_version=envelope_version,
        created_at=created_at or T0.isoformat(),
    )


def _fetch_event_row(conn: sqlite3.Connection, event_id: str) -> EmittedEventRow:
    cursor = conn.execute(
        "SELECT id, event_type, source, envelope_version, correlation_id, "
        "causation_id, payload, created_at, dispatched_at "
        "FROM events_outbox WHERE id = ?",
        (event_id,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return EmittedEventRow(*row)


def _count(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("SELECT COUNT(*) FROM events_outbox")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return int(row[0])


def test_enqueue_inserts_row_and_returns_canonical_state(
    conn: sqlite3.Connection,
) -> None:
    repo = EventOutboxRepo(conn)
    event = _event(payload={"k": "v"})

    returned = repo.enqueue(event)
    stored = _fetch_event_row(conn, event.id)

    assert stored == EmittedEventRow(
        id=event.id,
        event_type=event.event_type,
        source=event.source,
        envelope_version=event.envelope_version,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        payload='{"k": "v"}',
        created_at=event.created_at,
        dispatched_at=None,
    )
    assert returned == stored


def test_enqueue_serializes_payload_with_sorted_keys(
    conn: sqlite3.Connection,
) -> None:
    repo = EventOutboxRepo(conn)
    event = _event(payload={"b": 2, "a": 1})

    repo.enqueue(event)

    assert _fetch_event_row(conn, event.id).payload == '{"a": 1, "b": 2}'


def test_enqueue_preserves_non_ascii_payload(conn: sqlite3.Connection) -> None:
    repo = EventOutboxRepo(conn)
    event = _event(payload={"title": "héllo 🌍", "quote": "“smart”"})

    repo.enqueue(event)

    stored = _fetch_event_row(conn, event.id)
    assert stored.payload == '{"quote": "“smart”", "title": "héllo 🌍"}'
    assert json.loads(stored.payload) == event.payload


def test_enqueue_persists_null_causation(conn: sqlite3.Connection) -> None:
    repo = EventOutboxRepo(conn)
    event = _event(causation_id=None)

    returned = repo.enqueue(event)
    stored = _fetch_event_row(conn, event.id)

    assert stored.causation_id is None
    assert returned.causation_id is None


def test_enqueue_rolls_back_on_duplicate_id(conn: sqlite3.Connection) -> None:
    repo = EventOutboxRepo(conn)
    repo.enqueue(_event(id="evt-1", payload={"first": True}))

    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue(_event(id="evt-1", payload={"second": True}))

    assert _count(conn) == 1
    assert _fetch_event_row(conn, "evt-1").payload == '{"first": true}'


def test_emitted_event_new_mints_uuid7_and_iso_timestamp() -> None:
    event = EmittedEvent.new(
        event_type="content-classified",
        source="prism",
        correlation_id="env-1",
        causation_id=None,
        payload={"k": "v"},
    )

    assert UUID(event.id).version == 7
    assert datetime.fromisoformat(event.created_at).tzinfo is not None

    event_with_now = EmittedEvent.new(
        event_type="content-classified",
        source="prism",
        correlation_id="env-1",
        causation_id=None,
        payload={"k": "v"},
        now=T0,
    )
    assert event_with_now.created_at == T0.isoformat()


def test_events_outbox_columns_match_spec(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info('events_outbox')")
    try:
        names = [row[1] for row in cursor.fetchall()]
    finally:
        cursor.close()
    assert names == [
        "id",
        "event_type",
        "source",
        "envelope_version",
        "correlation_id",
        "causation_id",
        "payload",
        "created_at",
        "dispatched_at",
    ]


def test_events_outbox_indexes_match_spec(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA index_list('events_outbox')")
    try:
        indexes = {str(row[1]) for row in cursor.fetchall()}
    finally:
        cursor.close()
    assert "idx_events_outbox_pending" in indexes
    assert "idx_events_outbox_correlation" in indexes
