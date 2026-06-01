"""ROB-412 repair prod watch/recommendation schema drift.

Revision ID: 20260602_rob412_repair
Revises: 2489d4709dec
Create Date: 2026-06-02

Production reached the ROB-406 merge head while the ROB-337/ROB-403
ancestor DDL was not physically present.  Keep this migration idempotent so it
is safe on databases where those ancestor revisions did run normally, and safe
on the drifted production database that only needs the missing additive shape.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_rob412_repair"
down_revision: str | Sequence[str] | None = "2489d4709dec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ALERT_OPERATOR_CONSTRAINTS = (
    "ck_investment_watch_alerts_operator",
    "ck_investment_watch_alerts_ck_investment_watch_alerts_operator",
)
_ALERT_COMBINE_CONSTRAINTS = (
    "ck_investment_watch_alerts_combine",
    "ck_investment_watch_alerts_ck_investment_watch_alerts_combine",
)
_EVENT_OPERATOR_CONSTRAINTS = (
    "ck_investment_watch_events_operator",
    "ck_investment_watch_events_ck_investment_watch_events_operator",
)


def _drop_constraints(table: str, names: tuple[str, ...]) -> None:
    for name in names:
        op.execute(f"ALTER TABLE review.{table} DROP CONSTRAINT IF EXISTS {name}")


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE review.investment_report_items
        ADD COLUMN IF NOT EXISTS watch_recommendation JSONB
        """
    )

    op.execute(
        """
        ALTER TABLE review.investment_watch_alerts
        ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)
        """
    )
    op.execute(
        """
        ALTER TABLE review.investment_watch_alerts
        ADD COLUMN IF NOT EXISTS conditions JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE review.investment_watch_alerts
        ADD COLUMN IF NOT EXISTS combine TEXT NOT NULL DEFAULT 'and'
        """
    )
    _drop_constraints("investment_watch_alerts", _ALERT_OPERATOR_CONSTRAINTS)
    op.execute(
        """
        ALTER TABLE review.investment_watch_alerts
        ADD CONSTRAINT ck_investment_watch_alerts_operator
        CHECK (operator IN ('above','below','between'))
        """
    )
    _drop_constraints("investment_watch_alerts", _ALERT_COMBINE_CONSTRAINTS)
    op.execute(
        """
        ALTER TABLE review.investment_watch_alerts
        ADD CONSTRAINT ck_investment_watch_alerts_combine
        CHECK (combine IN ('and'))
        """
    )

    op.execute(
        """
        ALTER TABLE review.investment_watch_events
        ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)
        """
    )
    _drop_constraints("investment_watch_events", _EVENT_OPERATOR_CONSTRAINTS)
    op.execute(
        """
        ALTER TABLE review.investment_watch_events
        ADD CONSTRAINT ck_investment_watch_events_operator
        CHECK (operator IN ('above','below','between'))
        """
    )


def downgrade() -> None:
    """No-op: repair is additive/idempotent and preserves production data."""
    return None
