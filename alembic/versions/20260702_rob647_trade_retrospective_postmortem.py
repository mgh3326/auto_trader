"""add postmortem structuring columns to trade_retrospectives (ROB-647)

Revision ID: 20260702_rob647
Revises: 20260702_rob641
Create Date: 2026-07-02

Additive-only. Adds six nullable columns to review.trade_retrospectives so a
retrospective can carry structured postmortem context:

* trigger_type          — what triggered the retro (fill/rejected_order/expired/
                          thesis_change/policy_violation/…), CHECK-constrained
                          and deliberately distinct from ``outcome`` (kis
                          reconcile collapses expired -> cancelled at the outcome
                          layer, so ``expired`` only survives here).
* root_cause_class      — CHECK-constrained taxonomy (user_input/analysis/policy/
                          execution/harness), ported from tradingcodex.
* intended_vs_happened  — validated JSONB deviation structure.
* next_actions          — validated JSONB list (Linear identifiers only; no API
                          client in-repo).
* guardrail_fired       — identifier of the guard that fired.
* policy_version        — policy version cited by the postmortem (P5 alignment).

No existing column/constraint is modified: existing save callers stay
backward-compatible (all new fields optional; next_actions is obligatory only
when trigger_type is set — enforced in the service layer, not the DB).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260702_rob647"
down_revision: str | Sequence[str] | None = "20260702_rob641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "trade_retrospectives"
_SCHEMA = "review"

_NEW_COLUMNS = (
    ("trigger_type", sa.Text()),
    ("root_cause_class", sa.Text()),
    ("intended_vs_happened", postgresql.JSONB(astext_type=sa.Text())),
    ("next_actions", postgresql.JSONB(astext_type=sa.Text())),
    ("guardrail_fired", sa.Text()),
    ("policy_version", sa.Text()),
)


def upgrade() -> None:
    for name, col_type in _NEW_COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, col_type, nullable=True),
            schema=_SCHEMA,
        )
    op.create_check_constraint(
        "ck_trade_retrospectives_trigger_type",
        _TABLE,
        "trigger_type IS NULL OR trigger_type IN ("
        "'fill','partial_fill','rejected_order','cancelled','expired',"
        "'thesis_change','policy_violation','stale_evidence','guardrail_block'"
        ")",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_retrospectives_root_cause_class",
        _TABLE,
        "root_cause_class IS NULL OR root_cause_class IN ("
        "'user_input','analysis','policy','execution','harness'"
        ")",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_retrospectives_root_cause_class",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_trade_retrospectives_trigger_type",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, name, schema=_SCHEMA)
