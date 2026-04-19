# dada.stream contract integration

The Pydantic models in `src/prism/manifest.py` are the canonical
local representation of the service manifest, and
`contracts/service-manifest.schema.json` is the machine-readable
JSON Schema export — regenerate it via `uv run prism schema >
contracts/service-manifest.schema.json` whenever the models change.

## Specs this service conforms to

- `~/projects/dada.stream/platform/contracts/service-manifest.md` —
  manifest shape every dada.stream service publishes; modeled by
  `ServiceManifest` and loaded from `./service.json` at startup.
- `~/projects/dada.stream/platform/contracts/event-types.md` —
  event envelope and pipeline event payloads; modeled when the
  service emits and consumes events.
- `~/projects/dada.stream/platform/contracts/api-conventions.md` —
  HTTP conventions for service endpoints; surfaces via the HTTP
  skeleton in `src/prism/api.py`.
- `~/projects/dada.stream/platform/domains/content-model.md` —
  content envelope shape this service ingests and classifies;
  modeled by `ContentEnvelope` in `src/prism/envelope.py`;
  JSON Schema export at `contracts/content-envelope.schema.json`.
