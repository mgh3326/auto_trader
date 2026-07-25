"""Widen research.backtest_runs.runner for the production R2 envelope.

Revision ID: 20260722_rob1023_widen_runner
Revises: 20260720_rob976_support
Create Date: 2026-07-22 10:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260722_rob1023_widen_runner"
down_revision: str = "20260720_rob976_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve exact runner lineage; widening is additive and data-safe."""
    op.alter_column(
        "backtest_runs",
        "runner",
        schema="research",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Refuse lossy narrowing if a post-upgrade runner exceeds 16 chars."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM research.backtest_runs
                WHERE char_length(runner) > 16
            ) THEN
                RAISE EXCEPTION
                    'cannot narrow research.backtest_runs.runner: value exceeds 16 chars';
            END IF;
        END
        $$
        """
    )
    op.alter_column(
        "backtest_runs",
        "runner",
        schema="research",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
