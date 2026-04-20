# ADR 010: Two-layer exception handling with identity-based deduplication in `operation()`

## Status

Accepted

## Date

2026-04-19

## Context

`operation()` must handle two distinct failure modes and leave a
consistent span + run row in both:

1. **User-block failure:** the yielded-to block raises. The span
   should be marked `ERROR`, the exception recorded on the span,
   `record_error` called to transition the run row, and the
   exception re-raised.
2. **Audit-persistence failure:** one of `record_start` /
   `record_success` / `record_error` itself raises (DB locked,
   disk full, schema drift). The span should still reflect
   `ERROR` status — otherwise the trace goes quiet precisely
   when the audit layer is broken — and the exception must
   propagate.

A single try/except layer can't tell these apart. A bare boolean
flag (`span_marked_error = True`) can't distinguish "I'm
passing through a re-raise of the same exception the inner
except handled" from "a different exception is propagating now"
— and that distinction matters: duplicating the recording would
misattribute the persistence failure to the user, or the
primary status description could get overwritten by the
secondary failure.

## Decision

Two layers. The inner except catches user-block exceptions,
records them on the span, calls `record_error`, assigns
`handled_exc = exc`, and re-raises. The outer except catches
anything that reaches the persistence boundary and guards on
exception **identity**:

```python
handled_exc: Exception | None = None
try:
    repo.record_start(...)
    try:
        yield ctx
    except Exception as exc:
        span.set_status(Status(StatusCode.ERROR, repr(exc)))
        span.record_exception(exc)
        handled_exc = exc
        repo.record_error(...)
        raise
    repo.record_success(...)
except Exception as exc:
    if exc is not handled_exc:
        span.record_exception(exc)
        if handled_exc is None:
            span.set_status(Status(StatusCode.ERROR, repr(exc)))
    raise
```

- Same identity → inner except already recorded; passthrough;
  skip the duplicate event.
- Different identity with `handled_exc is None` → persistence
  raised directly (no user exception); record it and set the
  status description.
- Different identity with `handled_exc is not None` → user
  exception already set the primary status description;
  `record_error` raised on top; record the secondary as a
  separate span event but don't clobber the primary description.

## Alternatives considered

- **Single except layer.** Can't tell user-block errors apart
  from persistence errors; either loses ERROR status on
  persistence-only failures or double-records.
- **Boolean flag (`span_marked_error`).** Doesn't
  differentiate re-raise of the same exception from a different
  exception propagating. The bug: a `record_error` failure
  after a user exception would be suppressed because the flag
  is set.
- **Try/except per persistence call (three separate blocks for
  start / success / error).** Explodes in line count; harder
  to reason about the combined state machine; mostly repeats
  the same `span.record_exception(exc); raise` boilerplate.
- **Let persistence failures crash silently.** The span stays
  `UNSET` and the operator sees nothing for the failure case
  — exactly when they'd want the most signal.

## Consequences

- **Positive:** persistence-layer failures surface on the span
  even when they cascade with user exceptions.
- **Positive:** no duplicate events on re-raise of the same
  exception.
- **Positive:** primary user cause stays as the span's status
  description; subsequent failures don't overwrite it.
- **Negative:** contributes to `audit.py` exceeding the
  slice-prompt's ~400-line soft target (currently 413). The
  linear prose of two layers reads more clearly than a
  helper-extracted single layer.
- **Negative:** subtle control flow. A future refactor that
  "simplifies" back to one layer would re-introduce the bugs
  this ADR is pinned against.

## Revisit when

A helper extracted to make the two-layer pattern compact
(e.g., `_finalize_on_exit(span, handled_exc, exc)`), or if
OpenTelemetry exposes a span-error-recording helper that
handles identity deduplication internally. Either change
shrinks the code; keep the ADR pointing at the replacement.

## References

- `src/prism/audit.py` — `operation()` context manager; the
  `try/except raise; outer except:` structure encoded in
  lines ~320–385.
- `tests/test_audit.py` —
  `test_operation_marks_span_error_when_audit_persistence_raises`
  and
  `test_operation_records_both_exceptions_when_record_error_also_fails`
  pin the two branches the identity guard distinguishes.
