"""Read the scraper's outbox table into minimally-parsed rows.

The classifier reads the scraper's outbox read-only and extracts
only two fields from the JSON payload (``entityType`` and
``entityId``). Everything else in the payload is intentionally
ignored — see the plan's "Envelope mapping (v1 bridge)" section
for the decoupling rationale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class OutboxRow:
    """The subset of a scraper outbox row the classifier consumes."""

    source_event_id: int
    event_type: str
    entity_type: str
    entity_id: str
    created_at: str


@dataclass(frozen=True)
class OutboxBatch:
    """Result of a single ``rows_after`` scan.

    ``rows`` holds only parseable rows. ``next_cursor`` is the
    highest outbox id the scan observed — valid or malformed —
    so a caller can advance past a malformed block instead of
    getting wedged behind it.
    """

    rows: list[OutboxRow]
    next_cursor: int | None


class ScraperOutboxReader:
    """Read-only reader over the scraper's ``outbox`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def rows_after(
        self,
        cursor_id: int,
        limit: int = 100,
    ) -> OutboxBatch:
        """Scan up to ``limit`` rows with ``id > cursor_id``.

        Rows whose payload isn't a JSON object or lacks string
        ``entityType`` / ``entityId`` are skipped silently, but
        their id still counts toward ``next_cursor`` so a
        malformed block cannot wedge a polling loop.
        """
        cursor = self._conn.execute(
            "SELECT id, event_type, payload, created_at "
            "FROM outbox WHERE id > ? ORDER BY id ASC LIMIT ?",
            (cursor_id, limit),
        )
        try:
            raw_rows = cursor.fetchall()
        finally:
            cursor.close()

        rows: list[OutboxRow] = []
        for raw in raw_rows:
            parsed = _extract(raw["payload"])
            if parsed is None:
                continue
            entity_type, entity_id = parsed
            rows.append(
                OutboxRow(
                    source_event_id=raw["id"],
                    event_type=raw["event_type"],
                    entity_type=entity_type,
                    entity_id=entity_id,
                    created_at=raw["created_at"],
                )
            )
        next_cursor = raw_rows[-1]["id"] if raw_rows else None
        return OutboxBatch(rows=rows, next_cursor=next_cursor)


def _extract(payload: str | None) -> tuple[str, str] | None:
    if not isinstance(payload, str):
        return None
    try:
        obj: Any = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    entity_type = obj.get("entityType")
    entity_id = obj.get("entityId")
    if not isinstance(entity_type, str) or not isinstance(entity_id, str):
        return None
    return entity_type, entity_id
