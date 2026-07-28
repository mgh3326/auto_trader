"""ROB-1109 restore the active watch intent ledger.

Revision ID: 20260728_rob1109_watch_intent
Revises: 20260728_rob1103_watch_links
Create Date: 2026-07-28 00:00:00.000000

ROB-265 Plan 5 misclassified ``review.watch_order_intent_ledger`` as a
legacy action-center table.  The model and ROB-402 auto-execution path remain
active, so this migration corrects that classification; it does not roll back
the other ROB-265 removals.

The table definition mirrors both ``daf4130b13ce`` and
``WatchOrderIntentLedger``.  The watch-event outcome constraint also gains a
``pending`` state so auto-execution cannot be recorded as successful before
the executor returns evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

revision: str = "20260728_rob1109_watch_intent"
down_revision: str | Sequence[str] | None = "20260728_rob1103_watch_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_EVENT_TABLE = "investment_watch_events"
_EVENT_OUTCOME_CONSTRAINT = "ck_investment_watch_events_outcome"
_EVENT_OUTCOME_LEGACY_CONSTRAINT = (
    "ck_investment_watch_events_ck_investment_watch_events_outcome"
)
_OUTCOME_WITH_PENDING = (
    "outcome IN ('notified','review_required','preview_attached','pending',"
    "'executed','expired','ignored','failed')"
)
_OUTCOME_WITHOUT_PENDING = (
    "outcome IN ('notified','review_required','preview_attached',"
    "'executed','expired','ignored','failed')"
)


def _replace_event_outcome_constraint(expression: str) -> None:
    # Older migrations ran through the metadata naming convention and could
    # persist the doubled name. Raw DDL avoids applying that convention again
    # while accepting both deployed variants.
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_EVENT_TABLE} "
        f'DROP CONSTRAINT IF EXISTS "{_EVENT_OUTCOME_CONSTRAINT}"'
    )
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_EVENT_TABLE} "
        f'DROP CONSTRAINT IF EXISTS "{_EVENT_OUTCOME_LEGACY_CONSTRAINT}"'
    )
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_EVENT_TABLE} "
        f'ADD CONSTRAINT "{_EVENT_OUTCOME_CONSTRAINT}" CHECK ({expression})'
    )


def upgrade() -> None:
    op.create_table(
        "watch_order_intent_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("condition_type", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(18, 8), nullable=False),
        sa.Column("threshold_key", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("execution_source", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("limit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("notional", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("notional_krw_input", sa.Numeric(18, 2), nullable=True),
        sa.Column("max_notional_krw", sa.Numeric(18, 2), nullable=True),
        sa.Column("notional_krw_evaluated", sa.Numeric(18, 2), nullable=True),
        sa.Column("fx_usd_krw_used", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "execution_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "blocking_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("blocked_by", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "preview_line",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("triggered_value", sa.Numeric(18, 8), nullable=True),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "correlation_id",
            name="uq_watch_intent_correlation_id",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')",
            name="watch_intent_ledger_side",
        ),
        sa.CheckConstraint(
            "account_mode = 'kis_mock'",
            name="watch_intent_ledger_account_mode",
        ),
        sa.CheckConstraint(
            "execution_source = 'watch'",
            name="watch_intent_ledger_execution_source",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        schema=_SCHEMA,
    )

    op.create_index(
        "ix_watch_intent_kst_date",
        "watch_order_intent_ledger",
        ["kst_date"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_watch_intent_market_symbol",
        "watch_order_intent_ledger",
        ["market", "symbol"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_watch_intent_state_created_at",
        "watch_order_intent_ledger",
        ["lifecycle_state", "created_at"],
        schema=_SCHEMA,
    )
    op.create_index(
        "uq_watch_intent_previewed_idempotency",
        "watch_order_intent_ledger",
        ["idempotency_key"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=text("lifecycle_state = 'previewed'"),
    )

    _replace_event_outcome_constraint(_OUTCOME_WITH_PENDING)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM review.watch_order_intent_ledger
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: watch_order_intent_ledger contains audit rows';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM review.investment_watch_events
                WHERE outcome = 'pending'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: investment_watch_events contains pending outcomes';
            END IF;
        END
        $$;
        """
    )

    _replace_event_outcome_constraint(_OUTCOME_WITHOUT_PENDING)
    op.drop_index(
        "uq_watch_intent_previewed_idempotency",
        table_name="watch_order_intent_ledger",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_watch_intent_state_created_at",
        table_name="watch_order_intent_ledger",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_watch_intent_market_symbol",
        table_name="watch_order_intent_ledger",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_watch_intent_kst_date",
        table_name="watch_order_intent_ledger",
        schema=_SCHEMA,
    )
    op.drop_table("watch_order_intent_ledger", schema=_SCHEMA)
