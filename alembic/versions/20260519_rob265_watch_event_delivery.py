"""rob-265 add delivery tracking to investment_watch_events

Plan 4 hardening — adds delivery_status / delivery_reason /
delivered_at / delivery_attempts so a failed or skipped Hermes
review-trigger delivery is auditable and re-attemptable instead of
silently consuming the alert.

Revision ID: 20260519_rob265_delivery
Revises: 20260518_rob265
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260519_rob265_delivery"
down_revision: str | None = "20260518_rob265"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investment_watch_events",
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        schema="review",
    )
    op.add_column(
        "investment_watch_events",
        sa.Column("delivery_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_watch_events",
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_watch_events",
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema="review",
    )
    op.create_check_constraint(
        "ck_investment_watch_events_delivery_status",
        "investment_watch_events",
        "delivery_status IN ('pending','delivered','skipped','failed')",
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_delivery_status_created",
        "investment_watch_events",
        ["delivery_status", "created_at"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_watch_events_delivery_status_created",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_constraint(
        "ck_investment_watch_events_delivery_status",
        "investment_watch_events",
        type_="check",
        schema="review",
    )
    op.drop_column("investment_watch_events", "delivery_attempts", schema="review")
    op.drop_column("investment_watch_events", "delivered_at", schema="review")
    op.drop_column("investment_watch_events", "delivery_reason", schema="review")
    op.drop_column("investment_watch_events", "delivery_status", schema="review")
