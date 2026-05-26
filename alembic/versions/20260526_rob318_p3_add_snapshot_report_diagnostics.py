"""rob-318 phase 3: snapshot_report_diagnostics on investment_reports (additive)

Revision ID: 20260526_rob318_p3
Revises: 20260525_rob315
Create Date: 2026-05-26

Adds 1 nullable JSONB column ``snapshot_report_diagnostics`` to
``review.investment_reports``. Purely additive and informational: it holds the
deterministic report-level diagnostics bundle
``{why_no_action, data_sufficiency_by_source, report_quality_summary}``
(ROB-318 Phase 3 PR-B). No CHECK, no index — it does not gate publishing.
Existing rows stay readable because the column is nullable (legacy reports
carry NULL).

See: docs/plans/2026-05-26-ROB-318-phase3-deterministic-report-diagnostics-plan.md
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260526_rob318_p3"
down_revision: str | None = "20260525_rob315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investment_reports",
        sa.Column(
            "snapshot_report_diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column(
        "investment_reports", "snapshot_report_diagnostics", schema="review"
    )
