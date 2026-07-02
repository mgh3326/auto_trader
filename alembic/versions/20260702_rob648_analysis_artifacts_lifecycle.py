"""ROB-648 analysis artifact lifecycle fields

content_hash (server-computed, nullable + lazy backfill), version (in-place
bump), readiness_label (reduced advisory enum). All additive; existing
save/list/get stay backward compatible.

Revision ID: 20260702_rob648
Revises: 20260702_rob641
Create Date: 2026-07-02 06:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_rob648"
down_revision: str | None = "20260702_rob641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # content_hash: nullable so pre-existing rows are not backfilled in-migration
    # — the next save() computes and stores it (lazy backfill, ROB-648).
    op.add_column(
        "analysis_artifacts",
        sa.Column("content_hash", sa.Text(), nullable=True),
        schema="review",
    )
    # version: additive Integer, in-place bump on correlation_id re-save when the
    # payload content actually changes. server_default 1 backfills existing rows.
    op.add_column(
        "analysis_artifacts",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        schema="review",
    )
    # readiness_label: caller-declared advisory. Reduced hard enum (tradingcodex
    # lane labels excluded) — NULL allowed.
    op.add_column(
        "analysis_artifacts",
        sa.Column("readiness_label", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "ck_analysis_artifacts_readiness_label",
        "analysis_artifacts",
        "readiness_label IS NULL OR readiness_label IN ("
        "'screen_grade','not_decision_ready',"
        "'ready_for_order_review','blocked'"
        ")",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_analysis_artifacts_readiness_label",
        "analysis_artifacts",
        schema="review",
        type_="check",
    )
    op.drop_column("analysis_artifacts", "readiness_label", schema="review")
    op.drop_column("analysis_artifacts", "version", schema="review")
    op.drop_column("analysis_artifacts", "content_hash", schema="review")
