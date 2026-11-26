"""Add source timing, article identity and relation evidence to news.

Revision ID: 20261127_hk6302
Revises: 20261126_hk6674
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261127_hk6302"
down_revision: str | None = "20261126_hk6674"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("news_articles", sa.Column("published_offset", sa.String(6)))
    op.add_column("news_articles", sa.Column("revised_at", sa.DateTime(timezone=True)))
    op.add_column("news_articles", sa.Column("provider_article_id", sa.Text()))
    op.add_column(
        "news_articles",
        sa.Column("source_document_ids", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.add_column(
        "news_article_related_symbols", sa.Column("relation_type", sa.String(10))
    )
    op.add_column(
        "news_article_related_symbols",
        sa.Column("relation_evidence", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.create_check_constraint(
        "ck_news_article_related_symbols_relation_type",
        "news_article_related_symbols",
        "relation_type IS NULL OR relation_type IN ('direct', 'sector', 'market')",
    )
    op.execute(
        "UPDATE news_article_related_symbols "
        "SET relation_type = 'direct', relation_evidence = "
        "jsonb_build_object('kind', 'legacy_candidate', 'source', source, "
        "'matched_term', matched_term, 'candidate', COALESCE(raw, '{}'::jsonb)) "
        "WHERE relation_type IS NULL OR relation_evidence IS NULL"
    )
    op.alter_column("news_article_related_symbols", "relation_type", nullable=False)
    op.alter_column("news_article_related_symbols", "relation_evidence", nullable=False)
    op.create_check_constraint(
        "ck_news_article_related_symbols_relation_evidence",
        "news_article_related_symbols",
        "relation_evidence <> '{}'::jsonb",
    )
    op.add_column(
        "research_reports",
        sa.Column("first_received_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE research_reports SET first_received_at = created_at "
        "WHERE first_received_at IS NULL"
    )
    op.alter_column("research_reports", "first_received_at", nullable=False)
    op.add_column("research_reports", sa.Column("published_date", sa.Date()))
    op.add_column(
        "research_reports", sa.Column("revised_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("research_reports", "published_date")
    op.drop_column("research_reports", "revised_at")
    op.alter_column("research_reports", "first_received_at", nullable=True)
    op.drop_column("research_reports", "first_received_at")
    op.drop_constraint(
        "ck_news_article_related_symbols_relation_evidence",
        "news_article_related_symbols",
        type_="check",
    )
    op.drop_constraint(
        "ck_news_article_related_symbols_relation_type",
        "news_article_related_symbols",
        type_="check",
    )
    op.alter_column("news_article_related_symbols", "relation_evidence", nullable=True)
    op.alter_column("news_article_related_symbols", "relation_type", nullable=True)
    op.drop_column("news_article_related_symbols", "relation_evidence")
    op.drop_column("news_article_related_symbols", "relation_type")
    op.drop_column("news_articles", "source_document_ids")
    op.drop_column("news_articles", "provider_article_id")
    op.drop_column("news_articles", "revised_at")
    op.drop_column("news_articles", "published_offset")
