# ADR 012: DELETE-then-INSERT as upsert for `vec0` virtual tables

## Status

Accepted

## Date

2026-04-20

## Context

sqlite-vec's `vec0` virtual tables reject SQLite's standard
upsert idioms: `INSERT OR REPLACE` raises
`Cannot UPSERT a virtual table`, and
`INSERT INTO ... ON CONFLICT DO UPDATE` is likewise unsupported.

`EmbeddingRepo.save` needs upsert semantics: when the same
`(envelope_id, model_version)` is re-embedded (e.g., after a
model-cache refresh, or when the ingest loop retries a failed
embedding), the new vector must replace the stored one under the
same PK. Without working upsert, the save path either duplicates
rows (violating the PK) or needs separate UPDATE / INSERT paths
in application code.

## Decision

`EmbeddingRepo.save` issues `DELETE FROM embeddings WHERE id = ?`
immediately followed by `INSERT INTO embeddings (...)` within a
single transaction. Both statements succeed together or roll back
together; a committed reader never sees the row absent between
the two operations. The error path follows the repo's standard
cursor/transaction discipline (ADR 001) — rollback on
`sqlite3.Error`, re-raise.

A module-level comment in `save` points at this ADR so a
future reader doesn't "simplify" the two-statement path back to
`INSERT OR REPLACE`.

## Alternatives considered

- **`INSERT OR REPLACE INTO embeddings (...)`.** SQLite's
  natural upsert. Rejected by vec0 with
  `Cannot UPSERT a virtual table`.
- **`INSERT INTO embeddings (...) ON CONFLICT(id) DO UPDATE`.**
  Same rejection from vec0. Not an option.
- **Try `UPDATE` first; if `rowcount == 0`, fall through to
  `INSERT`.** Correct but doubles the common-case write path
  and introduces a narrow interleaving where concurrent readers
  could see stale state between the UPDATE and a retry INSERT.
  The single-transaction DELETE+INSERT collapses this to a
  cleaner semantic.
- **Soft-delete with a `deleted_at` column.** Vec0 doesn't
  support filter-during-KNN, so soft-deleted rows would still
  match similarity queries. Rejected.
- **Drop and recreate the whole table on each save.** Absurd
  at any scale; mentioned only for completeness.

## Consequences

- **Positive:** correct upsert under vec0's virtual-table
  constraints, within a single committed transaction.
- **Positive:** the pattern transplants directly to any future
  vec0 table in this repo (e.g., `embeddings_v2`,
  `embeddings_sparse`) without rediscovering the constraint.
- **Negative:** two statements per save instead of one. At
  ingest cadence (tens to hundreds of envelopes per pass) the
  cost is negligible; at hypothetical bulk-reembed cadence
  (millions of rows) the pattern becomes a real factor and
  batch rewrites would be preferable — separate concern.
- **Neutral:** the `save` method is slightly longer than the
  non-vec0 repo's `INSERT` — both cursors are tracked and
  closed in `finally`, same discipline.

## Revisit when

sqlite-vec supports native upsert on vec0 virtual tables
(`INSERT OR REPLACE` or `ON CONFLICT DO UPDATE`), or if
high-throughput re-embed workloads need bulk rewrite
semantics that DELETE+INSERT per row can't satisfy.

## References

- `src/prism/embedding_repo.py` — `EmbeddingRepo.save` method,
  specifically the two-statement transaction.
- `migrations/versions/8a27a28168d0_embeddings.py` — the vec0
  table this upsert idiom applies to.
- ADR 011 — composite-identity encoding on the same table;
  paired bridge-internal constraints of working within
  sqlite-vec's virtual-table shape.
- ADR 001 — raw-sqlite3 discipline; the cursor + transaction
  pattern the save method follows.
