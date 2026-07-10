"""ROB-811 add naver_research_detail_cache

Revision ID: 20260710_rob811_detail_cache
Revises: 20260707_rob757_toss_fill_poller
Create Date: 2026-07-10 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260710_rob811_detail_cache"
down_revision = "20260707_rob757_toss_fill_poller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "naver_research_detail_cache",
        sa.Column("nid", sa.Text(), nullable=False),
        sa.Column("target_price", sa.Numeric(), nullable=True),
        sa.Column("rating", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("nid", name="pk_naver_research_detail_cache"),
    )


def downgrade() -> None:
    op.drop_table("naver_research_detail_cache")