"""Preserve provider publication time and first receipt for news articles.

Revision ID: 20261126_hk6674
Revises: 20261126_hk6663_movers_val
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261126_hk6674"
down_revision: str | None = "20261126_hk6663_movers_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_articles", sa.Column("published_at_raw", sa.Text(), nullable=True)
    )
    op.add_column(
        "news_articles",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_articles", sa.Column("published_precision", sa.String(10), nullable=True)
    )
    op.add_column(
        "news_articles", sa.Column("published_at_reason", sa.String(40), nullable=True)
    )
    op.add_column(
        "news_articles",
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("news_articles", "first_received_at")
    op.drop_column("news_articles", "published_at_reason")
    op.drop_column("news_articles", "published_precision")
    op.drop_column("news_articles", "published_at")
    op.drop_column("news_articles", "published_at_raw")
