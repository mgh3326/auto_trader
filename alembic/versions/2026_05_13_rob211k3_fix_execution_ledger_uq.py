"""ROB-211 K3 widen execution_ledger unique constraint to include account_mode and venue.

The original constraint (broker, broker_order_id, fill_seq) did not include
account_mode or venue, which would cause false conflicts between fills from
different accounts or venues sharing the same broker order ID.  The new key
(broker, account_mode, venue, broker_order_id, fill_seq) disambiguates these
dimensions while still allowing websocket/REST reconcile updates to land on
the same logical fill.

Revision ID: 20260513_rob211k3
Revises: 20260513_rob211
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision = "20260513_rob211k3"
down_revision = "20260513_rob211"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_execution_ledger_broker_order_fill",
        "execution_ledger",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_execution_ledger_fill",
        "execution_ledger",
        ["broker", "account_mode", "venue", "broker_order_id", "fill_seq"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_execution_ledger_fill",
        "execution_ledger",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_execution_ledger_broker_order_fill",
        "execution_ledger",
        ["broker", "broker_order_id", "fill_seq"],
        schema="review",
    )
