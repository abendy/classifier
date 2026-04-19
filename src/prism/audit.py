"""Run audit records and the ``operation()`` context manager.

Every audited pipeline step (ingest pass today; classify, embed,
eval as later slices bring them online) enters ``operation()``,
which writes a ``pending`` row into the ``runs`` table on entry
and updates it to ``success`` or ``error`` on exit. One flat
operation per call for now — nested spans + correlation/causation
threading arrive with the OpenTelemetry slice.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from uuid_utils import uuid7

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass(frozen=True)
class RunRow:
    """Row-shape mirror of the ``runs`` table.

    ``outputs`` and ``metadata`` are the already-serialized JSON
    strings, not the dicts — mirrors the column, not the caller-
    facing shape.
    """

    id: str
    operation: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    status: str
    envelope_id: str | None
    correlation_id: str | None
    causation_id: str | None
    trace_id: str | None
    outputs: str | None
    error: str | None
    metadata: str | None


class RunRepo:
    """CRUD over ``runs`` using raw sqlite3."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record_start(
        self,
        *,
        run_id: str,
        operation: str,
        started_at: datetime,
        envelope_id: str | None,
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        """Insert a row with ``status='pending'``. Commits."""
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "INSERT INTO runs "
                "(id, operation, started_at, status, envelope_id, "
                "correlation_id, causation_id) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    run_id,
                    operation,
                    started_at.isoformat(),
                    envelope_id,
                    correlation_id,
                    causation_id,
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()

    def record_success(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        duration_ms: int,
        envelope_id: str | None,
        correlation_id: str | None,
        causation_id: str | None,
        outputs_json: str | None,
        metadata_json: str | None,
    ) -> None:
        """Update row to ``status='success'`` and populate outputs.

        Raises KeyError if the run_id row doesn't exist. Commits.
        """
        self._finalize(
            run_id,
            status="success",
            finished_at=finished_at,
            duration_ms=duration_ms,
            envelope_id=envelope_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            error=None,
            outputs_json=outputs_json,
            metadata_json=metadata_json,
        )

    def record_error(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        duration_ms: int,
        envelope_id: str | None,
        correlation_id: str | None,
        causation_id: str | None,
        error: str,
        outputs_json: str | None,
        metadata_json: str | None,
    ) -> None:
        """Update row to ``status='error'`` and populate error + outputs.

        Raises KeyError if the run_id row doesn't exist. Commits.
        """
        self._finalize(
            run_id,
            status="error",
            finished_at=finished_at,
            duration_ms=duration_ms,
            envelope_id=envelope_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            error=error,
            outputs_json=outputs_json,
            metadata_json=metadata_json,
        )

    def _finalize(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        duration_ms: int,
        envelope_id: str | None,
        correlation_id: str | None,
        causation_id: str | None,
        error: str | None,
        outputs_json: str | None,
        metadata_json: str | None,
    ) -> None:
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = self._conn.execute(
                "UPDATE runs SET finished_at = ?, duration_ms = ?, "
                "status = ?, envelope_id = ?, correlation_id = ?, "
                "causation_id = ?, outputs = ?, error = ?, metadata = ? "
                "WHERE id = ?",
                (
                    finished_at.isoformat(),
                    duration_ms,
                    status,
                    envelope_id,
                    correlation_id,
                    causation_id,
                    outputs_json,
                    error,
                    metadata_json,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise KeyError(run_id)
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()


@dataclass
class _OperationState:
    run_id: str
    name: str
    envelope_id: str | None
    correlation_id: str | None
    causation_id: str | None
    outputs_json: str | None = field(default=None)
    metadata_json: str | None = field(default=None)


class OperationContext:
    """Handle yielded by ``operation()`` for annotating an in-flight run.

    ``run_id`` and ``name`` are read-only. Outputs, metadata, and
    correlation fields are set through the public methods; the
    stored values flush to the row on context-manager exit.
    """

    def __init__(self, state: _OperationState) -> None:
        self._state = state

    @property
    def run_id(self) -> str:
        return self._state.run_id

    @property
    def name(self) -> str:
        return self._state.name

    def attach_outputs(self, outputs: dict[str, Any]) -> None:
        """Serialize + store outputs; raise TypeError eagerly if not JSON-safe."""
        self._state.outputs_json = _serialize(outputs)

    def attach_metadata(self, metadata: dict[str, Any]) -> None:
        """Serialize + store metadata; raise TypeError eagerly if not JSON-safe."""
        self._state.metadata_json = _serialize(metadata)

    def set_correlation(
        self,
        *,
        envelope_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Update correlation fields; the latest non-None value wins per field.

        Passing None for a field leaves the current value untouched so callers
        can update one field without disturbing the others.
        """
        if envelope_id is not None:
            self._state.envelope_id = envelope_id
        if correlation_id is not None:
            self._state.correlation_id = correlation_id
        if causation_id is not None:
            self._state.causation_id = causation_id


@contextmanager
def operation(
    name: str,
    *,
    repo: RunRepo,
    envelope_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> Iterator[OperationContext]:
    """Record a pipeline step as one ``runs`` row.

    On entry: mints a uuid7 ``run_id`` and writes a ``pending`` row.
    On clean exit: updates the row to ``success``. On exception:
    updates the row to ``error`` and re-raises.
    """
    clock = now if now is not None else _utc_now
    started_at = clock()
    run_id = str(uuid7())
    repo.record_start(
        run_id=run_id,
        operation=name,
        started_at=started_at,
        envelope_id=envelope_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    state = _OperationState(
        run_id=run_id,
        name=name,
        envelope_id=envelope_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    ctx = OperationContext(state)
    try:
        yield ctx
    except Exception as exc:
        finished_at = clock()
        repo.record_error(
            run_id,
            finished_at=finished_at,
            duration_ms=_duration_ms(started_at, finished_at),
            envelope_id=state.envelope_id,
            correlation_id=state.correlation_id,
            causation_id=state.causation_id,
            error=repr(exc),
            outputs_json=state.outputs_json,
            metadata_json=state.metadata_json,
        )
        raise
    finished_at = clock()
    repo.record_success(
        run_id,
        finished_at=finished_at,
        duration_ms=_duration_ms(started_at, finished_at),
        envelope_id=state.envelope_id,
        correlation_id=state.correlation_id,
        causation_id=state.causation_id,
        outputs_json=state.outputs_json,
        metadata_json=state.metadata_json,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _duration_ms(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() * 1000)


def _json_default(obj: object) -> str:
    """Coerce the types ``asdict``-ish payloads commonly carry.

    Stays narrow on purpose: ``{"bad": object()}`` must raise
    ``TypeError`` so ``attach_outputs`` surfaces bad inputs at the
    call site rather than letting ``str(obj)`` silently hide them.
    """
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, UUID | Path):
        return str(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _serialize(value: dict[str, Any]) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True)
