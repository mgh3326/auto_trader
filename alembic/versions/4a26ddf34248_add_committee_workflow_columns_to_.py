"""add committee workflow columns to trading_decision_sessions

Revision ID: 4a26ddf34248
Revises: 1c9e8d7f6a5b
Create Date: 2026-05-05 07:32:35.007046
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "4a26ddf34248"
down_revision = "1c9e8d7f6a5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trading_decision_sessions",
        sa.Column("workflow_status", sa.Text(), nullable=True),
    )
    op.add_column(
        "trading_decision_sessions",
        sa.Column("automation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "trading_decision_sessions",
        sa.Column("account_mode", sa.Text(), nullable=True),
    )
    op.add_column(
        "trading_decision_sessions",
        sa.Column("artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "trading_decision_sessions_account_mode_allowed",
        "trading_decision_sessions",
        "account_mode IS NULL OR account_mode IN ('kis_mock','alpaca_paper','kis_live','db_simulated')",
    )
    op.create_check_constraint(
        "trading_decision_sessions_workflow_status_allowed",
        "trading_decision_sessions",
        "workflow_status IS NULL OR workflow_status IN ("
        "'created','evidence_generating','evidence_ready','debate_ready',"
        "'trader_draft_ready','risk_review_ready','auto_approved',"
        "'preview_ready','journal_ready','completed','failed_evidence',"
        "'failed_trader_draft','failed_risk_review','preview_blocked'"
        ")",
    )
    op.create_index(
        "ix_trading_decision_sessions_committee_workflow",
        "trading_decision_sessions",
        ["source_profile", "workflow_status"],
        unique=False,
        postgresql_where=sa.text("source_profile = 'committee_mock_paper'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trading_decision_sessions_committee_workflow",
        table_name="trading_decision_sessions",
    )
    op.drop_constraint(
        "trading_decision_sessions_workflow_status_allowed",
        "trading_decision_sessions",
        type_="check",
    )
    op.drop_constraint(
        "trading_decision_sessions_account_mode_allowed",
        "trading_decision_sessions",
        type_="check",
    )
    op.drop_column("trading_decision_sessions", "artifacts")
    op.drop_column("trading_decision_sessions", "account_mode")
    op.drop_column("trading_decision_sessions", "automation")
    op.drop_column("trading_decision_sessions", "workflow_status")
