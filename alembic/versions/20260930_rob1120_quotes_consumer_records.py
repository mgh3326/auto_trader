"""#1120 quotes:toss shadow consumer — append-only firing + ladder tables.

Revision ID: 20260930_rob1120_quotes
Revises: 20260928_task847_h5_state
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260930_rob1120_quotes"
down_revision: str | Sequence[str] | None = "20260928_task847_h5_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSIONS = "'nxt_pre','krx_regular','nxt_after','us_pre','us_regular','us_after'"

_IMMUTABILITY_DDL = (
    """
    CREATE OR REPLACE FUNCTION review.reject_quotes_consumer_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'review.% is append-only; % rejected',
            TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_quotes_trigger_firings_append_only
    BEFORE UPDATE OR DELETE ON review.quotes_trigger_firings
    FOR EACH ROW EXECUTE FUNCTION review.reject_quotes_consumer_mutation()
    """,
    """
    CREATE TRIGGER trg_quotes_trigger_firings_truncate
    BEFORE TRUNCATE ON review.quotes_trigger_firings
    FOR EACH STATEMENT EXECUTE FUNCTION
        review.reject_quotes_consumer_mutation()
    """,
    """
    CREATE TRIGGER trg_ladder_touch_events_append_only
    BEFORE UPDATE OR DELETE ON review.ladder_touch_events
    FOR EACH ROW EXECUTE FUNCTION review.reject_quotes_consumer_mutation()
    """,
    """
    CREATE TRIGGER trg_ladder_touch_events_truncate
    BEFORE TRUNCATE ON review.ladder_touch_events
    FOR EACH STATEMENT EXECUTE FUNCTION
        review.reject_quotes_consumer_mutation()
    """,
)


def upgrade() -> None:
    op.create_table(
        "quotes_trigger_firings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("source_symbol", sa.Text(), nullable=True),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("reference_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("current_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "would_kick",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("suppress_reason", sa.Text(), nullable=True),
        sa.Column(
            "daily_would_kick_count",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_would_kick_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("not_evaluable_reason", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_quotes_trigger_firings_dedupe"),
        sa.CheckConstraint(
            "trigger_type IN ('index_spike','holding_spike','vi_proxy','own_fill')",
            name="ck_quotes_trigger_firings_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('fired','not_evaluable')",
            name="ck_quotes_trigger_firings_outcome",
        ),
        sa.CheckConstraint(
            "market IS NULL OR market IN ('kr','us','crypto','other')",
            name="ck_quotes_trigger_firings_market",
        ),
        sa.CheckConstraint(
            f"session IS NULL OR session IN ({_SESSIONS})",
            name="ck_quotes_trigger_firings_session",
        ),
        sa.CheckConstraint(
            "suppress_reason IS NULL OR suppress_reason IN ("
            "'daily_cap','cooldown','not_evaluable')",
            name="ck_quotes_trigger_firings_suppress",
        ),
        sa.CheckConstraint(
            "NOT (outcome = 'fired' AND not_evaluable_reason IS NOT NULL)",
            name="ck_quotes_trigger_firings_ne_consistency",
        ),
        schema="review",
    )
    op.create_index(
        "ix_quotes_trigger_firings_type_ts",
        "quotes_trigger_firings",
        ["trigger_type", "event_ts"],
        schema="review",
    )
    op.create_index(
        "ix_quotes_trigger_firings_symbol_ts",
        "quotes_trigger_firings",
        ["symbol", "event_ts"],
        schema="review",
    )
    op.create_index(
        "ix_quotes_trigger_firings_kst_date",
        "quotes_trigger_firings",
        ["kst_date"],
        schema="review",
    )

    op.create_table(
        "ladder_touch_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("order_ledger", sa.Text(), nullable=False),
        sa.Column("order_ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("client_order_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("anchor_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("event_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("distance_pct", sa.Numeric(10, 6), nullable=True),
        sa.Column("event_ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("nxt_tradable", sa.Boolean(), nullable=True),
        sa.Column("died_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("stream_entry_id", sa.Text(), nullable=True),
        sa.Column("fill_ledger_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_ladder_touch_events_dedupe"),
        sa.CheckConstraint(
            "event_type IN ('approach','touch','fill')",
            name="ck_ladder_touch_events_type",
        ),
        sa.CheckConstraint(
            "order_ledger IN ("
            "'kis_live_order_ledger','toss_live_order_ledger',"
            "'live_order_ledger')",
            name="ck_ladder_touch_events_ledger",
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="ck_ladder_touch_events_side"
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')", name="ck_ladder_touch_events_market"
        ),
        sa.CheckConstraint(
            f"session IS NULL OR session IN ({_SESSIONS})",
            name="ck_ladder_touch_events_session",
        ),
        schema="review",
    )
    op.create_index(
        "ix_ladder_touch_events_rung",
        "ladder_touch_events",
        ["order_ledger", "order_ledger_id"],
        schema="review",
    )
    op.create_index(
        "ix_ladder_touch_events_symbol_ts",
        "ladder_touch_events",
        ["symbol", "event_ts"],
        schema="review",
    )
    op.create_index(
        "ix_ladder_touch_events_type_ts",
        "ladder_touch_events",
        ["event_type", "event_ts"],
        schema="review",
    )

    for statement in _IMMUTABILITY_DDL:
        op.execute(statement)

    # Stage-4 role may not exist in dev/CI databases — conditional GRANT.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.quotes_trigger_firings, "
        "review.ladder_touch_events TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ladder_touch_events_truncate "
        "ON review.ladder_touch_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ladder_touch_events_append_only "
        "ON review.ladder_touch_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_quotes_trigger_firings_truncate "
        "ON review.quotes_trigger_firings"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_quotes_trigger_firings_append_only "
        "ON review.quotes_trigger_firings"
    )
    op.execute("DROP FUNCTION IF EXISTS review.reject_quotes_consumer_mutation()")
    op.drop_index(
        "ix_ladder_touch_events_type_ts",
        table_name="ladder_touch_events",
        schema="review",
    )
    op.drop_index(
        "ix_ladder_touch_events_symbol_ts",
        table_name="ladder_touch_events",
        schema="review",
    )
    op.drop_index(
        "ix_ladder_touch_events_rung",
        table_name="ladder_touch_events",
        schema="review",
    )
    op.drop_table("ladder_touch_events", schema="review")
    op.drop_index(
        "ix_quotes_trigger_firings_kst_date",
        table_name="quotes_trigger_firings",
        schema="review",
    )
    op.drop_index(
        "ix_quotes_trigger_firings_symbol_ts",
        table_name="quotes_trigger_firings",
        schema="review",
    )
    op.drop_index(
        "ix_quotes_trigger_firings_type_ts",
        table_name="quotes_trigger_firings",
        schema="review",
    )
    op.drop_table("quotes_trigger_firings", schema="review")
