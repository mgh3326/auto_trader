"""add research run tables

Revision ID: d34d6def084b
Revises: ce5d470cc894
Create Date: 2026-04-28 21:17:11.532211

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d34d6def084b"
down_revision: Union[str, Sequence[str], None] = "ce5d470cc894"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

instrument_type_enum = postgresql.ENUM(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    # 1. research_runs
    op.create_table(
        "research_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("market_scope", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("source_profile", sa.Text(), nullable=False),
        sa.Column("strategy_name", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("market_brief", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_freshness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "source_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "advisory_links",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed', 'archived')", name="research_runs_status_allowed"),
        sa.CheckConstraint(
            "stage IN ('preopen', 'intraday', 'nxt_aftermarket', 'us_open')",
            name="research_runs_stage_allowed",
        ),
        sa.CheckConstraint(
            "market_scope IN ('kr', 'us', 'crypto')",
            name="research_runs_market_scope_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_uuid"),
    )
    op.create_index(
        "ix_research_runs_user_generated_at",
        "research_runs",
        ["user_id", sa.text("generated_at DESC")],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_research_runs_market_stage_generated_at",
        "research_runs",
        ["market_scope", "stage", sa.text("generated_at DESC")],
    )
    op.create_index(op.f("ix_research_runs_run_uuid"), "research_runs", ["run_uuid"], unique=True)
    op.create_index(op.f("ix_research_runs_user_id"), "research_runs", ["user_id"], unique=False)
    op.create_foreign_key(
        None, "research_runs", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    # 2. research_run_candidates
    op.create_table(
        "research_run_candidates",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("research_run_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False, server_default="none"),
        sa.Column("candidate_kind", sa.Text(), nullable=False),
        sa.Column("proposed_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("proposed_qty", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("source_freshness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('buy','sell','none')", name="research_run_candidates_side_allowed"),
        sa.CheckConstraint(
            "candidate_kind IN ('pending_order','holding','screener_hit','proposed','other')",
            name="research_run_candidates_kind_allowed",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 100)",
            name="research_run_candidates_confidence_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_uuid"),
    )
    op.create_index(
        op.f("ix_research_run_candidates_candidate_uuid"),
        "research_run_candidates",
        ["candidate_uuid"],
        unique=True,
    )
    op.create_index(
        op.f("ix_research_run_candidates_research_run_id"),
        "research_run_candidates",
        ["research_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_candidates_symbol"),
        "research_run_candidates",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_run_candidates_run_symbol",
        "research_run_candidates",
        ["research_run_id", "symbol"],
        unique=False,
    )
    op.create_foreign_key(
        None,
        "research_run_candidates",
        "research_runs",
        ["research_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 3. research_run_pending_reconciliations
    op.create_table(
        "research_run_pending_reconciliations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("research_run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("nxt_classification", sa.Text(), nullable=True),
        sa.Column("nxt_actionable", sa.Boolean(), nullable=True),
        sa.Column("gap_pct", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "decision_support",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('buy','sell')", name="research_run_pending_reconciliations_side_allowed"),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="research_run_pending_reconciliations_market_allowed",
        ),
        sa.CheckConstraint(
            "classification IN ("
            "'maintain','near_fill','too_far','chasing_risk',"
            "'data_mismatch','kr_pending_non_nxt','unknown_venue','unknown')",
            name="research_run_pending_reconciliations_classification_allowed",
        ),
        sa.CheckConstraint(
            "nxt_classification IS NULL OR nxt_classification IN ("
            "'buy_pending_at_support','buy_pending_too_far','buy_pending_actionable',"
            "'sell_pending_near_resistance','sell_pending_too_optimistic',"
            "'sell_pending_actionable','non_nxt_pending_ignore_for_nxt',"
            "'holding_watch_only','data_mismatch_requires_review','unknown')",
            name="research_run_pending_reconciliations_nxt_classification_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_research_run_id"),
        "research_run_pending_reconciliations",
        ["research_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_order_id"),
        "research_run_pending_reconciliations",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_symbol"),
        "research_run_pending_reconciliations",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_run_pending_reconciliations_run_symbol",
        "research_run_pending_reconciliations",
        ["research_run_id", "symbol"],
        unique=False,
    )
    op.create_foreign_key(
        None,
        "research_run_pending_reconciliations",
        "research_runs",
        ["research_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "research_run_pending_reconciliations",
        "research_run_candidates",
        ["candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_table("research_run_pending_reconciliations")
    op.drop_table("research_run_candidates")
    op.drop_table("research_runs")
