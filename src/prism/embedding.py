"""Single-text dense embedder backed by a fastembed-shaped backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


EMBEDDING_DIMENSION = 1024
FASTEMBED_MODEL_NAME = "BAAI/bge-large-en-v1.5"
MODEL_VERSION = "bge-large-en@1.5"


class Embedder:
    """Single-text embedder backed by any fastembed-shaped object."""

    def __init__(self, backend: Any) -> None:
        """``backend`` must expose
        ``embed(texts: list[str]) -> Iterable[np.ndarray]``.
        Production: ``fastembed.TextEmbedding(...)``. Tests:
        a stub returning deterministic ndarrays.
        """
        self._backend = backend

    def embed(self, text: str) -> NDArray[np.float32]:
        """Return the dense vector for a single text. Returns a
        1-D ``np.float32`` array of length ``EMBEDDING_DIMENSION``."""
        vectors = list(self._backend.embed([text]))
        if len(vectors) != 1:
            raise ValueError(
                f"backend returned {len(vectors)} vectors for 1 text"
            )
        vector = vectors[0]
        if vector.dtype != np.float32:
            vector = vector.astype(np.float32)
        if vector.shape != (EMBEDDING_DIMENSION,):
            raise ValueError(
                f"expected shape ({EMBEDDING_DIMENSION},), got {vector.shape}"
            )
        return vector


def create_default_embedder() -> Embedder:
    """Instantiate the production Embedder backed by fastembed's
    default dense model.

    ``fastembed 0.8`` no longer ships ``BAAI/bge-m3`` in
    ``TextEmbedding``. We use the supported 1024-dim
    ``BAAI/bge-large-en-v1.5`` variant so storage shape and the
    embedder wrapper contract stay unchanged.
    """
    from fastembed import TextEmbedding

    return Embedder(TextEmbedding(model_name=FASTEMBED_MODEL_NAME))
