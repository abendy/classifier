"""Single-text dense embedder backed by a fastembed-shaped backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


EMBEDDING_DIMENSION = 1024
MODEL_VERSION = "bge-m3@1.5"


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


def create_bge_m3_embedder() -> Embedder:
    """Instantiate the production Embedder backed by fastembed's
    BGE-M3 model. First call downloads ~2 GB of ONNX weights to
    the fastembed cache; subsequent calls are warm."""
    from fastembed import TextEmbedding

    return Embedder(TextEmbedding(model_name="BAAI/bge-m3"))
