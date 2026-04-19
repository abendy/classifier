# classifier

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
uv run classifier config    # validate config.yaml and summarize
uv run classifier manifest  # validate and print service.json
uv run classifier status    # summarize the service's contract
uv run classifier schema    # export the manifest JSON Schema
uv run classifier db        # probe the storage layer (sqlite, vec, duckdb)
uv run classifier serve     # run the HTTP service (binds 127.0.0.1)
```

Edit `config.yaml` to match your environment — the scraper DB path,
model choices, and thresholds are the values most likely to need
tuning. If you change a value that belongs to every environment,
update `config.example.yaml` and commit that — never commit
`config.yaml` directly.
