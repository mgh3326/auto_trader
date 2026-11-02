"""ROB-1460: per-lot exit plans and mechanical loss-cut decisions.

Additive: two new tables, no change to any existing table. Both are append-only:
the app role gets SELECT and INSERT, and triggers refuse UPDATE and DELETE for
every role. Losing the decisions table would let a trigger fire twice, so
downgrade refuses while either table holds rows.

Revision ID: 20261102_rob1460_exit_plans
Revises: 20261101_rob1440_intent_gw
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261102_rob1460_exit_plans"
down_revision: str | Sequence[str] | None = "20261101_rob1440_intent_gw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("lot_exit_plans", "mechanical_loss_cut_decisions")

#: One statement per entry. The DB test installs these so it exercises this DDL.
GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.rob1460_append_only_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_lot_exit_plans_append_only ON review.lot_exit_plans",
    """
    CREATE TRIGGER trg_lot_exit_plans_append_only
    BEFORE UPDATE OR DELETE ON review.lot_exit_plans
    FOR EACH ROW EXECUTE FUNCTION review.rob1460_append_only_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_mechanical_loss_cut_decisions_append_only "
    "ON review.mechanical_loss_cut_decisions",
    """
    CREATE TRIGGER trg_mechanical_loss_cut_decisions_append_only
    BEFORE UPDATE OR DELETE ON review.mechanical_loss_cut_decisions
    FOR EACH ROW EXECUTE FUNCTION review.rob1460_append_only_guard()
    """,
)

DOWNGRADE_GUARD = """
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM review.lot_exit_plans)
       OR EXISTS (SELECT 1 FROM review.mechanical_loss_cut_decisions) THEN
        RAISE EXCEPTION 'ROB-1460 tables hold rows; export and clear them deliberately'
            USING ERRCODE = 'restrict_violation';
    END IF;
END $$
"""


def upgrade() -> None:
    op.create_table(
        "lot_exit_plans",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("stop_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("stop_basis", sa.Text(), nullable=False),
        sa.Column("stop_reason", sa.Text()),
        sa.Column("reference_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("average_cost", sa.Numeric(24, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column(
            "add_ladder",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("max_adds", sa.Integer(), nullable=False),
        sa.Column("adds_used", sa.Integer(), nullable=False),
        sa.Column("add_verdict", sa.Text()),
        sa.Column("backfilled", sa.Boolean(), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("rung_index", sa.Integer()),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("policy_content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_mode",
            "market",
            "symbol",
            "version",
            name="uq_lot_exit_plans_lot_version",
        ),
        sa.CheckConstraint(
            "account_mode IN ('kis_live','toss_live','upbit')",
            name="ck_lot_exit_plans_account_mode",
        ),
        sa.CheckConstraint(
            "market IN ('equity_kr','equity_us','crypto')",
            name="ck_lot_exit_plans_market",
        ),
        sa.CheckConstraint("version >= 1", name="ck_lot_exit_plans_version"),
        sa.CheckConstraint(
            "event IN ('entry','add','backfill')", name="ck_lot_exit_plans_event"
        ),
        sa.CheckConstraint(
            "stop_basis IN ('support','pct_floor','volatility')",
            name="ck_lot_exit_plans_stop_basis",
        ),
        sa.CheckConstraint("stop_price > 0", name="ck_lot_exit_plans_stop_positive"),
        sa.CheckConstraint(
            "reference_price > 0", name="ck_lot_exit_plans_reference_positive"
        ),
        sa.CheckConstraint(
            "average_cost > 0", name="ck_lot_exit_plans_average_positive"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_lot_exit_plans_quantity_positive"),
        sa.CheckConstraint(
            "max_adds >= 0 AND adds_used >= 0", name="ck_lot_exit_plans_adds"
        ),
        sa.CheckConstraint(
            "backfilled = (event = 'backfill')", name="ck_lot_exit_plans_backfilled"
        ),
        schema="review",
    )
    op.create_index(
        "ix_lot_exit_plans_lot_latest",
        "lot_exit_plans",
        ["account_mode", "market", "symbol", sa.text("version DESC")],
        schema="review",
    )
    op.create_table(
        "mechanical_loss_cut_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("backfilled", sa.Boolean(), nullable=False),
        sa.Column("trigger_family", sa.Text(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("session", sa.Date(), nullable=False),
        sa.Column("close_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("stop_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("average_cost", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pct", sa.Numeric(12, 4), nullable=False),
        sa.Column("held_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("open_sell_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("protected_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("intended_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("intended_limit_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("intended_notional", sa.Numeric(28, 8), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("per_order_cap", sa.Numeric(28, 8), nullable=False),
        sa.Column("within_per_order_cap", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("policy_content_hash", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "mode",
            "account_mode",
            "market",
            "symbol",
            "trigger_key",
            name="uq_mechanical_loss_cut_decisions_trigger",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow','live')", name="ck_mechanical_loss_cut_decisions_mode"
        ),
        sa.CheckConstraint(
            "(mode = 'live') = (proposal_id IS NOT NULL)",
            name="ck_mechanical_loss_cut_decisions_live_has_proposal",
        ),
        sa.CheckConstraint(
            "intended_quantity > 0 AND intended_limit_price > 0",
            name="ck_mechanical_loss_cut_decisions_order_positive",
        ),
        schema="review",
    )
    op.create_index(
        "ix_mechanical_loss_cut_decisions_lot_session",
        "mechanical_loss_cut_decisions",
        ["mode", "account_mode", "market", "symbol", sa.text("session DESC")],
        schema="review",
    )
    op.create_index(
        "ix_mechanical_loss_cut_decisions_session",
        "mechanical_loss_cut_decisions",
        ["session"],
        schema="review",
    )
    op.create_index(
        "ix_mechanical_loss_cut_decisions_proposal",
        "mechanical_loss_cut_decisions",
        ["proposal_id"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )
    for statement in GUARD_DDL:
        op.execute(statement)
    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.lot_exit_plans TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE review.lot_exit_plans_id_seq TO at_app; "
        "GRANT SELECT, INSERT ON review.mechanical_loss_cut_decisions TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE "
        "review.mechanical_loss_cut_decisions_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.drop_index(
        "ix_mechanical_loss_cut_decisions_proposal",
        table_name="mechanical_loss_cut_decisions",
        schema="review",
    )
    op.drop_index(
        "ix_mechanical_loss_cut_decisions_session",
        table_name="mechanical_loss_cut_decisions",
        schema="review",
    )
    op.drop_index(
        "ix_mechanical_loss_cut_decisions_lot_session",
        table_name="mechanical_loss_cut_decisions",
        schema="review",
    )
    for table in reversed(_TABLES):
        op.drop_table(table, schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.rob1460_append_only_guard()")
