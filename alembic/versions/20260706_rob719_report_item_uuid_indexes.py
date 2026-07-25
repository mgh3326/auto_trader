"""ROB-719 report_item_uuid indexes

Revision ID: 20260706_rob719
Revises: 20260705_rob714
Create Date: 2026-07-06 00:00:00.000000

Additive btree indexes on ``review.trade_forecasts(report_item_uuid)`` and
``review.trade_retrospectives(report_item_uuid)`` so the ROB-715 report-detail
bundle batch-map join (``item_loop_links.py`` ``.in_(...)``) stays cheap as
ROB-714 place-time forecast volume grows. Purely additive — no column/constraint
change.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260706_rob719"
down_revision: str | Sequence[str] | None = "20260705_rob714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "trade_forecasts": "ix_trade_forecasts_report_item_uuid",
    "trade_retrospectives": "ix_trade_retrospectives_report_item_uuid",
}


def upgrade() -> None:
    for table, index in _INDEXES.items():
        op.create_index(index, table, ["report_item_uuid"], schema="review")


def downgrade() -> None:
    for table, index in _INDEXES.items():
        op.drop_index(index, table_name=table, schema="review")