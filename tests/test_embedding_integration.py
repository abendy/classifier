"""Integration test exercising fastembed's real default dense model."""

from __future__ import annotations

import numpy as np
import pytest

from prism.embedding import (
    EMBEDDING_DIMENSION,
    MODEL_VERSION,
    create_default_embedder,
)


@pytest.mark.integration
def test_real_default_embedder_produces_expected_shape() -> None:
    """Downloads the default dense model on first run (~1.2 GB).
    Asserts only shape
    and dtype — the vectors themselves are model-dependent and
    not pinned here; that's a separate eval concern."""
    embedder = create_default_embedder()
    vec = embedder.embed("the quick brown fox")
    assert vec.shape == (EMBEDDING_DIMENSION,)
    assert vec.dtype == np.float32
    assert MODEL_VERSION == "bge-large-en@1.5"
