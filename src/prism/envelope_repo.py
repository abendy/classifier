"""SQLite-backed repository for ContentEnvelope persistence."""

from __future__ import annotations

import sqlite3

from prism.envelope import ContentEnvelope


class EnvelopeRepo:
    """CRUD-minimal envelope store over a sqlite3.Connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, envelope: ContentEnvelope) -> None:
        """Insert a new envelope; duplicate id or source_id raises IntegrityError."""
        if envelope.source.source_id is None:
            raise ValueError("envelope.source.source_id is required for save")
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "INSERT INTO content_envelopes "
                "(id, source_id, envelope_json, ingested_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    envelope.identity.id,
                    envelope.source.source_id,
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

    def refresh_by_source_id(self, envelope: ContentEnvelope) -> None:
        """Update the row identified by source_id in place.

        Preserves the stored identity.id and system.created_at;
        every other field is taken from `envelope`. Raises KeyError
        when no row exists for the source_id.
        """
        if envelope.source.source_id is None:
            raise ValueError("envelope.source.source_id is required for refresh")
        existing = self._fetch_by_source_id(envelope.source.source_id)
        if existing is None:
            raise KeyError(envelope.source.source_id)
        merged = _merge_preserving(existing, envelope)
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "UPDATE content_envelopes "
                "SET envelope_json = ?, ingested_at = ? "
                "WHERE source_id = ?",
                (
                    merged.model_dump_json(by_alias=True, exclude_none=True),
                    merged.source.ingested_at.isoformat(),
                    merged.source.source_id,
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

    def exists_by_source_id(self, source_id: str) -> bool:
        """Return True when an envelope with the given source_id is stored."""
        cursor = self._conn.execute(
            "SELECT 1 FROM content_envelopes WHERE source_id = ? LIMIT 1",
            (source_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return row is not None

    def _fetch_by_source_id(self, source_id: str) -> ContentEnvelope | None:
        cursor = self._conn.execute(
            "SELECT envelope_json FROM content_envelopes WHERE source_id = ?",
            (source_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        return ContentEnvelope.model_validate_json(row[0])


# Sections the scraper-side mapper writes today. Anything else belongs
# to a downstream pipeline (classification, routing, state, relationships)
# and must survive a mapper-driven refresh — the mapper has no right to
# erase state it didn't author. This carve-out is a bridge artifact;
# once the scraper emits spec-conformant envelopes directly, the full
# envelope arrives over the wire and per-section merging is moot.
_MAPPER_OWNED_SECTIONS: frozenset[str] = frozenset(
    {"identity", "source", "content", "system"}
)


def _merge_preserving(
    existing: ContentEnvelope, new: ContentEnvelope
) -> ContentEnvelope:
    """Return `new` with identity.id and system.created_at from `existing`,
    and non-mapper-owned sections carried forward from `existing`."""
    data = new.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["identity"]["id"] = existing.identity.id
    data["system"]["createdAt"] = existing.system.created_at.isoformat()
    existing_data = existing.model_dump(mode="json", by_alias=True, exclude_none=True)
    for section, value in existing_data.items():
        if section in _MAPPER_OWNED_SECTIONS:
            continue
        data.setdefault(section, value)
    return ContentEnvelope.model_validate(data)
