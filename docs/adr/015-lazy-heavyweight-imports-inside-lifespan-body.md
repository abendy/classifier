# ADR 015: Import heavyweight lifespan-owned subsystems lazily inside the lifespan body

## Status

Accepted

## Date

2026-04-20

## Context

The FastAPI lifespan in `src/prism/serve.py` owns resources
that are expensive to *import*, not just to construct. The
first case is `create_bge_m3_embedder`, which transitively
imports `fastembed`, which transitively imports
`onnxruntime` — a C-extension that compiles shared libraries
and reads model metadata on import. Cost is tens to hundreds
of milliseconds plus a non-trivial resident memory bump,
paid once per interpreter.

`prism.api` attaches the lifespan to the FastAPI app, so
`import prism.api` transitively imports `prism.serve`. A
naive `from prism.embedding import create_bge_m3_embedder`
at `serve.py`'s top would land `fastembed` in the import
graph of every test that does `from prism.api import app`
for `TestClient` probes, every CLI check, every schema
introspection script — costs that have nothing to do with
the embedder being constructed.

Phase 2 will add more lifespan-owned heavyweight subsystems
(LLM clients, vector indexes, classify workers). The same
import-cost trap will recur unless the rule is pinned.

## Decision

Heavyweight imports whose use is confined to the lifespan
function body are imported *inside* that function body, not
at module top. The runtime cost is paid once per serve boot
(where it belongs), not on every test collection or
unrelated import. Type annotations on the module may still
reference these symbols via `TYPE_CHECKING`.

## Alternatives considered

- **Top-level import.** Pays the cost on every import of
  `prism.api` or `prism.serve`, including tests and CLI
  probes that never enter the lifespan. Rejected — this is
  the trap being avoided.
- **Separate module imported via `importlib.import_module` at
  runtime.** Adds indirection for no benefit — the lazy
  in-body idiom is a one-line change that expresses the same
  thing with less machinery. Rejected.
- **Env-var gate on a top-level import
  (`if os.getenv("PRISM_SKIP_HEAVY_IMPORTS"): ...`).** Leaks
  runtime configuration into import-time behavior; every test
  environment now needs the var; default path stays expensive.
  Rejected.

## Consequences

- **Positive:** `import prism.api` stays cheap. Tests and
  operator probes don't pay the fastembed import cost; serve
  boot pays it exactly once per process.
- **Positive:** the rule generalizes — Phase 2's LLM client,
  vector index, and classify-worker imports follow the same
  pattern, so the import graph of `prism.api` stays shallow
  regardless of how many subsystems the lifespan owns.
- **Negative:** lazy imports inside function bodies are
  slightly less idiomatic than top-level imports; linters may
  eventually nag (none do today). A one-line
  `from ... import ...` inside the lifespan body is explicit
  enough that this cost is trivial.
- **Neutral:** only runtime imports move into the body.
  `TYPE_CHECKING` imports for type annotations stay at top
  because they carry no import-time cost.

## References

- `src/prism/serve.py` — `lifespan` function with the
  `create_bge_m3_embedder` import inside its body.
- `src/prism/api.py` — attaches `lifespan` to the FastAPI
  app; the entry point whose import cost this ADR protects.
- ADR 008 — bypass OTel one-shot `set_tracer_provider`;
  paired on "lifespan owns orchestration state."
