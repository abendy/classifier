"""Integration test exercising fastembed's real BGE-M3 model."""

from __future__ import annotations

import numpy as np
import pytest

from prism.embedding import (
    EMBEDDING_DIMENSION,
    MODEL_VERSION,
    create_bge_m3_embedder,
)


@pytest.mark.integration
def test_real_bge_m3_embedder_produces_expected_shape() -> None:
    """Downloads BGE-M3 on first run (~2 GB). Asserts only shape
    and dtype — the vectors themselves are model-dependent and
    not pinned here; that's a separate eval concern."""
    embedder = create_bge_m3_embedder()
    vec = embedder.embed("the quick brown fox")
    assert vec.shape == (EMBEDDING_DIMENSION,)
    assert vec.dtype == np.float32
    assert MODEL_VERSION == "bge-m3@1.5"
