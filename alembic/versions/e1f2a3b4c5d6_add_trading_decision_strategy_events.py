"""add trading_decision_strategy_events

Revision ID: e1f2a3b4c5d6
Revises: d3703007a676
Create Date: 2026-04-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d3703007a676"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trading_decision_strategy_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("normalized_summary", sa.Text(), nullable=True),
        sa.Column(
            "affected_markets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_sectors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_themes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_symbols",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "severity", sa.SmallInteger(), nullable=False, server_default=sa.text("2")
        ),
        sa.Column(
            "confidence",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("50"),
        ),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('user','hermes','tradingagents','news','market_data','scheduler')",
            name="trading_decision_strategy_events_source_allowed",
        ),
        sa.CheckConstraint(
            "event_type IN ('operator_market_event','earnings_event','macro_event',"
            "'sector_rotation','technical_break','risk_veto',"
            "'cash_budget_change','position_change')",
            name="trading_decision_strategy_events_type_allowed",
        ),
        sa.CheckConstraint(
            "severity BETWEEN 1 AND 5",
            name="trading_decision_strategy_events_severity_range",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 100",
            name="trading_decision_strategy_events_confidence_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_uuid"),
    )
    op.create_foreign_key(
        None,
        "trading_decision_strategy_events",
        "trading_decision_sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        None,
        "trading_decision_strategy_events",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_trading_decision_strategy_events_event_uuid"),
        "trading_decision_strategy_events",
        ["event_uuid"],
        unique=True,
    )
    op.create_index(
        op.f("ix_trading_decision_strategy_events_created_by_user_id"),
        "trading_decision_strategy_events",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_trading_decision_strategy_events_session_id_partial",
        "trading_decision_strategy_events",
        ["session_id"],
        unique=False,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "ix_trading_decision_strategy_events_user_created_at",
        "trading_decision_strategy_events",
        ["created_by_user_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_trading_decision_strategy_events_created_at",
        "trading_decision_strategy_events",
        [sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trading_decision_strategy_events_created_at",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        "ix_trading_decision_strategy_events_user_created_at",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        "ix_trading_decision_strategy_events_session_id_partial",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        op.f("ix_trading_decision_strategy_events_created_by_user_id"),
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        op.f("ix_trading_decision_strategy_events_event_uuid"),
        table_name="trading_decision_strategy_events",
    )
    op.drop_table("trading_decision_strategy_events")
