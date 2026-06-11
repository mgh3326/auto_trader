"""ROB-516 operator session context append-only store

Revision ID: 20260611_rob516
Revises: 20260610_rob491
Create Date: 2026-06-11 13:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260611_rob516"
down_revision: str | None = "20260610_rob491"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_session_context",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "entry_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kst_date", sa.Date(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'claude'"),
        ),
        sa.Column("session_label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_uuid",
            name="uq_operator_session_context_entry_uuid",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_operator_session_context_market",
        ),
        sa.CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_operator_session_context_account_scope",
        ),
        sa.CheckConstraint(
            "entry_type IN ("
            "'plan','decision','deferred','rejected_candidate','constraint',"
            "'open_question','next_action','handoff_note'"
            ")",
            name="ck_operator_session_context_entry_type",
        ),
        sa.CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="ck_operator_session_context_created_by",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(refs) = 'object'",
            name="ck_operator_session_context_refs_object",
        ),
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_market_date_created",
        "operator_session_context",
        ["market", "kst_date", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_entry_type_date",
        "operator_session_context",
        ["entry_type", "kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_refs_gin",
        "operator_session_context",
        ["refs"],
        unique=False,
        schema="review",
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_session_context_refs_gin",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_index(
        "ix_operator_session_context_entry_type_date",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_index(
        "ix_operator_session_context_market_date_created",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_table("operator_session_context", schema="review")
