"""Tests for SyncStateRepo against a real migrated SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from prism.sync_state import SyncStateRepo

if TYPE_CHECKING:
    import sqlite3

T0 = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


def _fetch_updated_at(conn: sqlite3.Connection, name: str) -> str:
    cursor = conn.execute(
        "SELECT updated_at FROM sync_state WHERE name = ?",
        (name,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return str(row[0])


def test_get_missing_name_returns_none(conn: sqlite3.Connection) -> None:
    repo = SyncStateRepo(conn)
    assert repo.get("never-set") is None


def test_set_then_get_returns_stored_value(conn: sqlite3.Connection) -> None:
    repo = SyncStateRepo(conn)
    repo.set("cursor", "42", now=T0)
    assert repo.get("cursor") == "42"


def test_set_twice_upserts_without_error(conn: sqlite3.Connection) -> None:
    repo = SyncStateRepo(conn)
    repo.set("cursor", "1", now=T0)
    repo.set("cursor", "2", now=T0 + timedelta(seconds=5))
    assert repo.get("cursor") == "2"


def test_set_writes_updated_at_on_both_insert_and_update(
    conn: sqlite3.Connection,
) -> None:
    repo = SyncStateRepo(conn)
    repo.set("cursor", "1", now=T0)
    first_updated_at = _fetch_updated_at(conn, "cursor")
    assert first_updated_at == T0.isoformat()

    later = T0 + timedelta(seconds=5)
    repo.set("cursor", "2", now=later)
    second_updated_at = _fetch_updated_at(conn, "cursor")
    assert second_updated_at == later.isoformat()
    assert second_updated_at != first_updated_at


def test_sync_state_has_three_columns_in_order(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info('sync_state')")
    try:
        names = [row[1] for row in cursor.fetchall()]
    finally:
        cursor.close()
    assert names == ["name", "value", "updated_at"]


def test_values_for_distinct_names_are_independent(conn: sqlite3.Connection) -> None:
    repo = SyncStateRepo(conn)
    repo.set("a", "1", now=T0)
    repo.set("b", "2", now=T0)
    assert repo.get("a") == "1"
    assert repo.get("b") == "2"
