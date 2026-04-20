"""One-pass orchestration from scraper outbox to saved envelopes."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from prism.audit import RunRepo, operation
from prism.embedding import EMBEDDING_DIMENSION, MODEL_VERSION
from prism.embedding_repo import EmbeddingRepo
from prism.envelope_repo import EnvelopeRepo
from prism.ingest_queue import IngestQueueRepo
from prism.logs import log_event
from prism.scraper_mapper import ScraperMapper
from prism.scraper_outbox import ScraperOutboxReader
from prism.sync_state import SyncStateRepo

if TYPE_CHECKING:
    from datetime import datetime

    from prism.embedding import Embedder
    from prism.scraper_outbox import OutboxRow

CURSOR_NAME = "x-sync-outbox-cursor"
BOOKMARK_ENTITY_TYPE = "bookmark"
BOOKMARK_CREATED_EVENT = "bookmark.created"
TWEET_ENTITY_TYPE = "tweet"
TWEET_REFRESH_EVENTS: frozenset[str] = frozenset({"record.synced", "record.enriched"})


@dataclass(frozen=True)
class IngestStats:
    """Counters for a single ``ingest_once`` pass."""

    observed: int
    skipped_already_saved: int
    processed: int
    refreshed: int
    mapper_returned_none: int
    failed: int
    malformed: int
    embedded: int = 0
    embed_skipped_no_body: int = 0
    embed_failed: int = 0


def ingest_once(
    *,
    scraper_conn: sqlite3.Connection,
    classifier_conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    poll_limit: int = 100,
    process_limit: int = 100,
    now: datetime | None = None,
) -> IngestStats:
    """Run one poll-then-process pass over the ingest pipeline."""
    sync_repo = SyncStateRepo(classifier_conn)
    queue_repo = IngestQueueRepo(classifier_conn)
    envelope_repo = EnvelopeRepo(classifier_conn)
    embedding_repo = EmbeddingRepo(classifier_conn)
    mapper = ScraperMapper(scraper_conn)
    outbox = ScraperOutboxReader(scraper_conn)
    run_repo = RunRepo(classifier_conn)

    started_monotonic = time.monotonic()
    with operation("ingest", repo=run_repo) as op:
        run_id = op.run_id
        # Capture the trace id while the operation span is current so the
        # post-block ingest.done event can reference it explicitly; log_event
        # auto-injects trace_id only inside a recording span, and ingest.done
        # fires after the span has closed (see "no log on error" below).
        span_ctx = trace.get_current_span().get_span_context()
        trace_id = format(span_ctx.trace_id, "032x") if span_ctx.is_valid else None
        log_event("ingest.start", run_id=run_id, operation="ingest")
        observed, malformed = _poll(
            outbox, queue_repo, sync_repo, envelope_repo, poll_limit, now
        )
        (
            skipped,
            processed,
            refreshed,
            mapped_none,
            failed,
            embedded,
            embed_skipped_no_body,
            embed_failed,
        ) = _process(
            queue_repo,
            envelope_repo,
            mapper,
            process_limit,
            now,
            embedder=embedder,
            run_repo=run_repo,
            parent_run_id=run_id,
            embedding_repo=embedding_repo,
        )
        stats = IngestStats(
            observed=observed,
            skipped_already_saved=skipped,
            processed=processed,
            refreshed=refreshed,
            mapper_returned_none=mapped_none,
            failed=failed,
            malformed=malformed,
            embedded=embedded,
            embed_skipped_no_body=embed_skipped_no_body,
            embed_failed=embed_failed,
        )
        op.attach_outputs(asdict(stats))
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    # Only after the run is durably marked success does the terminal
    # event fire — a failed record_success would raise above, and
    # the "no log on error" rule leaves no success-shaped line behind.
    # When tracing isn't configured, trace_id is None; omit the key
    # entirely rather than emit a null sentinel field.
    trace_fields = {"trace_id": trace_id} if trace_id is not None else {}
    log_event(
        "ingest.done",
        run_id=run_id,
        operation="ingest",
        duration_ms=duration_ms,
        **trace_fields,
        **asdict(stats),
    )
    return stats


def _poll(
    outbox: ScraperOutboxReader,
    queue_repo: IngestQueueRepo,
    sync_repo: SyncStateRepo,
    envelope_repo: EnvelopeRepo,
    poll_limit: int,
    now: datetime | None,
) -> tuple[int, int]:
    cursor_id = int(sync_repo.get(CURSOR_NAME) or "0")
    batch = outbox.rows_after(cursor_id, limit=poll_limit)
    observed = 0
    malformed = 0
    for row in batch.rows:
        resolved = _resolve_source_id(row, envelope_repo)
        if resolved is None:
            continue
        source_id, is_malformed = resolved
        if is_malformed:
            malformed += 1
            continue
        inserted = queue_repo.observe(
            source_event_id=row.source_event_id,
            source_id=source_id,
            entity_type=row.entity_type,
            event_type=row.event_type,
            now=now,
        )
        if inserted:
            observed += 1
    if batch.next_cursor is not None:
        sync_repo.set(CURSOR_NAME, str(batch.next_cursor), now=now)
    return observed, malformed


def _resolve_source_id(
    row: OutboxRow, envelope_repo: EnvelopeRepo
) -> tuple[str, bool] | None:
    """Return (source_id, is_malformed) for rows we act on, None to drop silently.

    - bookmark.created → tweet id extracted from the "tweetId:folderId"
      entity_id; a malformed entity_id returns an empty source_id with
      is_malformed=True so the caller can count it.
    - tweet record.synced/record.enriched → enqueue only when an envelope
      already exists for that tweet id (refresh path).
    - anything else drops silently.
    """
    if row.entity_type == BOOKMARK_ENTITY_TYPE and row.event_type == BOOKMARK_CREATED_EVENT:
        tweet_id = _extract_tweet_id(row.entity_id)
        if tweet_id is None:
            return "", True
        return tweet_id, False
    if (
        row.entity_type == TWEET_ENTITY_TYPE
        and row.event_type in TWEET_REFRESH_EVENTS
        and envelope_repo.exists_by_source_id(row.entity_id)
    ):
        return row.entity_id, False
    return None


def _extract_tweet_id(bookmark_entity_id: str) -> str | None:
    """Parse "tweetId:folderId" (or "tweetId:root") → tweet id.

    Returns None when the shape doesn't match, so callers can count it
    as malformed instead of silently inventing an odd source_id.
    """
    parts = bookmark_entity_id.split(":", 1)
    if len(parts) != 2:
        return None
    tweet_id, folder = parts
    if not tweet_id or not folder:
        return None
    return tweet_id


def _process(
    queue_repo: IngestQueueRepo,
    envelope_repo: EnvelopeRepo,
    mapper: ScraperMapper,
    process_limit: int,
    now: datetime | None,
    *,
    embedder: Embedder | None,
    run_repo: RunRepo,
    parent_run_id: str,
    embedding_repo: EmbeddingRepo,
) -> tuple[int, int, int, int, int, int, int, int]:
    skipped = 0
    processed = 0
    refreshed = 0
    mapped_none = 0
    failed = 0
    embedded = 0
    embed_skipped_no_body = 0
    embed_failed = 0

    for item in queue_repo.list_pending(limit=process_limit, now=now):
        try:
            envelope = mapper.fetch_envelope(item.source_id)
        except Exception as exc:
            queue_repo.mark_failed(
                item.source_event_id,
                error=repr(exc),
                backoff_seconds=_backoff_seconds(item.attempts + 1),
                now=now,
            )
            failed += 1
            continue

        if envelope is None:
            queue_repo.mark_failed(
                item.source_event_id,
                error="mapper returned None",
                backoff_seconds=_backoff_seconds(item.attempts + 1),
                now=now,
            )
            mapped_none += 1
            continue

        stored_envelope_id: str | None = None
        try:
            if envelope_repo.exists_by_source_id(item.source_id):
                # Refresh preserves the stored identity.id (ADR 007). The
                # mapper-minted envelope.identity.id on the input is a
                # per-observation UUID, not a stable handle; downstream
                # keys (embeddings, runs rows) must use the preserved id
                # so every observation of the same content shares one
                # envelope-scoped identity.
                merged = envelope_repo.refresh_by_source_id(envelope)
                stored_envelope_id = merged.identity.id
                refreshed += 1
            else:
                envelope_repo.save(envelope)
                stored_envelope_id = envelope.identity.id
                processed += 1
        except sqlite3.IntegrityError:
            # Race: concurrent writer saved between the exists check and
            # INSERT. Treat as already-saved; operator sees the counter.
            skipped += 1
        except Exception as exc:
            queue_repo.mark_failed(
                item.source_event_id,
                error=repr(exc),
                backoff_seconds=_backoff_seconds(item.attempts + 1),
                now=now,
            )
            failed += 1
            continue

        if stored_envelope_id is not None and embedder is not None:
            body = envelope.content.body
            if not body or not body.strip():
                embed_skipped_no_body += 1
            else:
                try:
                    # correlation_id tracks the content envelope across the
                    # pipeline (plan §Event contract); causation_id tracks the
                    # immediate trigger — here, the parent ingest run.
                    with operation(
                        "embed",
                        repo=run_repo,
                        envelope_id=stored_envelope_id,
                        correlation_id=stored_envelope_id,
                        causation_id=parent_run_id,
                    ) as embed_op:
                        vector = embedder.embed(body)
                        embedding_repo.save(
                            envelope_id=stored_envelope_id,
                            model_version=MODEL_VERSION,
                            vector=vector,
                            now=now,
                        )
                        embed_op.attach_outputs(
                            {
                                "model_version": MODEL_VERSION,
                                "dimension": EMBEDDING_DIMENSION,
                            }
                        )
                    embedded += 1
                except Exception as exc:
                    queue_repo.mark_failed(
                        item.source_event_id,
                        error=f"embed: {exc!r}",
                        backoff_seconds=_backoff_seconds(item.attempts + 1),
                        now=now,
                    )
                    embed_failed += 1
                    continue

        queue_repo.mark_dispatched(item.source_event_id, now=now)

    return (
        skipped,
        processed,
        refreshed,
        mapped_none,
        failed,
        embedded,
        embed_skipped_no_body,
        embed_failed,
    )


def _backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * (2 ** (attempts - 1)))
