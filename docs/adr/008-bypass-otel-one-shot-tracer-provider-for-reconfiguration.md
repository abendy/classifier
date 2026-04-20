# ADR 008: Bypass OTel's one-shot `set_tracer_provider` for reconfiguration

## Status

Accepted

## Date

2026-04-19

## Context

OpenTelemetry's public API `trace.set_tracer_provider()` is
one-shot — the first call installs the provider, subsequent calls
silently no-op and log a warning at the OTel level. That breaks
two real needs in this repo:

1. Tests reconfigure the provider per fixture so each test gets a
   clean span history (in-memory exporter between tests, Phoenix
   OTLP in one test, disabled provider in another).
2. Any future hot-reload path that changes
   `audit.phoenix.enabled` at runtime needs to swap the
   installed provider without restarting the process.

The tracing wiring lives in `prism.tracing`. `configure_tracing`
and `install_in_memory_exporter` are both expected to produce a
usable provider regardless of what was installed previously.

## Decision

The `_install()` helper in `prism.tracing` bypasses
`set_tracer_provider` and writes
`opentelemetry.trace._TRACER_PROVIDER` directly. The previous
provider, if any, is shut down (`previous.shutdown()`) so its
`BatchSpanProcessor` worker threads exit rather than
accumulating across swaps. We accept the coupling to OTel's
private module variable as the cost of reconfigurable behavior.

## Alternatives considered

- **Env-var-only configuration with init-once.** Operator sets
  `OTEL_EXPORTER_OTLP_ENDPOINT` before startup; `configure_tracing`
  calls `set_tracer_provider` exactly once. Works for
  production but makes tests that want different providers
  per fixture impossible. Rejected.
- **Custom tracer facade that rebinds internally.** A
  prism-owned `Tracer` wrapper could swap its backing provider
  without touching OTel's global. Every span emission now goes
  through the facade; every test assertion checks facade state,
  not real OTel spans; we lose OTel's ecosystem (instrumentation
  packages, Phoenix's OTel-native path). Heavy. Rejected.
- **Subprocess-per-test isolation.** Each test runs in its own
  Python process so OTel's global starts fresh. Slow; breaks
  shared fixtures; no clear runtime benefit. Rejected.
- **Accept one-shot, configure only once in a session fixture.**
  Tests can't differentiate enabled / disabled / in-memory
  configurations. Rejected.

## Consequences

- **Positive:** tests configure per-fixture; reconfiguration
  swaps cleanly; no batch-span-worker thread leaks.
- **Positive:** bypass is encapsulated in one helper (`_install`)
  — the coupling surface is small.
- **Negative:** we depend on the private OTel module variable
  name `_TRACER_PROVIDER`. An OTel upgrade renaming or
  relocating it breaks `_install` in a way the typechecker
  can't catch (dynamic attribute set).
- **Neutral:** `previous.shutdown()` makes swap deterministic;
  without it, batch processors would continue draining in the
  background after replacement.

## Revisit when

OpenTelemetry's public API exposes a reconfiguration mechanism
(e.g., a `reset_tracer_provider()` helper or a non-one-shot
setter), or the private variable renames / relocates in an
upgrade.

## References

- `src/prism/tracing.py` — `_install`,
  `configure_tracing`, `install_in_memory_exporter`.
- `tests/test_tracing.py` —
  `test_reconfiguration_shuts_down_previous_provider` pins the
  shutdown-on-swap behavior.
