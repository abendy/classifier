"""events_outbox

Append-only record of every event the classifier emits.
A future dispatcher (separate slice; not this one) publishes
from this table to the eventual event bus and stamps
`dispatched_at`.

The wire envelope shape (dada.stream event-types.md) has
eight fields; seven get their own typed column so a future
dispatcher can index, filter, and aggregate without JSON-
parsing every row. The eighth (`payload`) holds the per-
event-type body as JSON because its shape varies by type.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "3434894db396"
down_revision: str | None = "6dffc3940f1e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "events_outbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("envelope_version", sa.Text, nullable=False),
        sa.Column("correlation_id", sa.Text, nullable=False),
        sa.Column("causation_id", sa.Text, nullable=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("dispatched_at", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_events_outbox_pending",
        "events_outbox",
        ["created_at"],
        sqlite_where=sa.text("dispatched_at IS NULL"),
    )
    op.create_index(
        "idx_events_outbox_correlation",
        "events_outbox",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_events_outbox_correlation", table_name="events_outbox")
    op.drop_index("idx_events_outbox_pending", table_name="events_outbox")
    op.drop_table("events_outbox")
