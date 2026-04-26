"""Integration tests for the ingest_once orchestration."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite
from prism.embedding import EMBEDDING_DIMENSION, MODEL_VERSION, Embedder
from prism.embedding_repo import EmbeddingRepo
from prism.envelope import ContentEnvelope
from prism.envelope_repo import EnvelopeRepo
from prism.ingest import CURSOR_NAME, IngestStats, ingest_once
from prism.ingest_queue import IngestQueueRepo
from prism.scraper_mapper import open_scraper_readonly
from prism.sync_state import SyncStateRepo
from prism.topic_prototype_repo import TopicPrototypeRepo, TopicPrototypeRow
from prism.tracing import install_in_memory_exporter

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
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
    conn = connect_sqlite(db_path, load_vec=True)
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
    kw.setdefault("service_version", "test")
    kw.setdefault("confidence_threshold", 0.55)
    kw.setdefault("retrieval_top_k", 5)
    kw.setdefault("ollama_model", "qwen2.5:7b-instruct-q4_K_M")
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


class _StubBackend:
    def __init__(
        self,
        vector: np.ndarray | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._vector = (
            vector if vector is not None
            else np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
        )
        self._raises = raises
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> Iterable[np.ndarray]:
        self.calls.append(list(texts))
        if self._raises is not None:
            raise self._raises
        return [self._vector.copy() for _ in texts]


class _StubLlmClient:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self._response = response

    def pick(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        _ = (system, user, schema, timeout_s)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _envelope_id_for(conn: sqlite3.Connection, source_id: str) -> str:
    (envelope_id,) = _fetch_one(
        conn,
        "SELECT id FROM content_envelopes WHERE source_id = ?",
        (source_id,),
    )
    return envelope_id


def _seed_topic_prototype(
    conn: sqlite3.Connection,
    *,
    topic_id: str = "history",
    vector: np.ndarray | None = None,
) -> None:
    prototype_vector = (
        vector
        if vector is not None
        else np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
    )
    TopicPrototypeRepo(conn).save_many(
        [
            TopicPrototypeRow(
                topic_id=topic_id,
                exemplar_idx=0,
                text=f"{topic_id} description",
                model_version=MODEL_VERSION,
                vector=prototype_vector,
                created_at=T0,
            )
        ]
    )


def _pick_payload(topic_id: str, confidence: float) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "confidence": confidence,
        "reasoning": "best match",
    }


def _event_rows(conn: sqlite3.Connection) -> list[tuple[str, dict[str, Any]]]:
    cursor = conn.execute(
        "SELECT event_type, payload FROM events_outbox ORDER BY rowid"
    )
    try:
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [(str(event_type), json.loads(str(payload))) for event_type, payload in rows]


def test_embedder_provided_embeds_each_saved_envelope(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="hello world")

    stub = _StubBackend()
    stats = _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    assert stats.embedded == 1
    assert stats.embed_failed == 0
    assert stats.embed_skipped_no_body == 0
    envelope_id = _envelope_id_for(classifier_conn, "t-1")
    assert EmbeddingRepo(classifier_conn).exists(envelope_id, MODEL_VERSION)


def test_ingest_classify_writes_confident_classification(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="roman archives")
    _seed_topic_prototype(classifier_conn, topic_id="history")

    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0,
        embedder=Embedder(_StubBackend()),
        llm_client=_StubLlmClient(_pick_payload("history", 0.91)),
        confidence_threshold=0.55,
        retrieval_top_k=5,
        ollama_model="test-ollama",
        service_version="prism-test",
    )

    assert stats.classified_confident == 1
    assert stats.classified_low_confidence == 0
    assert _count(classifier_conn, "classifications") == 1
    assert _count(classifier_conn, "topic_assignments") == 1
    confidence, reason = _fetch_one(
        classifier_conn,
        "SELECT confidence, low_confidence_reason FROM classifications",
    )
    assert confidence == 0.91
    assert reason is None
    assert _event_rows(classifier_conn) == [
        (
            "content-ingested",
            {
                "contentType": "post",
                "envelopeId": _envelope_id_for(classifier_conn, "t-1"),
                "service": "x-sync",
                "sourceId": "t-1",
            },
        ),
        (
            "content-classified",
            {
                "classifiedBy": (
                    f"prism@prism-test + {MODEL_VERSION} + test-ollama"
                ),
                "confidence": 0.91,
                "envelopeId": _envelope_id_for(classifier_conn, "t-1"),
                "tags": [],
                "topicAssignments": [
                    {"confidence": 0.91, "topicId": "history"}
                ],
            },
        ),
    ]


@pytest.mark.parametrize(
    ("llm_payload", "expected_reason", "expects_confidence"),
    [
        (_pick_payload("history", 0.42), "below-threshold", True),
        (_pick_payload("", 0.8), "llm-pick-unsure", False),
        (_pick_payload("science", 0.9), "out-of-band", False),
        (
            {"topic_id": "history", "confidence": 1.5, "reasoning": "bad"},
            "llm-output-malformed",
            False,
        ),
    ],
)
def test_ingest_classify_writes_low_confidence(
    scraper_db: Path,
    classifier_conn: sqlite3.Connection,
    llm_payload: dict[str, Any],
    expected_reason: str,
    expects_confidence: bool,
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="roman archives")
    _seed_topic_prototype(classifier_conn, topic_id="history")

    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0,
        embedder=Embedder(_StubBackend()),
        llm_client=_StubLlmClient(llm_payload),
        confidence_threshold=0.55,
        retrieval_top_k=5,
        ollama_model="test-ollama",
        service_version="prism-test",
    )

    assert stats.classified_confident == 0
    assert stats.classified_low_confidence == 1
    assert _count(classifier_conn, "classifications") == 1
    assert _count(classifier_conn, "topic_assignments") == 0
    confidence, reason = _fetch_one(
        classifier_conn,
        "SELECT confidence, low_confidence_reason FROM classifications",
    )
    assert confidence is None
    assert reason == expected_reason
    events = _event_rows(classifier_conn)
    assert [event_type for event_type, _payload in events] == [
        "content-ingested",
        "content-classified-low-confidence",
    ]
    low_payload = events[1][1]
    assert low_payload["reason"] == expected_reason
    assert low_payload["candidateTopics"] == [{"topicId": "history"}]
    assert ("confidence" in low_payload) is expects_confidence
    if expects_confidence:
        assert low_payload["confidence"] == 0.42


def test_ingest_classify_marks_failed_on_empty_retrieval(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="roman archives")

    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0,
        embedder=Embedder(_StubBackend()),
        llm_client=_StubLlmClient(_pick_payload("history", 0.91)),
        confidence_threshold=0.55,
        retrieval_top_k=5,
        ollama_model="test-ollama",
        service_version="prism-test",
    )

    assert stats.classify_failed == 1
    assert _count(classifier_conn, "classifications") == 0
    assert _count(classifier_conn, "topic_assignments") == 0
    status, attempts, last_error = _fetch_one(
        classifier_conn,
        "SELECT status, attempts, last_error FROM ingest_queue "
        "WHERE source_event_id = ?",
        (1,),
    )
    assert status == "pending"
    assert attempts == 1
    assert "classify.retrieval-empty" in last_error
    assert _event_rows(classifier_conn) == [
        (
            "content-ingested",
            {
                "contentType": "post",
                "envelopeId": _envelope_id_for(classifier_conn, "t-1"),
                "service": "x-sync",
                "sourceId": "t-1",
            },
        )
    ]


def test_ingest_classify_marks_failed_on_llm_exception(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="roman archives")
    _seed_topic_prototype(classifier_conn, topic_id="history")

    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0,
        embedder=Embedder(_StubBackend()),
        llm_client=_StubLlmClient(RuntimeError("boom")),
        confidence_threshold=0.55,
        retrieval_top_k=5,
        ollama_model="test-ollama",
        service_version="prism-test",
    )

    assert stats.classify_failed == 1
    assert _count(classifier_conn, "classifications") == 0
    status, attempts, last_error = _fetch_one(
        classifier_conn,
        "SELECT status, attempts, last_error FROM ingest_queue "
        "WHERE source_event_id = ?",
        (1,),
    )
    assert status == "pending"
    assert attempts == 1
    assert "classify.pick" in last_error
    assert _event_rows(classifier_conn) == [
        (
            "content-ingested",
            {
                "contentType": "post",
                "envelopeId": _envelope_id_for(classifier_conn, "t-1"),
                "service": "x-sync",
                "sourceId": "t-1",
            },
        )
    ]


def test_ingest_classify_skipped_when_disabled(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="roman archives")
    _seed_topic_prototype(classifier_conn, topic_id="history")

    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0,
        embedder=Embedder(_StubBackend()),
        llm_client=None,
        service_version="prism-test",
    )

    assert stats.embedded == 1
    assert stats.classified_confident == 0
    assert stats.classified_low_confidence == 0
    assert stats.classify_skipped_no_embedding == 0
    assert stats.classify_failed == 0
    assert _count(classifier_conn, "classifications") == 0
    assert _count(classifier_conn, "topic_assignments") == 0
    assert _count(classifier_conn, "events_outbox") == 0


def test_no_embedder_leaves_embed_counters_and_table_empty(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1")

    stats = _run(scraper_db, classifier_conn, now=T0)

    assert stats.embedded == 0
    assert stats.embed_skipped_no_body == 0
    assert stats.embed_failed == 0
    assert _count(classifier_conn, "embeddings") == 0


def test_empty_body_skips_embedding_without_failing(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="   ")

    stub = _StubBackend()
    stats = _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    assert stats.embedded == 0
    assert stats.embed_skipped_no_body == 1
    assert stats.embed_failed == 0
    assert stats.processed == 1
    assert _count(classifier_conn, "embeddings") == 0
    (status,) = _fetch_one(
        classifier_conn,
        "SELECT status FROM ingest_queue WHERE source_event_id = ?",
        (1,),
    )
    assert status == "done"
    assert stub.calls == []


def test_embed_failure_leaves_queue_item_pending_but_envelope_saved(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="hello")

    stub = _StubBackend(raises=RuntimeError("boom"))
    stats = _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    assert stats.embed_failed == 1
    assert stats.embedded == 0
    assert stats.processed == 1
    assert EnvelopeRepo(classifier_conn).exists_by_source_id("t-1")
    status, attempts = _fetch_one(
        classifier_conn,
        "SELECT status, attempts FROM ingest_queue WHERE source_event_id = ?",
        (1,),
    )
    assert status == "pending"
    assert attempts == 1
    assert _count(classifier_conn, "embeddings") == 0


def test_refresh_path_also_embeds(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    EnvelopeRepo(classifier_conn).save(_minimal_envelope("t-1", "existing-1"))
    with _writer(scraper_db) as w:
        _seed_tweet_event(w, tweet_id="t-1", event_type="record.enriched")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="refreshed body")

    distinctive = np.arange(EMBEDDING_DIMENSION, dtype=np.float32)
    stub = _StubBackend(vector=distinctive)
    stats = _run(
        scraper_db,
        classifier_conn,
        now=T0 + timedelta(seconds=5),
        embedder=Embedder(stub),
    )

    assert stats.refreshed == 1
    assert stats.embedded == 1
    # The embedding is keyed by the *preserved* envelope id, not the
    # per-observation UUID the mapper minted for this pass (ADR 007).
    stored = EmbeddingRepo(classifier_conn).get("existing-1", MODEL_VERSION)
    assert stored is not None
    assert np.array_equal(stored.vector, distinctive)
    assert _count(classifier_conn, "embeddings") == 1


def test_per_envelope_run_row_is_written_with_causation(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    install_in_memory_exporter()
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="hello")

    stub = _StubBackend()
    _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    ops = classifier_conn.execute(
        "SELECT operation FROM runs ORDER BY operation"
    ).fetchall()
    assert [row[0] for row in ops] == ["embed", "ingest"]

    envelope_id = _envelope_id_for(classifier_conn, "t-1")
    (ingest_run_id,) = _fetch_one(
        classifier_conn,
        "SELECT id FROM runs WHERE operation = ?",
        ("ingest",),
    )
    embed_envelope_id, embed_correlation, embed_causation = _fetch_one(
        classifier_conn,
        "SELECT envelope_id, correlation_id, causation_id "
        "FROM runs WHERE operation = ?",
        ("embed",),
    )
    assert embed_envelope_id == envelope_id
    assert embed_correlation == envelope_id
    assert embed_causation == ingest_run_id


def test_embed_span_is_child_of_ingest_span(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    exporter = install_in_memory_exporter()
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="hello")

    stub = _StubBackend()
    _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"ingest", "embed"}
    ingest_span = spans["ingest"]
    embed_span = spans["embed"]
    assert embed_span.parent is not None
    assert ingest_span.context is not None
    assert embed_span.context is not None
    assert embed_span.parent.span_id == ingest_span.context.span_id
    assert embed_span.context.trace_id == ingest_span.context.trace_id
    embed_attrs = dict(embed_span.attributes or {})
    ingest_attrs = dict(ingest_span.attributes or {})
    envelope_id = _envelope_id_for(classifier_conn, "t-1")
    assert embed_attrs["prism.envelope_id"] == envelope_id
    assert embed_attrs["prism.correlation_id"] == envelope_id
    assert embed_attrs["prism.causation_id"] == ingest_attrs["prism.run_id"]


def test_stub_backend_receives_envelope_body(
    scraper_db: Path, classifier_conn: sqlite3.Connection
) -> None:
    with _writer(scraper_db) as w:
        _seed_bookmark_created(w, tweet_id="t-1")
        _seed_user(w)
        _seed_tweet(w, tweet_id="t-1", text="full tweet body")

    stub = _StubBackend()
    _run(scraper_db, classifier_conn, now=T0, embedder=Embedder(stub))

    assert stub.calls == [["full tweet body"]]
