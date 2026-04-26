"""One-pass orchestration from scraper outbox to saved envelopes."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from prism.audit import RunRepo, operation
from prism.classification_repo import ClassificationRepo
from prism.classify_pipeline import CLASSIFY_FAILED, classify_envelope
from prism.embedding import EMBEDDING_DIMENSION, MODEL_VERSION
from prism.embedding_repo import EmbeddingRepo
from prism.envelope_repo import EnvelopeRepo
from prism.events_outbox import EventOutboxRepo
from prism.ingest_queue import IngestQueueRepo, fail_queue_item
from prism.logs import log_event
from prism.scraper_mapper import ScraperMapper
from prism.scraper_outbox import ScraperOutboxReader
from prism.sync_state import SyncStateRepo
from prism.topic_prototype_repo import TopicPrototypeRepo

if TYPE_CHECKING:
    from datetime import datetime

    from prism.embedding import Embedder
    from prism.llm_client import LlmClient
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
    classified_confident: int = 0
    classified_low_confidence: int = 0
    classify_skipped_no_embedding: int = 0
    classify_failed: int = 0


@dataclass(frozen=True)
class IngestDeps:
    queue_repo: IngestQueueRepo
    envelope_repo: EnvelopeRepo
    embedding_repo: EmbeddingRepo
    classification_repo: ClassificationRepo
    event_outbox_repo: EventOutboxRepo
    topic_repo: TopicPrototypeRepo
    run_repo: RunRepo
    mapper: ScraperMapper


@dataclass(frozen=True)
class IngestPassResult:
    observed: int = 0
    skipped_already_saved: int = 0
    processed: int = 0
    refreshed: int = 0
    mapper_returned_none: int = 0
    failed: int = 0
    malformed: int = 0
    embedded: int = 0
    embed_skipped_no_body: int = 0
    embed_failed: int = 0
    classified_confident: int = 0
    classified_low_confidence: int = 0
    classify_skipped_no_embedding: int = 0
    classify_failed: int = 0


def ingest_once(
    *,
    scraper_conn: sqlite3.Connection,
    classifier_conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    llm_client: LlmClient | None = None,
    confidence_threshold: float,
    retrieval_top_k: int,
    ollama_model: str,
    service_version: str,
    poll_limit: int = 100,
    process_limit: int = 100,
    now: datetime | None = None,
) -> IngestStats:
    """Run one poll-then-process pass over the ingest pipeline."""
    sync_repo = SyncStateRepo(classifier_conn)
    queue_repo = IngestQueueRepo(classifier_conn)
    envelope_repo = EnvelopeRepo(classifier_conn)
    embedding_repo = EmbeddingRepo(classifier_conn)
    classification_repo = ClassificationRepo(classifier_conn)
    event_outbox_repo = EventOutboxRepo(classifier_conn)
    topic_repo = TopicPrototypeRepo(classifier_conn)
    mapper = ScraperMapper(scraper_conn)
    outbox = ScraperOutboxReader(scraper_conn)
    run_repo = RunRepo(classifier_conn)
    deps = IngestDeps(
        queue_repo=queue_repo,
        envelope_repo=envelope_repo,
        embedding_repo=embedding_repo,
        classification_repo=classification_repo,
        event_outbox_repo=event_outbox_repo,
        topic_repo=topic_repo,
        run_repo=run_repo,
        mapper=mapper,
    )

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
        result = _process(
            deps,
            process_limit,
            now,
            embedder=embedder,
            parent_run_id=run_id,
            llm_client=llm_client,
            confidence_threshold=confidence_threshold,
            retrieval_top_k=retrieval_top_k,
            ollama_model=ollama_model,
            service_version=service_version,
        )
        stats = IngestStats(
            observed=observed,
            skipped_already_saved=result.skipped_already_saved,
            processed=result.processed,
            refreshed=result.refreshed,
            mapper_returned_none=result.mapper_returned_none,
            failed=result.failed,
            malformed=malformed,
            embedded=result.embedded,
            embed_skipped_no_body=result.embed_skipped_no_body,
            embed_failed=result.embed_failed,
            classified_confident=result.classified_confident,
            classified_low_confidence=result.classified_low_confidence,
            classify_skipped_no_embedding=result.classify_skipped_no_embedding,
            classify_failed=result.classify_failed,
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
    deps: IngestDeps,
    process_limit: int,
    now: datetime | None,
    *,
    embedder: Embedder | None,
    parent_run_id: str,
    llm_client: LlmClient | None,
    confidence_threshold: float,
    retrieval_top_k: int,
    ollama_model: str,
    service_version: str,
) -> IngestPassResult:
    skipped = 0
    processed = 0
    refreshed = 0
    mapped_none = 0
    failed = 0
    embedded = 0
    embed_skipped_no_body = 0
    embed_failed = 0
    classified_confident = 0
    classified_low_confidence = 0
    classify_skipped_no_embedding = 0
    classify_failed = 0

    for item in deps.queue_repo.list_pending(limit=process_limit, now=now):
        try:
            envelope = deps.mapper.fetch_envelope(item.source_id)
        except Exception as exc:
            fail_queue_item(
                item,
                queue_repo=deps.queue_repo,
                error_prefix="",
                exc=repr(exc),
                now=now,
            )
            failed += 1
            continue

        if envelope is None:
            fail_queue_item(
                item,
                queue_repo=deps.queue_repo,
                error_prefix="",
                exc="mapper returned None",
                now=now,
            )
            mapped_none += 1
            continue

        stored_envelope_id: str | None = None
        embedded_this_pass = False
        try:
            if deps.envelope_repo.exists_by_source_id(item.source_id):
                # Refresh preserves the stored identity.id (ADR 007). The
                # mapper-minted envelope.identity.id on the input is a
                # per-observation UUID, not a stable handle; downstream
                # keys (embeddings, runs rows) must use the preserved id
                # so every observation of the same content shares one
                # envelope-scoped identity.
                merged = deps.envelope_repo.refresh_by_source_id(envelope)
                stored_envelope_id = merged.identity.id
                refreshed += 1
            else:
                deps.envelope_repo.save(envelope)
                stored_envelope_id = envelope.identity.id
                processed += 1
        except sqlite3.IntegrityError:
            # Race: concurrent writer saved between the exists check and
            # INSERT. Treat as already-saved; operator sees the counter.
            skipped += 1
        except Exception as exc:
            fail_queue_item(
                item,
                queue_repo=deps.queue_repo,
                error_prefix="",
                exc=repr(exc),
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
                        repo=deps.run_repo,
                        envelope_id=stored_envelope_id,
                        correlation_id=stored_envelope_id,
                        causation_id=parent_run_id,
                    ) as embed_op:
                        vector = embedder.embed(body)
                        deps.embedding_repo.save(
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
                    embedded_this_pass = True
                except Exception as exc:
                    fail_queue_item(
                        item,
                        queue_repo=deps.queue_repo,
                        error_prefix="embed",
                        exc=exc,
                        now=now,
                    )
                    embed_failed += 1
                    continue

        if llm_client is not None and stored_envelope_id is not None and embedded_this_pass:
            classify_outcome = classify_envelope(
                envelope=envelope,
                stored_envelope_id=stored_envelope_id,
                item=item,
                queue_repo=deps.queue_repo,
                run_repo=deps.run_repo,
                classification_repo=deps.classification_repo,
                event_outbox_repo=deps.event_outbox_repo,
                topic_repo=deps.topic_repo,
                embedding_repo=deps.embedding_repo,
                llm_client=llm_client,
                confidence_threshold=confidence_threshold,
                retrieval_top_k=retrieval_top_k,
                ollama_model=ollama_model,
                service_version=service_version,
                now=now,
            )
            if classify_outcome is CLASSIFY_FAILED:
                classify_failed += 1
                continue
            if classify_outcome.is_confident:
                classified_confident += 1
            else:
                classified_low_confidence += 1
        elif llm_client is not None:
            classify_skipped_no_embedding += 1

        deps.queue_repo.mark_dispatched(item.source_event_id, now=now)

    return IngestPassResult(
        skipped_already_saved=skipped,
        processed=processed,
        refreshed=refreshed,
        mapper_returned_none=mapped_none,
        failed=failed,
        embedded=embedded,
        embed_skipped_no_body=embed_skipped_no_body,
        embed_failed=embed_failed,
        classified_confident=classified_confident,
        classified_low_confidence=classified_low_confidence,
        classify_skipped_no_embedding=classify_skipped_no_embedding,
        classify_failed=classify_failed,
    )
