# ADR 006: The scraper-classifier bridge — scope, contract, retirement

## Status

Accepted

## Date

2026-04-19

## Context

The dada.stream contract calls for components to own their data and
exchange it across explicit envelopes: each edge service prepares its
own content envelopes and publishes `content-ingested` events;
downstream services (like the classifier) subscribe and consume
already-shaped envelopes. That boundary is the point of the contract
layer.

The scraper doesn't emit dada.stream envelopes today. Its outbox
emits pre-dada.stream events whose shape is explicitly scheduled to
change as the scraper converges toward the spec. Until it does, the
classifier has to paper over the gap somewhere.

ADR 004 named one half of this gap — the payload shape — and refused
to couple to it (two fields only: `entityType`, `entityId`). ADR 005
named another — cursor advancement through malformed payloads. Both
framed themselves as narrow tactical choices. Neither stated the
broader architectural fact: **the classifier is currently operating
an inbound bridge that knows scraper internals, and that bridge is
temporary by design**.

Slice 5's ingest orchestration made the bridge surface larger:

- `scraper_mapper.py` reads the scraper's `tweets`, `users`, `media`
  tables directly to synthesize envelopes.
- `scraper_outbox.py` reads the scraper's `outbox` table directly.
- `ingest.py` encodes scraper-semantic rules: `bookmark.created` as
  the authoritative bookmarked-tweet signal, `tweetId:folderId`
  entity-id parsing, `record.synced`/`record.enriched` as refresh
  events, tweet events refused unless an envelope already exists.

Each of those rules exists to translate scraper-internal choices
into classifier-usable signals. None of them would survive a
proper event-bus contract in which the scraper emits
`content-ingested` envelopes directly.

Left unnamed, the bridge expands. Every new scraper feature (new
event type, new entity-id shape, new enrichment phase) invites
another "small" translation rule inside the classifier.

## Decision

Treat `scraper_mapper.py`, `scraper_outbox.py`, and the
scraper-semantic logic inside `ingest.py` as a **named, bounded
bridge module**. The bridge has an explicit contract, an explicit
retirement trigger, and explicit rules about what can live inside
it.

**Scope of the bridge.** Code that knows any of the following is
bridge code:

- The shape of the scraper's SQLite schema (`tweets`, `users`,
  `media`, `outbox` columns).
- The shape of the scraper's outbox payload beyond the two fields
  ADR 004 already extracts.
- The scraper's event-type vocabulary (`bookmark.created`,
  `record.synced`, `record.enriched`, `bookmark.deleted`, ...).
- The internal structure of scraper-assigned identifiers
  (e.g., bookmark `entityId` as `tweetId:folderId`).

**Rules inside the bridge.** The bridge is allowed to:

- Read scraper tables read-only.
- Parse scraper event types to decide bootstrap vs. refresh.
- Parse scraper-assigned identifier structures defensively.
- Apply translation rules like "`bookmark.created` is the only
  bootstrap path" or "tweet events refresh-only when an envelope
  already exists" — these encode scraper semantics, not classifier
  semantics.

**Rules outside the bridge.** Classifier core (envelope model,
envelope repo invariants, classification pipeline, audit layer)
must not depend on any scraper-specific behavior. If a change to
classifier core requires touching `scraper_mapper.py` /
`scraper_outbox.py` / the bridge section of `ingest.py`, that is
a coupling violation and the alternative should be preferred.

**Refresh semantics encode the bridge's scope, not the
envelope's.** Because the bridge synthesizes partial envelopes
(only the mapper-owned sections: `identity`, `source`, `content`,
`system`), envelope refresh preserves every other section
(`classification`, `routing`, `state`, `relationships`) from the
stored copy. That rule is a bridge artifact: once the scraper
emits complete envelopes, refresh becomes full replacement keyed
by `source_id` and the per-section carve-out is no longer needed.
The invariant-level refresh rules (`identity.id` and
`system.created_at` preservation) are separate and outlive the
bridge — see ADR 007.

## Alternatives considered

- **Push the bridge into the scraper now.** The scraper emits
  `content-ingested` envelopes; the classifier subscribes directly.
  The correct long-term shape, but requires cross-repo work on a
  codebase the classifier doesn't own. Rejected for this phase;
  remains the retirement target.
- **Leave the bridge unnamed and let each slice add a rule ad hoc.**
  The current trajectory. Rejected: the bridge already grew in
  slice 5 with nothing to stop it growing further.
- **Split the bridge into its own Python package.** Tempting for the
  strongest possible boundary, but premature — three files, tightly
  coupled to the classifier's envelope model, and scheduled for
  deletion. Package extraction is deletion churn. Rejected.
- **Move the bridge into `~/projects/dada.stream/platform/` as a
  shared adapter for any edge service that hasn't converged yet.**
  Attractive if other services need the same shim, but nothing does
  today. Rejected until a second bridge consumer appears.

## Consequences

- **Positive:** the bridge has an explicit boundary; reviewers can
  point at any new scraper-semantic rule and ask "does this belong
  in the bridge?" rather than relitigating the principle.
- **Positive:** the retirement plan is concrete — the listed files
  are the ones that get deleted; the scraper-semantic rules inside
  `ingest.py` collapse into "receive envelope, save or refresh."
- **Negative:** the bridge is a second set of responsibilities for
  the classifier repo to carry until the scraper converges. Every
  maintenance touch there is work the classifier team is doing on
  behalf of a contract the scraper should have met.
- **Neutral:** the bridge inherits ADR 004 (two-field payload
  decoupling) and ADR 005 (cursor decoupling). Those ADRs survive
  inside the bridge's lifetime and are deleted with it.

## Revisit when

The scraper starts emitting `content-ingested` events whose payload
is a dada.stream-conformant `ContentEnvelope`. At that point:

1. `scraper_mapper.py` is deleted.
2. `scraper_outbox.py` is replaced by an event-bus consumer (or
   deleted if the bus transport is shared infrastructure).
3. `ingest.py`'s `_resolve_source_id` / `_extract_tweet_id` logic
   is deleted. The ingest loop simplifies to "read envelopes,
   save-or-refresh by `source_id`."
4. `_MAPPER_OWNED_SECTIONS` carve-out in `envelope_repo.py` is
   deleted; refresh becomes full replacement.
5. ADRs 004 and 005 are marked superseded by this one.

A partial retirement (e.g., scraper emits conformant payload but
classifier still reads bookmarks from tables) is acceptable; the
bridge shrinks incrementally but the boundary stays named.

## References

- `src/prism/scraper_mapper.py` — tables-to-envelope bridge.
- `src/prism/scraper_outbox.py` — outbox-to-rows bridge.
- `src/prism/ingest.py` — scraper-semantic rules: `_resolve_source_id`,
  `_extract_tweet_id`, `BOOKMARK_CREATED_EVENT`,
  `TWEET_REFRESH_EVENTS`.
- `src/prism/envelope_repo.py` — `_MAPPER_OWNED_SECTIONS` carve-out
  for refresh.
- ADR 004 — two-field payload decoupling (bridge-internal).
- ADR 005 — cursor decoupling (bridge-internal).
- ADR 007 — envelope refresh invariants (outlives the bridge).
- `~/projects/dada.stream/contracts/service-manifest.md` — the
  envelope contract the scraper is converging toward.
- `~/projects/x-bookmarks-scraper/src/lib/outbox/event-envelope.ts` —
  the scraper's current (non-conformant) event envelope shape.
- `.project/plans/2026-04-18-classification-service-foundation.md`,
  "Envelope mapping (v1 bridge)" section.
