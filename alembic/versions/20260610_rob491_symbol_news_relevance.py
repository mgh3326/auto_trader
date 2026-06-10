"""add symbol_news_relevance (ROB-491)

Revision ID: 20260610_rob491
Revises: c07e44daf745
Create Date: 2026-06-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260610_rob491"
down_revision: str | None = "c07e44daf745"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_news_relevance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("feed_source", sa.String(length=40), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("relationship", sa.String(length=20), nullable=True),
        sa.Column("relevance", sa.String(length=10), nullable=True),
        sa.Column("price_relevance", sa.String(length=20), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("judged_by", sa.String(length=100), nullable=True),
        sa.Column("judged_at", sa.DateTime(), nullable=True),
        sa.Column("hints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["article_id"], ["news_articles.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "article_id",
            "market",
            "symbol",
            name="uq_symbol_news_relevance_article_market_symbol",
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_symbol_news_relevance_market",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'excluded')",
            name="ck_symbol_news_relevance_status",
        ),
        sa.CheckConstraint(
            "relationship IS NULL OR relationship IN "
            "('direct', 'material_indirect', 'incidental', 'unrelated')",
            name="ck_symbol_news_relevance_relationship",
        ),
        sa.CheckConstraint(
            "relevance IS NULL OR relevance IN ('high', 'medium', 'low')",
            name="ck_symbol_news_relevance_relevance",
        ),
        sa.CheckConstraint(
            "price_relevance IS NULL OR price_relevance IN "
            "('catalyst', 'explainer', 'background', 'none')",
            name="ck_symbol_news_relevance_price_relevance",
        ),
    )
    op.create_index(
        "ix_symbol_news_relevance_article_id",
        "symbol_news_relevance",
        ["article_id"],
    )
    op.create_index(
        "ix_symbol_news_relevance_market_symbol_status",
        "symbol_news_relevance",
        ["market", "symbol", "status"],
    )
    op.create_index(
        "ix_symbol_news_relevance_status_first_seen",
        "symbol_news_relevance",
        ["status", "first_seen_at"],
    )


def downgrade() -> None:
    op.drop_table("symbol_news_relevance")
