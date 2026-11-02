"""ROB-1474: paper/demo strategy attribution and one-send journal.

Revision ID: 20261102_rob1474_mock_orders
Revises: 20261102_rob1460_exit_plans
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261102_rob1474_mock_orders"
down_revision: str | Sequence[str] | None = "20261102_rob1460_exit_plans"
branch_labels = None
depends_on = None

GUARD_DDL = (
    """CREATE FUNCTION review.mock_strategy_orders_guard() RETURNS trigger AS $$
    BEGIN
      IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'mock strategy send journal cannot be erased'
          USING ERRCODE = 'restrict_violation';
      END IF;
      IF (NEW.strategy_id, NEW.venue, NEW.account_ref, NEW.episode_id,
          NEW.symbol, NEW.side, NEW.qty, NEW.notional, NEW.client_order_id, NEW.exit_due_at)
          IS DISTINCT FROM
         (OLD.strategy_id, OLD.venue, OLD.account_ref, OLD.episode_id,
          OLD.symbol, OLD.side, OLD.qty, OLD.notional, OLD.client_order_id, OLD.exit_due_at)
         OR (OLD.status <> 'planned' AND NEW.status = 'planned')
         OR (OLD.status IN ('closed','canceled','rejected') AND NEW.status <> OLD.status)
         OR (OLD.broker_order_id IS NOT NULL AND
             NEW.broker_order_id IS DISTINCT FROM OLD.broker_order_id)
      THEN
        RAISE EXCEPTION 'mock strategy immutable intent or terminal fence changed'
          USING ERRCODE = 'restrict_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""",
    """CREATE TRIGGER trg_mock_strategy_orders_guard
       BEFORE UPDATE OR DELETE ON review.mock_strategy_orders
       FOR EACH ROW EXECUTE FUNCTION review.mock_strategy_orders_guard()""",
    """CREATE TRIGGER trg_mock_strategy_orders_truncate
       BEFORE TRUNCATE ON review.mock_strategy_orders
       FOR EACH STATEMENT EXECUTE FUNCTION review.mock_strategy_orders_guard()""",
)


def upgrade() -> None:
    op.create_table(
        "mock_strategy_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("strategy_id", sa.String(128), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("episode_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Numeric(38, 18)),
        sa.Column("notional", sa.Numeric(38, 18)),
        sa.Column("client_order_id", sa.String(128), nullable=False, unique=True),
        sa.Column("broker_order_id", sa.String(128)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("exit_due_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("entry_price", sa.Numeric(38, 18)),
        sa.Column("exit_price", sa.Numeric(38, 18)),
        sa.Column("realized_pnl", sa.Numeric(38, 18)),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "venue IN ('alpaca_paper','alpaca_paper_crypto',"
            "'binance_spot_demo','binance_futures_demo')",
            name="paper_demo_venue",
        ),
        sa.CheckConstraint("side IN ('buy','sell','short','cover')", name="side"),
        sa.CheckConstraint(
            "status IN ('planned','sending','uncertain','accepted','partially_filled',"
            "'filled','canceled','rejected','closed')",
            name="status",
        ),
        sa.CheckConstraint(
            "(qty IS NOT NULL OR notional IS NOT NULL) AND "
            "(qty IS NULL OR (qty > 0 AND qty < 'Infinity'::numeric)) AND "
            "(notional IS NULL OR (notional > 0 AND notional < 'Infinity'::numeric))",
            name="positive_size",
        ),
        schema="review",
    )
    op.create_index(
        "ix_mock_strategy_due",
        "mock_strategy_orders",
        ["venue", "account_ref", "exit_due_at"],
        schema="review",
    )
    for statement in GUARD_DDL:
        op.execute(statement)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='at_app') THEN "
        "GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT, UPDATE ON review.mock_strategy_orders TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE review.mock_strategy_orders_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM review.mock_strategy_orders) THEN "
        "RAISE EXCEPTION 'cannot discard mock strategy send journal'; END IF; END $$"
    )
    op.drop_table("mock_strategy_orders", schema="review")
    op.execute("DROP FUNCTION review.mock_strategy_orders_guard()")
