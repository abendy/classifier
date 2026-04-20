"""Integration tests for the ingest_once orchestration."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.envelope import ContentEnvelope
from prism.envelope_repo import EnvelopeRepo
from prism.ingest import CURSOR_NAME, IngestStats, ingest_once
from prism.ingest_queue import IngestQueueRepo
from prism.scraper_mapper import open_scraper_readonly
from prism.sync_state import SyncStateRepo
from prism.tracing import install_in_memory_exporter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

T0 = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
_TS = "2026-04-19T11:00:00+00:00"
EMPTY_STATS = IngestStats(0, 0, 0, 0, 0, 0, 0)

_SCRAPER_DDL = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL, available_at TEXT NOT NULL,
    dispatched_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE tweets (id TEXT PRIMARY KEY, text TEXT, author_id TEXT,
    created_at TEXT, lang TEXT, full_json TEXT, unavailable_at TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, username TEXT);
CREATE TABLE media (tweet_id TEXT, type TEXT, url TEXT, preview_image_url TEXT);
"""


@pytest.fixture
def scraper_db(tmp_path: Path) -> Path:
    path = tmp_path / "scraper.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCRAPER_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def classifier_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    conn = connect_sqlite(db_path, load_vec=False)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _writer(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _seed_bookmark_created(
    conn: sqlite3.Connection, *, tweet_id: str = "t-1", folder: str = "root"
) -> None:
    payload = json.dumps({"entityType": "bookmark", "entityId": f"{tweet_id}:{folder}"})
    conn.execute(
        "INSERT INTO outbox (event_type, payload, created_at, available_at) "
        "VALUES (?, ?, ?, ?)",
        ("bookmark.created", payload, _TS, _TS),
    )


def _seed_tweet_event(
    conn: sqlite3.Connection,
    *,
    tweet_id: str = "t-1",
    event_type: str = "record.synced",
) -> None:
    payload = json.dumps({"entityType": "tweet", "entityId": tweet_id})
    conn.execute(
        "INSERT INTO outbox (event_type, payload, created_at, available_at) "
        "VALUES (?, ?, ?, ?)",
        (event_type, payload, _TS, _TS),
    )


def _seed_raw_outbox(
    conn: sqlite3.Connection, *, event_type: str, payload: str
) -> None:
    conn.execute(
        "INSERT INTO outbox (event_type, payload, created_at, available_at) "
        "VALUES (?, ?, ?, ?)",
        (event_type, payload, _TS, _TS),
    )


def _seed_tweet(
    conn: sqlite3.Connection,
    *,
    tweet_id: str = "t-1",
    text: str = "hello world",
    full_json: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tweets "
        "(id, text, author_id, created_at, lang, full_json, unavailable_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tweet_id, text, "u-1", _TS, "en", full_json, None),
    )


def _upgrade_tweet_to_full(
    conn: sqlite3.Connection,
    *,
    tweet_id: str,
    text: str,
    full_json: str,
) -> None:
    conn.execute(
        "UPDATE tweets SET text = ?, full_json = ? WHERE id = ?",
        (text, full_json, tweet_id),
    )


def _seed_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO users (id, name, username) VALUES ('u-1', 'Ada', 'ada')"
    )


def _run(
    scraper_db: Path, classifier_conn: sqlite3.Connection, **kw: Any
) -> IngestStats:
    scraper_conn = open_scraper_readonly(scraper_db)
    try:
        return ingest_once(
            scraper_conn=scraper_conn, classifier_conn=classifier_conn, **kw
        )
    finally:
        scraper_conn.close()


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> tuple:
    cursor = conn.execute(sql, params)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    assert row is not None
    return row


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(_fetch_one(conn, f"SELECT COUNT(*) FROM {table}")[0])


def _minimal_envelope(source_id: str, envelope_id: str) -> ContentEnvelope:
    return ContentEnvelope.model_validate({
        "identity": {"id": envelope_id},
        "source": {"service": "x-sync", "sourceId": source_id, "ingestedAt": T0},
        "content": {"type": "post"},
        "system": {"version": "1.0.0", "createdAt": T0, "updatedAt": T0},
    })


def test_empty_scraper_is_a_no_op(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats == EMPTY_STATS
    assert _count(classifier_conn, "ingest_queue") == 0
    assert _count(classifier_conn, "content_envelopes") == 0


def test_bookmark_created_saves_a_new_envelope(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.observed == 1
    assert stats.processed == 1
    assert stats.refreshed == 0
    assert stats.skipped_already_saved == 0
    assert stats.mapper_returned_none == 0
    assert stats.failed == 0
    assert stats.malformed == 0
    assert EnvelopeRepo(classifier_conn).exists_by_source_id("t-1") is True


def test_bookmark_with_folder_id_extracts_tweet_id(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1", folder="folder-a")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.processed == 1
    assert EnvelopeRepo(classifier_conn).exists_by_source_id("t-1") is True


def test_malformed_bookmark_entity_id_is_counted(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_raw_outbox(
            w,
            event_type="bookmark.created",
            payload=json.dumps({"entityType": "bookmark", "entityId": "no-colon-here"}),
        )
        _seed_raw_outbox(
            w,
            event_type="bookmark.created",
            payload=json.dumps({"entityType": "bookmark", "entityId": ":only-folder"}),
        )

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.malformed == 2
    assert stats.observed == 0
    assert _count(classifier_conn, "ingest_queue") == 0


def test_non_handled_entity_types_and_events_are_dropped(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_raw_outbox(
            w,
            event_type="user.synced",
            payload=json.dumps({"entityType": "user", "entityId": "u-9"}),
        )
        _seed_raw_outbox(
            w,
            event_type="media.synced",
            payload=json.dumps({"entityType": "media", "entityId": "m-9"}),
        )
        _seed_raw_outbox(
            w,
            event_type="bookmark.deleted",
            payload=json.dumps({"entityType": "bookmark", "entityId": "t-1:root"}),
        )

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.observed == 0
    assert stats.malformed == 0
    assert _count(classifier_conn, "ingest_queue") == 0


def test_tweet_event_for_unbookmarked_tweet_is_dropped(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    """Tweet events without a pre-existing envelope drop at poll time."""
    with _writer(scraper_db) as w:
        _seed_tweet_event(w, tweet_id="t-1", event_type="record.synced")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.observed == 0
    assert _count(classifier_conn, "ingest_queue") == 0


def test_tweet_event_refreshes_existing_envelope(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    EnvelopeRepo(classifier_conn).save(_minimal_envelope("t-1", "existing-1"))

    with _writer(scraper_db) as w:
        _seed_tweet_event(w, tweet_id="t-1", event_type="record.enriched")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0 + timedelta(seconds=5))
    assert stats.observed == 1
    assert stats.refreshed == 1
    assert stats.processed == 0

    refreshed = EnvelopeRepo(classifier_conn).get("existing-1")
    assert refreshed is not None
    assert refreshed.identity.id == "existing-1"
    assert refreshed.system.created_at == T0
    assert refreshed.system.updated_at > T0


def test_mapper_returns_none_marks_queue_failed(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-ghost")

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.observed == 1
    assert stats.mapper_returned_none == 1
    assert stats.processed == 0

    status, attempts = _fetch_one(
        classifier_conn,
        "SELECT status, attempts FROM ingest_queue WHERE source_event_id = ?",
        (1,),
    )
    assert status == "pending"
    assert attempts == 1


def test_repeated_call_with_no_new_rows_is_idempotent(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    first = _run(scraper_db, classifier_conn, now=T0)
    assert first.processed == 1

    second = _run(scraper_db, classifier_conn, now=T0 + timedelta(seconds=10))
    assert second == EMPTY_STATS


def test_cursor_persists_across_calls(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        for n in (1, 2, 3):
            _seed_bookmark_created(w, tweet_id=f"t-{n}")
            _seed_tweet(w, tweet_id=f"t-{n}")
        _seed_user(w)

    first = _run(scraper_db, classifier_conn, poll_limit=2, now=T0)
    assert first.observed == 2
    assert SyncStateRepo(classifier_conn).get(CURSOR_NAME) == "2"

    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-4")
        _seed_tweet(w, tweet_id="t-4")

    second = _run(
        scraper_db, classifier_conn, poll_limit=100, now=T0 + timedelta(seconds=5),
    )
    assert second.observed == 2
    assert SyncStateRepo(classifier_conn).get(CURSOR_NAME) == "4"


def test_cursor_advances_past_malformed_window(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        for _ in range(4):
            _seed_raw_outbox(w, event_type="record.synced", payload="{not json")
        _seed_bookmark_created(w, tweet_id="t-5")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-5")

    stats = _run(scraper_db, classifier_conn, now=T0)
    assert stats.observed == 1
    assert SyncStateRepo(classifier_conn).get(CURSOR_NAME) == "5"
    assert _count(classifier_conn, "ingest_queue") == 1


def test_pre_saved_envelope_plus_pending_item_refreshes(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    """Queue item whose envelope already exists refreshes rather than skipping."""
    IngestQueueRepo(classifier_conn).observe(
        source_event_id=1, source_id="t-1", entity_type="tweet",
        event_type="record.enriched", now=T0,
    )
    EnvelopeRepo(classifier_conn).save(_minimal_envelope("t-1", "existing-1"))
    with _writer(scraper_db) as w:
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0 + timedelta(seconds=5))
    assert stats.refreshed == 1
    assert stats.processed == 0
    assert stats.skipped_already_saved == 0
    (status,) = _fetch_one(
        classifier_conn,
        "SELECT status FROM ingest_queue WHERE source_event_id = ?",
        (1,),
    )
    assert status == "done"


def test_stub_envelope_is_upgraded_by_later_enrichment_event(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    """End-to-end regression for the original stale-envelope bug.

    First ingest: stub tweet (full_json NULL) + bookmark.created → envelope
    saved with isComplete=False and minimal body. Later: scraper enriches
    the tweet and emits record.enriched → refresh path updates the stored
    envelope so isComplete flips to True and the body actually improves.
    """
    with _writer(scraper_db) as w:
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="short", full_json=None)
        _seed_bookmark_created(w, tweet_id="t-1")

    first = _run(scraper_db, classifier_conn, now=T0)
    assert first.processed == 1
    assert first.refreshed == 0

    (envelope_id,) = _fetch_one(
        classifier_conn,
        "SELECT id FROM content_envelopes WHERE source_id = ?",
        ("t-1",),
    )
    repo = EnvelopeRepo(classifier_conn)
    stored_before = repo.get(envelope_id)
    assert stored_before is not None
    assert stored_before.source.source_data == {"isComplete": False}
    assert stored_before.content.body == "short"
    before_created_at = stored_before.system.created_at

    later = T0 + timedelta(minutes=5)
    with _writer(scraper_db) as w:
        _upgrade_tweet_to_full(
            w,
            tweet_id="t-1",
            text="full tweet body with details",
            full_json='{"entities": {"urls": []}}',
        )
        _seed_tweet_event(w, tweet_id="t-1", event_type="record.enriched")

    second = _run(scraper_db, classifier_conn, now=later)
    assert second.refreshed == 1
    assert second.processed == 0

    stored_after = repo.get(envelope_id)
    assert stored_after is not None
    assert stored_after.source.source_data == {"isComplete": True}
    assert stored_after.content.body == "full tweet body with details"
    assert stored_after.system.created_at == before_created_at
    assert stored_after.system.updated_at > before_created_at


def test_ingest_once_writes_exactly_one_run_row_per_call(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)

    (count,) = _fetch_one(
        classifier_conn,
        "SELECT COUNT(*) FROM runs WHERE operation = ?",
        ("ingest",),
    )
    assert count == 1

    (outputs_json, status) = _fetch_one(
        classifier_conn,
        "SELECT outputs, status FROM runs WHERE operation = ? "
        "ORDER BY started_at DESC LIMIT 1",
        ("ingest",),
    )
    assert status == "success"
    assert json.loads(outputs_json) == asdict(stats)


def test_ingest_once_emits_start_and_done_log_events(
    scraper_db: Path,
    classifier_conn: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)

    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line]
    assert [e["event"] for e in events] == ["ingest.start", "ingest.done"]
    assert events[0]["run_id"] == events[1]["run_id"]
    for key in asdict(stats):
        assert events[1][key] == getattr(stats, key)


def test_ingest_once_omits_trace_id_when_tracing_unconfigured(
    scraper_db: Path,
    classifier_conn: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When no provider is installed, "everything else works identically"
    # per the tracing slice contract: the ingest.start/done log events
    # are emitted, but trace_id is absent (not null) since there's no
    # recording span to source it from.
    import opentelemetry.trace as _trace_api

    monkeypatch.setattr(_trace_api, "_TRACER_PROVIDER", None)

    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    _run(scraper_db, classifier_conn, now=T0)

    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line]
    assert [e["event"] for e in events] == ["ingest.start", "ingest.done"]
    for event in events:
        assert "trace_id" not in event
        assert "span_id" not in event


def test_ingest_once_populates_trace_id_and_canonical_log_fields(
    scraper_db: Path,
    classifier_conn: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_in_memory_exporter()
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    _run(scraper_db, classifier_conn, now=T0)

    (trace_id,) = _fetch_one(
        classifier_conn,
        "SELECT trace_id FROM runs WHERE operation = ?",
        ("ingest",),
    )
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)

    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line]
    assert [e["event"] for e in events] == ["ingest.start", "ingest.done"]
    for event in events:
        assert event["service"] == "prism"
        assert event["operation"] == "ingest"
        assert event["trace_id"] == trace_id
    # ingest.start is auto-injected via the active span; ingest.done fires
    # after record_success so its trace_id is threaded explicitly from the
    # operation context (same trace_id either way — the spec's "canonical
    # log shape" on a per-operation basis).
    assert isinstance(events[1]["duration_ms"], int)
