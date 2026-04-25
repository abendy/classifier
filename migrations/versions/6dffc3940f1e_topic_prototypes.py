"""topic_prototypes

Topic-prototype dense vectors keyed by
``(topic_id, exemplar_idx, model_version)`` in a sqlite-vec
``vec0`` virtual table. Synthetic TEXT PK encodes the composite
identity (ADR 011). Aux columns surface the parts for filter
queries and carry ``text`` for audit. Dimension is fixed at
1024 (default dense model, ADR 017).
"""

from __future__ import annotations

from alembic import op

revision: str = "6dffc3940f1e"
down_revision: str | None = "8a27a28168d0"
branch_labels: str | None = None
depends_on: str | None = None


def _load_vec_extension() -> None:
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    try:
        import sqlite_vec
    except ImportError as exc:
        raise RuntimeError(
            "sqlite-vec is required to manage the topic_prototypes "
            "virtual table; install sqlite-vec and retry."
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
        CREATE VIRTUAL TABLE topic_prototypes USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[1024],
            +topic_id TEXT,
            +exemplar_idx INTEGER,
            +text TEXT,
            +model_version TEXT,
            +created_at TEXT
        )
        """
    )


def downgrade() -> None:
    _load_vec_extension()
    op.execute("DROP TABLE topic_prototypes")
