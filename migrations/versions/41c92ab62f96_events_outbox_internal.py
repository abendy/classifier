"""events_outbox_internal

Adds the ``internal`` flag distinguishing synthetic causation-anchor
events from genuinely-emitted public events. The pending partial
index narrows to ``dispatched_at IS NULL AND internal = 0`` so a
future dispatcher's poll naturally skips internal rows. ADR 022
pins the rationale.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "41c92ab62f96"
down_revision: str | None = "4d82af96f3f8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "events_outbox",
        sa.Column(
            "internal",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.drop_index(
        "idx_events_outbox_pending",
        table_name="events_outbox",
    )
    op.create_index(
        "idx_events_outbox_pending",
        "events_outbox",
        ["created_at"],
        sqlite_where=sa.text("dispatched_at IS NULL AND internal = 0"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_events_outbox_pending",
        table_name="events_outbox",
    )
    op.create_index(
        "idx_events_outbox_pending",
        "events_outbox",
        ["created_at"],
        sqlite_where=sa.text("dispatched_at IS NULL"),
    )
    op.drop_column("events_outbox", "internal")
