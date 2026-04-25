# prism

Multi-faceted content classification for any dada.stream content envelope.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- ~1.2 GB free disk for the default dense model cache (skippable; see below)

## Getting started

The service ships with a versioned template for runtime config; the
live `config.yaml` is per-environment and gitignored.

```bash
cp config.example.yaml config.yaml
just install                # runs `uv sync`, creating .venv/
source .venv/bin/activate   # optional — `uv run` works without it
just check                  # lint + typecheck + test
```

Deactivate with `deactivate` from any shell.

Then exercise the CLI:

```bash
prism config    # validate config.yaml and summarize
prism manifest  # validate and print service.json
prism status    # summarize the service's contract
prism schema    # export the manifest JSON Schema
prism db        # probe the storage layer (sqlite, vec, duckdb)
prism serve     # run the HTTP service (binds 127.0.0.1)
```

### Development commands

`prism dev` exposes thin smoke and iteration helpers that wrap the
same functions `prism serve` calls. They are useful for manual
verification without booting uvicorn.

| Command | Purpose |
| --- | --- |
| `prism dev seed-scraper [--path P] [--rows N] [--force]` | Write the scraper's four-table DDL plus N synthetic bookmark rows. |
| `prism dev ingest-once [--no-embed]` | Run one `ingest_once` pass against the configured DBs and print stats. |
| `prism dev embed "<text>"` | Boot the default embedder, embed once, print model + first 8 vector components. |
| `prism dev load-topics [--catalog P]` | Embed the topic catalog (`topics.yaml`) into `topic_prototypes`. Run after editing the catalog. |
| `prism dev retrieve-topics <envelope-id> [--top-k N]` | Retrieve top-k topic candidates for an envelope's stored embedding. |
| `prism dev pick-topic <envelope-id> [--top-k N]` | Retrieve candidates and run the LLM pick. Requires a running Ollama daemon. |

Typical first-run loop on a fresh checkout:

```bash
uv run prism dev seed-scraper --rows 3
uv run prism dev ingest-once
```

### Topic catalog

The classifier reads its topic catalog from
`pipeline.topic.catalog_path` (default `topics.yaml`). On a fresh
checkout copy the example file:

```bash
cp topics.example.yaml topics.yaml
uv run prism dev load-topics
```

The example ships with three topics so the loader can be exercised
end-to-end before curation begins.

### LLM backend (Ollama)

`prism dev pick-topic` and downstream classification depend on a
local [Ollama](https://ollama.com) daemon. Install it once, then
pull the configured model:

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
ollama serve  # leave running in another terminal
```

Override `pipeline.topic.ollama_url` or `pipeline.topic.ollama_model`
in `config.yaml` to point at a different daemon or use a different
model.

Edit `config.yaml` to match your environment — the scraper DB path,
model choices, and thresholds are the values most likely to need
tuning. If you change a value that belongs to every environment,
update `config.example.yaml` and commit that — never commit
`config.yaml` directly.

## Running the service

`prism serve` starts the HTTP API and a background ingest loop
that polls the scraper outbox every `ingest.poll_interval_ms`.
Each pass saves or refreshes envelopes, embeds their bodies, and
records per-envelope runs in `runs` for Phoenix.

### Default dense model weights

With `ingest.embedding_enabled: true` (the default), the first
`prism serve` boot downloads ~1.2 GB of
`BAAI/bge-large-en-v1.5` ONNX weights via fastembed and caches
them on disk; later boots reuse the cache. The download path is
printed on first run.

To pre-warm the cache before first serve or before running the
integration suite:

```bash
uv run python -c "from prism.embedding import create_default_embedder; create_default_embedder()"
```

To skip both the download and the embed phase (e.g., for smoke
runs), set `ingest.embedding_enabled: false` in `config.yaml`.

### Integration tests

`pyproject.toml` deselects the integration suite by default
(`addopts = -m 'not integration'`). Run it explicitly once the
model cache is warm:

```bash
uv run pytest tests/test_serve_integration.py -m integration
```

### Phoenix (optional trace viewer)

Set `audit.phoenix.enabled: true` in `config.yaml`, then run
Phoenix locally:

```bash
docker run -p 6006:6006 arizephoenix/phoenix:latest
```

Open <http://localhost:6006> to see ingest and per-envelope
embed spans threaded by `correlationId` (stable envelope id)
and `causationId` (parent run). Set `enabled: false` to skip —
`runs` rows still populate; only the Phoenix export is suppressed.
