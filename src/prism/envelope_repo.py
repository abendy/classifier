"""SQLite-backed repository for ContentEnvelope persistence."""

from __future__ import annotations

import sqlite3

from prism.envelope import ContentEnvelope


class EnvelopeRepo:
    """CRUD-minimal envelope store over a sqlite3.Connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, envelope: ContentEnvelope) -> None:
        """Insert a new envelope; duplicate id raises IntegrityError."""
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "INSERT INTO content_envelopes "
                "(id, envelope_json, ingested_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    envelope.identity.id,
                    envelope.model_dump_json(by_alias=True, exclude_none=True),
                    envelope.source.ingested_at.isoformat(),
                    envelope.system.created_at.isoformat(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()

    def get(self, envelope_id: str) -> ContentEnvelope | None:
        """Return the envelope for id, or None when missing."""
        cursor = self._conn.execute(
            "SELECT envelope_json FROM content_envelopes WHERE id = ?",
            (envelope_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        return ContentEnvelope.model_validate_json(row[0])

    def exists(self, envelope_id: str) -> bool:
        """Return True when an envelope with id is stored."""
        cursor = self._conn.execute(
            "SELECT 1 FROM content_envelopes WHERE id = ? LIMIT 1",
            (envelope_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return bool(row)
