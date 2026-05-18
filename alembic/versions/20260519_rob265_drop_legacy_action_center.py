"""rob-265 plan 5 — drop legacy analysis_* and watch_order_intent_ledger tables

Destructive cut closing out ROB-265. The legacy action-center surface
(analysis_reports / analysis_stage_results / analysis_order_candidates)
and the watch order intent audit ledger are removed. The replacement
schema is the ``investment_*`` family added in earlier ROB-265 plans.

The runbook PR notes carry pre-drop row counts. ``downgrade()`` is
intentionally unsupported — this is a one-way cut.

Revision ID: 20260519_rob265_drop_legacy
Revises: 20260519_rob265_delivery
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260519_rob265_drop_legacy"
down_revision: str | None = "20260519_rob265_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CASCADE drops dependent indexes / constraints automatically.
    # Tables are FK-internal (analysis_stage_results + analysis_order_candidates
    # both FK → analysis_reports); no external FK references after Plan 5.
    op.execute("DROP TABLE IF EXISTS review.analysis_order_candidates CASCADE")
    op.execute("DROP TABLE IF EXISTS review.analysis_stage_results CASCADE")
    op.execute("DROP TABLE IF EXISTS review.analysis_reports CASCADE")
    op.execute("DROP TABLE IF EXISTS review.watch_order_intent_ledger CASCADE")


def downgrade() -> None:
    raise NotImplementedError(
        "ROB-265 plan 5 destructive cut — legacy analysis_* and "
        "watch_order_intent_ledger tables are intentionally not recoverable "
        "from this migration. Restore from backup if a rollback is needed."
    )
