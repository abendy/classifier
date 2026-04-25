"""embeddings

Dense-vector storage keyed by ``(envelope_id, model_version)``.
sqlite-vec ``vec0`` virtual tables allow only a single PRIMARY KEY
column, so the composite identity the plan requires is encoded in
a synthetic ``id`` TEXT key. ``envelope_id`` and ``model_version``
are carried as auxiliary columns so callers can filter on either
without parsing the synthetic key; ``created_at`` is carried for
per-row audit. Dimension is fixed at 1024 (default dense model); a new
dimension requires a new migration (drop + recreate or parallel
table).
"""

from __future__ import annotations

from alembic import op

revision: str = "8a27a28168d0"
down_revision: str | None = "e4a4f7422d4c"
branch_labels: str | None = None
depends_on: str | None = None


def _load_vec_extension() -> None:
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    try:
        import sqlite_vec
    except ImportError as exc:
        raise RuntimeError(
            "sqlite-vec is required to manage the embeddings virtual "
            "table; install sqlite-vec and retry."
        ) from exc
    raw.enable_load_extension(True)
    try:
        sqlite_vec.load(raw)
    finally:
        raw.enable_load_extension(False)


def upgrade() -> None:
    _load_vec_extension()
    op.execute(
        """
        CREATE VIRTUAL TABLE embeddings USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[1024],
            +envelope_id TEXT,
            +model_version TEXT,
            +created_at TEXT
        )
        """
    )


def downgrade() -> None:
    _load_vec_extension()
    op.execute("DROP TABLE embeddings")
