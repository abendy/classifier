"""One-pass orchestration from scraper outbox to saved envelopes."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from prism.audit import RunRepo, operation
from prism.classification_repo import ClassificationRepo, ClassificationWrite
from prism.embedding import EMBEDDING_DIMENSION, MODEL_VERSION
from prism.embedding_repo import EmbeddingRepo
from prism.envelope import ContentEnvelope, TopicAssignment
from prism.envelope_repo import EnvelopeRepo
from prism.events_outbox import EmittedEvent, EventOutboxRepo
from prism.ingest_queue import IngestQueueRepo
from prism.logs import log_event
from prism.scraper_mapper import ScraperMapper
from prism.scraper_outbox import ScraperOutboxReader
from prism.sync_state import SyncStateRepo
from prism.topic_prototype_repo import TopicPrototypeRepo
from prism.topic_retrieval import retrieve_top_k_for_envelope

if TYPE_CHECKING:
    from prism.embedding import Embedder
    from prism.ingest_queue import IngestQueueItem
    from prism.llm_client import LlmClient
    from prism.scraper_outbox import OutboxRow

CURSOR_NAME = "x-sync-outbox-cursor"
BOOKMARK_ENTITY_TYPE = "bookmark"
BOOKMARK_CREATED_EVENT = "bookmark.created"
TWEET_ENTITY_TYPE = "tweet"
TWEET_REFRESH_EVENTS: frozenset[str] = frozenset({"record.synced", "record.enriched"})


@dataclass(frozen=True)
class _ClassifyOutcome:
    is_confident: bool


class _ClassifyFailed(Enum):
    MARK_FAILED = "mark_failed"


_CLASSIFY_FAILED = _ClassifyFailed.MARK_FAILED


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


def ingest_once(
    *,
    scraper_conn: sqlite3.Connection,
    classifier_conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    llm_client: LlmClient | None = None,
    confidence_threshold: float = 0.55,
    retrieval_top_k: int = 5,
    ollama_model: str = "qwen2.5:7b-instruct-q4_K_M",
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
            classified_confident,
            classified_low_confidence,
            classify_skipped_no_embedding,
            classify_failed,
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
            classification_repo=classification_repo,
            event_outbox_repo=event_outbox_repo,
            topic_repo=topic_repo,
            llm_client=llm_client,
            confidence_threshold=confidence_threshold,
            retrieval_top_k=retrieval_top_k,
            ollama_model=ollama_model,
            service_version=service_version,
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
            classified_confident=classified_confident,
            classified_low_confidence=classified_low_confidence,
            classify_skipped_no_embedding=classify_skipped_no_embedding,
            classify_failed=classify_failed,
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
    classification_repo: ClassificationRepo,
    event_outbox_repo: EventOutboxRepo,
    topic_repo: TopicPrototypeRepo,
    llm_client: LlmClient | None,
    confidence_threshold: float,
    retrieval_top_k: int,
    ollama_model: str,
    service_version: str,
) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int]:
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
        embedded_this_pass = False
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
                    embedded_this_pass = True
                except Exception as exc:
                    queue_repo.mark_failed(
                        item.source_event_id,
                        error=f"embed: {exc!r}",
                        backoff_seconds=_backoff_seconds(item.attempts + 1),
                        now=now,
                    )
                    embed_failed += 1
                    continue

        if llm_client is not None and stored_envelope_id is not None and embedded_this_pass:
            classify_outcome = _classify_envelope(
                envelope=envelope,
                stored_envelope_id=stored_envelope_id,
                item=item,
                queue_repo=queue_repo,
                run_repo=run_repo,
                classification_repo=classification_repo,
                event_outbox_repo=event_outbox_repo,
                topic_repo=topic_repo,
                embedding_repo=embedding_repo,
                llm_client=llm_client,
                confidence_threshold=confidence_threshold,
                retrieval_top_k=retrieval_top_k,
                ollama_model=ollama_model,
                service_version=service_version,
                now=now,
            )
            if classify_outcome is _CLASSIFY_FAILED:
                classify_failed += 1
                continue
            if classify_outcome.is_confident:
                classified_confident += 1
            else:
                classified_low_confidence += 1
        elif llm_client is not None:
            classify_skipped_no_embedding += 1

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
        classified_confident,
        classified_low_confidence,
        classify_skipped_no_embedding,
        classify_failed,
    )


def _backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * (2 ** (attempts - 1)))


def _now_dt(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _classify_envelope(
    *,
    envelope: ContentEnvelope,
    stored_envelope_id: str,
    item: IngestQueueItem,
    queue_repo: IngestQueueRepo,
    run_repo: RunRepo,
    classification_repo: ClassificationRepo,
    event_outbox_repo: EventOutboxRepo,
    topic_repo: TopicPrototypeRepo,
    embedding_repo: EmbeddingRepo,
    llm_client: LlmClient,
    confidence_threshold: float,
    retrieval_top_k: int,
    ollama_model: str,
    service_version: str,
    now: datetime | None,
) -> _ClassifyOutcome | _ClassifyFailed:
    """Classify an embedded envelope; persist the result; emit events.

    Order of operations:
      1. Mint a synthetic `content-ingested` event in the outbox
         (its id becomes the causation_id for everything that follows).
      2. Retrieve top-k topic candidates from prototype storage.
      3. Inside `operation("classify", ...)`, run pick_topic and build
         the ClassificationWrite + emitted event payload.
      4. classification_repo.upsert(write).
      5. event_outbox_repo.enqueue(emitted_event).

    Empty retrieval candidates surface as a queue mark_failed
    (operator-visible setup error). LLM transport errors and
    schema-violation crashes propagate as queue mark_failed; the next
    pass retries with a fresh run_id.

    Write-order rationale: upsert lands first because it is the durable
    source of truth; the outbox event is the dispatch signal that
    follows. If the outbox enqueue fails after a successful upsert, the
    next attempt re-classifies (new run_id), overwrites the
    classifications row, and emits a fresh event. The brief
    inconsistency window (classification stored, no event emitted)
    self-resolves on retry.
    """
    from prism.topic_llm_pick import pick_topic

    classified_at = _now_dt(now)
    ingested_event = EmittedEvent.new(
        event_type="content-ingested",
        source="prism",
        correlation_id=stored_envelope_id,
        causation_id=None,
        payload={
            "envelopeId": stored_envelope_id,
            "contentType": envelope.content.type,
            "service": envelope.source.service,
            "sourceId": envelope.source.source_id,
        },
        now=classified_at,
    )
    try:
        event_outbox_repo.enqueue(ingested_event)
    except sqlite3.Error as exc:
        queue_repo.mark_failed(
            item.source_event_id,
            error=f"classify.synthetic-ingested: {exc!r}",
            backoff_seconds=_backoff_seconds(item.attempts + 1),
            now=now,
        )
        return _CLASSIFY_FAILED

    candidates = retrieve_top_k_for_envelope(
        stored_envelope_id,
        embedding_repo=embedding_repo,
        topic_repo=topic_repo,
        model_version=MODEL_VERSION,
        k=retrieval_top_k,
    )
    if not candidates:
        queue_repo.mark_failed(
            item.source_event_id,
            error="classify.retrieval-empty: no topic prototypes loaded for model",
            backoff_seconds=_backoff_seconds(item.attempts + 1),
            now=now,
        )
        return _CLASSIFY_FAILED

    classified_by = f"prism@{service_version} + {MODEL_VERSION} + {ollama_model}"
    descriptions = topic_repo.get_descriptions(
        (candidate.topic_id for candidate in candidates), MODEL_VERSION
    )
    try:
        with operation(
            "classify",
            repo=run_repo,
            envelope_id=stored_envelope_id,
            correlation_id=stored_envelope_id,
            causation_id=ingested_event.id,
        ) as classify_op:
            pick_result = pick_topic(
                envelope_body=envelope.content.body or "",
                candidates=candidates,
                descriptions_by_topic_id=descriptions,
                llm_client=llm_client,
                confidence_threshold=confidence_threshold,
            )
            run_id = classify_op.run_id
            classify_op.attach_outputs(
                {
                    "chosen_topic_id": pick_result.chosen_topic_id,
                    "low_confidence_reason": pick_result.low_confidence_reason,
                }
            )
    except Exception as exc:
        queue_repo.mark_failed(
            item.source_event_id,
            error=f"classify.pick: {exc!r}",
            backoff_seconds=_backoff_seconds(item.attempts + 1),
            now=now,
        )
        return _CLASSIFY_FAILED

    if pick_result.chosen_topic_id is not None and pick_result.pick is not None:
        assignment = TopicAssignment.model_validate(
            {
                "topicId": pick_result.chosen_topic_id,
                "confidence": pick_result.pick.confidence,
            }
        )
        write = ClassificationWrite(
            envelope_id=stored_envelope_id,
            active_run_id=run_id,
            tags=None,
            topic_assignments=[assignment],
            confidence=pick_result.pick.confidence,
            low_confidence_reason=None,
            classified_by=classified_by,
            classified_at=classified_at,
        )
        emitted = EmittedEvent.new(
            event_type="content-classified",
            source="prism",
            correlation_id=stored_envelope_id,
            causation_id=ingested_event.id,
            payload={
                "envelopeId": stored_envelope_id,
                "tags": [],
                "topicAssignments": [
                    {
                        "topicId": pick_result.chosen_topic_id,
                        "confidence": pick_result.pick.confidence,
                    }
                ],
                "confidence": pick_result.pick.confidence,
                "classifiedBy": classified_by,
            },
            now=classified_at,
        )
        is_confident = True
    elif pick_result.low_confidence_reason is not None:
        low_confidence_reason = pick_result.low_confidence_reason
        candidate_topics_payload = [
            {"topicId": candidate.topic_id} for candidate in candidates
        ]
        write = ClassificationWrite(
            envelope_id=stored_envelope_id,
            active_run_id=run_id,
            tags=None,
            topic_assignments=[],
            confidence=None,
            low_confidence_reason=low_confidence_reason,
            classified_by=classified_by,
            classified_at=classified_at,
        )
        payload: dict[str, Any] = {
            "envelopeId": stored_envelope_id,
            "reason": low_confidence_reason,
            "candidateTopics": candidate_topics_payload,
        }
        if low_confidence_reason == "below-threshold" and pick_result.pick is not None:
            payload["confidence"] = pick_result.pick.confidence
        emitted = EmittedEvent.new(
            event_type="content-classified-low-confidence",
            source="prism",
            correlation_id=stored_envelope_id,
            causation_id=ingested_event.id,
            payload=payload,
            now=classified_at,
        )
        is_confident = False
    else:
        raise RuntimeError(
            f"TopicPickResult invariant violated: {pick_result!r}"
        )

    try:
        classification_repo.upsert(write)
        event_outbox_repo.enqueue(emitted)
    except (sqlite3.Error, ValueError) as exc:
        queue_repo.mark_failed(
            item.source_event_id,
            error=f"classify.persist: {exc!r}",
            backoff_seconds=_backoff_seconds(item.attempts + 1),
            now=now,
        )
        return _CLASSIFY_FAILED

    return _ClassifyOutcome(is_confident=is_confident)
