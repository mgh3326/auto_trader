"""rob-269 phase 3: snapshot metadata on investment_reports (additive)

Revision ID: 20260519_rob269_p3
Revises: 20260519_rob269_p1
Create Date: 2026-05-19

Adds 6 nullable columns + 1 CHECK constraint + 1 index to
``review.investment_reports``. All additive. Existing rows stay readable
because every new column is nullable and the CHECK has a legacy clause
(``snapshot_freshness_summary IS NULL``) that lets historical reports
without snapshot metadata pass.

The CHECK is the Decision 4 layer (i) of the 3-layer stale gate — it
guarantees that a row whose ``snapshot_freshness_summary.overall`` is
``hard_stale`` / ``failed`` / ``unavailable`` cannot be ``published``.
The service-layer gate is flag-gated by
``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED``; the DB CHECK is
unconditional.

See: docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md §3e and
docs/superpowers/plans/2026-05-19-rob-269-phase-3-report-generator.md §1.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260519_rob269_p3"
down_revision: str | None = "20260519_rob269_p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investment_reports",
        sa.Column("snapshot_bundle_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_reports",
        sa.Column("snapshot_policy_version", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_reports",
        sa.Column(
            "snapshot_coverage_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_reports",
        sa.Column(
            "snapshot_freshness_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_reports",
        sa.Column(
            "source_conflicts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_reports",
        sa.Column(
            "unavailable_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )

    op.create_index(
        "ix_investment_reports_snapshot_bundle_uuid",
        "investment_reports",
        ["snapshot_bundle_uuid"],
        schema="review",
    )

    op.create_check_constraint(
        "ck_investment_reports_no_published_on_hard_stale",
        "investment_reports",
        # Legacy clause: rows without snapshot metadata are exempt so
        # ROB-265 reports keep working unchanged.
        # Live clause: when snapshot_freshness_summary is set, its
        # ``overall`` key must be a non-stale status before publish.
        "status <> 'published' "
        "OR snapshot_freshness_summary IS NULL "
        "OR (snapshot_freshness_summary->>'overall') IN ('fresh','soft_stale','partial')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_investment_reports_no_published_on_hard_stale",
        "investment_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_reports_snapshot_bundle_uuid",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_column("investment_reports", "unavailable_sources", schema="review")
    op.drop_column("investment_reports", "source_conflicts", schema="review")
    op.drop_column("investment_reports", "snapshot_freshness_summary", schema="review")
    op.drop_column("investment_reports", "snapshot_coverage_summary", schema="review")
    op.drop_column("investment_reports", "snapshot_policy_version", schema="review")
    op.drop_column("investment_reports", "snapshot_bundle_uuid", schema="review")
