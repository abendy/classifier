"""Classifier-side queue mirroring scraper outbox events.

The scraper's ``outbox`` is authoritative for upstream event
state; the classifier records observed events into its own
``ingest_queue`` table so retries and status transitions never
mutate the scraper's DB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class IngestQueueItem:
    """Row-shape mirror of the ``ingest_queue`` table."""

    source_event_id: int
    source_id: str
    entity_type: str
    event_type: str
    observed_at: str
    attempts: int
    available_at: str
    dispatched_at: str | None
    status: str
    last_error: str | None


_SELECT_COLS = (
    "source_event_id, source_id, entity_type, event_type, observed_at, "
    "attempts, available_at, dispatched_at, status, last_error"
)


class IngestQueueRepo:
    """CRUD over ``ingest_queue`` using raw sqlite3."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def observe(
        self,
        *,
        source_event_id: int,
        source_id: str,
        entity_type: str,
        event_type: str,
        now: datetime | None = None,
    ) -> bool:
        """Insert a new pending row; return True on first observation."""
        ts = _iso(now)
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                f"INSERT OR IGNORE INTO ingest_queue ({_SELECT_COLS}) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, NULL, 'pending', NULL)",
                (source_event_id, source_id, entity_type, event_type, ts, ts),
            )
            inserted = cursor.rowcount > 0
            self._conn.commit()
            return inserted
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()

    def list_pending(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[IngestQueueItem]:
        """Return up to ``limit`` pending items with ``available_at <= now``."""
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM ingest_queue "
            "WHERE status = 'pending' AND available_at <= ? "
            "ORDER BY available_at ASC LIMIT ?",
            (_iso(now), limit),
        )
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
        return [IngestQueueItem(*r) for r in rows]

    def mark_dispatched(
        self,
        source_event_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """Transition a row to ``done`` with ``dispatched_at`` set."""
        self._update_one(
            "UPDATE ingest_queue SET status = 'done', dispatched_at = ? "
            "WHERE source_event_id = ?",
            (_iso(now), source_event_id),
            source_event_id,
        )

    def mark_failed(
        self,
        source_event_id: int,
        *,
        error: str,
        backoff_seconds: int,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> None:
        """Record a failure and trip ``dead_letter`` when attempts exhaust."""
        now_dt = now if now is not None else datetime.now(UTC)
        next_available = (now_dt + timedelta(seconds=backoff_seconds)).isoformat()
        self._update_one(
            "UPDATE ingest_queue SET attempts = attempts + 1, last_error = ?, "
            "available_at = ?, status = CASE WHEN attempts + 1 >= ? "
            "THEN 'dead_letter' ELSE status END WHERE source_event_id = ?",
            (error, next_available, max_attempts, source_event_id),
            source_event_id,
        )

    def max_source_event_id(self) -> int | None:
        """Return the largest observed ``source_event_id`` or None."""
        cursor = self._conn.execute(
            "SELECT MAX(source_event_id) FROM ingest_queue"
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return None if row is None or row[0] is None else int(row[0])

    def _update_one(
        self,
        sql: str,
        params: tuple,
        missing_key: int,
    ) -> None:
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(sql, params)
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise KeyError(missing_key)
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()


def backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * (2 ** (attempts - 1)))


def fail_queue_item(
    item: IngestQueueItem,
    *,
    queue_repo: IngestQueueRepo,
    error_prefix: str,
    exc: Exception | str,
    now: datetime | None,
) -> None:
    """Mark an ingest queue item failed with the standard backoff."""
    error = exc if isinstance(exc, str) else f"{error_prefix}: {exc!r}"
    queue_repo.mark_failed(
        item.source_event_id,
        error=error,
        backoff_seconds=backoff_seconds(item.attempts + 1),
        now=now,
    )


def _iso(now: datetime | None) -> str:
    return (now if now is not None else datetime.now(UTC)).isoformat()
