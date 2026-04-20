# ADR 016: Serve-time long-lived loops own no span

## Status

Accepted

## Date

2026-04-20

## Context

`prism serve` runs the ingest pipeline as a single
`asyncio.Task` inside the FastAPI lifespan. Each iteration
calls `ingest_once`, which opens its own
`operation("ingest", ...)` span per pass (ADR 010); the
embed phase inside that pass opens nested per-envelope
`operation("embed", ...)` spans (ADR 013).

A plausible-looking addition is to also wrap the loop itself
in a span — "one trace covers the whole serve session."
That temptation needs to be resisted. A span that opens at
lifespan enter and closes at lifespan exit runs for the life
of the serve process: hours or days. Phoenix renders it as
one enormous open span with hundreds of per-pass children,
which:

- obscures the per-pass shape that actually matters to
  operators debugging ingest behavior,
- never closes in a normal serve session, so downstream
  tooling that assumes span closure (sampling, indexing,
  trace-UI sorting) misbehaves,
- adds an artificial root that carries no useful attributes
  — the serve process identity is already in `service.name`
  on the OTel resource.

Phase 2 will add more serve-time coordinators (classify,
route, reconcile workers). Each will face the same
"should the loop own a span?" question; pinning the rule
here avoids re-debating it three times.

## Decision

Long-lived serve-time coordinators open no span of their
own. They coordinate cadence, handle pass-level errors, and
delegate all tracing to the callee that performs one unit
of work. Each iteration's callee opens its own span via
`operation()`; the loop stays invisible to tracing.

Pass-level errors (DB down, unexpected exceptions in the
callee) are observable via `log_event(...)`, which stamps
timestamp and service identity. An error that happens
between passes has no span — which is the right answer,
since no unit of work was attempted for it to belong to.

## Alternatives considered

- **One loop-lifetime span.** Never closes during a normal
  serve session; obscures per-pass shape; breaks tooling
  that expects closure. Rejected — the trap the ADR exists
  to name.
- **One "loop-tick" span per iteration wrapping the
  callee.** Duplicates the span the callee already opens;
  adds a noise layer with no extra information. Rejected.
- **Auto-closing loop span on a timer (e.g., every 10 min).**
  Artificial boundaries that don't correspond to any real
  semantic unit. Operators can't reason about "which pass
  landed in which span." Rejected.

## Consequences

- **Positive:** Phoenix traces are a flat series of per-pass
  spans, which maps cleanly to "which attempt did what."
  Embed spans nest under their ingest pass per ADR 013.
- **Positive:** the rule generalizes — every future
  serve-time loop (classify, route, reconcile) stays out of
  tracing by convention, delegating to `operation()` calls
  at the work-unit layer.
- **Neutral:** pass-level failures between iterations log
  without a span. That's the right shape — no work unit was
  attempted, so no span should exist. If a future slice
  genuinely needs loop-level traces (e.g., to group recovery
  attempts), the right tool is a *bounded* operation with
  its own context, not a lifetime span.

## References

- `src/prism/serve.py` — `ingest_loop` function; coordinates
  cadence and error handling with no `operation()` or
  `tracer.start_as_current_span` wrapping.
- ADR 010 — two-layer exception handling in `operation()`;
  the per-pass span mechanics the loop delegates to.
- ADR 013 — correlation/causation on per-envelope spans;
  embed children inherit correlation from their ingest pass
  span, not from the loop.
