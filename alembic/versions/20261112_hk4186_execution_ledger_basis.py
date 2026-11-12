"""Add forward-only fill time and cost provenance to execution ledger.

Revision ID: 20261112_hk4186_basis
Revises: 20261112_hk4187_retro_decision
"""

import sqlalchemy as sa

from alembic import op

revision = "20261112_hk4186_basis"
down_revision = "20261112_hk4187_retro_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_ledger", sa.Column("filled_at_basis", sa.Text()), schema="review"
    )
    op.add_column(
        "execution_ledger", sa.Column("fee_basis", sa.Text()), schema="review"
    )
    op.add_column(
        "execution_ledger", sa.Column("tax_amount", sa.Numeric(20, 4)), schema="review"
    )
    op.create_check_constraint(
        op.f("ck_execution_ledger_execution_ledger_filled_at_basis"),
        "execution_ledger",
        "filled_at_basis IN ('broker_fill_time','broker_order_time','observed_at')",
        schema="review",
    )
    op.create_check_constraint(
        op.f("ck_execution_ledger_execution_ledger_fee_basis"),
        "execution_ledger",
        "fee_basis IN ('broker_reported','not_provided')",
        schema="review",
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
            GRANT SELECT (filled_at_basis, fee_basis, tax_amount), INSERT (filled_at_basis, fee_basis, tax_amount), UPDATE (filled_at_basis, fee_basis, tax_amount) ON review.execution_ledger TO at_app;
        END IF;
    END $$""")


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_ledger_execution_ledger_fee_basis"),
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_execution_ledger_execution_ledger_filled_at_basis"),
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.drop_column("execution_ledger", "tax_amount", schema="review")
    op.drop_column("execution_ledger", "fee_basis", schema="review")
    op.drop_column("execution_ledger", "filled_at_basis", schema="review")
