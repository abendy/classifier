# ADR 014: Mutating repo methods return the canonical post-mutation state

## Status

Accepted

## Date

2026-04-20

## Context

ADR 007 pins refresh invariants: `refresh_by_source_id`
preserves the stored `identity.id` and `system.created_at`
while overwriting every other field from the input envelope.
The input envelope's `identity.id` is whatever the mapper
minted for this observation — a per-observation UUID, not a
stable handle.

Slice 7b needed the stable id to key downstream embedding rows
to the content rather than the observation. The first
implementation read `identity.id` off the input envelope and
landed a correctness bug: same content across two observations
produced two different embedding keys, defeating the point of
upsert. The fix: `refresh_by_source_id` returns the merged
envelope, so callers thread the preserved id off the return
value.

## Decision

Repo methods that mutate rows and compute a post-mutation
canonical state **return** that state.
`refresh_by_source_id(envelope: ContentEnvelope) -> ContentEnvelope`
is the first expression of the pattern. Future methods that
merge, upsert, or otherwise transform input into a stable
stored form follow the same shape.

The rule does not apply to mutations that produce no
caller-visible derived state (e.g., an `INSERT OR IGNORE` that
just records activity). Those may still return `None`.

## Alternatives considered

- **Return `None`; callers re-fetch via a new public
  `get_by_source_id`.** Extra round trip, new read method with
  exactly one caller context today, state duplication between
  the mutation's in-memory merge and the re-fetch. Rejected.
- **Return `None`; callers read the input envelope.** Landed
  the correctness bug this ADR exists to prevent — input
  `identity.id` is transient; reading it silently produces
  wrong keys. Rejected.
- **Return only the stable id(s) that changed.** Works for the
  embedding case but doesn't generalize: future mutations will
  want to expose merged content and timestamps too. Rejected
  as too narrow.
- **Mutate the input object in place.** Pydantic frozen models
  forbid it. Even without that, returning a distinct
  post-mutation value is clearer than silently rewriting the
  caller's input. Rejected.

## Consequences

- **Positive:** callers that need post-mutation state get it
  zero-cost — no second query, no re-merge.
- **Positive:** the return type declares the contract in the
  signature; readers see "this method gives you stable state
  back" without reading the body.
- **Negative:** callers that don't need the return value
  either bind-and-ignore or get a `reportUnusedExpression`
  noise from basedpyright. Acceptable — the cost is trivial
  and the caller is free to just not bind it.
- **Neutral:** pure-side-effect mutations are exempt, so the
  rule is a convention for the "mutate + expose derived state"
  subset, not an absolute.

## References

- `src/prism/envelope_repo.py` — `refresh_by_source_id` return
  type and docstring.
- ADR 007 — the refresh invariants whose stable id this
  ADR's return-value convention exposes.
- `src/prism/ingest.py` — `_process` embed phase, the first
  caller that relies on the returned stable `identity.id`.
