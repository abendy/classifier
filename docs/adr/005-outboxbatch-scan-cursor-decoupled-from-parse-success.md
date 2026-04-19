# ADR 005: Advance the outbox polling cursor independently of parse success

## Status

Accepted — scoped to ADR 006 (bridge-internal; retires with the bridge)

## Date

2026-04-19

## Context

The scraper's outbox table is append-only with monotonically
increasing `id`. The classifier polls it; each scan asks for rows
with `id > cursor`. A run of malformed payloads (per ADR 004, the
reader silently skips rows that don't yield `entityType` +
`entityId`) is possible today and expected to persist until the
scraper's cleanup lands.

If the polling cursor is advanced only via successfully-parsed
rows, a window of all-malformed rows starves the loop: the caller
has no successful id to advance past, so the next scan re-reads
the same malformed window, forever.

## Decision

`ScraperOutboxReader.rows_after(cursor, limit)` returns
`OutboxBatch(rows, next_cursor)`. `next_cursor` is the highest
outbox `id` observed during the scan — regardless of whether the
row parsed into an `OutboxRow`. Consumers advance their polling
cursor via `next_cursor`, not via
`max(row.source_event_id for row in rows)`.

## Alternatives considered

- **Return `list[OutboxRow]`; caller advances via
  `max(r.source_event_id ...)`.** The naive shape. Rejected: a
  window of all malformed rows returns `[]`; caller cannot compute
  a max; polling loop wedges behind the malformed block
  indefinitely. The regression test
  `test_next_cursor_advances_past_all_malformed_window` exists to
  keep this bug from being reintroduced.
- **Raise on malformed payload and require the caller to handle
  it.** Pushes the skip policy to every caller and couples them to
  the payload shape the reader exists to hide. Fragile.
- **Emit a sentinel "skipped" row in the return list.** Mixes
  successful and skipped rows; every caller has to filter anyway.

## Consequences

- **Positive:** the polling loop always advances; a malformed
  window cannot stall the pipeline.
- **Negative:** the two-field `OutboxBatch` is slightly more API
  surface than a bare list. The orchestration slice has to
  persist `next_cursor` somewhere outside the ingest queue —
  the queue's `max_source_event_id` only reflects
  successfully-parsed rows, so it can't double as the scan
  cursor.
- **Neutral:** malformed rows still disappear silently. Surfacing
  skip counts to an observability layer is a separate concern.

## Revisit when

The pipeline migrates off polling entirely (push, bus transport);
the scan-cursor / queue-cursor split becomes moot.

## References

- `src/prism/scraper_outbox.py` — `OutboxBatch`,
  `ScraperOutboxReader.rows_after`, `_extract`.
- `tests/test_scraper_outbox.py` — 
  `test_next_cursor_advances_past_all_malformed_window`.
- ADR 004 — the two-field extraction policy this decision
  complements.
