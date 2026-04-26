# ADR 022: events_outbox internal flag and typed event vocabulary

## Status

Accepted

## Date

2026-04-25

## Context

The classifier writes one row per emitted event into
`events_outbox`. ADR 020 framed the table as the durable record
of classifier-emitted events for a future dispatcher to publish
onto the eventual event bus.

Two distinct issues sat under that frame:

- **Synthetic `content-ingested` events.** When `_classify_envelope`
  pops an ingest-queue row, it mints a `content-ingested` event
  and writes it to `events_outbox` so the classify run's
  `causation_id` references a real envelope id. The synthesis
  exists because no real event bus carries `content-ingested`
  events yet; the classifier has to anchor causation locally.
  These rows are *not* meant to be published - when the
  dispatcher lands, publishing them would put `content-ingested`
  events from the classifier on the bus, colliding with real
  `content-ingested` events from edge services.
- **Event-type names as string literals.** `"content-classified"`,
  `"content-classified-low-confidence"`, `"content-ingested"`
  appeared inline in classify-pipeline code, tests, and
  `service.json`. A typo would corrupt the wire vocabulary
  silently.

## Decision

- Add an `internal BOOLEAN NOT NULL DEFAULT FALSE` column to
  `events_outbox`. Synthetic causation-anchor events pass
  `internal=True`; genuinely-emitted public events leave it
  `False`. The pending index becomes partial on
  `WHERE dispatched_at IS NULL AND internal = 0` so dispatcher
  polling skips internal rows by construction.
- Type the event vocabulary via a `Literal` alias `EventType` in
  `prism.events_outbox`. The `EmittedEvent.event_type` field and
  the column read/write are typed; basedpyright catches typos at
  the call site.

## Alternatives considered

- **Separate table for internal causation anchors.** Considered.
  Rejected because the data shape is identical to public events
  (envelope id, type, source, timestamp, correlation id, causation
  id, payload, version) and the only behavioral difference is "do
  we publish?" A boolean column expresses that difference at the
  cost of one bit per row; a separate table doubles the storage
  surface and forces the dispatcher to reason about two sources.
- **Rename synthetic events to a non-conformant type
  (`internal-classify-anchor` or similar).** Considered. Rejected
  because the synthetic-event approach exists to preserve
  wire-format conformance - the classifier's audit chain looks
  exactly like the future bus's would. Renaming defeats the
  conformance and complicates the migration when a real bus
  starts emitting `content-ingested` events. Keeping the type
  honest while marking the row internal preserves both
  properties.
- **Filter at dispatcher time without a column** (e.g. by
  matching `correlation_id == envelope_id AND causation_id IS
  NULL`). Considered. Rejected because that pattern is incidental
  to the current synthetic shape; a future internal-event kind
  with different correlation semantics would break the heuristic.
  The explicit column survives shape evolution.
- **`kind TEXT` column instead of `internal BOOLEAN`.** A string
  vocabulary (`"public"` / `"internal"` / future kinds) is more
  extensible. Rejected because the only known distinction today
  is "do we publish?" - a boolean encodes that exactly. If a
  future kind needs more granularity, swapping a boolean to a
  string-discriminator is one Alembic revision away.
- **String literals for event types (the prior shape).**
  Rejected because a typo only fails when a test inspects that
  branch. The Literal alias is a one-line change with full
  basedpyright coverage at every call site.

## Consequences

- **Positive:** the future dispatcher's filter is a partial-index
  scan with no JSON path extraction. Internal rows never reach
  the bus.
- **Positive:** typed event vocabulary catches typos across
  classify-pipeline code, tests, and any future emitter at type-
  check time.
- **Positive:** synthetic-event semantics are now visible in the
  schema, not just in code comments. An operator inspecting
  `events_outbox` sees `internal=1` rows and understands they're
  not public.
- **Negative:** adding a new event type means editing both the
  Literal and `service.json`. Bounded cost; new event types are
  ADR-gated already.
- **Negative:** the dispatcher (separate near-future slice) now
  has a column it must respect; agents proposing a "simpler
  dispatcher" pattern that ignores `internal` will silently leak
  internal events onto the bus. The partial index makes the right
  query the obvious one but does not enforce it.
- **Neutral:** SQLite stores booleans as 0/1; the dataclass field
  stays Python `bool` with conversion at the repo boundary.

## References

- `./.project/plans/2026-04-18-classification-service-foundation.md`
  section "Event contract" - wire envelope shape; section
  "Reconcile and emit" - outcome taxonomy.
- `./.project/reviews/2026-04-25-post-phase-2-structural-review.md`
  section 6 "Events outbox publishes a type the manifest says is
  subscribed"; section 7 "Event names are call-site strings rather
  than typed vocabulary".
- `./migrations/versions/41c92ab62f96_events_outbox_internal.py` - the
  schema change this ADR pins.
- `./src/prism/events_outbox.py` - the `EventType` alias and the
  `internal` field on `EmittedEvent` / `EmittedEventRow`.
- `./src/prism/classify_pipeline.py` - synthetic
  `content-ingested` events pass `internal=True`.
- `./docs/adr/020-typed-columns-for-events-outbox.md` - typed-
  columns precedent; this ADR extends 020 rather than superseding
  it.
- `./docs/adr/006-scraper-classifier-bridge-scope-and-retirement.md`
  - when the bridge retires and real `content-ingested` events
  flow over a real bus, the synthetic-event synthesis goes away
  but the `internal` column stays for any future internal-emission
  scenarios.
