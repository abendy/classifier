"""OpenTelemetry tracing wiring for prism.

``configure_tracing(audit_config)`` installs a ``TracerProvider``
keyed off ``audit.phoenix.enabled`` — enabled attaches a
``BatchSpanProcessor`` → ``OTLPSpanExporter`` pointed at
``{local_url}/v1/traces``; disabled installs a provider with no
exporter so spans still get trace ids locally (``runs.trace_id``
populates, Phoenix sees nothing). Both variants stamp the provider
with a ``service.name="prism"`` resource so Phoenix groups spans
under the component's real identity rather than the default
``unknown_service``.

``install_in_memory_exporter()`` is the test-only variant: a
``SimpleSpanProcessor`` wrapping an ``InMemorySpanExporter`` that
tests read via ``get_finished_spans()``.

Reconfiguration replaces the installed provider silently and shuts
the previous one down so its span-processor worker threads exit
rather than accumulating across swaps. The API's
``set_tracer_provider`` is one-shot, so we bypass it and set the
module global directly — production calls ``configure_tracing``
once at startup, tests configure per-fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import opentelemetry.trace as _trace_api
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

if TYPE_CHECKING:
    from prism.config import AuditConfig

_SERVICE_NAME = "prism"


def configure_tracing(audit_config: AuditConfig) -> None:
    """Install a prism TracerProvider keyed off ``audit.phoenix.enabled``."""
    provider = TracerProvider(resource=_resource())
    if audit_config.phoenix.enabled:
        exporter = OTLPSpanExporter(
            endpoint=f"{audit_config.phoenix.local_url}/v1/traces"
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    _install(provider)


def install_in_memory_exporter() -> InMemorySpanExporter:
    """Install a test TracerProvider and return the in-memory exporter."""
    provider = TracerProvider(resource=_resource())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _install(provider)
    return exporter


def _resource() -> Resource:
    return Resource.create({"service.name": _SERVICE_NAME})


def _install(provider: TracerProvider) -> None:
    previous = _trace_api._TRACER_PROVIDER
    _trace_api._TRACER_PROVIDER = provider
    if isinstance(previous, TracerProvider):
        previous.shutdown()
