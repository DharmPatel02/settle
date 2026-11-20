"""Create the core payments and ledger tables.

Revision ID: 20261118_0001
Revises:
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261118_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTERNAL_FUNDING_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_name", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("allow_negative", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(currency) = 3", name="currency_length"),
        sa.CheckConstraint(
            "allow_negative OR balance_minor >= 0",
            name="nonnegative_balance_unless_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
    )
    op.create_table(
        "transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("risk_score", sa.Numeric(), nullable=True),
        sa.Column("risk_model_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="positive_amount"),
        sa.CheckConstraint("from_account_id <> to_account_id", name="different_accounts"),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'REJECTED', 'PENDING_REVIEW', 'DECLINED')",
            name="valid_status",
        ),
        sa.CheckConstraint("char_length(currency) = 3", name="currency_length"),
        sa.ForeignKeyConstraint(
            ["from_account_id"], ["accounts.id"], name="fk_transfers_from_account_id_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id"], ["accounts.id"], name="fk_transfers_to_account_id_accounts"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transfers"),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("idem_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED')", name="valid_status"),
        sa.PrimaryKeyConstraint("client_id", "idem_key", name="pk_idempotency_keys"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("transfer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor <> 0", name="nonzero_amount"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_ledger_entries_account_id_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"], ["transfers.id"], name="fk_ledger_entries_transfer_id_transfers"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    )
    op.create_index(
        "ix_ledger_entries_account_id_id", "ledger_entries", ["account_id", "id"], unique=False
    )

    accounts = sa.table(
        "accounts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("owner_name", sa.Text()),
        sa.column("currency", sa.String(3)),
        sa.column("balance_minor", sa.BigInteger()),
        sa.column("allow_negative", sa.Boolean()),
    )
    op.bulk_insert(
        accounts,
        [
            {
                "id": EXTERNAL_FUNDING_ID,
                "owner_name": "EXTERNAL_FUNDING",
                "currency": "USD",
                "balance_minor": 0,
                "allow_negative": True,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_account_id_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("idempotency_keys")
    op.drop_table("transfers")
    op.drop_table("accounts")
