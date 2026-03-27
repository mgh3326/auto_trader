"""add RSS news fields to news_articles

Revision ID: f3a4b5c6d7e8
Revises: f2c1e9b7a4d0
Create Date: 2026-03-27 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "f2c1e9b7a4d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Rename columns to match model attribute names
    op.alter_column(
        "news_articles",
        "content",
        new_column_name="article_content",
        existing_type=sa.Text(),
        existing_comment="기사 본문",
    )
    op.drop_index("ix_news_articles_published_at", table_name="news_articles")
    op.alter_column(
        "news_articles",
        "published_at",
        new_column_name="article_published_at",
        existing_type=sa.DateTime(),
        existing_comment="기사 발행일시",
    )
    op.create_index(
        "ix_news_articles_article_published_at",
        "news_articles",
        ["article_published_at"],
    )

    # 2. Add new RSS columns
    op.add_column(
        "news_articles",
        sa.Column(
            "feed_source",
            sa.String(length=50),
            nullable=True,
            comment="RSS 피드 소스 (mk_stock, yna_market 등)",
        ),
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="키워드 배열",
        ),
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "is_analyzed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="LLM 분석 완료 여부",
        ),
    )

    # 3. Make article_content nullable (RSS news may lack full content)
    op.alter_column(
        "news_articles",
        "article_content",
        existing_type=sa.Text(),
        nullable=True,
        existing_comment="기사 본문",
    )

    # 4. Add indexes
    op.create_index("ix_news_articles_feed_source", "news_articles", ["feed_source"])
    op.create_index(
        "ix_news_articles_keywords",
        "news_articles",
        ["keywords"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_news_articles_published_feed",
        "news_articles",
        ["article_published_at", "feed_source"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop new indexes
    op.drop_index("ix_news_articles_published_feed", table_name="news_articles")
    op.drop_index("ix_news_articles_keywords", table_name="news_articles")
    op.drop_index("ix_news_articles_feed_source", table_name="news_articles")

    # Restore article_content to NOT NULL
    op.alter_column(
        "news_articles",
        "article_content",
        existing_type=sa.Text(),
        nullable=False,
        existing_comment="기사 본문",
    )

    # Drop new columns
    op.drop_column("news_articles", "is_analyzed")
    op.drop_column("news_articles", "keywords")
    op.drop_column("news_articles", "feed_source")

    # Rename columns back to original
    op.drop_index("ix_news_articles_article_published_at", table_name="news_articles")
    op.alter_column(
        "news_articles",
        "article_published_at",
        new_column_name="published_at",
        existing_type=sa.DateTime(),
        existing_comment="기사 발행일시",
    )
    op.create_index(
        "ix_news_articles_published_at",
        "news_articles",
        ["published_at"],
    )
    op.alter_column(
        "news_articles",
        "article_content",
        new_column_name="content",
        existing_type=sa.Text(),
        existing_comment="기사 본문",
    )
