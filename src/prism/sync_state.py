"""Durable key-value store for classifier synchronization cursors."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class SyncStateRepo:
    """Upsert-style key-value store over ``sync_state``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, name: str) -> str | None:
        """Return the stored value for ``name``, or None when absent."""
        cursor = self._conn.execute(
            "SELECT value FROM sync_state WHERE name = ?",
            (name,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return None if row is None else str(row[0])

    def set(
        self,
        name: str,
        value: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Upsert ``name`` to ``value``; refresh ``updated_at`` on both paths."""
        ts = (now if now is not None else datetime.now(UTC)).isoformat()
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "INSERT INTO sync_state (name, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (name, value, ts),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
