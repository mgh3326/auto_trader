"""add Alpaca Paper stale preview cleanup-required state

Revision ID: 9b7c6d5e4f32
Revises: 4a26ddf34248
Create Date: 2026-05-05 13:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9b7c6d5e4f32"
down_revision: str | Sequence[str] | None = "4a26ddf34248"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "alpaca_paper_order_ledger"
_SCHEMA = "review"
_CONSTRAINT = "alpaca_paper_ledger_lifecycle_state"

_CANONICAL_STATES_WITH_CLEANUP = (
    "'planned','previewed','validated','submitted','filled',"
    "'position_reconciled','sell_validated','closed','final_reconciled','anomaly',"
    "'stale_preview_cleanup_required'"
)

_CANONICAL_STATES_WITHOUT_CLEANUP = (
    "'planned','previewed','validated','submitted','filled',"
    "'position_reconciled','sell_validated','closed','final_reconciled','anomaly'"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"lifecycle_state IN ({_CANONICAL_STATES_WITH_CLEANUP})",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    # Preserve rows on downgrade without deleting data. The cleanup-required state
    # is an operator-facing stale/anomaly marker, so map it to the existing
    # canonical anomaly bucket before restoring the old CHECK constraint.
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        "SET lifecycle_state = 'anomaly', "
        "    error_summary = COALESCE(error_summary, "
        "        'Downgraded from stale_preview_cleanup_required') "
        "WHERE lifecycle_state = 'stale_preview_cleanup_required'"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"lifecycle_state IN ({_CANONICAL_STATES_WITHOUT_CLEANUP})",
        schema=_SCHEMA,
    )
