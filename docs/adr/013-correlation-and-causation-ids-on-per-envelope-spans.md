# ADR 013: Thread both `correlation_id` and `causation_id` on per-envelope operation spans

## Status

Accepted

## Date

2026-04-20

## Context

Per-envelope pipeline stages — embed today; classify, route,
reconcile later — nest inside a parent ingest (or future batch)
operation span. The plan's §Event contract treats
`correlationId` (the stable content-envelope handle that
persists across observations and pipeline stages) and
`causationId` (the immediate trigger run id) as distinct
fields, both captured on run records and OTel traces.

The first per-envelope stage (embed, slice 7b) initially
collapsed the two: the slice prompt said "do not thread
`correlation_id` on the embed span — `envelope_id` already
carries it." Review caught the alignment gap against the plan.
`operation()` already accepts both kwargs; only the call site
needed to pass them.

## Decision

Every per-envelope operation span passes three kwargs:

- `envelope_id=<stored envelope id>` — the entity this run acts on
- `correlation_id=<stored envelope id>` — the cross-stage content thread
- `causation_id=<parent run id>` — the immediate trigger

`envelope_id` and `correlation_id` carry the same value on
envelope-scoped stages, but they are not synonyms. `envelope_id`
identifies the entity this run operates on; `correlation_id` is
the cross-operation thread that survives as the pipeline hands
off (embed → classify → route), so a single envelope's full
trajectory is filterable as one timeline.

## Alternatives considered

- **`envelope_id` alone; treat correlation as implicit.** The
  slice-7b prompt's original shape. Collapses two plan-contract
  fields into one, breaks filter-by-content-thread across
  stages, silently drops the "run records carry both IDs" plan
  requirement. Rejected.
- **`correlation_id` alone; derive `envelope_id` from it.**
  Loses the per-entity filter on the `runs` table and conflates
  "this run's subject" with "this pipeline's thread." Rejected.
- **Omit `causation_id`, rely on OTel `parent_span_id`.**
  `parent_span_id` is span-scoped, lost when spans close or get
  sampled away. `causation_id` is run-row-scoped and durable.
  Rejected.

## Consequences

- **Positive:** every per-envelope stage is filterable on three
  independent axes — entity (`envelope_id`), content thread
  (`correlation_id`), trigger chain (`causation_id`).
- **Positive:** the three-kwarg call is copy-pasteable — future
  classify/route/reconcile slices reuse the exact shape.
- **Neutral:** `envelope_id` and `correlation_id` duplicate on
  envelope-scoped operations today. Intentional: the fields
  serve different filters even when values coincide.

## Revisit when

A batch operation ships that acts on envelope *sets* rather
than single envelopes — the envelope_id vs correlation_id
distinction will stop being "same value, different axes" and
start requiring real decisions about which id goes where.

## References

- `src/prism/ingest.py` — `_process` embed phase, the first
  per-envelope nested span using the three-kwarg pattern.
- `src/prism/audit.py` — `operation()` kwarg surface and the
  `runs` table columns.
- ADR 010 — two-layer exception handling in `operation()`;
  paired with this ADR on per-envelope span mechanics.
