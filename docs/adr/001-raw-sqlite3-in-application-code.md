# ADR 001: Use raw sqlite3 in application code; confine SQLAlchemy to migrations

## Status

Accepted

## Date

2026-04-19

## Context

The storage slice wires SQLite (with `sqlite-vec`), DuckDB (attach-only,
read path), and Alembic. Alembic pulls in SQLAlchemy transitively, so
SQLAlchemy is already installed. The question is whether to use it in
application code (repos, CLI, API) or keep it behind the migration
boundary.

At this point in the project the query surface is tiny — a handful of
simple reads and writes per repo. Picking an abstraction before the
query patterns are visible risks locking in a shape that doesn't fit.

## Decision

Application code uses raw `sqlite3` with parameterized queries.
SQLAlchemy is imported only inside `migrations/env.py` and files under
`migrations/versions/` (for `engine_from_config`, future
`op.create_table`, `sa.Column`, `sa.text`). No SQLAlchemy sessions, no
ORM models, no `declarative_base` in application code.

## Alternatives considered

- **SQLAlchemy ORM across the app.** Ergonomic, but premature — locks
  in a particular abstraction before we know what shape queries take.
  Rejected for v1.
- **SQLAlchemy Core (`Table()` + `select()`).** Less opinionated than
  the ORM, but still requires schema declarations in two places (the
  migration and Core metadata). Rejected; adds weight without clear
  payoff at this scale.
- **A custom query-builder layer.** Over-engineered for the current
  query surface. Rejected.

## Consequences

- **Positive:** minimal abstraction, easy to reason about, fast. The
  repo layer stays transparent — a reader sees the SQL that actually
  executes.
- **Negative:** every repo implements its own cursor hygiene and
  transaction wrapping. The pattern is consistent but not enforced by
  types.
- **Neutral:** SQLAlchemy stays installed (transitively via Alembic).
  A single repo can adopt SQLAlchemy internally in the future without
  disturbing the rest of the app.

## Revisit when

A repo's query logic grows beyond ~10 hand-written SQL strings, or
when a JOIN across 3+ tables becomes routine.

## References

- `src/classifier/db.py` — `connect_sqlite`, `attach_duckdb`,
  `migration_head` all use raw `sqlite3` / `duckdb`.
- `migrations/env.py` — the only place `sqlalchemy` is imported.
- `migrations/versions/fd63ca4f8371_baseline.py` — baseline revision;
  establishes the migration chain without touching application code.
