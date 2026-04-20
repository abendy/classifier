"""Structured JSON log lines to stderr."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace


def log_event(event: str, **fields: Any) -> None:
    """Emit one JSON line to stderr with ``ts``, ``event``, and ``fields``.

    Auto-injects ``service="prism"`` and — when called inside an
    active OTel span — ``trace_id`` / ``span_id`` so every log line
    in a trace context is cross-referenceable. Caller kwargs win
    over defaults.
    """
    payload: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "service": "prism",
    }
    span = trace.get_current_span()
    span_ctx = span.get_span_context()
    if span.is_recording() and span_ctx.is_valid:
        payload["trace_id"] = format(span_ctx.trace_id, "032x")
        payload["span_id"] = format(span_ctx.span_id, "016x")
    payload.update(fields)
    line = json.dumps(payload, default=str, sort_keys=True)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
