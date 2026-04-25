# ADR 018: Python full-scan cosine for topic-prototype retrieval

## Status

Accepted

## Date

2026-04-25

## Context

Topic-head retrieval (`prism.topic_retrieval`) ranks topic
candidates for an envelope by cosine similarity over the
prototype vectors stored in the `topic_prototypes` vec0 table.
The plan (§Topic head — retrieval + LLM-pick) calls for "cosine
similarity between item embedding and topic prototypes, take
top 5 candidates"; aggregation across multiple prototypes per
topic is max-similarity (best-matching exemplar wins for that
topic).

`sqlite-vec` offers two read paths against a `vec0` table:

1. **KNN syntax**: `WHERE embedding MATCH ? AND k = ?` — uses
   the table's built-in nearest-neighbor index, optimized for
   large prototype sets, but tied to the table's default
   distance metric (L2 on unnormalized vectors, which is not
   cosine).
2. **Function-call syntax**:
   `ORDER BY vec_distance_cosine(embedding, ?) LIMIT ?` —
   computes cosine per row, full-scan, no index acceleration.

Or the read path can fetch every row into Python and rank in
numpy.

Phase 1's scale is small: a curated catalog of ~75 topics with
~3–4 prototypes each (~300 rows). Vector size is 1024-d float32
(~4 KB/row, ~1.2 MB total for the full set). A full table scan
plus a numpy dot product against the query is sub-millisecond
on the Mini and trivially correct.

The `MATCH`/`k=?` KNN path is unsuitable as-is — its default
metric is not cosine, prototype vectors are not L2-normalized
at write time (BGE-large-en-v1.5 outputs are not unit-length
by default), and switching to `vec_distance_cosine` inside an
`ORDER BY` is also a full scan but pushes the math into SQLite
without aggregation help. Aggregation across exemplars per
topic — selecting the max-scoring prototype per `topic_id`
— still has to happen in application code regardless of which
ranking path is chosen.

## Decision

The retrieval read path fetches every prototype for the named
`model_version` into Python via
`TopicPrototypeRepo.list_vectors_for_model`, computes cosine
similarity per row in numpy, aggregates by topic with
max-similarity, sorts descending, and returns the top-k. No
sqlite-vec KNN syntax, no `vec_distance_cosine` SQL, no
caching layer.

The trigger to revisit is when the prototype count grows large
enough that the full-scan dominates retrieval latency on the
production hot path (a working threshold to monitor: ~5,000
rows, ~6× current scale, where a per-call ~25 ms scan starts to
matter under sustained ingest). At that point the swap target
is sqlite-vec's KNN with pre-normalized prototypes (write-side
change to L2-normalize before storage so KNN's L2 ordering
matches cosine ordering), evaluated against the current
read-then-rank path on real catalog data.

## Alternatives considered

- **`vec0` `MATCH ? AND k = ?` KNN with pre-normalized
  prototypes.** Storage-side change (normalize at write time)
  plus query-side `MATCH`. Works, but premature for the
  current scale and adds a write-side invariant that has to be
  maintained on every embedder swap or recompute. Rejected
  for now; the working threshold above is the trigger to
  revisit.
- **`ORDER BY vec_distance_cosine(embedding, ?) LIMIT ?` in
  SQL.** Cosine without the normalization invariant, but still
  a full scan, and aggregation across exemplars-per-topic
  cannot be done inside the same SQL without a non-trivial
  CTE or post-processing step that defeats the locality
  benefit. Rejected — moves the same work behind a less
  testable boundary.
- **Cache the prototype matrix in process memory.** A
  `(N, 1024)` float32 array (~1.2 MB) loaded once per serve
  process would let retrieval skip the SQLite read. Rejected
  for this slice — retrieval is not yet on the production hot
  path, the read is cheap, and a cache adds invalidation
  responsibility (every `prism dev load-topics` would need to
  signal). Becomes interesting when retrieval lives inside
  the ingest loop; defer the decision until then.
- **Switch to dot product with pre-normalized prototypes (no
  cosine division).** Optimization, not correctness. Couples
  retrieval to a write-side invariant. Rejected for the same
  reasons as the KNN-with-normalization option.

## Consequences

- **Positive:** the retrieval module is a pure function of
  `(query_vector, repo state, model_version, k)` — testable
  with stub repos, no SQL hidden behind it, no caching to
  invalidate. Aggregation logic is one dict comprehension and
  a sort, readable in isolation.
- **Positive:** zero coupling to sqlite-vec KNN semantics —
  the prototype storage shape can change (additional
  exemplars, alternate text encodings, future per-row
  weights) without touching the ranking code.
- **Positive:** no write-side invariants. Prototype vectors
  are stored exactly as the embedder produced them; any
  inspection or recompute against stored data sees raw model
  output.
- **Negative:** O(N) per call where N is prototype count.
  At ~300 rows the per-call cost is sub-millisecond; at
  ~5,000 rows it begins to matter on a hot path. The
  threshold is operationally observable (retrieval span
  duration in Phoenix when retrieval lands on the ingest
  path).
- **Negative:** every retrieval call does a SQLite read and a
  bytes→numpy conversion of the full prototype set. No
  amortization across calls. Cache layer is the natural
  upgrade when call rate makes the redundant work visible.
- **Neutral:** sqlite-vec is still loaded for the
  `topic_prototypes` table to exist (the `vec0` storage
  format requires it), even though the retrieval path
  doesn't use any vec0-specific query syntax. The extension
  load cost is paid once per connection regardless.

## References

- `./src/prism/topic_retrieval.py` — the retrieval functions
  this ADR pins.
- `./src/prism/topic_prototype_repo.py` — `list_vectors_for_model`
  is the read entry point; `np.frombuffer(..., dtype=np.float32)`
  is the byte-decode mirror of the write side.
- `./docs/adr/011-synthetic-text-pk-for-vec0-composite-identity.md`
  — composite-key encoding the read path complements.
- `./docs/adr/017-default-dense-embedder-model-selection.md`
  — `MODEL_VERSION` is the bridge between envelope and
  prototype vectors that retrieval filters on.
- Plan §Topic head (retrieval + LLM-pick) — the contract this
  retrieval implements the first half of.
