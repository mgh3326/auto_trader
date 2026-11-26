"""Record exact Alpaca paper crypto fees and net received quantity.

Revision ID: hk6784_crypto_fee
Revises: 6f28395ebfc2
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "hk6784_crypto_fee"
down_revision: str | Sequence[str] | None = "6f28395ebfc2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widen-only precision change on a small paper ledger (approved by auto_trader-design).
    # expand-contract: contract-step 6784
    op.alter_column(
        "alpaca_paper_order_ledger",
        "fee_amount",
        schema="review",
        existing_type=sa.Numeric(20, 4),
        type_=sa.Numeric(38, 18),
        existing_nullable=True,
    )
    op.add_column(
        "alpaca_paper_order_ledger",
        sa.Column("fee_evidence_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "alpaca_paper_order_ledger",
        sa.Column("net_received_qty", sa.Numeric(38, 18), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "alpaca_paper_ledger_fee_evidence_reason",
        "alpaca_paper_order_ledger",
        "fee_evidence_reason IS NULL OR fee_evidence_reason IN ("
        "'fee_activity_missing','fee_activity_unreadable','fee_activity_order_mismatch',"
        "'fee_activity_symbol_mismatch','fee_activity_type_unknown','fee_currency_ambiguous',"
        "'fee_fill_set_incomplete','fee_activity_coverage_incomplete',"
        "'fee_exceeds_filled_quantity','broker_order_id_missing',"
        "'fee_activity_unattributable','fee_pending_eod')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "alpaca_paper_ledger_fee_evidence_reason",
        "alpaca_paper_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_column("alpaca_paper_order_ledger", "net_received_qty", schema="review")
    op.drop_column("alpaca_paper_order_ledger", "fee_evidence_reason", schema="review")
    # Keep the widened precision: crypto fee rows can exceed the old scale.
