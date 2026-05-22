"""replace testnet with demo ledger

Revision ID: e26f8bac2e5f
Revises: dce117125f45
Create Date: 2026-05-22 22:56:16.892034

ROB-298 PR 1 — drop the testnet-only ledger introduced in ROB-286 and
replace it with a unified `binance_demo_order_ledger` keyed by a
`product` discriminator ('spot' | 'usdm_futures'). PR 1 only writes
'spot' rows; PR 2 reuses the same table for futures.

No data preservation: the testnet path was operator-acknowledged as
removable (ROB-298 comment d258c471-...).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e26f8bac2e5f"
down_revision: Union[str, Sequence[str], None] = "dce117125f45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop testnet ledger first (forward-only cut).
    # Use IF EXISTS so the smoke-test roundtrip (downgrade → upgrade) in a
    # single session does not blow up: downgrade intentionally leaves the
    # testnet table absent (see docstring note), so re-running the upgrade
    # must tolerate already-absent testnet indexes/table.
    op.execute(
        "DROP INDEX IF EXISTS ix_binance_testnet_ledger_parent_client_order_id"
    )
    op.execute("DROP INDEX IF EXISTS ix_binance_testnet_ledger_created_at")
    op.execute("DROP INDEX IF EXISTS ix_binance_testnet_ledger_lifecycle_state")
    op.execute("DROP INDEX IF EXISTS ix_binance_testnet_ledger_broker_order_id")
    op.execute("DROP INDEX IF EXISTS ix_binance_testnet_ledger_instrument_id")
    op.execute("DROP TABLE IF EXISTS binance_testnet_order_ledger")

    op.create_table(
        "binance_demo_order_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "instrument_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "crypto_instruments.id",
                name="fk_binance_demo_ledger_instrument_id_crypto_instruments",
            ),
            nullable=False,
        ),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("venue_host", sa.Text(), nullable=False),
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
        sa.Column("planned_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("previewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("submitted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("filled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancelled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("anomaly_reason", sa.Text(), nullable=True),
        sa.Column("anomaly_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notional_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("notional_override_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_binance_demo_ledger_client_order_id"),
        sa.CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="binance_demo_ledger_product",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'closed','cancelled','reconciled','anomaly'"
            ")",
            name="binance_demo_ledger_lifecycle_state",
        ),
        sa.CheckConstraint(
            "side IN ('BUY','SELL')",
            name="binance_demo_ledger_side",
        ),
        sa.CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_demo_ledger_order_type",
        ),
    )
    op.create_index(
        "ix_binance_demo_ledger_product", "binance_demo_order_ledger", ["product"]
    )
    op.create_index(
        "ix_binance_demo_ledger_instrument_id",
        "binance_demo_order_ledger",
        ["instrument_id"],
    )
    op.create_index(
        "ix_binance_demo_ledger_broker_order_id",
        "binance_demo_order_ledger",
        ["broker_order_id"],
    )
    op.create_index(
        "ix_binance_demo_ledger_lifecycle_state",
        "binance_demo_order_ledger",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_binance_demo_ledger_created_at",
        "binance_demo_order_ledger",
        ["created_at"],
    )
    op.create_index(
        "ix_binance_demo_ledger_parent_client_order_id",
        "binance_demo_order_ledger",
        ["parent_client_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_binance_demo_ledger_parent_client_order_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_created_at", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_lifecycle_state", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_broker_order_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_instrument_id", table_name="binance_demo_order_ledger")
    op.drop_index("ix_binance_demo_ledger_product", table_name="binance_demo_order_ledger")
    op.drop_table("binance_demo_order_ledger")
    # Note: downgrade does NOT recreate binance_testnet_order_ledger.
    # ROB-298 is a forward-only cut; rolling back would require restoring
    # the testnet model/service code as well. Use the down_revision
    # `dce117125f45` only as a marker, not for round-trip migration.
