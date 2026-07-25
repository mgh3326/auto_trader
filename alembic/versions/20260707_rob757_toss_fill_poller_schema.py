"""ROB-757 Toss fill poller schema

Revision ID: 20260707_rob757_toss_fill_poller
Revises: 20260707_rob755_source_id_idx
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260707_rob757_toss_fill_poller"
down_revision: str | None = "20260707_rob755_source_id_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "execution_ledger_broker",
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "execution_ledger_broker",
        "execution_ledger",
        "broker IN ('kis','upbit','toss')",
        schema="review",
    )
    op.create_table(
        "toss_fill_poll_state",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scope", name=op.f("pk_toss_fill_poll_state")),
        schema="review",
    )


def downgrade() -> None:
    op.drop_table("toss_fill_poll_state", schema="review")
    op.drop_constraint(
        "execution_ledger_broker",
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "execution_ledger_broker",
        "execution_ledger",
        "broker IN ('kis','upbit')",
        schema="review",
    )
