"""Index transfers by source account and time for risk features.

Revision ID: 20261124_0004
Revises: 20261124_0003
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261124_0004"
down_revision: str | None = "20261124_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transfers_from_account_id_created_at",
        "transfers",
        ["from_account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_transfers_from_account_id_created_at", table_name="transfers")
