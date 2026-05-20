"""rob-279 p1: add investment_stage_runs and investment_stage_artifacts

Revision ID: 20260520_rob279_p1
Revises: 20260520_rob274_p2
Create Date: 2026-05-20

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260520_rob279_p1"
down_revision: str | None = "20260520_rob274_p2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "investment_stage_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "snapshot_bundle_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column("generator_version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','blocked')",
            name=op.f("ck_investment_stage_runs_status"),
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_stage_runs_bundle_uuid",
        "investment_stage_runs",
        ["snapshot_bundle_uuid"],
        schema="review",
    )

    op.create_table(
        "investment_stage_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "artifact_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage_type", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("key_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("buy_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sell_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("risk_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("missing_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cited_snapshot_uuids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::uuid[]"),
        ),
        sa.Column("freshness_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name=op.f("ck_investment_stage_artifacts_verdict"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name=op.f("ck_investment_stage_artifacts_confidence_range"),
        ),
        sa.CheckConstraint(
            "stage_type IN ("
            "'market','news','portfolio_journal','watch_context','candidate_universe',"
            "'bull_reducer','bear_reducer','risk_review')",
            name=op.f("ck_investment_stage_artifacts_stage_type_v1"),
        ),
        sa.ForeignKeyConstraint(
            ["run_uuid"],
            ["review.investment_stage_runs.run_uuid"],
            name="fk_investment_stage_artifacts_run_uuid_investment_stage_runs",
            ondelete="CASCADE",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_stage_artifacts_run_stage",
        "investment_stage_artifacts",
        ["run_uuid", "stage_type"],
        unique=True,
        schema="review",
    )


def downgrade() -> None:
    op.drop_index("ix_investment_stage_artifacts_run_stage", table_name="investment_stage_artifacts", schema="review")
    op.drop_table("investment_stage_artifacts", schema="review")
    op.drop_index("ix_investment_stage_runs_bundle_uuid", table_name="investment_stage_runs", schema="review")
    op.drop_table("investment_stage_runs", schema="review")
