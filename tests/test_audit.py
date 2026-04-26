"""Tests for RunRepo, OperationContext, and the operation() context manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from opentelemetry.trace.status import StatusCode

from prism.audit import OperationContext, RunRepo, operation
from prism.tracing import install_in_memory_exporter

if TYPE_CHECKING:
    import sqlite3

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )


T0 = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)

_COLS = [
    "id",
    "operation",
    "started_at",
    "finished_at",
    "duration_ms",
    "status",
    "envelope_id",
    "correlation_id",
    "causation_id",
    "trace_id",
    "outputs",
    "error",
    "metadata",
]


def _fetch_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM runs WHERE id = ?",
        (run_id,),
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None, f"no row for run_id={run_id}"
    return dict(zip(_COLS, row, strict=True))


def _count_runs(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("SELECT COUNT(*) FROM runs")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return int(row[0])


def _clock(*values: datetime) -> Any:
    """Build a zero-arg callable that yields each datetime once."""
    it = iter(values)
    return lambda: next(it)


def test_record_start_inserts_pending_row(conn: sqlite3.Connection) -> None:
    RunRepo(conn).record_start(
        run_id="run-1",
        operation="ingest",
        started_at=T0,
        envelope_id=None,
        correlation_id=None,
        causation_id=None,
    )
    row = _fetch_run(conn, "run-1")
    assert row["status"] == "pending"
    assert row["operation"] == "ingest"
    assert row["started_at"] == T0.isoformat()
    assert row["finished_at"] is None
    assert row["duration_ms"] is None
    assert row["outputs"] is None
    assert row["error"] is None
    assert row["metadata"] is None
    assert row["trace_id"] is None


def test_record_success_completes_row(conn: sqlite3.Connection) -> None:
    repo = RunRepo(conn)
    repo.record_start(
        run_id="run-2",
        operation="ingest",
        started_at=T0,
        envelope_id=None,
        correlation_id=None,
        causation_id=None,
    )
    finished_at = T0 + timedelta(milliseconds=250)
    repo.record_success(
        "run-2",
        finished_at=finished_at,
        duration_ms=250,
        envelope_id=None,
        correlation_id=None,
        causation_id=None,
        outputs_json='{"observed": 3}',
        metadata_json=None,
    )
    row = _fetch_run(conn, "run-2")
    assert row["status"] == "success"
    assert row["finished_at"] == finished_at.isoformat()
    assert row["duration_ms"] == 250
    assert row["outputs"] == '{"observed": 3}'
    assert row["error"] is None


def test_record_success_on_unknown_run_id_raises_key_error(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with pytest.raises(KeyError):
        repo.record_success(
            "nope",
            finished_at=T0,
            duration_ms=0,
            envelope_id=None,
            correlation_id=None,
            causation_id=None,
            outputs_json=None,
            metadata_json=None,
        )


def test_record_error_marks_the_row(conn: sqlite3.Connection) -> None:
    repo = RunRepo(conn)
    repo.record_start(
        run_id="run-3",
        operation="ingest",
        started_at=T0,
        envelope_id=None,
        correlation_id=None,
        causation_id=None,
    )
    finished_at = T0 + timedelta(milliseconds=42)
    repo.record_error(
        "run-3",
        finished_at=finished_at,
        duration_ms=42,
        envelope_id=None,
        correlation_id=None,
        causation_id=None,
        error="boom",
        outputs_json=None,
        metadata_json=None,
    )
    row = _fetch_run(conn, "run-3")
    assert row["status"] == "error"
    assert row["error"] == "boom"
    assert row["duration_ms"] == 42
    assert row["finished_at"] == finished_at.isoformat()


def test_record_error_on_unknown_run_id_raises_key_error(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with pytest.raises(KeyError):
        repo.record_error(
            "nope",
            finished_at=T0,
            duration_ms=0,
            envelope_id=None,
            correlation_id=None,
            causation_id=None,
            error="x",
            outputs_json=None,
            metadata_json=None,
        )


def test_operation_success_writes_outputs_and_duration(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    clock = _clock(T0, T0 + timedelta(milliseconds=150))
    with operation("x", repo=repo, now=clock) as op:
        op.attach_outputs({"n": 1})
    row = _fetch_run(conn, op.run_id)
    assert row["status"] == "success"
    assert row["outputs"] == '{"n": 1}'
    assert row["duration_ms"] == 150
    assert row["operation"] == "x"
    assert row["error"] is None


def test_operation_records_error_and_reraises(conn: sqlite3.Connection) -> None:
    repo = RunRepo(conn)
    clock = _clock(T0, T0 + timedelta(seconds=1))
    captured_run_id: str | None = None
    with pytest.raises(RuntimeError, match="boom"), operation("x", repo=repo, now=clock) as op:
        captured_run_id = op.run_id
        raise RuntimeError("boom")
    assert captured_run_id is not None
    row = _fetch_run(conn, captured_run_id)
    assert row["status"] == "error"
    assert row["error"].startswith("RuntimeError(")
    assert row["duration_ms"] == 1000


def test_attach_outputs_rejects_non_serializable_input(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op, pytest.raises(TypeError):
        op.attach_outputs({"bad": object()})
    row = _fetch_run(conn, op.run_id)
    assert row["status"] == "success"
    assert row["outputs"] is None


def test_attach_metadata_rejects_non_serializable_input(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op, pytest.raises(TypeError):
        op.attach_metadata({"bad": object()})
    row = _fetch_run(conn, op.run_id)
    assert row["status"] == "success"
    assert row["metadata"] is None


def test_set_correlation_mid_operation_updates_row_on_exit(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op:
        op.set_correlation(envelope_id="env-1", correlation_id="env-1")
    row = _fetch_run(conn, op.run_id)
    assert row["envelope_id"] == "env-1"
    assert row["correlation_id"] == "env-1"
    assert row["causation_id"] is None


def test_set_correlation_later_call_overrides_earlier(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo, envelope_id="seed") as op:
        op.set_correlation(envelope_id="override")
    row = _fetch_run(conn, op.run_id)
    assert row["envelope_id"] == "override"


def test_operation_records_correlation_on_error_exit(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    captured_run_id: str | None = None
    with pytest.raises(ValueError), operation("x", repo=repo) as op:
        captured_run_id = op.run_id
        op.set_correlation(envelope_id="env-err", causation_id="cause-1")
        raise ValueError("nope")
    assert captured_run_id is not None
    row = _fetch_run(conn, captured_run_id)
    assert row["status"] == "error"
    assert row["envelope_id"] == "env-err"
    assert row["causation_id"] == "cause-1"


def test_attach_metadata_stores_serialized_json(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op:
        op.attach_metadata({"k": "v", "n": 2})
    row = _fetch_run(conn, op.run_id)
    stored = json.loads(row["metadata"])
    assert stored == {"k": "v", "n": 2}


def test_attach_outputs_coerces_datetime_uuid_path(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    ts = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    uid = UUID("00000000-0000-0000-0000-000000000001")
    p = Path("/tmp/x")
    with operation("x", repo=repo) as op:
        op.attach_outputs({"when": ts, "uid": uid, "path": p})
    row = _fetch_run(conn, op.run_id)
    stored = json.loads(row["outputs"])
    assert stored == {"when": ts.isoformat(), "uid": str(uid), "path": str(p)}


def test_each_operation_call_writes_exactly_one_row(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    for _ in range(3):
        with operation("x", repo=repo):
            pass
    assert _count_runs(conn) == 3


def test_runs_table_has_expected_columns_in_order(
    conn: sqlite3.Connection,
) -> None:
    cursor = conn.execute("PRAGMA table_info('runs')")
    try:
        names = [row[1] for row in cursor.fetchall()]
    finally:
        cursor.close()
    assert names == _COLS


def test_operation_context_exposes_run_id_and_name(
    conn: sqlite3.Connection,
) -> None:
    repo = RunRepo(conn)
    with operation("classify", repo=repo) as op:
        assert isinstance(op, OperationContext)
        assert op.name == "classify"
        assert op.run_id
        assert len(op.run_id) >= 32


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """Install a fresh in-memory span exporter; return it for assertions."""
    return install_in_memory_exporter()


def test_operation_emits_span_with_operation_name(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with operation("ingest", repo=repo):
        pass
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["ingest"]


def test_operation_span_carries_prism_attributes(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with operation(
        "classify",
        repo=repo,
        envelope_id="env-1",
        correlation_id="corr-1",
        causation_id="cause-1",
    ) as op:
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["prism.operation"] == "classify"
    assert attrs["prism.run_id"] == op.run_id
    assert attrs["prism.envelope_id"] == "env-1"
    assert attrs["prism.correlation_id"] == "corr-1"
    assert attrs["prism.causation_id"] == "cause-1"


def test_operation_populates_trace_id_on_runs_row(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op:
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].context is not None
    expected = format(spans[0].context.trace_id, "032x")
    row = _fetch_run(conn, op.run_id)
    assert row["trace_id"] == expected
    assert len(row["trace_id"]) == 32


def test_operation_exception_marks_span_error_and_records(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with pytest.raises(RuntimeError, match="boom"), operation("x", repo=repo):
        raise RuntimeError("boom")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "RuntimeError" in (span.status.description or "")
    exc_events = [e for e in span.events if e.name == "exception"]
    assert len(exc_events) == 1


def test_nested_operation_produces_parent_child_spans(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with operation("outer", repo=repo), operation("inner", repo=repo):
        pass
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"outer", "inner"}
    outer_span = spans["outer"]
    inner_span = spans["inner"]
    assert outer_span.parent is None
    assert inner_span.parent is not None
    assert outer_span.context is not None
    assert inner_span.context is not None
    assert inner_span.parent.span_id == outer_span.context.span_id
    assert inner_span.context.trace_id == outer_span.context.trace_id


def test_set_correlation_mid_op_mirrors_to_span_attribute(
    conn: sqlite3.Connection, exporter: InMemorySpanExporter
) -> None:
    repo = RunRepo(conn)
    with operation("x", repo=repo) as op:
        op.set_correlation(envelope_id="env-1")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["prism.envelope_id"] == "env-1"


def test_operation_marks_span_error_when_audit_persistence_raises(
    conn: sqlite3.Connection,
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # record_success failing mid-operation would otherwise leave the span
    # UNSET — we want the trace to show the audit-layer failure, not go
    # quiet when the very component responsible for persistence breaks.
    repo = RunRepo(conn)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit-db down")

    monkeypatch.setattr(repo, "record_success", boom)
    with pytest.raises(RuntimeError, match="audit-db down"), operation("x", repo=repo):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "audit-db down" in (span.status.description or "")
    exc_events = [e for e in span.events if e.name == "exception"]
    assert len(exc_events) == 1


def test_operation_records_both_exceptions_when_record_error_also_fails(
    conn: sqlite3.Connection,
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User-block raises, then record_error itself raises. The propagated
    # exception is the record_error failure (last-raised), but the span
    # must surface BOTH: status description stays on the primary user
    # cause, and a second exception event logs the persistence failure
    # so the trace doesn't hide the audit-layer breakage.
    repo = RunRepo(conn)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("record_error boom")

    monkeypatch.setattr(repo, "record_error", boom)
    with pytest.raises(RuntimeError, match="record_error boom"), operation(
        "x", repo=repo
    ):
        raise ValueError("user boom")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "ValueError" in (span.status.description or "")
    exc_events = [e for e in span.events if e.name == "exception"]
    assert len(exc_events) == 2
    types = [e.attributes.get("exception.type") for e in exc_events if e.attributes]
    assert "ValueError" in types
    assert "RuntimeError" in types
