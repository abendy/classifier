"""baseline

Establishes the migration chain. Creates no tables.
"""

from __future__ import annotations

revision: str = "fd63ca4f8371"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
