# ADR 007: Envelope refresh preserves identity.id and system.created_at

## Status

Accepted

## Date

2026-04-19

## Context

The classifier persists `ContentEnvelope` rows in
`content_envelopes`, keyed by `identity.id` (PRIMARY KEY) with a
`UNIQUE` index on `source.sourceId`. Slice 5 added
`EnvelopeRepo.refresh_by_source_id` so a later event for the same
`source_id` can update the stored envelope in place — driven today
by scraper enrichment (see ADR 006, the bridge), and eventually by
any edge service that re-emits an envelope when its own backing
data changes.

Two questions about refresh need a stable answer, independent of
where the new envelope came from:

1. **Does `identity.id` change on refresh?** If yes, downstream
   references (classification runs, emitted events, audit rows)
   lose their link to the stored envelope whenever a refresh
   happens.
2. **Does `system.created_at` change on refresh?** If yes, the
   envelope loses the fact that it represents a single logical
   record observed over time; each refresh looks like a fresh
   record to anyone reading timestamps.

These are envelope-level invariants, not scraper-specific rules.
They apply whether the new envelope arrives from the v1 bridge or
from a spec-conformant `content-ingested` event bus.

## Decision

On refresh:

- **`identity.id` is preserved** from the stored envelope.
- **`system.created_at` is preserved** from the stored envelope.
- **`system.updated_at`** takes the new envelope's value.
- **`source.ingested_at`** takes the new envelope's value.
- **`source.sourceId`** is the lookup key; it is, by construction,
  the same in both envelopes.
- Every other field in the sections the new envelope populates is
  overwritten with the new value.

The envelope therefore reads as "one logical record, born at
`system.createdAt`, last observed at `source.ingestedAt`, last
written at `system.updatedAt`." The stable `identity.id` is the
durable handle other systems can reference.

Non-mapper-owned sections (`classification`, `routing`, `state`,
`relationships`) are preserved on refresh while the v1 bridge is
in place — see ADR 006. That preservation rule retires with the
bridge; this ADR's invariants do not.

## Alternatives considered

- **Regenerate `identity.id` on refresh.** Simpler implementation
  (`save()` path only; no merge logic). Rejected: any external
  reference to the envelope id becomes dangling the moment a
  refresh lands. Cheap to preserve; expensive to re-couple.
- **Delete-then-reinsert.** Keeps the schema simple but has the
  same `identity.id` churn problem and invites cascade issues
  the moment foreign keys exist. Rejected.
- **Treat `source.ingestedAt` as `createdAt` (i.e., reset both
  timestamps on refresh).** Loses the durable "when did we first
  see this" signal, which is load-bearing for dedup audits and
  for the runs-table lineage the audit slice introduces. Rejected.
- **Make the envelope append-only and store refreshes as a new
  revision row.** A defensible long-term move if we ever need an
  audit trail inside `content_envelopes` itself, but the runs
  table already carries per-observation lineage. Rejected for now;
  revisitable when the audit layer matures.

## Consequences

- **Positive:** downstream references to `identity.id` stay valid
  across refreshes. Audit and correlation IDs don't need to be
  invalidated on every scraper re-emit.
- **Positive:** timestamp semantics are predictable —
  `createdAt < updatedAt` always holds after any refresh; the
  first observation's birth timestamp survives.
- **Negative:** the refresh path requires a read-then-merge-then-
  write sequence rather than a single `INSERT ... ON CONFLICT`.
  Mitigated by the fact that the path runs at ingest cadence, not
  classification cadence.
- **Neutral:** prior-state content is not retained inside
  `content_envelopes`. The runs table (audit slice) carries
  per-observation payloads when that level of history is needed.

## Revisit when

- We need to preserve prior-state envelope snapshots inside
  `content_envelopes` itself (at which point the table becomes
  revision-oriented, and the invariants here combine with a
  revision-id concept).
- An event ever arrives claiming to change a `source.sourceId`
  (i.e., an edge service re-emits under a new id). That event
  violates the current model — the lookup key IS the identifier
  — and would force either a rename protocol or a
  never-rename rule. Neither exists yet.

## References

- `src/prism/envelope_repo.py` — `refresh_by_source_id`,
  `_merge_preserving`.
- `tests/test_envelope_repo.py` —
  `test_refresh_preserves_identity_id_and_created_at`,
  `test_refresh_updates_ingested_at_column`,
  `test_refresh_preserves_non_mapper_sections`.
- `tests/test_ingest.py` —
  `test_stub_envelope_is_upgraded_by_later_enrichment_event`
  (end-to-end regression).
- ADR 006 — bridge-scoped carve-out (non-mapper sections
  preserved while the v1 bridge is in place).
