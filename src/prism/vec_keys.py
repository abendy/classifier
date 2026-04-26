"""Composite-key encoding for sqlite-vec storage tables (ADR 011)."""

from __future__ import annotations

ROW_KEY_DELIMITER = "\x1f"


def encode(*parts: str) -> str:
    for part in parts:
        if ROW_KEY_DELIMITER in part:
            raise ValueError(f"row-key part contains delimiter: {part!r}")
    return ROW_KEY_DELIMITER.join(parts)
