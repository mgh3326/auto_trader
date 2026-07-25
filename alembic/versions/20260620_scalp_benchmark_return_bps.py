"""add benchmark_return_bps to scalping_daily_reviews

Revision ID: 20260620_scalp_benchmark
Revises: 885e50ac5bb1
Create Date: 2026-06-20

Phase 1 — daily buy&hold benchmark column for the demo scalping review.
Additive nullable; safe forward/backward.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260620_scalp_benchmark"
down_revision: str | Sequence[str] | None = "885e50ac5bb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scalping_daily_reviews",
        sa.Column("benchmark_return_bps", sa.Numeric(12, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scalping_daily_reviews", "benchmark_return_bps")
