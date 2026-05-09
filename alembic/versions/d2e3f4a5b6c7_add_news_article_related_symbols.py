"""add news_article_related_symbols (ROB-153)

Revision ID: d2e3f4a5b6c7
Revises: b1c2d3e4
Create Date: 2026-05-09 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_article_related_symbols",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "article_id",
            sa.BigInteger(),
            sa.ForeignKey("news_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("matched_term", sa.String(length=120), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_news_article_related_symbols_market",
        ),
        sa.UniqueConstraint(
            "article_id",
            "market",
            "symbol",
            "source",
            name="uq_news_article_related_symbols_article_market_symbol_source",
        ),
    )
    op.create_index(
        "ix_news_article_related_symbols_article_id",
        "news_article_related_symbols",
        ["article_id"],
    )
    op.create_index(
        "ix_news_article_related_symbols_article_rank",
        "news_article_related_symbols",
        ["article_id", "rank"],
    )
    op.create_index(
        "ix_news_article_related_symbols_market_symbol_article",
        "news_article_related_symbols",
        ["market", "symbol", "article_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_news_article_related_symbols_market_symbol_article",
        table_name="news_article_related_symbols",
    )
    op.drop_index(
        "ix_news_article_related_symbols_article_rank",
        table_name="news_article_related_symbols",
    )
    op.drop_index(
        "ix_news_article_related_symbols_article_id",
        table_name="news_article_related_symbols",
    )
    op.drop_table("news_article_related_symbols")
