# ADR 021: Two-table normal form for classifications and topic_assignments

## Status

Accepted

## Date

2026-04-25

## Context

The classifier writes one canonical "current classification"
record per envelope: tags, topic assignments, aggregate
confidence, classifier identity string, classified-at
timestamp, and a pointer to the live run. On reclassification
the prior record is overwritten in place; the audit history
lives on the immutable `runs` table.

The wire-format `Classification` block (envelope.py) holds
four optional fields: `tags: list[str] | None`,
`topic_assignments: list[TopicAssignment] | None`,
`classified_at: datetime | None`, `classified_by: str | None`.
`TopicAssignment` itself is a four-field shape: `topic_id`,
`subtopic_id?`, `confidence?`, `matched_terms?`. Per the
plan §Subtopic head, when multiple subtopics under one
parent topic apply, the classifier emits one
`TopicAssignment` per subtopic that shares the parent
`topicId` — so the *list* cardinality is genuine, not a
single-row aggregation.

The classifier also persists two operational fields the
wire shape does not carry: `active_run_id` (the live `runs`
row pointer for inbox staleness checks) and
`low_confidence_reason` (the discriminator that splits
confident classifications from those that fell into the
operator-review path).

The shape question: how do we persist this in SQLite?

- **JSON-blob:** one `classifications` row with envelope_id
  PK, active_run_id, low_confidence_reason, and a single
  `classification_json TEXT` column carrying the entire
  wire `Classification` block. Reads parse JSON;
  topic-assignment lookups (`WHERE topic_id = 'history'`)
  require `json_each` CTEs.
- **Two-table normal form:** `classifications` carries
  scalar columns (envelope_id, active_run_id, tags,
  confidence, low_confidence_reason, classified_by,
  classified_at). `topic_assignments` carries one row per
  assignment with envelope_id, topic_id, subtopic_id,
  confidence, matched_terms. Read paths join on envelope_id.

## Decision

Adopt the **two-table normal form**.

The `topic_assignments` cardinality is genuine (per
§Subtopic head) and several near-future read paths query by
topic ("show me every envelope tagged with topic X" for
analytics, retraining, eval-split construction). Those
queries are O(log n) on a typed `topic_id` index and
O(n) JSON path scans on a blob.

The `classifications` row stays scalar — `tags` is a JSON
list-of-string column because tag set membership is opaque
to the storage layer (the tags head emits freeform tags;
no tag-equality query path exists yet, and JSON1 path
queries handle the analytics case when it lands).

## Alternatives considered

- **JSON-blob (`classification_json` column).** Tempting
  because the wire shape *is* JSON. Rejected because:
  1. The plan's §HTTP API includes `get-low-confidence`
     and the inbox flow (`accept-classification`,
     `edit-classification`). Operator inbox queries
     filter by `low_confidence_reason IS NOT NULL` —
     trivial with a column, awkward with a blob.
  2. Topic-assignment cardinality means any query "by
     topic" becomes a JSON-path scan over every row. The
     fan-out table makes those queries indexable.
  3. Re-classification is a full replace — the prior
     proposal becomes audit history on the `runs` table,
     not state on this row. JSON-blob storage and a
     fan-out table converge in cost on the write path
     (one insert + N inserts in either form).
  4. Operational debugging (an operator runs `sqlite3` to
     check "what topic did envelope X get?") is one query
     against typed columns; the blob form forces JSON
     extraction.

- **Three tables (`classifications` + `topic_assignments`
  + `tags`).** Considered. Rejected because tags are
  freeform (per plan §Tags head — freeform), tag
  cardinality per envelope is small (2–6), and no
  tag-equality read path is named in the plan or the
  manifest commands. The JSON list column is sufficient;
  if a tag-equality query path materializes later, JSON1
  path indexing or migrating to a fan-out table is one
  Alembic revision away.

- **Single fan-out table only (no `classifications` row).**
  Considered. Rejected because the discriminator
  (`low_confidence_reason`) and the live-pointer
  (`active_run_id`) are envelope-level, not assignment-
  level. Storing them on every assignment row would
  duplicate state; a single row is the right cardinality.

- **Foreign key from `topic_assignments.envelope_id` to
  `classifications.envelope_id`.** Considered. Rejected
  because no other migration in this project declares
  FKs (ADR 001 confines SQLAlchemy to migrations only;
  raw sqlite3 in app code does not enable
  `PRAGMA foreign_keys=ON` by default). The repo's
  transactional DELETE+INSERT inside `upsert` provides
  atomicity; introducing FKs as a one-off here would
  diverge from the established pattern without solving a
  current correctness gap.

## Consequences

- **Positive:** topic-id queries are indexable.
- **Positive:** inbox-triage queries (
  `WHERE low_confidence_reason IS NOT NULL`) hit a partial
  index without JSON extraction.
- **Positive:** typed columns keep operator debugging
  one-line.
- **Positive:** the discriminated-union invariant
  (`low_confidence_reason IS NULL` ⟺ confident) is
  enforced at the repo seam (`_validate_write`); no
  malformed rows can land.
- **Negative:** reads that want the full wire-shape
  `Classification` block reconstruct it from a join. The
  reconstruction lives in the slice that needs it
  (`get-classification` HTTP); this slice does not pay
  the cost.
- **Negative:** a future schema change to add an
  envelope-level field is a migration. Acceptable: the
  envelope-level fields are governed by the plan §Event
  contract; changes are ADR-gated.
- **Neutral:** `tags` stays JSON. Tag cardinality is small
  and freeform; the cost-benefit favors a single column
  until a tag-equality query path lands.

## References

- `./.project/plans/2026-04-18-classification-service-foundation.md`
  § "Storage" / "Repository pattern" — `ClassificationRepo`
  named; § "Subtopic head" — `TopicAssignment` cardinality
  rationale; § "Reconcile and emit" — discriminated-union
  outcome.
- `./.project/README.md` § "Phase 2 architectural decisions
  (pre-ingest-integration)" — the broader context for this
  slice.
- `./migrations/versions/4d82af96f3f8_classifications.py` — the
  schema this ADR pins.
- `./src/prism/classification_repo.py` — repo + dataclasses.
- `./src/prism/envelope.py` — `TopicAssignment` Pydantic
  model; `Classification` wire-shape.
- `./docs/adr/001-raw-sqlite3-in-application-code.md` — repo
  follows this pattern.
- `./docs/adr/014-mutating-repo-methods-return-canonical-post-mutation-state.md`
  — `upsert` returns canonical state.
- `./docs/adr/020-typed-columns-for-events-outbox.md` —
  same typed-vs-blob trade-off applied at a different
  storage layer; this ADR's reasoning mirrors it.
