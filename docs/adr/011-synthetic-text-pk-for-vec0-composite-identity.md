# ADR 011: Encode composite identity as a synthetic TEXT PK for `vec0` tables

## Status

Accepted

## Date

2026-04-20

## Context

sqlite-vec's `vec0` virtual table accepts exactly **one** PRIMARY
KEY column. That constraint collides with how the plan wants
embeddings tracked: keyed by `(envelope_id, model_version)` so
A/B models coexist per envelope without overwriting each other
when a new model version re-embeds the same content.

Auxiliary columns (prefixed `+`) are stored but don't participate
in KNN indexing — they carry metadata without inflating the
index.

Plan reference: "Embedding" section — "A/B candidates once seed
labels accumulate" (jina-v3, Qwen3-Embedding-4B); "every run
record captures `embedding_model_version`."

The `embeddings` table's PK is a synthetic TEXT column named
`id`, composed as `envelope_id` + ASCII 0x1F (unit separator) +
`model_version`. The component parts are stored as auxiliary
columns (`+envelope_id`, `+model_version`, `+created_at`) so
callers can filter by either component without parsing the
synthetic key. `EmbeddingRepo.save` / `get` / `exists` all take
the `(envelope_id, model_version)` pair; the synthetic encoding
is an implementation detail.

`save` rejects inputs containing 0x1F in either component with
`ValueError`. The delimiter choice and the input validation are
paired defenses: 0x1F is a control character that UUID v7
envelope ids and `vendor/model@version`-shaped model strings
never carry; the validation is belt-and-suspenders against a
future input that somehow does.

## Alternatives considered

- **Single-column `envelope_id` PK, overwrite on re-embed.**
  The slice prompt's original shape. Loses A/B coexistence —
  re-embedding under a new model version erases the prior row.
  Rejected against plan intent.
- **One vec0 table per model version
  (`embeddings_bge_m3`, `embeddings_jina_v3`, …).** Forces a
  new migration on every model addition and spreads reads
  across `UNION ALL` queries. Rejected.
- **Stop using vec0; use a regular SQLite table with a BLOB
  column and do KNN manually.** Gives up sqlite-vec's indexed
  KNN. Rejected.
- **Use vec0 with `envelope_id` PK + a separate non-vec table
  tracking `(envelope_id, model_version)` history.** Every
  query becomes a join; complicates the repo and muddles which
  row the KNN index actually points at. Rejected.

## Consequences

- **Positive:** multiple model versions coexist per envelope
  without migration pressure.
- **Positive:** auxiliary columns expose both components for
  filter queries; the synthetic key stays opaque to callers.
- **Negative:** the synthetic key is opaque — a reader of the
  raw SQLite file sees `"<envelope_id>\x1f<model_version>"` as
  one column rather than two. Mitigated by auxiliary columns
  that expose both parts directly. Queries reference the aux
  columns; the synthetic key stays an implementation detail.
- **Neutral:** save/get/exists take two args instead of one —
  a small API-surface cost in exchange for the A/B capability.

## Revisit when

sqlite-vec grows native support for composite primary keys on
vec0, at which point the synthetic key (and its validation) can
collapse into a two-column PK and the auxiliary columns stop
being needed.

## References

- `migrations/versions/8a27a28168d0_embeddings.py` — vec0
  table definition with synthetic `id` + auxiliary columns.
- `src/prism/embedding_repo.py` — `_row_key` helper that
  encodes the pair, and the `EmbeddingRepo` methods that take
  `(envelope_id, model_version)`.
- ADR 012 — upsert idiom for the same vec0 table; the two
  ADRs are paired bridge-internal constraints of working
  within sqlite-vec's virtual-table shape.
