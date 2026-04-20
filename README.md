# prism

Multi-faceted content classification for any dada.stream content envelope.

## Getting started

The service ships with a versioned template for runtime config; the
live `config.yaml` is per-environment and gitignored.

```bash
cp config.example.yaml config.yaml
just install
just check
```

Then exercise the CLI:

```bash
uv run prism config    # validate config.yaml and summarize
uv run prism manifest  # validate and print service.json
uv run prism status    # summarize the service's contract
uv run prism schema    # export the manifest JSON Schema
uv run prism db        # probe the storage layer (sqlite, vec, duckdb)
uv run prism serve     # run the HTTP service (binds 127.0.0.1)
```

Edit `config.yaml` to match your environment — the scraper DB path,
model choices, and thresholds are the values most likely to need
tuning. If you change a value that belongs to every environment,
update `config.example.yaml` and commit that — never commit
`config.yaml` directly.

## Running the service

`prism serve` starts the HTTP API and a background ingest loop
that polls the scraper outbox every `ingest.poll_interval_ms`.
Each pass saves or refreshes envelopes, embeds their bodies, and
records per-envelope runs in `runs` for Phoenix. Set
`ingest.embedding_enabled: false` to skip embedding (and the
~2 GB BGE-M3 model download) for smoke runs.

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
