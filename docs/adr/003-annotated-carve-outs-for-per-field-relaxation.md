# ADR 003: Relax strict validation per field with `Annotated[...]` carve-outs

## Status

Accepted

## Date

2026-04-19

## Context

ADR 002 committed every Pydantic model to `strict=True`. That
posture is the right default — but in practice a handful of fields
need looser behavior without softening the whole model:

- `datetime` fields legitimately arrive as ISO-8601 strings when a
  model is validated from a JSON-parsed `dict`.
- `Path` fields loaded from YAML come through as strings.
- Required ID fields need a "non-empty string" constraint that
  strict mode doesn't express on its own.
- The service manifest's `port`/`interval` fields are typed
  `int | float` in Python but must export as JSON Schema `"number"`
  so the JSON Schema matches the dada.stream spec — even though a
  float port is semantically nonsensical.

At this commit the envelope model adds two more carve-outs
(`_DateTime`, `_NonEmptyStr`) alongside the two already in manifest
and config (`ManifestNumber`, `_Path`). The pattern is now
unambiguously cross-cutting; it earns an ADR.

## Decision

When strict mode is too tight for a specific field, relax via an
`Annotated` alias rather than softening `model_config`. Two
sub-patterns are in use:

- **Type relaxation** — `Annotated[T, Field(strict=False)]` accepts
  coerced values on the dict-validation path. Used for datetime
  strings and filesystem paths loaded from YAML.
- **Schema type override** — `Annotated[T, WithJsonSchema({...})]`
  keeps the Python type honest while pinning what the exported
  JSON Schema declares. Used for `ManifestNumber`, where the Python
  type is `int | float` but the schema must say `"number"`.

The aliases live alongside the models that use them and are
module-private (`_Path`, `_DateTime`, `_NonEmptyStr`) unless they
need cross-module reuse (`ManifestNumber`).

## Alternatives considered

- **Drop `strict=True` globally.** Loses strict typing on every
  field to fix a handful. Rejected.
- **Override `model_config` per model.** Larger blast radius;
  harder to audit which fields are strict and which aren't.
- **Use Pydantic's opinionated validators (`FilePath`, `HttpUrl`).**
  `FilePath` checks the path exists at validate time — a side
  effect the loader does not want. Rejected.

## Consequences

- **Positive:** strict-at-model-level without fighting legitimate
  coercion; the exported JSON Schema stays aligned with the
  dada.stream spec.
- **Negative:** the carve-out aliases look esoteric to a first-time
  reader. This ADR carries the justification.
- **Neutral:** each carve-out is a one-line `Annotated` alias, so
  the surface stays small even if more accrue.

## Revisit when

Pydantic offers a more ergonomic way to express
"strict-with-named-exceptions."

## References

- `src/prism/manifest.py` — `ManifestNumber` (schema override).
- `src/prism/config.py` — `_Path` (type relaxation).
- `src/prism/envelope.py` — `_DateTime`, `_NonEmptyStr` (type
  relaxation; string constraint).
- ADR 002 — the strict-mode baseline this ADR relaxes per field.
