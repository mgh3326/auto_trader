"""ROB-734 mirror counterfactual metadata and retrospective account key."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260706_rob734"
down_revision: str | Sequence[str] | None = "20260706_rob719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("mirror_cohort", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("mirror_source_bucket", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_report_item_uuid",
        "kis_mock_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_mirror_cohort_created",
        "kis_mock_order_ledger",
        ["mirror_cohort", "created_at"],
        schema="review",
    )
    op.create_check_constraint(
        "ck_kis_mock_ledger_mirror_cohort",
        "kis_mock_order_ledger",
        "mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual')",
        schema="review",
    )
    op.create_check_constraint(
        "ck_kis_mock_ledger_mirror_source_bucket",
        "kis_mock_order_ledger",
        "mirror_source_bucket IS NULL OR mirror_source_bucket IN "
        "('place_original','watch_trigger','deferred_min_rung')",
        schema="review",
    )
    op.drop_constraint(
        "uq_trade_retrospectives_correlation_id",
        "trade_retrospectives",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_trade_retrospectives_correlation_account",
        "trade_retrospectives",
        ["correlation_id", "account_mode"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_trade_retrospectives_correlation_account",
        "trade_retrospectives",
        schema="review",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_trade_retrospectives_correlation_id",
        "trade_retrospectives",
        ["correlation_id"],
        schema="review",
    )
    op.drop_constraint(
        "ck_kis_mock_ledger_mirror_source_bucket",
        "kis_mock_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_constraint(
        "ck_kis_mock_ledger_mirror_cohort",
        "kis_mock_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_index(
        "ix_kis_mock_ledger_mirror_cohort_created",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_kis_mock_ledger_report_item_uuid",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_column("kis_mock_order_ledger", "mirror_source_bucket", schema="review")
    op.drop_column("kis_mock_order_ledger", "mirror_cohort", schema="review")
    op.drop_column("kis_mock_order_ledger", "report_item_uuid", schema="review")
