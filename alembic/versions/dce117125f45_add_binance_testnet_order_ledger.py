"""add binance_testnet_order_ledger

Revision ID: dce117125f45
Revises: 4facd9697962
Create Date: 2026-05-21 08:00:56.736062

ROB-286 — order lifecycle ledger for the Binance Spot testnet execution
adapter. All writes go through
``app.services.brokers.binance.testnet.ledger.service.BinanceTestnetLedgerService``;
the repository is module-internal. Mirrors the shape of
``review.alpaca_paper_order_ledger`` (ROB-84) but with a scalping-specific
state vocabulary (tp_sl_armed / tp_sl_triggered).

CHECK constraint on ``lifecycle_state`` is the DB-side guard; the
9-state machine validation lives in the service layer.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "dce117125f45"
down_revision: Union[str, Sequence[str], None] = "4facd9697962"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — create binance_testnet_order_ledger."""
    op.create_table(
        "binance_testnet_order_ledger",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "crypto_instruments.id",
                name="fk_binance_testnet_ledger_instrument_id_crypto_instruments",
            ),
            nullable=False,
        ),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("parent_client_order_id", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(28, 12), nullable=False),
        sa.Column("price", sa.Numeric(28, 12), nullable=True),
        sa.Column("tp_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("sl_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column(
            "planned_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "previewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "submitted_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "filled_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "tp_sl_armed_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "tp_sl_triggered_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "cancelled_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "last_reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("anomaly_reason", sa.Text(), nullable=True),
        sa.Column(
            "anomaly_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("notional_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("notional_override_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "client_order_id", name="uq_binance_testnet_ledger_client_order_id"
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'tp_sl_armed','tp_sl_triggered','closed','cancelled',"
            "'reconciled','anomaly'"
            ")",
            name="binance_testnet_ledger_lifecycle_state",
        ),
        sa.CheckConstraint(
            "side IN ('BUY','SELL')",
            name="binance_testnet_ledger_side",
        ),
        sa.CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_testnet_ledger_order_type",
        ),
    )
    op.create_index(
        "ix_binance_testnet_ledger_instrument_id",
        "binance_testnet_order_ledger",
        ["instrument_id"],
    )
    op.create_index(
        "ix_binance_testnet_ledger_broker_order_id",
        "binance_testnet_order_ledger",
        ["broker_order_id"],
    )
    op.create_index(
        "ix_binance_testnet_ledger_lifecycle_state",
        "binance_testnet_order_ledger",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_binance_testnet_ledger_created_at",
        "binance_testnet_order_ledger",
        ["created_at"],
    )
    op.create_index(
        "ix_binance_testnet_ledger_parent_client_order_id",
        "binance_testnet_order_ledger",
        ["parent_client_order_id"],
    )


def downgrade() -> None:
    """Downgrade schema — drop binance_testnet_order_ledger."""
    op.drop_index(
        "ix_binance_testnet_ledger_parent_client_order_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_created_at",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_lifecycle_state",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_broker_order_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_index(
        "ix_binance_testnet_ledger_instrument_id",
        table_name="binance_testnet_order_ledger",
    )
    op.drop_table("binance_testnet_order_ledger")
