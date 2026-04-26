"""classifications

The classifier's record of "what I last said about envelope
X." `classifications` holds one row per envelope; the
`active_run_id` column points at the live `runs` row. The
discriminated `confidence` / `low_confidence_reason` pair
distinguishes confident classifications (with topic
assignments) from low-confidence ones (with the four
LowConfidenceReason values). `topic_assignments` is the fan-
out: one row per applied topic (or per parent-topic +
subtopic pair when multiple subtopics under one topic
apply, per the plan §Subtopic head).

ADR 021 pins the two-table normal form vs JSON-blob
trade-off.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "4d82af96f3f8"
down_revision: str | None = "3434894db396"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "classifications",
        sa.Column("envelope_id", sa.Text, primary_key=True),
        sa.Column("active_run_id", sa.Text, nullable=False),
        sa.Column("tags", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("low_confidence_reason", sa.Text, nullable=True),
        sa.Column("classified_by", sa.Text, nullable=False),
        sa.Column("classified_at", sa.Text, nullable=False),
    )
    op.create_index(
        "idx_classifications_low_confidence",
        "classifications",
        ["classified_at"],
        sqlite_where=sa.text("low_confidence_reason IS NOT NULL"),
    )
    op.create_table(
        "topic_assignments",
        sa.Column("envelope_id", sa.Text, nullable=False),
        sa.Column("topic_id", sa.Text, nullable=False),
        sa.Column("subtopic_id", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("matched_terms", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_topic_assignments_envelope",
        "topic_assignments",
        ["envelope_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_topic_assignments_envelope",
        table_name="topic_assignments",
    )
    op.drop_table("topic_assignments")
    op.drop_index(
        "idx_classifications_low_confidence",
        table_name="classifications",
    )
    op.drop_table("classifications")
