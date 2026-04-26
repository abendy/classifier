"""Append-only outbox for classifier-emitted events.

The classifier writes one row here per emitted event. A
future dispatcher reads pending rows and
publishes them to the bus.

The dada.stream event envelope has eight wire fields:
``id``, ``type``, ``source``, ``timestamp``,
``correlationId``, ``causationId``, ``payload``, ``version``.
Seven get their own typed column on the row; ``payload``
stays JSON because its shape varies by event type. ADR 020
pins the rationale.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from uuid_utils import uuid7

from prism.envelope import ENVELOPE_VERSION

EventType = Literal[
    "content-ingested",
    "content-classified",
    "content-classified-low-confidence",
]


@dataclass(frozen=True)
class EmittedEvent:
    """Caller-facing event handle.

    Fields mirror the dada.stream wire envelope on a one-to-
    one basis except for the column-rename conventions
    documented in the module docstring and ADR 020.
    """

    id: str
    event_type: EventType
    source: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    envelope_version: str
    created_at: str
    internal: bool = False

    @classmethod
    def new(
        cls,
        *,
        event_type: EventType,
        source: str,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        envelope_version: str = ENVELOPE_VERSION,
        now: datetime | None = None,
        internal: bool = False,
    ) -> EmittedEvent:
        """Mint a fresh EmittedEvent with a uuid7 id and ISO timestamp.

        Use ``new`` for real emissions; construct the dataclass
        directly only in tests that pin an explicit id or
        timestamp.
        """
        timestamp = (now if now is not None else datetime.now(UTC)).isoformat()
        return cls(
            id=str(uuid7()),
            event_type=event_type,
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
            envelope_version=envelope_version,
            created_at=timestamp,
            internal=internal,
        )


@dataclass(frozen=True)
class EmittedEventRow:
    """Row-shape mirror of the ``events_outbox`` table.

    ``payload`` is the already-serialized JSON string, not the
    dict — mirrors the column, not the caller-facing shape.
    """

    id: str
    event_type: EventType
    source: str
    envelope_version: str
    correlation_id: str
    causation_id: str | None
    payload: str
    created_at: str
    dispatched_at: str | None
    internal: bool


_INSERT_SQL = (
    "INSERT INTO events_outbox "
    "(id, event_type, source, envelope_version, correlation_id, "
    "causation_id, payload, created_at, dispatched_at, internal) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)"
)


class EventOutboxRepo:
    """CRUD over ``events_outbox`` using raw sqlite3."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def enqueue(self, event: EmittedEvent) -> EmittedEventRow:
        """Insert + commit; return the canonical post-mutation row.

        ``dispatched_at`` is always NULL on a freshly enqueued
        row per ADR 014. Raises ``sqlite3.IntegrityError`` on
        duplicate ``id``; callers must mint fresh ids
        (``EmittedEvent.new``) per emission.
        """
        payload_json = json.dumps(
            event.payload,
            sort_keys=True,
            ensure_ascii=False,
        )
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                _INSERT_SQL,
                (
                    event.id,
                    event.event_type,
                    event.source,
                    event.envelope_version,
                    event.correlation_id,
                    event.causation_id,
                    payload_json,
                    event.created_at,
                    int(event.internal),
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
        return EmittedEventRow(
            id=event.id,
            event_type=event.event_type,
            source=event.source,
            envelope_version=event.envelope_version,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            payload=payload_json,
            created_at=event.created_at,
            dispatched_at=None,
            internal=event.internal,
        )
