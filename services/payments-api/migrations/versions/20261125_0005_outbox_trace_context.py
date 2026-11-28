"""Store the trace context of the request that wrote each outbox event.

Revision ID: 20261125_0005
Revises: 20261124_0004
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261125_0005"
down_revision: str | None = "20261124_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("trace_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "trace_context")
