"""rob-265 add investment_* tables (additive)

Revision ID: 20260518_rob265
Revises: f974ac12e573
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260518_rob265"
down_revision: str | None = "f974ac12e573"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


def upgrade() -> None:
    # ----------------------------------------------------------------
    # review.investment_reports
    # ----------------------------------------------------------------
    op.create_table(
        "investment_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column(
            "execution_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'advisory_only'"),
        ),
        sa.Column("created_by_profile", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=True),
        sa.Column("thesis_text", sa.Text(), nullable=True),
        sa.Column("no_action_note", sa.Text(), nullable=True),
        sa.Column(
            "market_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "portfolio_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "previous_report_uuid", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','decided','expired','superseded')",
            name="ck_investment_reports_status",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('advisory_only','mock_preview')",
            name="ck_investment_reports_execution_mode",
        ),
        sa.CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_investment_reports_account_scope",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_reports_market",
        ),
        sa.CheckConstraint(
            "market_session IS NULL OR market_session IN "
            "('regular','nxt','pre','post','24x7')",
            name="ck_investment_reports_market_session",
        ),
        sa.CheckConstraint(
            "account_scope IS DISTINCT FROM 'kis_live' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_live_advisory_only",
        ),
        sa.CheckConstraint(
            "market_session IS DISTINCT FROM 'nxt' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_nxt_advisory_only",
        ),
        sa.UniqueConstraint("report_uuid", name="uq_investment_reports_report_uuid"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_reports_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_market_session_created",
        "investment_reports",
        ["market", "market_session", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_status_created",
        "investment_reports",
        ["status", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_report_type_created",
        "investment_reports",
        ["report_type", "created_at"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_report_items
    # ----------------------------------------------------------------
    op.create_table(
        "investment_report_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("item_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column(
            "target_kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'asset'"),
        ),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "watch_condition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "trigger_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("[]"),
        ),
        sa.Column(
            "max_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_kind IN ('action','watch','risk')",
            name="ck_investment_report_items_item_kind",
        ),
        sa.CheckConstraint(
            "status IN ('proposed','approved','denied','deferred','activated','expired')",
            name="ck_investment_report_items_status",
        ),
        sa.CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_report_items_target_kind",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_investment_report_items_side",
        ),
        sa.CheckConstraint(
            "intent IN ('buy_review','sell_review','risk_review',"
            "'trend_recovery_review','rebalance_review')",
            name="ck_investment_report_items_intent",
        ),
        sa.CheckConstraint(
            "item_kind <> 'watch' OR watch_condition IS NOT NULL",
            name="ck_investment_report_items_watch_has_condition",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["review.investment_reports.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "item_uuid", name="uq_investment_report_items_item_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_report_items_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_report",
        "investment_report_items",
        ["report_id", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_kind_status",
        "investment_report_items",
        ["item_kind", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_symbol",
        "investment_report_items",
        ["symbol"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_report_item_decisions
    # ----------------------------------------------------------------
    op.create_table(
        "investment_report_item_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "approved_payload_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approve','deny','defer','skip','partial_approve')",
            name="ck_investment_report_item_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["review.investment_report_items.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "decision_uuid",
            name="uq_investment_report_item_decisions_decision_uuid",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_report_item_decisions_idempotency_key",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_item_decisions_item_created",
        "investment_report_item_decisions",
        ["item_id", "created_at"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_watch_alerts
    # ----------------------------------------------------------------
    op.create_table(
        "investment_watch_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("alert_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "source_report_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "source_item_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(20, 8), nullable=False),
        sa.Column("threshold_key", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("action_mode", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "trigger_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("[]"),
        ),
        sa.Column(
            "max_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "activated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active','triggered','expired','canceled')",
            name="ck_investment_watch_alerts_status",
        ),
        sa.CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_watch_alerts_target_kind",
        ),
        sa.CheckConstraint(
            "operator IN ('above','below')",
            name="ck_investment_watch_alerts_operator",
        ),
        sa.CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required')",
            name="ck_investment_watch_alerts_action_mode",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_watch_alerts_market",
        ),
        sa.UniqueConstraint(
            "alert_uuid", name="uq_investment_watch_alerts_alert_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_alerts_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_market_status",
        "investment_watch_alerts",
        ["market", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_status_valid_until",
        "investment_watch_alerts",
        ["status", "valid_until"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_source_report",
        "investment_watch_alerts",
        ["source_report_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_source_item",
        "investment_watch_alerts",
        ["source_item_uuid"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_watch_events
    # ----------------------------------------------------------------
    op.create_table(
        "investment_watch_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("event_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "source_report_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "source_item_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("current_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("threshold", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "scanner_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("follow_up_report_item_id", sa.BigInteger(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('notified','review_required','preview_attached',"
            "'expired','ignored','failed')",
            name="ck_investment_watch_events_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["review.investment_watch_alerts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["follow_up_report_item_id"],
            ["review.investment_report_items.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "event_uuid", name="uq_investment_watch_events_event_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_watch_events_idempotency_key",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_alert_created",
        "investment_watch_events",
        ["alert_id", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_source_report",
        "investment_watch_events",
        ["source_report_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_kst_date",
        "investment_watch_events",
        ["kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_outcome_created",
        "investment_watch_events",
        ["outcome", "created_at"],
        schema="review",
    )


def downgrade() -> None:
    # Drop in reverse FK order.
    op.drop_index(
        "ix_investment_watch_events_outcome_created",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_kst_date",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_source_report",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_alert_created",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_table("investment_watch_events", schema="review")

    op.drop_index(
        "ix_investment_watch_alerts_source_item",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_source_report",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_status_valid_until",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_market_status",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_table("investment_watch_alerts", schema="review")

    op.drop_index(
        "ix_investment_report_item_decisions_item_created",
        table_name="investment_report_item_decisions",
        schema="review",
    )
    op.drop_table("investment_report_item_decisions", schema="review")

    op.drop_index(
        "ix_investment_report_items_symbol",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_index(
        "ix_investment_report_items_kind_status",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_index(
        "ix_investment_report_items_report",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_table("investment_report_items", schema="review")

    op.drop_index(
        "ix_investment_reports_report_type_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_reports_status_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_reports_market_session_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_table("investment_reports", schema="review")
