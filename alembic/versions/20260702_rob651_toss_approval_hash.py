"""add approval_hash to toss_live_order_ledger (ROB-651)

Revision ID: 20260702_rob651
Revises: 20260702_rob650
Create Date: 2026-07-02

Additive nullable column storing the content digest (``p6a-<16hex>``) of the
placed order's canonical payload — the approval-hash binding between
toss_preview_order and toss_place_order (ROB-651 P6-A).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_rob651"
down_revision: str | Sequence[str] | None = "20260702_rob650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("approval_hash", sa.Text(), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column(
        "toss_live_order_ledger", "approval_hash", schema="review"
    )
