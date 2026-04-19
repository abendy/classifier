"""sync_state

Durable key-value store for classifier-side synchronization
cursors. The poll loop persists its scraper outbox scan-cursor
here so a restart resumes cleanly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "cdbdd59d582e"
down_revision: str | None = "f02e66b925a3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "sync_state",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
