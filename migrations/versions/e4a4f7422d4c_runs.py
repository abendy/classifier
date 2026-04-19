"""runs

One row per pipeline operation. Every future audited step
(ingest pass, envelope classify, embedding batch, eval run)
writes here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e4a4f7422d4c"
down_revision: str | None = "cdbdd59d582e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("envelope_id", sa.Text, nullable=True),
        sa.Column("correlation_id", sa.Text, nullable=True),
        sa.Column("causation_id", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("outputs", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("metadata", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_runs_started_at",
        "runs",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_runs_started_at", table_name="runs")
    op.drop_table("runs")
