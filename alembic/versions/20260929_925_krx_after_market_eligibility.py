"""Add krx_after_market_eligibility (task #925).

Revision ID: 20260929_925_krx_after_market
Revises: 20260928_884_fanout_a_record
Create Date: 2026-09-29

Additive DDL only: one new table holding the operator-imported KRX
after-market (16:00-20:00 KST) eligibility list. No existing table, column
or row is touched and nothing is backfilled — an empty table means no list
has been imported, which every reader treats as not-eligible. The list is
loaded manually with scripts/import_krx_after_market_eligibility.py
(dry-run by default); there is no scheduler. Apply separately via
``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260929_925_krx_after_market"
down_revision: str | Sequence[str] | None = "20260928_884_fanout_a_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "krx_after_market_eligibility",
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("list_source", sa.Text(), nullable=False),
        sa.Column("list_asof", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "imported_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9A-Z]{6}$'",
            name="ck_krx_after_market_eligibility_symbol_format",
        ),
        sa.CheckConstraint(
            "length(btrim(list_source)) > 0",
            name="ck_krx_after_market_eligibility_list_source_nonblank",
        ),
        sa.PrimaryKeyConstraint("symbol", name="pk_krx_after_market_eligibility"),
    )


def downgrade() -> None:
    op.drop_table("krx_after_market_eligibility")
