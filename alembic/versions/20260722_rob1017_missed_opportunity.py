"""Admit missed_opportunity into the retrospective trigger taxonomy.

Revision ID: 20260722_rob1017_missed
Revises: 20260722_rob1023_widen_runner
Create Date: 2026-07-22

The change is additive at the domain level: existing rows are untouched and
the CHECK is widened by one value. Both historical constraint names are dropped
because SQLAlchemy's naming convention produced a double-prefixed name in
production while persistent test databases may carry the canonical name.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260722_rob1017_missed"
down_revision: str = "20260722_rob1023_widen_runner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "review.trade_retrospectives"
_CANONICAL = "ck_trade_retrospectives_trigger_type"
_PRODUCTION = "ck_trade_retrospectives_ck_trade_retrospectives_trigger_type"
_TRIGGERS_BEFORE = (
    "fill",
    "partial_fill",
    "rejected_order",
    "cancelled",
    "expired",
    "thesis_change",
    "policy_violation",
    "stale_evidence",
    "guardrail_block",
    "stop_loss",
)
_TRIGGERS_AFTER = (*_TRIGGERS_BEFORE, "missed_opportunity")


def _replace_trigger_check(values: tuple[str, ...]) -> None:
    for name in (_CANONICAL, _PRODUCTION):
        op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')
    values_sql = ",".join(f"'{value}'" for value in values)
    op.execute(
        f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_PRODUCTION}" '
        f"CHECK (trigger_type IS NULL OR trigger_type IN ({values_sql}))"
    )


def upgrade() -> None:
    _replace_trigger_check(_TRIGGERS_AFTER)


def downgrade() -> None:
    _replace_trigger_check(_TRIGGERS_BEFORE)
