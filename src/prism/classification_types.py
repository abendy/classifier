"""Cross-layer type aliases for classification.

Owns the LowConfidenceReason vocabulary because both the
LLM-pick layer (which emits values) and the storage layer
(which persists them) read the alias. Keeping it in either
of those modules creates a layer-direction problem.
"""

from __future__ import annotations

from typing import Literal

LowConfidenceReason = Literal[
    "below-threshold",
    "out-of-band",
    "llm-pick-unsure",
    "llm-output-malformed",
]
