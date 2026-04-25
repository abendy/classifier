"""Storage for embedded topic prototypes.

One row per (topic_id, exemplar_idx, model_version) in a sqlite-vec
``vec0`` virtual table. Aux columns carry the source text and
timestamps for audit. Synthetic TEXT PK encodes the composite
identity (ADR 011); upserts go via DELETE-then-INSERT (ADR 012).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import sqlite_vec

from prism.embedding import EMBEDDING_DIMENSION
from prism.embedding_repo import _ROW_KEY_DELIMITER

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable


@dataclass(frozen=True)
class TopicPrototypeRow:
    topic_id: str
    exemplar_idx: int
    text: str
    model_version: str
    vector: np.ndarray
    created_at: datetime | None = None


@dataclass(frozen=True)
class StoredTopicPrototype:
    topic_id: str
    exemplar_idx: int
    text: str
    model_version: str
    vector: np.ndarray


def _row_key(topic_id: str, exemplar_idx: int, model_version: str) -> str:
    if _ROW_KEY_DELIMITER in topic_id:
        raise ValueError(f"topic_id contains delimiter: {topic_id!r}")
    if _ROW_KEY_DELIMITER in model_version:
        raise ValueError(f"model_version contains delimiter: {model_version!r}")
    return (
        f"{topic_id}{_ROW_KEY_DELIMITER}{exemplar_idx}"
        f"{_ROW_KEY_DELIMITER}{model_version}"
    )


class TopicPrototypeRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def delete_for_topic(self, topic_id: str, model_version: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM topic_prototypes "
            "WHERE topic_id = ? AND model_version = ?",
            (topic_id, model_version),
        )
        try:
            return cursor.rowcount
        finally:
            cursor.close()

    def save_many(self, rows: Iterable[TopicPrototypeRow]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        now_default = datetime.now(UTC)
        seen_topics: set[tuple[str, str]] = set()
        for row in rows:
            key = (row.topic_id, row.model_version)
            if key not in seen_topics:
                self.delete_for_topic(row.topic_id, row.model_version)
                seen_topics.add(key)
        for row in rows:
            if row.vector.dtype != np.float32:
                raise ValueError(f"vector dtype must be float32, got {row.vector.dtype}")
            if row.vector.shape != (EMBEDDING_DIMENSION,):
                raise ValueError(
                    f"vector shape must be ({EMBEDDING_DIMENSION},), "
                    f"got {row.vector.shape}"
                )
            payload = sqlite_vec.serialize_float32(row.vector.tolist())
            self._conn.execute(
                "INSERT INTO topic_prototypes "
                "(id, embedding, topic_id, exemplar_idx, text, "
                "model_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _row_key(row.topic_id, row.exemplar_idx, row.model_version),
                    payload,
                    row.topic_id,
                    row.exemplar_idx,
                    row.text,
                    row.model_version,
                    (row.created_at or now_default).isoformat(),
                ),
            )
        return len(rows)

    def count(self, model_version: str) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM topic_prototypes WHERE model_version = ?",
            (model_version,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return 0 if row is None else int(row[0])

    def list_vectors_for_model(
        self, model_version: str
    ) -> list[StoredTopicPrototype]:
        """Return every prototype for ``model_version``."""
        cursor = self._conn.execute(
            "SELECT topic_id, exemplar_idx, text, embedding "
            "FROM topic_prototypes WHERE model_version = ?",
            (model_version,),
        )
        try:
            raw = cursor.fetchall()
        finally:
            cursor.close()

        out: list[StoredTopicPrototype] = []
        for row in raw:
            vector = np.frombuffer(row[3], dtype=np.float32)
            out.append(
                StoredTopicPrototype(
                    topic_id=str(row[0]),
                    exemplar_idx=int(row[1]),
                    text=str(row[2]),
                    model_version=model_version,
                    vector=vector,
                )
            )
        return out
