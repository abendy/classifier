# Architecture Decision Records

Each file in this directory records a single architectural decision:
what was decided, what was considered, and why. Decisions stay in the
historical record when superseded — the Status field points forward to
the replacement, but the body of the old ADR is not rewritten.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| 001 | Use raw sqlite3 in application code; confine SQLAlchemy to migrations | Accepted | 2026-04-19 |
| 002 | Validate Pydantic models with extra="forbid" and strict=True | Accepted | 2026-04-19 |
| 003 | Relax strict validation per field with `Annotated[...]` carve-outs | Accepted | 2026-04-19 |
| 004 | Extract only entityType and entityId from the scraper's outbox payload | Accepted | 2026-04-19 |
| 005 | Advance the outbox polling cursor independently of parse success | Accepted | 2026-04-19 |
| 006 | The scraper-classifier bridge — scope, contract, retirement | Accepted | 2026-04-19 |
| 007 | Envelope refresh preserves identity.id and system.created_at | Accepted | 2026-04-19 |
| 008 | Bypass OTel's one-shot set_tracer_provider for reconfiguration | Accepted | 2026-04-19 |
| 009 | Terminal log events fire after the operation's span closes, with trace_id threaded explicitly | Accepted | 2026-04-19 |
| 010 | Two-layer exception handling with identity-based deduplication in `operation()` | Accepted | 2026-04-19 |
| 011 | Encode composite identity as a synthetic TEXT PK for vec0 tables | Accepted | 2026-04-20 |
| 012 | DELETE-then-INSERT as upsert for vec0 virtual tables | Accepted | 2026-04-20 |
| 013 | Thread both correlation_id and causation_id on per-envelope operation spans | Accepted | 2026-04-20 |
| 014 | Mutating repo methods return the canonical post-mutation state | Accepted | 2026-04-20 |
| 015 | Import heavyweight lifespan-owned subsystems lazily inside the lifespan body | Accepted | 2026-04-20 |
| 016 | Serve-time long-lived loops own no span | Accepted | 2026-04-20 |
