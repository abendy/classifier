# ADR 002: Validate Pydantic models with extra="forbid" and strict=True

## Status

Accepted

## Date

2026-04-19

## Context

The dada.stream service manifest is the classifier's first
wire-format artifact. Its JSON Schema is published in `contracts/`
and consumed by other services. Drift between the Python model and
the schema — silent extra fields, lenient type coercion — would make
the contract a lie.

Pydantic v2 defaults are permissive: unknown fields are ignored,
numeric strings coerce into numbers, aliased keys and snake_case keys
both work. We need the opposite: one wire format, one shape, fail
loudly on anything else.

## Decision

Every Pydantic model inherits from a shared `_Base` that sets
`model_config = ConfigDict(alias_generator=to_camel,
extra="forbid", strict=True)`. `populate_by_name` is **not** set —
the wire format accepts only the aliased (camelCase) key, never the
Python snake_case attribute name. Unknown fields fail validation at
every nesting level.

## Alternatives considered

- **Permissive mode (`extra="allow"` or the default).** Would let
  silently-drifting contracts slip through — the contract would
  degrade one unknown field at a time. Rejected.
- **`populate_by_name=True`.** Would accept both the alias and the
  Python attribute name. Rejected: the wire format is one format,
  not two; accepting both blurs which one is canonical.
- **Plain `@dataclass` without Pydantic.** Cheaper at runtime but
  provides no JSON parsing, no `extra="forbid"` equivalent, no
  automatic JSON Schema export. Rejected.
- **`strict=False` (default permissive coercion).** Would let
  `"3200"` validate as `3200` and obscure real contract bugs at the
  type level. Rejected; per-field carve-outs handle the narrow
  cases where coercion is legitimately needed.

## Consequences

- **Positive:** unknown-field drift is caught at ingest; the wire
  format is one unambiguous shape; JSON Schema export stays clean
  and matches the Python model.
- **Negative:** per-field relaxation is occasionally needed where
  strict coercion is too tight (JSON numbers for int|float ports,
  datetime strings, etc.). A follow-up ADR will cover that pattern.
- **Neutral:** every Pydantic model inherits `_Base`; the
  boilerplate is centralized in one place.

## Revisit when

Pydantic v3 lands or we switch validators.

## References

- `src/classifier/manifest.py` — `_Base`; all manifest models
  inherit from it.
- `tests/test_manifest.py` — `test_unknown_field_at_root_is_rejected`,
  `test_unknown_field_in_nested_model_is_rejected`,
  `test_wrong_type_for_idempotent_is_rejected`,
  `test_snake_case_async_alias_is_rejected` — the rejection tests
  exercise the discipline directly.
