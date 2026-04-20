"""Tests for the configure_tracing + install_in_memory_exporter wiring."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from prism.config import AuditConfig, PhoenixConfig
from prism.tracing import configure_tracing, install_in_memory_exporter


def _audit_config(*, enabled: bool, local_url: str = "http://localhost:6006") -> AuditConfig:
    return AuditConfig(
        phoenix=PhoenixConfig(enabled=enabled, local_url=local_url),
        runs_retention_days=90,
    )


def _active_provider() -> TracerProvider:
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    return provider


def _exporters(provider: TracerProvider) -> list[object]:
    return [
        p.span_exporter
        for p in provider._active_span_processor._span_processors
        if isinstance(p, BatchSpanProcessor | SimpleSpanProcessor)
    ]


@pytest.fixture(autouse=True)
def _reset_between_tests() -> None:
    """Each test starts with a clean provider so prior-test state doesn't leak."""
    install_in_memory_exporter()


def test_configure_tracing_enabled_installs_otlp_exporter() -> None:
    configure_tracing(_audit_config(enabled=True))
    provider = _active_provider()
    batch_otlp = [
        p
        for p in provider._active_span_processor._span_processors
        if isinstance(p, BatchSpanProcessor) and isinstance(p.span_exporter, OTLPSpanExporter)
    ]
    assert len(batch_otlp) == 1


def test_configure_tracing_disabled_has_no_otlp_exporter_but_still_emits_trace_ids() -> None:
    configure_tracing(_audit_config(enabled=False))
    provider = _active_provider()
    assert not any(isinstance(e, OTLPSpanExporter) for e in _exporters(provider))

    tracer = trace.get_tracer("prism")
    with tracer.start_as_current_span("sanity") as span:
        ctx = span.get_span_context()
        assert ctx.is_valid
        assert ctx.trace_id != 0


def test_install_in_memory_exporter_returns_usable_exporter() -> None:
    exporter = install_in_memory_exporter()
    assert isinstance(exporter, InMemorySpanExporter)

    tracer = trace.get_tracer("prism")
    with tracer.start_as_current_span("x"):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "x"


def test_install_in_memory_exporter_sets_global_provider() -> None:
    install_in_memory_exporter()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_configure_tracing_builds_otlp_endpoint_from_local_url() -> None:
    configure_tracing(_audit_config(enabled=True, local_url="http://phoenix.example:9999"))
    provider = _active_provider()
    otlp = [e for e in _exporters(provider) if isinstance(e, OTLPSpanExporter)]
    assert len(otlp) == 1
    assert otlp[0]._endpoint == "http://phoenix.example:9999/v1/traces"


def test_reconfiguration_swaps_the_provider() -> None:
    install_in_memory_exporter()
    first = trace.get_tracer_provider()
    configure_tracing(_audit_config(enabled=False))
    second = trace.get_tracer_provider()
    assert first is not second


def test_configure_tracing_stamps_service_name_resource() -> None:
    configure_tracing(_audit_config(enabled=False))
    provider = _active_provider()
    assert provider.resource.attributes.get("service.name") == "prism"


def test_install_in_memory_exporter_stamps_service_name_resource() -> None:
    install_in_memory_exporter()
    provider = _active_provider()
    assert provider.resource.attributes.get("service.name") == "prism"


def test_reconfiguration_shuts_down_previous_provider() -> None:
    install_in_memory_exporter()
    previous = _active_provider()
    # TracerProvider registers an atexit handler in __init__ and clears it
    # in shutdown(); we use that as the observable signal that _install()
    # shut the old provider down (and, with it, its batch-span worker
    # thread) rather than leaving it to accumulate.
    assert previous._atexit_handler is not None
    configure_tracing(_audit_config(enabled=False))
    assert previous._atexit_handler is None
