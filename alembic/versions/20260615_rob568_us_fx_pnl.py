"""ROB-568 add US FX PnL fields.

Revision ID: 20260615_rob568_us_fx_pnl
Revises: 20260615_rob569_toss_review
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260615_rob568_us_fx_pnl"
down_revision: str | Sequence[str] | None = "20260615_rob569_toss_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FX_COLUMNS = (
    sa.Column("buy_fx_rate", sa.Numeric(18, 4), nullable=True),
    sa.Column("sell_fx_rate", sa.Numeric(18, 4), nullable=True),
    sa.Column("fx_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("security_pnl_usd", sa.Numeric(20, 4), nullable=True),
    sa.Column("security_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("total_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("fx_rate_source", sa.Text(), nullable=True),
    sa.Column("fx_pnl_accuracy", sa.Text(), nullable=True),
)

TABLES = (
    "trade_journals",
    "live_order_ledger",
    "toss_live_order_ledger",
    "trade_retrospectives",
)


def _add_fx_columns(table_name: str) -> None:
    for column in FX_COLUMNS:
        op.add_column(table_name, column.copy(), schema="review")


def _drop_fx_columns(table_name: str) -> None:
    for name in reversed([column.name for column in FX_COLUMNS]):
        op.drop_column(table_name, name, schema="review")


def upgrade() -> None:
    for table_name in TABLES:
        _add_fx_columns(table_name)

    op.drop_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
        schema="review",
    )

    for table_name in reversed(TABLES):
        _drop_fx_columns(table_name)
