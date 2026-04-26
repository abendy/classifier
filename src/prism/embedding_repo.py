"""SQLite-backed repository for dense envelope embeddings."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import sqlite_vec

from prism.embedding import EMBEDDING_DIMENSION
from prism.vec_keys import ROW_KEY_DELIMITER, encode


@dataclass(frozen=True)
class StoredEmbedding:
    envelope_id: str
    model_version: str
    created_at: str
    vector: np.ndarray


def _row_key(envelope_id: str, model_version: str) -> str:
    return encode(envelope_id, model_version)


class EmbeddingRepo:
    """Vector store over a sqlite3.Connection with the sqlite-vec extension loaded.

    The vec0 table uses a single TEXT PK that encodes the
    ``(envelope_id, model_version)`` pair, so re-embedding under a new
    model version coexists with the prior row rather than overwriting it.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(
        self,
        *,
        envelope_id: str,
        model_version: str,
        vector: np.ndarray,
        now: datetime | None = None,
    ) -> None:
        """Upsert the row for ``(envelope_id, model_version)``. Commits."""
        if ROW_KEY_DELIMITER in envelope_id:
            raise ValueError(
                "envelope_id must not contain the row-key delimiter"
            )
        if ROW_KEY_DELIMITER in model_version:
            raise ValueError(
                "model_version must not contain the row-key delimiter"
            )
        if vector.dtype != np.float32:
            raise ValueError(
                f"vector dtype must be float32, got {vector.dtype}"
            )
        if vector.shape != (EMBEDDING_DIMENSION,):
            raise ValueError(
                f"vector shape must be ({EMBEDDING_DIMENSION},), got {vector.shape}"
            )
        created_at = (now or datetime.now(UTC)).isoformat()
        payload = sqlite_vec.serialize_float32(vector.tolist())
        key = _row_key(envelope_id, model_version)
        # vec0 virtual tables reject INSERT OR REPLACE, so upsert is
        # expressed as DELETE-then-INSERT inside a single transaction.
        delete_cursor: sqlite3.Cursor | None = None
        insert_cursor: sqlite3.Cursor | None = None
        try:
            delete_cursor = self._conn.execute(
                "DELETE FROM embeddings WHERE id = ?",
                (key,),
            )
            insert_cursor = self._conn.execute(
                "INSERT INTO embeddings "
                "(id, embedding, envelope_id, model_version, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, payload, envelope_id, model_version, created_at),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if delete_cursor is not None:
                delete_cursor.close()
            if insert_cursor is not None:
                insert_cursor.close()

    def get(
        self, envelope_id: str, model_version: str
    ) -> StoredEmbedding | None:
        """Fetch the stored embedding for the pair, or None when missing."""
        cursor = self._conn.execute(
            "SELECT envelope_id, embedding, model_version, created_at "
            "FROM embeddings WHERE id = ?",
            (_row_key(envelope_id, model_version),),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        vector = np.frombuffer(row[1], dtype=np.float32)
        return StoredEmbedding(
            envelope_id=row[0],
            model_version=row[2],
            created_at=row[3],
            vector=vector,
        )

    def exists(self, envelope_id: str, model_version: str) -> bool:
        """Return True when a row exists for ``(envelope_id, model_version)``."""
        cursor = self._conn.execute(
            "SELECT 1 FROM embeddings WHERE id = ? LIMIT 1",
            (_row_key(envelope_id, model_version),),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return row is not None
