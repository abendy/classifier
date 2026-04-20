"""Background ingest loop and FastAPI lifespan for ``prism serve``.

The lifespan boots once per uvicorn process: it configures tracing,
opens both the scraper-side and classifier-side SQLite connections,
optionally constructs the BGE-M3 embedder, and launches an asyncio
task that calls ``ingest_once`` on a fixed cadence. On shutdown it
sets a stop event, awaits the task, and closes both connections.

The loop deliberately owns no span of its own — ``ingest_once``
opens an ``operation("ingest")`` span per pass, and that's the
only meaningful trace shape; a span covering the whole serve
lifetime would never close in Phoenix. Pass-level errors log a
single ``ingest.loop.error`` event and back off with doubling
sleep up to 30 s; the next successful pass resets the cadence.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prism.config import CONFIG_PATH, load_config
from prism.db import connect_sqlite
from prism.ingest import ingest_once
from prism.logs import log_event
from prism.tracing import configure_tracing

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI

    from prism.config import Config
    from prism.embedding import Embedder
    from prism.ingest import IngestStats


_BACKOFF_CAP_SECONDS = 30.0


@dataclass
class ServeState:
    """Container the lifespan owns and the loop reads from."""

    scraper_conn: sqlite3.Connection
    classifier_conn: sqlite3.Connection
    embedder: Embedder | None
    task: asyncio.Task[None] | None
    shutdown: asyncio.Event


async def ingest_loop(
    state: ServeState,
    cfg: Config,
    *,
    ingest_once_impl: Callable[..., IngestStats] = ingest_once,
) -> None:
    """Run ``ingest_once`` on ``cfg.ingest.poll_interval_ms`` cadence.

    Each pass runs in a worker thread because sqlite3 and fastembed
    are blocking. Pass-level exceptions log ``ingest.loop.error`` and
    double the sleep (capped at 30 s); a successful pass resets the
    cadence. The loop exits when ``state.shutdown`` is set, woken
    immediately via ``asyncio.wait_for`` rather than sleeping out the
    full backoff.
    """
    interval_s = cfg.ingest.poll_interval_ms / 1000
    backoff_s = interval_s
    while not state.shutdown.is_set():
        try:
            await asyncio.to_thread(
                ingest_once_impl,
                scraper_conn=state.scraper_conn,
                classifier_conn=state.classifier_conn,
                embedder=state.embedder,
            )
            backoff_s = interval_s
        except Exception as exc:
            log_event(
                "ingest.loop.error",
                error=repr(exc),
                backoff_seconds=backoff_s,
            )
            backoff_s = min(backoff_s * 2, _BACKOFF_CAP_SECONDS)
        with suppress(TimeoutError):
            await asyncio.wait_for(state.shutdown.wait(), timeout=backoff_s)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: boot tracing + connections + loop, then tear down.

    The embedder factory is imported lazily inside the body so that
    ``import prism.serve`` doesn't pull fastembed (and its ~2 GB
    model dependency) into test processes that never enter the
    lifespan.

    The outer try/finally unwinds partial startup so a failure
    between the first ``connect_sqlite`` and ``yield`` (most
    likely the embedder factory on a cold BGE-M3 download) still
    closes any resources we already acquired. An inner try/finally
    around ``await state.task`` keeps connection cleanup
    deterministic even when the background task surfaces an
    exception on shutdown — the exception still propagates, but
    both connections close and ``serve.stop`` still logs.
    """
    scraper_conn: sqlite3.Connection | None = None
    classifier_conn: sqlite3.Connection | None = None
    state: ServeState | None = None
    try:
        cfg = load_config(CONFIG_PATH)
        configure_tracing(cfg.audit)
        scraper_conn = connect_sqlite(
            cfg.ingest.scraper_db_path, load_vec=False
        )
        classifier_conn = connect_sqlite(
            cfg.storage.sqlite_path, load_vec=cfg.storage.sqlite_vec
        )
        embedder: Embedder | None = None
        if cfg.ingest.embedding_enabled:
            from prism.embedding import create_bge_m3_embedder

            embedder = create_bge_m3_embedder()
        state = ServeState(
            scraper_conn=scraper_conn,
            classifier_conn=classifier_conn,
            embedder=embedder,
            task=None,
            shutdown=asyncio.Event(),
        )
        state.task = asyncio.create_task(ingest_loop(state, cfg))
        log_event(
            "serve.start",
            embedding_enabled=cfg.ingest.embedding_enabled,
            poll_interval_ms=cfg.ingest.poll_interval_ms,
            phoenix_enabled=cfg.audit.phoenix.enabled,
        )
        app.state.prism = state
        yield
    finally:
        try:
            if state is not None and state.task is not None:
                state.shutdown.set()
                await state.task
        finally:
            if scraper_conn is not None:
                scraper_conn.close()
            if classifier_conn is not None:
                classifier_conn.close()
            log_event("serve.stop")
