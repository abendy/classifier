"""Tests for ScraperOutboxReader against a fake scraper outbox."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from prism.scraper_mapper import open_scraper_readonly
from prism.scraper_outbox import OutboxBatch, OutboxRow, ScraperOutboxReader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_DDL = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    dispatched_at TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT
);
"""

_CREATED = "2026-04-19T11:00:00+00:00"
_AVAILABLE = "2026-04-19T11:00:00+00:00"


@pytest.fixture
def scraper_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "scraper.db"
    seed = sqlite3.connect(db_path)
    try:
        seed.executescript(_DDL)
        seed.commit()
    finally:
        seed.close()
    return db_path


@contextlib.contextmanager
def _writer(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert(
    conn: sqlite3.Connection,
    *,
    event_type: str = "record.synced",
    entity_type: str | None = "Tweet",
    entity_id: object = "t-1",
    payload: str | None = None,
    created_at: str = _CREATED,
) -> None:
    if payload is None:
        body: dict[str, object] = {}
        if entity_type is not None:
            body["entityType"] = entity_type
        body["entityId"] = entity_id
        payload = json.dumps(body)
    conn.execute(
        "INSERT INTO outbox (event_type, payload, created_at, available_at) "
        "VALUES (?, ?, ?, ?)",
        (event_type, payload, created_at, _AVAILABLE),
    )


def _scan(db_path: Path, cursor_id: int = 0, limit: int = 100) -> OutboxBatch:
    conn = open_scraper_readonly(db_path)
    try:
        return ScraperOutboxReader(conn).rows_after(cursor_id, limit)
    finally:
        conn.close()


def test_empty_outbox_returns_empty_batch(scraper_db: Path) -> None:
    batch = _scan(scraper_db)
    assert batch.rows == []
    assert batch.next_cursor is None


def test_returns_all_rows_in_ascending_id_order(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert(w, entity_id="t-1")
        _insert(w, entity_id="t-2")
        _insert(w, entity_id="t-3")

    batch = _scan(scraper_db)
    assert [r.source_event_id for r in batch.rows] == [1, 2, 3]
    assert [r.entity_id for r in batch.rows] == ["t-1", "t-2", "t-3"]
    assert batch.next_cursor == 3


def test_cursor_filters_rows_with_id_greater_than_cursor(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        for n in range(1, 5):
            _insert(w, entity_id=f"t-{n}")

    batch = _scan(scraper_db, cursor_id=2)
    assert [r.source_event_id for r in batch.rows] == [3, 4]
    assert batch.next_cursor == 4


def test_limit_bounds_the_result_set(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        for n in range(10):
            _insert(w, entity_id=f"t-{n}")

    batch = _scan(scraper_db, limit=3)
    assert len(batch.rows) == 3
    assert [r.source_event_id for r in batch.rows] == [1, 2, 3]
    assert batch.next_cursor == 3


def test_well_formed_payload_surfaces_entity_fields(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert(w, event_type="bookmark.created", entity_type="Tweet", entity_id="t-42")

    batch = _scan(scraper_db)
    assert batch.rows == [
        OutboxRow(
            source_event_id=1,
            event_type="bookmark.created",
            entity_type="Tweet",
            entity_id="t-42",
            created_at=_CREATED,
        ),
    ]


def test_invalid_json_payload_is_silently_skipped(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert(w, entity_id="t-1")
        _insert(w, payload="{not json")
        _insert(w, entity_id="t-3")

    batch = _scan(scraper_db)
    assert [r.source_event_id for r in batch.rows] == [1, 3]
    assert batch.next_cursor == 3


def test_missing_or_non_string_entity_fields_are_skipped(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert(w, entity_id="t-1")
        _insert(w, payload=json.dumps({"entityType": "Tweet"}))
        _insert(w, payload=json.dumps({"entityId": "t-orphan"}))
        _insert(w, payload=json.dumps({"entityType": "Tweet", "entityId": 42}))
        _insert(w, payload=json.dumps(["not", "an", "object"]))
        _insert(w, entity_id="t-6")

    batch = _scan(scraper_db)
    assert [r.source_event_id for r in batch.rows] == [1, 6]
    assert batch.next_cursor == 6


def test_next_cursor_advances_past_all_malformed_window(scraper_db: Path) -> None:
    """A window with no parseable rows still reports progress."""
    with _writer(scraper_db) as w:
        for _ in range(4):
            _insert(w, payload="{not json")

    batch = _scan(scraper_db, limit=100)
    assert batch.rows == []
    assert batch.next_cursor == 4


def test_outbox_row_is_frozen(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert(w, entity_id="t-1")

    row = _scan(scraper_db).rows[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.__setattr__("entity_id", "mutated")
