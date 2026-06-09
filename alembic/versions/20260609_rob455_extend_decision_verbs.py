"""ROB-455 — extend the decision-verb CHECK with order-lifecycle verbs.

Adds ``cancel`` and ``reprice`` to
``ck_investment_report_item_decisions_decision`` so an operator can record an
adjustment outcome first-class instead of faking it with ``deny`` +
``decision_note``. No new item.status value is introduced — cancel/reprice
project onto the existing ``denied``/``approved`` statuses (see
``decisions.py::_ITEM_STATUS_BY_DECISION``), so the item-status CHECK is
untouched.

The metadata naming convention ``ck_%(table_name)s_%(constraint_name)s`` expands
the canonical name to a >63-char identifier that PostgreSQL truncates and
hashes to ``ck_investment_report_item_decisions_ck_investment_repor_9aa6`` on
databases built via ``create_all``. Drop the canonical and hashed forms
defensively (matching the ROB-274 p1 precedent), then recreate via ``op.f``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260609_rob455"
down_revision: str | None = "20260607_rob443b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "investment_report_item_decisions"
_CHECK = "ck_investment_report_item_decisions_decision"
# Truncated/hashed identifier present on convention-built (create_all) databases.
_HASHED = "ck_investment_report_item_decisions_ck_investment_repor_9aa6"

_NEW = (
    "decision IN ('approve','deny','defer','skip','partial_approve','cancel','reprice')"
)
_OLD = "decision IN ('approve','deny','defer','skip','partial_approve')"


def _drop_decision_check() -> None:
    for name in (
        _HASHED,
        _CHECK,
        # op.f-expanded form this migration emits on the create side.
        f"ck_investment_report_item_decisions_{_CHECK}",
    ):
        op.execute(f'ALTER TABLE review.{_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')


def upgrade() -> None:
    _drop_decision_check()
    op.create_check_constraint(op.f(_CHECK), _TABLE, _NEW, schema="review")


def downgrade() -> None:
    _drop_decision_check()
    op.create_check_constraint(op.f(_CHECK), _TABLE, _OLD, schema="review")
