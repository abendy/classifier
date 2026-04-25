# ADR 017: Use BAAI/bge-large-en-v1.5 as the default dense embedder

## Status

Accepted

## Date

2026-04-25

## Context

The plan (`§Embedding`) names BGE-M3 as the production dense
embedder: 1024-d, multilingual, ~570M params, 8K context, with
hybrid dense+sparse retrieval as a future-friendly property for
keyword-heavy post content. Phase 1 wired the embedder through
`fastembed.TextEmbedding(model_name="BAAI/bge-m3")` in
`create_bge_m3_embedder()`.

The currently installed `fastembed` (0.8.x) no longer ships
`BAAI/bge-m3` in its `TextEmbedding` model registry. Calling
the factory at serve boot raises before the FastAPI lifespan
finishes startup, so `prism serve` cannot come up. The unit-
test suite did not catch this because the integration test
that actually constructs a real `TextEmbedding` is deselected
by default (`addopts = -m 'not integration'`). The first
operator invocation of `prism serve` against the installed
deps was the discovery vector.

The storage layer is keyed by `(envelope_id, model_version)`
(ADR 011). The `vec0` table has a hard 1024-d schema (the
migration `8a27a28168d0_embeddings.py` declares
`embedding FLOAT[1024]`). Any replacement that doesn't match
1024-d would force a migration; any replacement at 1024-d that
keeps the `Embedder` interface (single `embed(text) -> list[float]`)
is a drop-in by storage shape.

`fastembed` 0.8's registry includes `BAAI/bge-large-en-v1.5`:
1024-d, English-only, ~335M params, 512-token context. It
shares BGE's training lineage and is the supported BGE family
member in the current `fastembed` line.

## Decision

The default dense embedder is `BAAI/bge-large-en-v1.5`. The
factory function is renamed `create_default_embedder` to drop
the model-specific name from the API surface so a future
swap doesn't drag a misleading function name behind it. The
exposed `MODEL_VERSION` constant is `bge-large-en@1.5`; rows
written against the prior (never-shipped) `bge-m3@1.5` value
have no production presence to migrate.

The schema-validator added alongside this swap
(`_validate_scraper_db_schema` in `serve.py`) is independent of
the model choice — it fails fast when the configured
`scraper_db_path` points at a SQLite file without the
scraper's `outbox` table, replacing the opaque ingest-time
loop errors operators were seeing during the same boot
investigation.

## Alternatives considered

- **Pin an older `fastembed` that still carries BGE-M3.**
  Rejected. Pinning to a transitively-broken version of a
  fast-moving dep leaves us off the upgrade path and trades
  one operator failure mode for another. The plan's BGE-M3
  selection rationale (multilingual + sparse) is a property
  ranking, not a contract — accepting the trade-off in the
  short term is cheaper than holding the dep tree back.
- **Switch to a different 1024-d multilingual model
  (e.g. `intfloat/multilingual-e5-large`).** Rejected for now.
  Phase 1 has no labeled data to A/B against; choosing a
  novel model on instinct adds risk without a way to measure
  the gain. The plan already lists "A/B candidates once seed
  labels accumulate: jina-v3, Qwen3-Embedding-4B" — the
  evaluation point is post-Phase-2, not now.
- **Host BGE-M3 ourselves outside the `fastembed` pipeline
  (sentence-transformers + ONNX export, or a dedicated
  inference server).** Rejected for v1. Pulls a meaningful
  amount of model-hosting infrastructure into a service whose
  current job is "embed once, store, classify" — the cost
  exceeds the win until ranking quality demonstrates a real
  multilingual or sparse-retrieval gap.
- **Drop the dimension to a smaller model
  (e.g. `bge-small-en-v1.5`, 384-d).** Rejected. Forces a
  migration of the `vec0` table and gives up retrieval
  quality without a measured need. 1024-d is the storage
  contract; staying at 1024-d keeps the swap a pure code
  change.

## Consequences

- **Positive:** `prism serve` boots on the currently-installed
  `fastembed` line. The Phase 1 exit criterion ("every new
  bookmark → envelope → embedding → traced") is now
  operator-verifiable end-to-end.
- **Positive:** factory naming (`create_default_embedder`) no
  longer encodes a specific model in the API; the next
  embedder swap is a one-line internal change.
- **Positive:** model footprint is smaller — ~1.2 GB on first
  load vs. BGE-M3's ~2 GB — which proportionally reduces cold-
  cache integration-test time and Mac Mini resident memory
  during serve.
- **Negative:** loss of multilingual coverage. Non-English
  content embeds against an English model and will retrieve
  poorly. Mitigation: bookmarks are predominantly English in
  the current corpus; multilingual quality is a measurable
  gap that re-opens this ADR when seed labels surface non-
  English content underperforming.
- **Negative:** loss of BGE-M3's sparse / multi-vector outputs.
  The plan's "hybrid dense+sparse retrieval useful for
  keyword-heavy post content" property is unrealized in v1.
  Topic-head retrieval in Phase 2 runs on dense vectors only;
  sparse retrieval is deferred until a supported model brings
  it back or a separate sparse pipeline is justified.
- **Negative:** context window drops from 8K to 512 tokens.
  Bookmark bodies that exceed 512 tokens will be truncated by
  the tokenizer. Mitigation: post bodies are well below this
  bound in practice; long-form content (articles, documents)
  is not yet a Phase 1 ingest target. Re-opens this ADR if
  long-form content becomes a primary ingest path before a
  supported long-context replacement lands.
- **Neutral:** the 1024-d storage shape is unchanged. ADR 011
  (synthetic composite key) and ADR 012 (DELETE-then-INSERT
  upsert) are unaffected.

## References

- `src/prism/embedding.py` — `FASTEMBED_MODEL_NAME`,
  `MODEL_VERSION`, `create_default_embedder`.
- `src/prism/serve.py` — lazy import of
  `create_default_embedder` inside the lifespan body
  (ADR 015) and the schema validator that complements this
  swap.
- `migrations/versions/8a27a28168d0_embeddings.py` — fixed
  1024-d `vec0` schema that constrains replacement candidates.
- ADR 011 — synthetic composite key encodes `model_version`,
  so swapping the embedder is a versioning question, not a
  schema question.
- `.project/tasks/multi-model-embedding-config.md` — deferred
  task to thread `config.pipeline.embedding.*` through the
  factory once a second model lands; this swap does not yet
  trigger it because there is still only one default.
