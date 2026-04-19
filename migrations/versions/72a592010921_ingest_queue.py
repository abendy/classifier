"""ingest_queue

Per-event retry state for outbox events the classifier has
observed and intends to process. Separate from the scraper's
outbox so the classifier never mutates upstream state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "72a592010921"
down_revision: str | None = "ac235e80c863"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_queue",
        sa.Column("source_event_id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.Column(
            "attempts", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("available_at", sa.Text, nullable=False),
        sa.Column("dispatched_at", sa.Text, nullable=True),
        sa.Column(
            "status", sa.Text, nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("last_error", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_ingest_queue_pending",
        "ingest_queue",
        ["available_at"],
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ingest_queue_pending",
        table_name="ingest_queue",
    )
    op.drop_table("ingest_queue")
