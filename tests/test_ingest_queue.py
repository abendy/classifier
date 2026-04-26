"""Tests for IngestQueueRepo against a real migrated SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from prism.ingest_queue import IngestQueueRepo

if TYPE_CHECKING:
    import sqlite3

T0 = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


def _observe(
    repo: IngestQueueRepo,
    source_event_id: int,
    *,
    source_id: str | None = None,
    entity_type: str = "Tweet",
    event_type: str = "record.synced",
    now: datetime | None = None,
) -> bool:
    return repo.observe(
        source_event_id=source_event_id,
        source_id=source_id or f"t-{source_event_id}",
        entity_type=entity_type,
        event_type=event_type,
        now=now or T0,
    )


def _count(conn: sqlite3.Connection) -> int:
    return int(_fetch_one(conn, "SELECT COUNT(*) FROM ingest_queue")[0])


def _fetch_one(conn: sqlite3.Connection, sql: str) -> tuple:
    cursor = conn.execute(sql)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def test_observe_is_idempotent_by_source_event_id(conn: sqlite3.Connection) -> None:
    repo = IngestQueueRepo(conn)
    assert _observe(repo, 1) is True
    assert _observe(repo, 1) is False
    assert _count(conn) == 1


def test_list_pending_is_ordered_by_available_at_ascending(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 2, now=T0 + timedelta(seconds=5))
    _observe(repo, 1, now=T0)

    items = repo.list_pending(now=T0 + timedelta(seconds=10))
    assert [i.source_event_id for i in items] == [1, 2]


def test_list_pending_respects_limit(conn: sqlite3.Connection) -> None:
    repo = IngestQueueRepo(conn)
    for n in range(1, 6):
        _observe(repo, n, now=T0 + timedelta(seconds=n))

    items = repo.list_pending(limit=3, now=T0 + timedelta(seconds=100))
    assert [i.source_event_id for i in items] == [1, 2, 3]


def test_list_pending_excludes_non_pending_rows(conn: sqlite3.Connection) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 1, now=T0)
    _observe(repo, 2, now=T0 + timedelta(seconds=1))
    _observe(repo, 3, now=T0 + timedelta(seconds=2))
    repo.mark_dispatched(1, now=T0 + timedelta(seconds=10))
    for _ in range(5):
        repo.mark_failed(
            2,
            error="boom",
            backoff_seconds=0,
            max_attempts=5,
            now=T0 + timedelta(seconds=10),
        )

    items = repo.list_pending(now=T0 + timedelta(seconds=100))
    assert [i.source_event_id for i in items] == [3]


def test_list_pending_excludes_future_available_items(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 1, now=T0)
    _observe(repo, 2, now=T0 + timedelta(seconds=60))

    items = repo.list_pending(now=T0 + timedelta(seconds=30))
    assert [i.source_event_id for i in items] == [1]


def test_mark_dispatched_transitions_row_to_done(conn: sqlite3.Connection) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 1, now=T0)

    dispatched_at = T0 + timedelta(seconds=5)
    repo.mark_dispatched(1, now=dispatched_at)

    assert repo.list_pending(now=T0 + timedelta(seconds=100)) == []
    status, dispatched_iso = _fetch_one(
        conn, "SELECT status, dispatched_at FROM ingest_queue WHERE source_event_id = 1"
    )
    assert status == "done"
    assert dispatched_iso == dispatched_at.isoformat()


def test_mark_dispatched_unknown_id_raises_key_error(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    with pytest.raises(KeyError):
        repo.mark_dispatched(404, now=T0)


def test_mark_failed_increments_attempts_and_applies_backoff(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 1, now=T0)

    fail_at = T0 + timedelta(seconds=5)
    repo.mark_failed(1, error="nope", backoff_seconds=30, now=fail_at)

    items = repo.list_pending(now=fail_at + timedelta(seconds=100))
    assert len(items) == 1
    item = items[0]
    assert item.attempts == 1
    assert item.last_error == "nope"
    assert item.available_at == (fail_at + timedelta(seconds=30)).isoformat()
    assert item.status == "pending"


def test_mark_failed_trips_dead_letter_at_max_attempts(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    _observe(repo, 1, now=T0)

    for attempt in range(5):
        repo.mark_failed(
            1,
            error=f"attempt-{attempt}",
            backoff_seconds=0,
            max_attempts=5,
            now=T0 + timedelta(seconds=attempt),
        )

    status, attempts = _fetch_one(
        conn, "SELECT status, attempts FROM ingest_queue WHERE source_event_id = 1"
    )
    assert status == "dead_letter"
    assert attempts == 5
    assert repo.list_pending(now=T0 + timedelta(seconds=100)) == []


def test_mark_failed_unknown_id_raises_key_error(conn: sqlite3.Connection) -> None:
    repo = IngestQueueRepo(conn)
    with pytest.raises(KeyError):
        repo.mark_failed(404, error="x", backoff_seconds=1, now=T0)


def test_max_source_event_id_returns_none_then_largest(
    conn: sqlite3.Connection,
) -> None:
    repo = IngestQueueRepo(conn)
    assert repo.max_source_event_id() is None
    _observe(repo, 3)
    _observe(repo, 1)
    _observe(repo, 42)
    assert repo.max_source_event_id() == 42


def test_ingest_queue_columns_match_spec(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info('ingest_queue')")
    try:
        names = [row[1] for row in cursor.fetchall()]
    finally:
        cursor.close()
    assert names == [
        "source_event_id",
        "source_id",
        "entity_type",
        "event_type",
        "observed_at",
        "attempts",
        "available_at",
        "dispatched_at",
        "status",
        "last_error",
    ]
