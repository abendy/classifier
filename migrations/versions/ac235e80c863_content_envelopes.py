"""content_envelopes

Creates the envelope cache table that the classifier writes
when ingesting content from an edge service and reads when
classifying.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "ac235e80c863"
down_revision: str | None = "fd63ca4f8371"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "content_envelopes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("envelope_json", sa.Text, nullable=False),
        sa.Column("ingested_at", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index(
        "idx_content_envelopes_ingested_at",
        "content_envelopes",
        ["ingested_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_content_envelopes_ingested_at",
        table_name="content_envelopes",
    )
    op.drop_table("content_envelopes")
