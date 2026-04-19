# dada.stream contract integration

The Pydantic models in `src/classifier/manifest.py` are the canonical
local representation of the service manifest, and
`contracts/service-manifest.schema.json` is the machine-readable
JSON Schema export — regenerate it via `uv run classifier schema >
contracts/service-manifest.schema.json` whenever the models change.

## Specs this service conforms to

- `~/projects/dada.stream/platform/contracts/service-manifest.md` —
  manifest shape every dada.stream service publishes; modeled by
  `ServiceManifest` and loaded from `./service.json` at startup.
- `~/projects/dada.stream/platform/contracts/event-types.md` —
  event envelope and pipeline event payloads; modeled in a later
  slice when the service emits and consumes events.
- `~/projects/dada.stream/platform/contracts/api-conventions.md` —
  HTTP conventions for service endpoints; surfaces when the HTTP
  skeleton lands in slice 5.
- `~/projects/dada.stream/platform/domains/content-model.md` —
  content envelope shape this service classifies; modeled with the
  envelope mapper in a later phase.
