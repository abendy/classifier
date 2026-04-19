# ADR 004: Extract only entityType and entityId from the scraper's outbox payload

## Status

Accepted

## Date

2026-04-19

## Context

The classifier reads the scraper's outbox table to learn that
content has arrived. The scraper's current payload is a
pre-dada.stream envelope — it predates the dada.stream spec and
will change as the scraper converges toward spec conformance.

The classifier needs the bare minimum from the outbox today to
unblock downstream work. It must not take a dependency on a
payload shape that is explicitly scheduled to change.

See the plan's "Envelope mapping (v1 bridge)" section in
`.project/plans/2026-04-18-classification-service-foundation.md`
for the retirement trigger.

## Decision

`ScraperOutboxReader` parses the JSON payload of each outbox row
and extracts exactly two fields: `entityType` and `entityId`.
Everything else — scraper-side metadata, provenance, idempotency
keys, the rest of the envelope — is not surfaced past the reader.
Rows whose payload is malformed or lacks those two string fields
are silently skipped.

## Alternatives considered

- **Parse the full scraper envelope.** Would couple the classifier
  to a payload shape the scraper is actively planning to change.
  Rejected: the whole point of the bridge is to survive that
  change without touching the classifier.
- **Wait for the scraper to emit dada.stream-conformant
  `content-ingested` events first.** Would block classifier work
  indefinitely on unrelated scraper cleanup. Rejected.
- **Use row-level columns only, no payload parsing.** The outbox
  table doesn't have an `entityId` column; the ID lives in the
  JSON. Not currently available.

## Consequences

- **Positive:** a two-field contact surface. The scraper can
  reshape its payload freely; the classifier only breaks if
  `entityType` or `entityId` are renamed or removed.
- **Negative:** malformed rows are skipped silently. The audit
  layer that surfaces skip counts does not exist yet; when the
  agent-era observability slice lands, it will.
- **Neutral:** the reader is read-only; no risk of perturbing
  the scraper's state.

## Revisit when

The scraper's cleanup lands and it starts emitting
dada.stream-conformant `content-ingested` events directly. At
that point, this bridge retires — the reader is replaced by a
spec-conformant consumer.

## References

- `src/prism/scraper_outbox.py` — `_extract`, `OutboxRow`,
  `ScraperOutboxReader.rows_after`.
- `.project/plans/2026-04-18-classification-service-foundation.md`
  — "Envelope mapping (v1 bridge)" section; retirement trigger.
