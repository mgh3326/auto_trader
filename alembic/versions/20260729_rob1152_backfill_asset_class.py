"""ROB-1152 backfill alpaca_paper_order_ledger.execution_asset_class

No schema change. ``execution_asset_class`` already exists (added by
c1d2e3f4a5b6, 2026-05-03) and remains a nullable Text column — this
migration adds no column, no constraint, and drops nothing.

Backfill derives the value from the SAME ROW's ``instrument_type`` column
(NOT NULL, enum-constrained to a fixed vocabulary). For the alpaca_paper
ledger specifically, ``instrument_type`` is verified (ROB-1152 investigation,
2026-07-29) to take only 'crypto' or 'equity_us' — a 1:1 mapping the service
layer itself already enforces as an invariant (see
``app/services/paper_approval_packet.py`` ``expected_instrument_type``).
This is a same-row column derivation, not a symbol-format guess.

Only NULL rows are touched; already-populated rows (execution_asset_class
IS NOT NULL) are left untouched. No row is deleted. This migration is a
no-op if run against a database with no NULL execution_asset_class rows.

Revision ID: 20260729_rob1152
Revises: 20260728_rob1109_watch_intent
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "20260729_rob1152"
down_revision = "20260728_rob1109_watch_intent"
branch_labels = None
depends_on = None

_TABLE = "alpaca_paper_order_ledger"
_SCHEMA = "review"


def upgrade() -> None:
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET execution_asset_class = CASE instrument_type "
        f"  WHEN 'crypto' THEN 'crypto' "
        f"  WHEN 'equity_us' THEN 'us_equity' "
        f"END "
        f"WHERE execution_asset_class IS NULL "
        f"  AND instrument_type IN ('crypto', 'equity_us')"
    )


def downgrade() -> None:
    # Deliberate no-op: reverting a backfill would destroy the derived
    # execution_asset_class values for rows that had none before, with no
    # way to distinguish "backfilled by this migration" from "populated by
    # normal writes after this migration ran". A backfill is not something
    # that should be reversed; NULL is the exceptional state, not the goal.
    pass
