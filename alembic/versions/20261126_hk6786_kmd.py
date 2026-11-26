"""Add append-only KR market-wide daily facts."""

# expand-contract: contract-step 6786

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261126_hk6786_kmd"
down_revision: str | None = "6f28395ebfc2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kr_market_breadth",
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("session_label", sa.String(20), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("up_count", sa.Integer(), nullable=False),
        sa.Column("down_count", sa.Integer(), nullable=False),
        sa.Column("flat_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("market", "trade_date", "observed_at"),
        sa.CheckConstraint("market IN ('KOSPI','KOSDAQ')", name="market"),
        sa.CheckConstraint(
            "session_label IN ('intraday','after_close')", name="session_label"
        ),
        sa.CheckConstraint(
            "up_count >= 0 AND down_count >= 0 AND flat_count >= 0",
            name="counts_nonnegative",
        ),
        sa.CheckConstraint(
            "total_count >= 0 AND fetched_count >= 0", name="coverage_nonnegative"
        ),
        sa.CheckConstraint(
            "complete = (fetched_count = total_count)",
            name="completeness_matches_counts",
        ),
    )
    op.create_table(
        "kr_market_investor_net",
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("investor_code", sa.String(20), nullable=False),
        sa.Column("investor_name", sa.String(40), nullable=True),
        sa.Column("net_amount", sa.Numeric(), nullable=False),
        sa.Column("buy_amount", sa.Numeric(), nullable=True),
        sa.Column("sell_amount", sa.Numeric(), nullable=True),
        sa.Column("buy_quantity", sa.Numeric(), nullable=True),
        sa.Column("sell_quantity", sa.Numeric(), nullable=True),
        sa.Column("amount_unit", sa.String(20), nullable=False),
        sa.Column("quantity_unit", sa.String(20), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("market", "trade_date", "investor_code"),
        sa.CheckConstraint("market IN ('KOSPI','KOSDAQ')", name="market"),
    )
    op.create_table(
        "kr_market_deposit_credit",
        sa.Column("bizdate", sa.Date(), primary_key=True, nullable=False),
        sa.Column("customer_deposit", sa.Numeric(), nullable=True),
        sa.Column("credit_loan", sa.Numeric(), nullable=True),
        sa.Column("credit_loan_diff", sa.Numeric(), nullable=True),
        sa.Column("beneficiary_certificate", sa.Numeric(), nullable=True),
        sa.Column("amount_unit", sa.String(20), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # expand-contract: contract-step 6786
    op.execute(
        """CREATE OR REPLACE FUNCTION public.kr_market_daily_reject_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is insert-only', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END
        $$"""
    )
    for table in (
        "kr_market_breadth",
        "kr_market_investor_net",
        "kr_market_deposit_credit",
    ):
        # expand-contract: contract-step 6786
        op.execute(
            f"CREATE TRIGGER trg_{table}_insert_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION public.kr_market_daily_reject_mutation()"
        )
        # expand-contract: contract-step 6786
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.kr_market_daily_reject_mutation()"
        )
    # Stage-4 role may not exist in development or CI databases.
    # expand-contract: contract-step 6786
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
        GRANT SELECT, INSERT ON kr_market_breadth, kr_market_investor_net,
        kr_market_deposit_credit TO at_app; END IF; END $$"""
    )


def downgrade() -> None:
    connection = op.get_bind()
    tables = ("kr_market_breadth", "kr_market_investor_net", "kr_market_deposit_credit")
    for table in tables:
        connection.execute(
            sa.text(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
        )
        count = connection.execute(
            sa.text(f"SELECT count(*) FROM public.{table}")
        ).scalar_one()
        if count:
            raise RuntimeError(f"refusing to downgrade: {table} contains records")
    for table in reversed(tables):
        # expand-contract: contract-step 6786
        op.execute(f"DROP TRIGGER trg_{table}_no_truncate ON {table}")
        # expand-contract: contract-step 6786
        op.execute(f"DROP TRIGGER trg_{table}_insert_only ON {table}")
        op.drop_table(table)
    # expand-contract: contract-step 6786
    op.execute("DROP FUNCTION public.kr_market_daily_reject_mutation()")
