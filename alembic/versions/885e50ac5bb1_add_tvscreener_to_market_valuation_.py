"""Add tvscreener to market_valuation_snapshots source enum

Revision ID: 885e50ac5bb1
Revises: 20260615_rob575
Create Date: 2026-06-16 14:02:48.783995

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "885e50ac5bb1"
down_revision: str | Sequence[str] | None = "20260615_rob575"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo', 'toss_openapi', 'tvscreener')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo', 'toss_openapi')",
    )
