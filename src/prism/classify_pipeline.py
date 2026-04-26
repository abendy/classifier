"""Classification sub-pipeline for embedded envelopes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from prism.audit import RunRepo, operation
from prism.classification_repo import (
    ClassificationRepo,
    ConfidentClassification,
    LowConfidenceClassification,
)
from prism.embedding import MODEL_VERSION
from prism.envelope import ContentEnvelope, TopicAssignment
from prism.events_outbox import EmittedEvent, EventOutboxRepo
from prism.ingest_queue import fail_queue_item

if TYPE_CHECKING:
    from prism.embedding_repo import EmbeddingRepo
    from prism.ingest_queue import IngestQueueItem, IngestQueueRepo
    from prism.llm_client import LlmClient
    from prism.topic_prototype_repo import TopicPrototypeRepo


@dataclass(frozen=True)
class ClassifyOutcome:
    is_confident: bool


class ClassifyFailed(Enum):
    MARK_FAILED = "mark_failed"


CLASSIFY_FAILED = ClassifyFailed.MARK_FAILED


def classify_envelope(
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
) -> ClassifyOutcome | ClassifyFailed:
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
    from prism.topic_llm_pick import TopicLowConfidence, TopicPicked, pick_topic
    from prism.topic_retrieval import retrieve_top_k_for_envelope

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
        internal=True,
    )
    try:
        event_outbox_repo.enqueue(ingested_event)
    except sqlite3.Error as exc:
        fail_queue_item(
            item,
            queue_repo=queue_repo,
            error_prefix="classify.synthetic-ingested",
            exc=exc,
            now=now,
        )
        return CLASSIFY_FAILED

    candidates = retrieve_top_k_for_envelope(
        stored_envelope_id,
        embedding_repo=embedding_repo,
        topic_repo=topic_repo,
        model_version=MODEL_VERSION,
        k=retrieval_top_k,
    )
    if not candidates:
        fail_queue_item(
            item,
            queue_repo=queue_repo,
            error_prefix="",
            exc="classify.retrieval-empty: no topic prototypes loaded for model",
            now=now,
        )
        return CLASSIFY_FAILED

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
                    "chosen_topic_id": (
                        pick_result.chosen_topic_id
                        if isinstance(pick_result, TopicPicked)
                        else None
                    ),
                    "low_confidence_reason": (
                        pick_result.reason
                        if isinstance(pick_result, TopicLowConfidence)
                        else None
                    ),
                }
            )
    except Exception as exc:
        fail_queue_item(
            item,
            queue_repo=queue_repo,
            error_prefix="classify.pick",
            exc=exc,
            now=now,
        )
        return CLASSIFY_FAILED

    if isinstance(pick_result, TopicPicked):
        assignment = TopicAssignment.model_validate(
            {
                "topicId": pick_result.chosen_topic_id,
                "confidence": pick_result.pick.confidence,
            }
        )
        write = ConfidentClassification(
            envelope_id=stored_envelope_id,
            active_run_id=run_id,
            tags=None,
            topic_assignments=[assignment],
            confidence=pick_result.pick.confidence,
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
    else:
        low_confidence_reason = pick_result.reason
        candidate_topics_payload = [
            {"topicId": candidate.topic_id} for candidate in candidates
        ]
        write = LowConfidenceClassification(
            envelope_id=stored_envelope_id,
            active_run_id=run_id,
            reason=low_confidence_reason,
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

    try:
        classification_repo.upsert(write)
        event_outbox_repo.enqueue(emitted)
    except (sqlite3.Error, ValueError) as exc:
        fail_queue_item(
            item,
            queue_repo=queue_repo,
            error_prefix="classify.persist",
            exc=exc,
            now=now,
        )
        return CLASSIFY_FAILED

    return ClassifyOutcome(is_confident=is_confident)


def _now_dt(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)
