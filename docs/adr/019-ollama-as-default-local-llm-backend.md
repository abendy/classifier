# ADR 019: Ollama as the default local LLM backend

## Status

Accepted

## Date

2026-04-25

## Context

The topic-head pick step (and every later facet head — subtopic,
tags, sentiment, intent) calls a small local LLM with the
envelope text and structured-output constraints, returning a
schema-validated JSON object. The plan (§Topic head — retrieval
+ LLM-pick) names *"small local LLM"* without pinning a runtime;
§Open decisions explicitly leaves the model choice (Qwen 2.5 7B
vs. Llama 3.1 8B vs. Gemma 2 9B) for after Phase 1 benchmarks.

The runtime question is independent of the model question: how
does Python talk to a locally-hosted LLM at all? Several
mature options exist on the Mini's target host (macOS, Apple
Silicon):

- **Ollama.** A daemon that wraps llama.cpp, exposes a stable
  HTTP API (`/api/chat`, `/api/generate`), and ships a
  package-manager-style model registry (`ollama pull
  qwen2.5:7b-instruct-q4_K_M`). JSON-mode constrained
  generation via the `format` request parameter (since
  Ollama 0.5).
- **llama-cpp-python.** Direct Python bindings to llama.cpp.
  In-process, no daemon, no HTTP. JSON-mode via grammar
  files or constrained-decoding hooks.
- **MLX (Apple-native).** Apple Silicon-optimized framework;
  fastest path on the Mini, but the structured-output
  story is younger.
- **vLLM.** GPU-first server runtime, mature constrained
  decoding, but CUDA-oriented and not a Mac-friendly path.
- **OpenAI-compatible APIs.** Any of the above plus a wrapper
  (LM Studio, llama-server, Ollama's openai-compat endpoint).
  Adds a layer; only useful when the consumer already speaks
  OpenAI.

The classifier needs structured output, deterministic
generation (`temperature=0.0` for eval reproducibility), and
operational simplicity on the always-on Mini. Process
isolation between the model runtime and the Python service is
also valuable: a hung or crashed inference doesn't take the
classifier with it.

## Decision

The default local LLM backend is **Ollama**. The classifier
talks to it via `httpx` POSTs to `/api/chat` with `format=<json
schema>` for JSON-mode constrained generation, validates the
returned content against a Pydantic model, and treats schema
violations as a recoverable per-envelope outcome (covered in a
separate ADR alongside ingest integration).

`pipeline.topic.ollama_url` and `pipeline.topic.ollama_model`
in `config.yaml` parameterize the daemon endpoint and the
model tag. Defaults: `http://localhost:11434` and
`qwen2.5:7b-instruct-q4_K_M`.

The `LlmClient` Protocol in `prism.llm_client` is the
structural injection seam for facet heads. `OllamaClient` is
the only concrete implementation today; alternative backends
can be added without touching the heads when measurement or
operational pressure justifies them.

## Alternatives considered

- **`llama-cpp-python` (in-process).** Eliminates the daemon
  and the HTTP hop, gaining a small latency win on each call.
  Rejected because:
  1. Loading a 7B-param model into the classifier process
     ties model lifecycle to service lifecycle. A model swap
     means a service restart.
  2. JSON-mode via llama.cpp grammars is more fragile than
     Ollama's `format` parameter — grammar files for arbitrary
     Pydantic schemas need conversion code that doesn't exist.
  3. Process crashes in inference take the classifier down
     with them.
- **MLX with a custom HTTP wrapper.** Apple Silicon native
  inference is the fastest path on the Mini, with measurable
  wins on per-call latency. Rejected for now because
  structured-output tooling around MLX is younger (no equivalent
  of Ollama's `format` parameter as of this ADR's date), and
  building a wrapper duplicates Ollama's daemon role for
  uncertain benefit. Worth revisiting when MLX's structured
  output matures or when production data shows Ollama's
  per-call cost dominating.
- **vLLM.** GPU-first runtime with mature constrained decoding
  (Outlines integration, JSON schemas via `guided_json`).
  Rejected because the Mini is the target host and vLLM's
  Mac/Apple-Silicon story is not the primary supported path;
  the operational complexity (CUDA, batching, request queuing)
  exceeds the current need (one envelope per pick, no
  concurrent demand).
- **OpenAI-compatible API wrapper around any of the above.**
  Adds a layer to translate OpenAI's request/response shape
  to whatever the underlying runtime expects. Useful when a
  consumer is already an OpenAI SDK; the classifier is not.
  Rejected — direct calls to the chosen runtime's native API
  are simpler.
- **Cloud LLM (Anthropic, OpenAI, etc.).** Out of scope for
  v1: the plan explicitly targets local inference on the
  Mini's hot path. Cloud options are appropriate for batch
  jobs (eval, backfill) that exceed the Mini's overnight
  budget; that's a separate decision tracked in the plan's
  Phase 4–5 sections.

## Consequences

- **Positive:** operational shape is "Ollama daemon runs as a
  user service; classifier talks to it over HTTP." Mature,
  documented, restartable independently of the classifier.
  `ollama pull` is the model-management story; no Python-side
  code paths for model download, format conversion, or weight
  caching.
- **Positive:** `format=<json schema>` is a first-class
  request parameter. The `TopicPick.model_json_schema()`
  output drops in unchanged; no grammar-file conversion, no
  Outlines/Instructor dependency.
- **Positive:** the `LlmClient` Protocol gives every facet
  head an injection seam. Tests stub a fake; production
  injects `OllamaClient`. When a second backend arrives, the
  consumers don't change.
- **Positive:** process isolation. A hung or crashed model
  inference does not take the classifier service down. The
  classifier's run record marks the affected envelope as
  failed/low-confidence and continues.
- **Negative:** an external daemon dependency. Operators need
  Ollama installed and running before `prism dev pick-topic`
  or downstream classification works. The README documents the
  `ollama pull` + `ollama serve` setup; the FastAPI lifespan
  does not validate Ollama reachability at boot (deferred to
  ingest-integration scope).
- **Negative:** HTTP round-trip cost per call (~1ms on
  localhost, more if the daemon is remote). For trickle ingest
  this is invisible; for backfill or bulk-reembed at scale a
  persistent `httpx.Client` connection pool addresses it
  (separate ingest-integration concern).
- **Negative:** `format=<schema>` is a generation hint, not a
  guarantee. Models can return JSON that violates the schema.
  The classifier validates the response with Pydantic and
  treats violations as a recoverable per-envelope outcome
  (separate ADR pinning the failure-mode taxonomy lands with
  ingest integration).
- **Neutral:** the small-LLM model choice (Qwen 2.5 7B,
  Llama 3.1 8B, Gemma 2 9B per the plan's Open decisions)
  remains independent of this ADR. Any of those is one
  `ollama pull` + config edit away.

## References

- `./src/prism/llm_client.py` — `LlmClient` Protocol +
  `OllamaClient` concrete implementation.
- `./src/prism/topic_llm_pick.py` — first consumer of the
  Protocol; pattern facet heads will reuse.
- `./src/prism/config.py` — `pipeline.topic.ollama_url` and
  `pipeline.topic.ollama_model` defaults.
- `./README.md` § "LLM backend (Ollama)" — operator setup.
- `./.project/plans/2026-04-18-classification-service-foundation.md`
  § "Topic head — retrieval + LLM-pick"; § "Open decisions"
  (small-LLM model choice unresolved, independent of this ADR).
- ADR 015 — lazy heavyweight imports; `httpx` is imported
  inside `OllamaClient.pick`, not at module top.
- ADR 017 — `MODEL_VERSION` for embeddings; the LLM backend
  decision is orthogonal but follows the same "config-driven,
  default to a sensible local choice" pattern.
