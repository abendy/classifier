"""Unit tests for the Embedder wrapper over a stub backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from prism.embedding import EMBEDDING_DIMENSION, MODEL_VERSION, Embedder

if TYPE_CHECKING:
    from collections.abc import Iterable


class _StubBackend:
    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> Iterable[np.ndarray]:
        self.calls.append(texts)
        return iter(self._vectors)


def test_embed_calls_backend_with_list_and_returns_first_vector() -> None:
    expected = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    stub = _StubBackend([expected])
    embedder = Embedder(stub)

    result = embedder.embed("hello")

    assert stub.calls == [["hello"]]
    assert result.shape == (EMBEDDING_DIMENSION,)
    assert result.dtype == np.float32
    assert np.array_equal(result, expected)


def test_embed_coerces_float64_to_float32() -> None:
    f64 = np.zeros(EMBEDDING_DIMENSION, dtype=np.float64)
    stub = _StubBackend([f64])
    embedder = Embedder(stub)

    result = embedder.embed("hello")

    assert result.dtype == np.float32


def test_embed_rejects_wrong_shape() -> None:
    stub = _StubBackend([np.zeros(512, dtype=np.float32)])
    embedder = Embedder(stub)

    with pytest.raises(ValueError, match="shape"):
        embedder.embed("hello")


def test_embed_rejects_multi_vector_response() -> None:
    stub = _StubBackend(
        [
            np.zeros(EMBEDDING_DIMENSION, dtype=np.float32),
            np.zeros(EMBEDDING_DIMENSION, dtype=np.float32),
        ]
    )
    embedder = Embedder(stub)

    with pytest.raises(ValueError, match="2 vectors"):
        embedder.embed("hello")


def test_embedding_dimension_is_1024() -> None:
    assert EMBEDDING_DIMENSION == 1024


def test_model_version_is_bge_m3_1_5() -> None:
    assert MODEL_VERSION == "bge-m3@1.5"
