# ADR 009: Terminal log events fire after the operation's span closes, with `trace_id` threaded explicitly

## Status

Accepted

## Date

2026-04-19

## Context

`operation()` opens a span and writes a `pending` run row on
entry. The row transitions to `success` only when the `with`
block exits cleanly and `record_success` commits. A log event
claiming "done" fired inside the `with` block would precede the
durable success record — if `record_success` then raises, we'd
have a success-shaped log line plus an error-shaped run row on
disk. That mismatch is exactly the regression ADR 007's lifecycle
invariants are meant to prevent.

Terminal log events also need `trace_id` so operators can
cross-reference log lines with Phoenix spans. `trace_id` is only
available from `trace.get_current_span().get_span_context()` while
the span is current — after `with` exit, the current span is the
parent (or NoOp if there is none). `log_event`'s auto-injection
won't fill `trace_id` on terminal events because the current
context has already moved on.

## Decision

Terminal log events fire **after** the `with operation(...)`
block exits. The caller captures `trace_id` inside the block
(while the span is current) and passes it explicitly as a
`trace_id=` kwarg to the post-block `log_event`. When tracing
isn't configured, the captured trace id is `None`, and the
kwarg is **omitted entirely** — not emitted as a `"trace_id":
null` sentinel.

```python
with operation("ingest", repo=run_repo) as op:
    trace_id = _current_trace_id()  # captured inside the span
    # ... work ...
# record_success has committed by now
kwargs = {"trace_id": trace_id} if trace_id is not None else {}
log_event("ingest.done", run_id=run_id, operation="ingest",
          duration_ms=ms, **asdict(stats), **kwargs)
```

## Alternatives considered

- **Fire terminal log inside the `with` block.** Breaks the
  "no success-shaped log before durable success" rule; a
  `record_success` failure afterwards produces an inconsistent
  pair.
- **Rely on `log_event`'s auto-injection.** Auto-injection reads
  the current span; outside the block, the current span has
  changed. The `trace_id` silently drops off terminal events
  and operators lose cross-reference.
- **Emit `"trace_id": null` when unconfigured.** Clutters output
  and implies "tracing was running but the span was invalid" —
  misleading. The 6a discipline ("don't emit null / sentinel
  fields") applies here too.
- **Move the `record_success` call outside the `with` block.**
  Breaks `operation()`'s contract: the context manager owns the
  success/error transition so callers never worry about partial
  state.
- **Add a `log_terminal` helper on `OperationContext` that
  captures trace_id automatically.** Plausible future
  refactor, but adds surface before a second caller exists
  that needs the pattern. Left to a future slice when
  repetition earns it.

## Consequences

- **Positive:** terminal log events fire only after the run row
  is durably `success`; operators can trust them.
- **Positive:** `trace_id` is populated when tracing is
  configured; absent (not null) when not.
- **Positive:** the pattern generalizes — every future step with
  a terminal summary event (`classify.done`, `embed.done`,
  `eval.done`) uses the same shape.
- **Negative:** the caller has to remember to capture `trace_id`
  inside the block. Easy to forget in copy-paste; lint won't
  catch it. A helper on `OperationContext` could automate this
  when repetition justifies the abstraction.

## Revisit when

A second caller writes a terminal log event (first per-envelope
step — classification or embedding). If the capture-and-thread
pattern repeats verbatim, extract to an
`OperationContext.log_terminal(event, **fields)` helper and
update this ADR to point at it.

## References

- `src/prism/ingest.py` — `ingest_once`'s post-block
  `log_event("ingest.done", ...)` call.
- `src/prism/logs.py` — `log_event`'s auto-injection gates on
  `span.is_recording() AND span_ctx.is_valid`; outside-block
  callers bypass the auto-path.
- `tests/test_ingest.py` —
  `test_ingest_once_populates_trace_id_and_canonical_log_fields`
  (configured path) and
  `test_ingest_once_omits_trace_id_when_tracing_unconfigured`
  (unconfigured path) pin both halves.
- ADR 007 — the lifecycle invariant this ADR honors at the
  log layer.
