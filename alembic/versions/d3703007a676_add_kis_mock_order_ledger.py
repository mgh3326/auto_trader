"""add kis_mock_order_ledger

Revision ID: d3703007a676
Revises: d34d6def084b
Create Date: 2026-04-29 11:02:14.936643

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3703007a676"
down_revision: Union[str, Sequence[str], None] = "d34d6def084b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

instrument_type_enum = postgresql.ENUM(
    "equity_kr",
    "equity_us",
    "crypto",
    "forex",
    "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "kis_mock_order_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column(
            "order_type", sa.Text(), nullable=False, server_default="limit"
        ),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column(
            "amount", sa.Numeric(20, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "fee", sa.Numeric(20, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "currency", sa.Text(), nullable=False, server_default="KRW"
        ),
        sa.Column("order_no", sa.Text(), nullable=True),
        sa.Column("order_time", sa.Text(), nullable=True),
        sa.Column("krx_fwdg_ord_orgno", sa.Text(), nullable=True),
        sa.Column(
            "account_mode", sa.Text(), nullable=False, server_default="kis_mock"
        ),
        sa.Column(
            "broker", sa.Text(), nullable=False, server_default="kis"
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="unknown"
        ),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("order_no", name="uq_kis_mock_ledger_order_no"),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="kis_mock_ledger_side"
        ),
        sa.CheckConstraint(
            "currency IN ('KRW','USD')", name="kis_mock_ledger_currency"
        ),
        sa.CheckConstraint(
            "account_mode = 'kis_mock'",
            name="kis_mock_ledger_account_mode_kis_mock",
        ),
        sa.CheckConstraint(
            "broker = 'kis'", name="kis_mock_ledger_broker_kis"
        ),
        sa.CheckConstraint(
            "status IN ('accepted','rejected','unknown')",
            name="kis_mock_ledger_status_allowed",
        ),
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_trade_date",
        "kis_mock_order_ledger",
        ["trade_date"],
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_symbol",
        "kis_mock_order_ledger",
        ["symbol"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kis_mock_ledger_symbol",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_kis_mock_ledger_trade_date",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_table("kis_mock_order_ledger", schema="review")
