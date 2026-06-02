# alembic/versions/20260603_rob423_news_citation_tables.py
"""rob-423 add investment_report_news_* tables (additive)

Revision ID: 20260603_rob423_news
Revises: 20260602_rob412_main_merge
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260603_rob423_news"
down_revision: str | None = "20260602_rob412_main_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "investment_report_news_fetch_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column(
            "returned_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "used_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("freshness_policy", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response_stored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok','empty','unavailable','error')",
            name="ck_investment_report_news_fetch_runs_status",
        ),
        sa.UniqueConstraint(
            "run_uuid", name="uq_investment_report_news_fetch_runs_run_uuid"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_news_fetch_runs_report_uuid",
        "investment_report_news_fetch_runs",
        ["report_uuid"],
        schema="review",
    )

    op.create_table(
        "investment_report_news_citations",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("citation_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("section_key", sa.Text(), nullable=True),
        sa.Column("fetch_run_id", sa.BigInteger(), nullable=True),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_article_id", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary_snapshot", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("relevance", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("decision_impact", sa.Text(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column(
            "metadata_json",
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
        sa.CheckConstraint(
            "relevance IN ('direct','related','market_context','crypto_context')",
            name="ck_investment_report_news_citations_relevance",
        ),
        sa.CheckConstraint(
            "role IN ('catalyst','risk','confirmation','contradiction','neutral','noise')",
            name="ck_investment_report_news_citations_role",
        ),
        sa.CheckConstraint(
            "decision_impact IN ('strengthen_buy','weaken_buy','strengthen_sell',"
            "'weaken_sell','hold_watch','no_action')",
            name="ck_investment_report_news_citations_decision_impact",
        ),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["review.investment_report_news_fetch_runs.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "citation_uuid",
            name="uq_investment_report_news_citations_citation_uuid",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_news_citations_report_uuid",
        "investment_report_news_citations",
        ["report_uuid"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_report_news_citations_report_uuid",
        table_name="investment_report_news_citations",
        schema="review",
    )
    op.drop_table("investment_report_news_citations", schema="review")
    op.drop_index(
        "ix_investment_report_news_fetch_runs_report_uuid",
        table_name="investment_report_news_fetch_runs",
        schema="review",
    )
    op.drop_table("investment_report_news_fetch_runs", schema="review")
