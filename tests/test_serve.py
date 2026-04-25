"""Unit tests for the serve lifespan + background ingest loop."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pytest
from fastapi import FastAPI

from prism.config import Config
from prism.db import connect_sqlite
from prism.embedding import EMBEDDING_DIMENSION, Embedder
from prism.ingest import IngestStats
from prism.serve import ServeState, ingest_loop, lifespan

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable
    from pathlib import Path


_EMPTY_STATS = IngestStats(0, 0, 0, 0, 0, 0, 0)


def _make_config(
    *, poll_interval_ms: int = 1000, embedding_enabled: bool = True
) -> Config:
    return Config.model_validate(
        {
            "service": {
                "http_port": 3200,
                "manifest_path": "service.json",
            },
            "pipeline": {
                "embedding": {"model": "bge-large-en", "version": "1.5"},
                "topic": {
                    "model": "qwen2.5-7b-instruct",
                    "quantization": "q4_k_m",
                    "confidence_threshold": 0.55,
                    "retrieval_top_k": 5,
                },
                "tags": {"model": "qwen2.5-7b-instruct", "max_tags": 6},
                "sentiment_intent": {"bundled_with": "tags"},
            },
            "ingest": {
                "source": "x-sync-outbox",
                "scraper_db_path": "/tmp/scraper.db",
                "poll_interval_ms": poll_interval_ms,
                "embedding_enabled": embedding_enabled,
            },
            "audit": {
                "phoenix": {"enabled": False, "local_url": "http://x"},
                "runs_retention_days": 90,
            },
            "storage": {
                "sqlite_path": "/tmp/p.db",
                "sqlite_vec": True,
                "duckdb_attach": False,
            },
            "jobs": {
                "backfill": {"batch_size": 200},
                "eval": {"holdout_ratio": 0.2},
                "dspy_compile": {"max_rounds": 5},
            },
        }
    )


def _make_state(embedder: Embedder | None = None) -> ServeState:
    return ServeState(
        scraper_conn=cast("sqlite3.Connection", object()),
        classifier_conn=cast("sqlite3.Connection", object()),
        embedder=embedder,
        task=None,
        shutdown=asyncio.Event(),
    )


class _StubBackend:
    def embed(self, texts: list[str]) -> Iterable[np.ndarray]:
        return iter(
            [np.zeros(EMBEDDING_DIMENSION, dtype=np.float32) for _ in texts]
        )


def test_ingest_loop_calls_ingest_once_repeatedly_until_shutdown() -> None:
    received: list[dict[str, Any]] = []
    embedder = Embedder(_StubBackend())
    state = _make_state(embedder=embedder)

    def stub(**kw: Any) -> IngestStats:
        received.append(kw)
        if len(received) >= 3:
            state.shutdown.set()
        return _EMPTY_STATS

    cfg = _make_config(poll_interval_ms=1)

    asyncio.run(
        asyncio.wait_for(
            ingest_loop(state, cfg, ingest_once_impl=stub), timeout=2.0
        )
    )

    assert len(received) >= 2
    for kw in received:
        assert kw["embedder"] is embedder
        assert kw["scraper_conn"] is state.scraper_conn
        assert kw["classifier_conn"] is state.classifier_conn


def test_ingest_loop_exits_promptly_on_shutdown() -> None:
    state = _make_state()

    def stub(**_: Any) -> IngestStats:
        return _EMPTY_STATS

    # poll_interval is 1 s; shutdown signal must wake the loop in well under
    # 30 s (the backoff cap) and also under the poll_interval — wait_for
    # races shutdown against the timeout.
    cfg = _make_config(poll_interval_ms=1000)

    async def driver() -> None:
        task = asyncio.create_task(
            ingest_loop(state, cfg, ingest_once_impl=stub)
        )
        await asyncio.sleep(0.05)
        state.shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(driver())


def test_loop_exception_logs_and_doubles_backoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded: list[float] = []
    state = _make_state()

    def stub(**_: Any) -> IngestStats:
        raise RuntimeError("boom")

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        recorded.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()
        if len(recorded) >= 5:
            state.shutdown.set()
            return None
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    cfg = _make_config(poll_interval_ms=1000)
    asyncio.run(ingest_loop(state, cfg, ingest_once_impl=stub))

    # interval_s baseline 1.0; failure doubles to 2, 4, 8, 16, then capped at 30
    assert recorded == [2.0, 4.0, 8.0, 16.0, 30.0]
    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line]
    error_events = [e for e in events if e["event"] == "ingest.loop.error"]
    assert len(error_events) == 5
    assert error_events[0]["backoff_seconds"] == 1.0
    assert error_events[1]["backoff_seconds"] == 2.0
    assert "RuntimeError" in error_events[0]["error"]


def test_loop_backoff_resets_after_successful_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[float] = []
    state = _make_state()
    calls = 0

    def stub(**_: Any) -> IngestStats:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom")
        return _EMPTY_STATS

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        recorded.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()
        if len(recorded) >= 4:
            state.shutdown.set()
            return None
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    cfg = _make_config(poll_interval_ms=1000)
    asyncio.run(ingest_loop(state, cfg, ingest_once_impl=stub))

    # iter 1 raises → 2.0; iter 2 raises → 4.0; iter 3 ok → reset to 1.0;
    # iter 4 ok → 1.0
    assert recorded == [2.0, 4.0, 1.0, 1.0]


def test_embedder_none_passes_through_to_ingest_once() -> None:
    received: list[dict[str, Any]] = []
    state = _make_state(embedder=None)

    def stub(**kw: Any) -> IngestStats:
        received.append(kw)
        state.shutdown.set()
        return _EMPTY_STATS

    cfg = _make_config(poll_interval_ms=1)

    asyncio.run(
        asyncio.wait_for(
            ingest_loop(state, cfg, ingest_once_impl=stub), timeout=2.0
        )
    )

    assert len(received) >= 1
    assert received[0]["embedder"] is None


class _FakeConnection:
    def __init__(self, path: Path, *, load_vec: bool) -> None:
        self.path = path
        self.load_vec = load_vec
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _install_lifespan_stubs(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    *,
    opens: list[_FakeConnection] | None = None,
    embedder_factory: Callable[[], Embedder] | None = None,
) -> list[_FakeConnection]:
    captured = opens if opens is not None else []

    def fake_connect_sqlite(path: Path, *, load_vec: bool) -> _FakeConnection:
        conn = _FakeConnection(path, load_vec=load_vec)
        captured.append(conn)
        return conn

    monkeypatch.setattr("prism.serve.connect_sqlite", fake_connect_sqlite)
    monkeypatch.setattr(
        "prism.serve._validate_scraper_db_schema",
        lambda _conn, _path: None,
    )
    monkeypatch.setattr("prism.serve.load_config", lambda _path: cfg)
    monkeypatch.setattr("prism.serve.configure_tracing", lambda _audit: None)
    monkeypatch.setattr(
        "prism.serve.ingest_once",
        lambda **_kw: _EMPTY_STATS,
    )
    if embedder_factory is not None:
        monkeypatch.setattr(
            "prism.embedding.create_default_embedder", embedder_factory
        )
    return captured


def test_lifespan_opens_and_closes_both_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(embedding_enabled=False)
    opens = _install_lifespan_stubs(monkeypatch, cfg)
    app = FastAPI()

    async def driver() -> None:
        async with lifespan(app):
            pass

    asyncio.run(driver())

    assert len(opens) == 2
    scraper, classifier = opens
    assert scraper.path == cfg.ingest.scraper_db_path
    assert scraper.load_vec is False
    assert classifier.path == cfg.storage.sqlite_path
    assert classifier.load_vec is cfg.storage.sqlite_vec
    assert scraper.closed is True
    assert classifier.closed is True


def test_lifespan_gates_embedder_on_embedding_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls = 0
    sentinel_embedder = Embedder(_StubBackend())

    def fake_factory() -> Embedder:
        nonlocal factory_calls
        factory_calls += 1
        return sentinel_embedder

    cfg_off = _make_config(embedding_enabled=False)
    _install_lifespan_stubs(monkeypatch, cfg_off, embedder_factory=fake_factory)
    app_off = FastAPI()

    async def drive_off() -> None:
        async with lifespan(app_off):
            assert app_off.state.prism.embedder is None

    asyncio.run(drive_off())
    assert factory_calls == 0

    cfg_on = _make_config(embedding_enabled=True)
    _install_lifespan_stubs(monkeypatch, cfg_on, embedder_factory=fake_factory)
    app_on = FastAPI()

    async def drive_on() -> None:
        async with lifespan(app_on):
            assert app_on.state.prism.embedder is sentinel_embedder

    asyncio.run(drive_on())
    assert factory_calls == 1


def test_ingest_loop_connection_usable_on_worker_thread(tmp_path: Path) -> None:
    """Regression: ``asyncio.to_thread`` must be able to use SQLite
    connections opened on the main thread. With the default
    ``check_same_thread=True`` this raises
    ``sqlite3.ProgrammingError`` on the worker thread, which landed
    the loop in permanent backoff. Exercising via the real
    ``connect_sqlite`` catches the regression at the thread boundary."""
    scraper_conn = connect_sqlite(tmp_path / "scraper.db", load_vec=False)
    classifier_conn = connect_sqlite(tmp_path / "prism.db", load_vec=False)
    state = ServeState(
        scraper_conn=scraper_conn,
        classifier_conn=classifier_conn,
        embedder=None,
        task=None,
        shutdown=asyncio.Event(),
    )
    observed: list[Exception] = []

    def stub(
        *,
        scraper_conn: sqlite3.Connection,
        classifier_conn: sqlite3.Connection,
        embedder: Embedder | None,
    ) -> IngestStats:
        del embedder
        try:
            scraper_conn.execute("SELECT 1").fetchone()
            classifier_conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            observed.append(exc)
        state.shutdown.set()
        return _EMPTY_STATS

    cfg = _make_config(poll_interval_ms=1)

    try:
        asyncio.run(
            asyncio.wait_for(
                ingest_loop(state, cfg, ingest_once_impl=stub), timeout=2.0
            )
        )
    finally:
        scraper_conn.close()
        classifier_conn.close()

    assert observed == []


def test_lifespan_closes_connections_when_embedder_factory_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: if a pre-yield setup step (typically the embedder
    factory on cold start) raises, both already-opened connections
    still need to close. The outer try/finally wraps acquisition so
    a partial boot unwinds cleanly."""
    cfg = _make_config(embedding_enabled=True)
    opens: list[_FakeConnection] = []

    def exploding_factory() -> Embedder:
        raise RuntimeError("model boot failed")

    _install_lifespan_stubs(
        monkeypatch, cfg, opens=opens, embedder_factory=exploding_factory
    )
    app = FastAPI()

    async def driver() -> None:
        async with lifespan(app):
            pytest.fail("lifespan should have raised during enter")

    with pytest.raises(RuntimeError, match="model boot failed"):
        asyncio.run(driver())

    assert len(opens) == 2
    assert all(c.closed for c in opens)


def test_lifespan_closes_connections_when_task_raises_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: if the background ingest task raises during
    teardown (``await state.task`` propagates), both connections
    must still close. The inner try/finally around ``await
    state.task`` keeps the exception surfacing while ensuring
    deterministic cleanup."""
    cfg = _make_config(embedding_enabled=False)
    opens: list[_FakeConnection] = []
    _install_lifespan_stubs(monkeypatch, cfg, opens=opens)

    async def raising_loop(state: ServeState, _cfg: Config) -> None:
        await state.shutdown.wait()
        raise RuntimeError("task exploded on shutdown")

    monkeypatch.setattr("prism.serve.ingest_loop", raising_loop)
    app = FastAPI()

    async def driver() -> None:
        async with lifespan(app):
            pass

    with pytest.raises(RuntimeError, match="task exploded on shutdown"):
        asyncio.run(driver())

    assert len(opens) == 2
    assert all(c.closed for c in opens)


def test_lifespan_closes_scraper_connection_when_schema_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(embedding_enabled=False)
    opens: list[_FakeConnection] = []
    _install_lifespan_stubs(monkeypatch, cfg, opens=opens)

    def bad_schema(_conn: object, _path: object) -> None:
        raise RuntimeError("missing required table 'outbox'")

    monkeypatch.setattr("prism.serve._validate_scraper_db_schema", bad_schema)
    app = FastAPI()

    async def driver() -> None:
        async with lifespan(app):
            pytest.fail("lifespan should have raised during enter")

    with pytest.raises(RuntimeError, match="required table 'outbox'"):
        asyncio.run(driver())

    assert len(opens) == 1
    assert opens[0].closed is True
