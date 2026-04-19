"""content_envelopes_source_id

Adds a source_id column (with a UNIQUE index) to content_envelopes
so the ingest loop can dedup by the envelope's origin id. The
existing table has no rows yet, so NOT NULL is safe.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f02e66b925a3"
down_revision: str | None = "72a592010921"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "content_envelopes",
        sa.Column("source_id", sa.Text, nullable=False),
    )
    op.create_index(
        "idx_content_envelopes_source_id",
        "content_envelopes",
        ["source_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_content_envelopes_source_id",
        table_name="content_envelopes",
    )
    op.drop_column("content_envelopes", "source_id")
