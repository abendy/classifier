"""Structured JSON log lines to stderr."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any


def log_event(event: str, **fields: Any) -> None:
    """Emit one JSON line to stderr with ``ts``, ``event``, and ``fields``."""
    payload: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, default=str, sort_keys=True)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
