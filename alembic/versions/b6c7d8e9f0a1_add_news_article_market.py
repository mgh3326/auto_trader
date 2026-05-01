"""add market scope to news articles

Revision ID: b6c7d8e9f0a1
Revises: e2f3a4b5c6d7
Create Date: 2026-05-01 14:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column(
            "market",
            sa.String(length=20),
            nullable=False,
            server_default="kr",
            comment="Market scope for the article (kr, us, crypto)",
        ),
    )
    op.create_index("ix_news_articles_market", "news_articles", ["market"])
    op.create_index(
        "ix_news_articles_market_published",
        "news_articles",
        ["market", "article_published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_news_articles_market_published", table_name="news_articles")
    op.drop_index("ix_news_articles_market", table_name="news_articles")
    op.drop_column("news_articles", "market")
