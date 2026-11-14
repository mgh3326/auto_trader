"""Add TREND-TSMOM-v1 durable signal and send-intent state (hk 3796 CT-3).

Additive: two new ``review`` tables, no change to any existing object.

Triggers hold the rules for any role, before and after the #789 Stage 4 role
split:

* ``review.binance_trend_signals``: INSERT only as ``observed``; no DELETE or
  TRUNCATE; the identity columns never change; a terminal signal
  (``closed``/``blocked``/``conflict_blocked``/``conflict_rejected``) is frozen,
  except that an operator may mark a conflict record resolved once.
* ``review.binance_trend_intents``: INSERT only as an unsent ``reserved`` row;
  no DELETE or TRUNCATE; the request columns never change; the state only moves
  forward. An ``uncertain`` intent can never go back to ``reserved``/``sending``,
  so a committed send is never dispatched again.

The app role gets SELECT, INSERT and UPDATE (no DELETE) on both tables.

Downgrade refuses while either table holds rows: dropping them would lose the
send fence and the uncertain-order evidence.

Revision ID: 20261114_hk4702_trend_state
Revises: 20261113_hk4282_flow_confirm
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261114_hk4702_trend_state"
down_revision: str | Sequence[str] | None = "20261113_hk4282_flow_confirm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.binance_trend_signals_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'observed' THEN
                RAISE EXCEPTION 'review.binance_trend_signals rows start as observed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.signal_key IS DISTINCT FROM OLD.signal_key
               OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
               OR NEW.symbol IS DISTINCT FROM OLD.symbol
               OR NEW.side IS DISTINCT FROM OLD.side
               OR NEW.decision_ts IS DISTINCT FROM OLD.decision_ts
               OR NEW.signal_price_text IS DISTINCT FROM OLD.signal_price_text
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'review.binance_trend_signals identity is immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.state IN ('closed','blocked','conflict_blocked','conflict_rejected') THEN
                -- The one allowed change to a terminal row: an operator marks a
                -- conflict record resolved, once, and nothing else changes.
                IF OLD.state IN ('conflict_blocked','conflict_rejected')
                   AND OLD.conflict_resolved_at IS NULL
                   AND NEW.conflict_resolved_at IS NOT NULL
                   AND (to_jsonb(NEW) - 'conflict_resolved_at' - 'conflict_resolved_by' - 'updated_at')
                       = (to_jsonb(OLD) - 'conflict_resolved_at' - 'conflict_resolved_by' - 'updated_at') THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'review.binance_trend_signals terminal row is frozen'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'review.binance_trend_signals is append-only (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_binance_trend_signals_guard "
    "ON review.binance_trend_signals",
    """
    CREATE TRIGGER trg_binance_trend_signals_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.binance_trend_signals
    FOR EACH ROW EXECUTE FUNCTION review.binance_trend_signals_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_binance_trend_signals_truncate "
    "ON review.binance_trend_signals",
    """
    CREATE TRIGGER trg_binance_trend_signals_truncate
    BEFORE TRUNCATE ON review.binance_trend_signals
    FOR EACH STATEMENT EXECUTE FUNCTION review.binance_trend_signals_guard()
    """,
    """
    CREATE OR REPLACE FUNCTION review.binance_trend_intents_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'reserved'
               OR NEW.executed_qty <> 0
               OR NEW.broker_order_id IS NOT NULL
               OR NEW.broker_status IS NOT NULL
               OR NEW.avg_price IS NOT NULL THEN
                RAISE EXCEPTION 'review.binance_trend_intents rows start as unsent reserved'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.client_order_id IS DISTINCT FROM OLD.client_order_id
               OR NEW.signal_key IS DISTINCT FROM OLD.signal_key
               OR NEW.leg_key IS DISTINCT FROM OLD.leg_key
               OR NEW.side IS DISTINCT FROM OLD.side
               OR NEW.qty IS DISTINCT FROM OLD.qty
               OR NEW.reduce_only IS DISTINCT FROM OLD.reduce_only
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'review.binance_trend_intents request is immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NOT (CASE OLD.state
                WHEN 'reserved' THEN NEW.state IN ('reserved','sending','rejected')
                WHEN 'sending' THEN NEW.state IN
                    ('sending','acknowledged','evidenced','settled','uncertain','rejected')
                WHEN 'acknowledged' THEN NEW.state IN
                    ('acknowledged','evidenced','settled','uncertain')
                WHEN 'evidenced' THEN NEW.state IN ('evidenced','settled')
                WHEN 'uncertain' THEN NEW.state IN
                    ('uncertain','evidenced','settled','rejected')
                ELSE FALSE
            END) THEN
                RAISE EXCEPTION 'review.binance_trend_intents state may only move forward (% -> %)',
                    OLD.state, NEW.state
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'review.binance_trend_intents is append-only (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_binance_trend_intents_guard "
    "ON review.binance_trend_intents",
    """
    CREATE TRIGGER trg_binance_trend_intents_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.binance_trend_intents
    FOR EACH ROW EXECUTE FUNCTION review.binance_trend_intents_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_binance_trend_intents_truncate "
    "ON review.binance_trend_intents",
    """
    CREATE TRIGGER trg_binance_trend_intents_truncate
    BEFORE TRUNCATE ON review.binance_trend_intents
    FOR EACH STATEMENT EXECUTE FUNCTION review.binance_trend_intents_guard()
    """,
)

DOWNGRADE_GUARD = """
    DO $$
    BEGIN
        LOCK TABLE review.binance_trend_signals, review.binance_trend_intents
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.binance_trend_signals)
           OR EXISTS (SELECT 1 FROM review.binance_trend_intents) THEN
            RAISE EXCEPTION 'binance trend state holds rows; export them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""

_SIGNAL_STATES = (
    "'observed','entry_reserved','holding','closed','blocked','uncertain',"
    "'conflict_blocked','conflict_rejected'"
)
_INTENT_STATES = (
    "'reserved','sending','acknowledged','evidenced','settled','uncertain','rejected'"
)


def upgrade() -> None:
    op.create_table(
        "binance_trend_signals",
        sa.Column("signal_key", sa.Text(), primary_key=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("decision_ts", sa.BigInteger(), nullable=False),
        sa.Column("signal_price_text", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("entry_notional_usdt", sa.Numeric(28, 12)),
        sa.Column("entry_client_order_id", sa.Text()),
        sa.Column("entry_qty", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column("entry_price", sa.Numeric(28, 12)),
        sa.Column("entered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_qty", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column(
            "realized_pnl_usdt", sa.Numeric(28, 12), nullable=False, server_default="0"
        ),
        sa.Column("fees_usdt", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column("exit_reason", sa.Text()),
        sa.Column("exit_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("conflict_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("conflict_detail", sa.Text()),
        sa.Column("conflict_resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("conflict_resolved_by", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_binance_trend_signal_correlation"
        ),
        sa.CheckConstraint(
            "side IN ('BUY','SELL')", name="ck_binance_trend_signal_side"
        ),
        sa.CheckConstraint(
            f"state IN ({_SIGNAL_STATES})", name="ck_binance_trend_signal_state"
        ),
        sa.CheckConstraint(
            "entry_notional_usdt IS NULL OR entry_notional_usdt > 0",
            name="ck_binance_trend_signal_notional_positive",
        ),
        sa.CheckConstraint(
            "state NOT IN ('entry_reserved','holding','uncertain') "
            "OR entry_notional_usdt IS NOT NULL",
            name="ck_binance_trend_signal_active_notional",
        ),
        sa.CheckConstraint(
            "(state IN ('conflict_blocked','conflict_rejected')) "
            "= (conflict_at IS NOT NULL)",
            name="ck_binance_trend_signal_conflict_record",
        ),
        sa.CheckConstraint(
            "conflict_detail IS NULL OR char_length(conflict_detail) <= 200",
            name="ck_binance_trend_signal_conflict_detail",
        ),
        sa.CheckConstraint(
            "(conflict_resolved_at IS NULL) = (conflict_resolved_by IS NULL)",
            name="ck_binance_trend_signal_resolution_pair",
        ),
        sa.CheckConstraint(
            "conflict_resolved_at IS NULL OR conflict_at IS NOT NULL",
            name="ck_binance_trend_signal_resolution_conflict",
        ),
        schema="review",
    )
    op.create_index(
        "ix_binance_trend_signals_symbol_state",
        "binance_trend_signals",
        ["symbol", "state"],
        schema="review",
    )
    op.create_table(
        "binance_trend_intents",
        sa.Column("client_order_id", sa.Text(), primary_key=True),
        sa.Column(
            "signal_key",
            sa.Text(),
            sa.ForeignKey("review.binance_trend_signals.signal_key"),
            nullable=False,
        ),
        sa.Column("leg_key", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(28, 12), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text()),
        sa.Column("broker_status", sa.Text()),
        sa.Column(
            "executed_qty", sa.Numeric(28, 12), nullable=False, server_default="0"
        ),
        sa.Column("avg_price", sa.Numeric(28, 12)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "signal_key", "leg_key", name="uq_binance_trend_intent_leg"
        ),
        sa.CheckConstraint(
            "side IN ('BUY','SELL')", name="ck_binance_trend_intent_side"
        ),
        sa.CheckConstraint(
            f"state IN ({_INTENT_STATES})", name="ck_binance_trend_intent_state"
        ),
        sa.CheckConstraint("qty > 0", name="ck_binance_trend_intent_qty_positive"),
        sa.CheckConstraint(
            "executed_qty >= 0", name="ck_binance_trend_intent_executed_nonnegative"
        ),
        schema="review",
    )
    op.create_index(
        "ix_binance_trend_intents_signal",
        "binance_trend_intents",
        ["signal_key"],
        schema="review",
    )
    for statement in GUARD_DDL:
        op.execute(statement)
    # The Stage 4 role may not exist in dev and CI databases (hard rule 18).
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT, UPDATE ON review.binance_trend_signals, "
        "review.binance_trend_intents TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.drop_table("binance_trend_intents", schema="review")
    op.drop_table("binance_trend_signals", schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.binance_trend_intents_guard()")
    op.execute("DROP FUNCTION IF EXISTS review.binance_trend_signals_guard()")
