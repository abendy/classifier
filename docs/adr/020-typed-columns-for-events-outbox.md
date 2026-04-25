# ADR 020: Typed columns for the events outbox table

## Status

Accepted

## Date

2026-04-25

## Context

The classifier emits dada.stream event envelopes
(`content-classified`, `content-classified-low-confidence`,
later `content-routed`, etc.). The wire format
(`platform/contracts/event-types.md` L14–26) is a JSON object
with eight top-level fields: `id`, `type`, `source`,
`timestamp`, `correlationId`, `causationId`, `payload`,
`version`.

The classifier persists every emitted event before it leaves
the process. A future dispatcher slice will read pending rows
from this storage and publish to the eventual event bus; in
the interim, "stored but not dispatched" is the durable
record that an event happened.

The shape question: how do we persist this JSON envelope
inside SQLite? Two reasonable options exist:

- **JSON-blob:** one `event_data TEXT` column carrying the
  full envelope, plus an `id` PK and a `dispatched_at`
  control column. Two columns hold all the data; the row is
  effectively a serialized envelope plus a dispatch flag.
- **Typed columns:** seven of the eight envelope fields get
  their own SQL column (`id`, `event_type`, `source`,
  `envelope_version`, `correlation_id`, `causation_id`,
  `created_at`); only `payload` (the per-event-type body)
  stays as a JSON-blob `TEXT` column.

## Decision

Adopt the **typed-columns** shape. Seven scalar columns +
JSON `payload` + `dispatched_at`.

## Alternatives considered

- **JSON-blob (full envelope in one column).** Tempting
  because the wire format is JSON, SQLite has solid JSON
  path support, and a single column trivially round-trips a
  wire envelope without column renaming.

  Rejected because:

  1. The dispatcher (separate near-future slice) polls for
     pending rows ordered by creation time, claims them, and
     publishes by event type. Without typed columns this is
     `WHERE json_extract(event_data, '$.timestamp') ...`
     against every row on every poll. Typed `created_at` and
     a partial `WHERE dispatched_at IS NULL` index together
     give an O(log pending) scan that does not degrade as
     dispatched rows accumulate.
  2. Cross-event correlation queries ("every event for
     envelope X") become path-extraction scans without a
     `correlation_id` column. Adding the column later means
     a schema migration plus a backfill pass over JSON blobs.
     Adding it now is one column on a fresh table.
  3. The classifier is the *first* dada.stream-conformant
     component. Setting the precedent that "outbox rows are
     typed, payload is JSON" lines up with how every other
     event-bus-bridging system in this ecosystem will need
     to query.
  4. Operational debugging is markedly easier with typed
     columns. `SELECT id, event_type, correlation_id,
     created_at FROM events_outbox WHERE dispatched_at IS
     NULL` is the single query an operator runs to know
     "what's stuck"; the JSON-blob form forces a `json_each`
     CTE for the same answer.

  The cost of typed columns is one schema-migration entry
  per added envelope-level field. The dada.stream envelope
  shape is governed and stable; new top-level fields are
  rare and ADR-gated. The migration cost is bounded.

- **Two-table normal form (envelope-row + payload-row split,
  or one row per `(id, key)` for typed extras).** Rejected
  as over-engineering for the current single-row-per-event
  pattern. Every emit is a single insert; every dispatch is
  a single read by id. There is no second cardinality on
  the envelope side that would justify a join.

- **Skip the table; emit straight to a log file or stdout.**
  Considered. Rejected because the absence of a real bus is
  exactly why the outbox needs to exist as a durable record.
  A log line is not durable across a service restart in any
  way the dispatcher can rely on; an SQLite row is.

## Consequences

- **Positive:** dispatcher polling is a fast partial-index
  scan. No JSON path extraction on the hot path.
- **Positive:** typed columns answer "what's the oldest
  pending event?" / "how many pending for envelope X?" with
  single-line `sqlite3` queries.
- **Positive:** correlation queries (`WHERE correlation_id =
  ?`) are indexable.
- **Positive:** schema diffs in migrations show which
  envelope-level fields the classifier persists; a future
  agent inspecting the codebase sees the contract obligation
  in the schema, not just in the docs.
- **Negative:** row column names diverge from wire field
  names (`event_type` vs `type`, `created_at` vs `timestamp`,
  `envelope_version` vs `version`). Renaming on read is a
  small constant tax in the dispatcher; invisible to
  consumers of the bus.
- **Negative:** adding a new envelope-level field means a
  schema migration. Acceptable: the envelope shape is
  governed and changes are ADR-gated.
- **Neutral:** payload stays JSON. Per-event-type bodies
  vary by type; typing them is structurally not available
  without a wide-table or per-type-table approach, both
  heavier than the JSON column trade-off warrants for the
  current emission cardinality.

## References

- `./.project/plans/2026-04-18-classification-service-foundation.md`
  § "Event contract" — wire envelope shape;
  § "Storage" — the `EventOutboxRepo` named in the repo list.
- `./migrations/versions/3434894db396_events_outbox.py` — the
  schema this ADR pins.
- `./src/prism/events_outbox.py` — the repo + dataclass
  consuming the schema.
- `./docs/adr/001-raw-sqlite3-in-application-code.md` — the
  repo follows this pattern (raw `sqlite3`, no ORM).
- `./docs/adr/014-mutating-repo-methods-return-canonical-post-mutation-state.md`
  — `enqueue` returns the canonical row.
- `./docs/adr/005-outboxbatch-scan-cursor-decoupled-from-parse-success.md`
  — the *upstream* (scraper) outbox; this ADR concerns the
  *downstream* (classifier-emitted) outbox. Independent.
