"""hk 4754 (3797 T-P8): admit ``kiwoom_mock_us`` in ``order_proposals``.

Revision ID: 20261116_hk4754_us_proposals
Revises: 20261114_hk4702_trend_state
Create Date: 2026-11-16

hk 4703 (T-P1) widened ``review.order_proposals.account_mode`` with
``'kiwoom_mock'`` only, deliberately leaving ``'kiwoom_mock_us'`` to this ticket
(design docs/design/3797-kiwoom-mock-ledger.md §4.1). The US mock lane is a
submittable, manual-approval place capability now, so the constraint must admit
its rows.

Constraint names: the ORM spells the CHECK ``ck_order_proposals_<short>``, while
20260710_rob816 created it under the literal ``order_proposals_account_mode``;
both spellings are dropped with IF EXISTS, the same drift handling hk 4703 uses.

No new object is created, so there is no new grant (hard rule 18). The downgrade
refuses while any proposal is tagged ``'kiwoom_mock_us'``, because the narrower
CHECK would reject an existing row.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261116_hk4754_us_proposals"
down_revision: str | Sequence[str] | None = "20261114_hk4702_trend_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_PROPOSAL_TABLE = "order_proposals"
_PROPOSAL_CHECK = "ck_order_proposals_order_proposals_account_mode"
_PROPOSAL_CHECK_NAMES = (_PROPOSAL_CHECK, "order_proposals_account_mode")

_OLD_ACCOUNT_MODES = (
    "kis_live",
    "kis_mock",
    "toss_live",
    "upbit",
    "db_simulated",
    "kiwoom_mock",
)
_NEW_ACCOUNT_MODES = (*_OLD_ACCOUNT_MODES, "kiwoom_mock_us")

DOWNGRADE_GUARD = f"""
DO $$
BEGIN
    LOCK TABLE {_SCHEMA}.{_PROPOSAL_TABLE} IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (
        SELECT 1 FROM {_SCHEMA}.{_PROPOSAL_TABLE}
        WHERE account_mode = 'kiwoom_mock_us'
    ) THEN
        RAISE EXCEPTION 'order_proposals rows use kiwoom_mock_us; export before downgrading'
            USING ERRCODE = 'restrict_violation';
    END IF;
END;
$$
"""


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _swap_proposal_account_mode_check(modes: Sequence[str]) -> None:
    for name in _PROPOSAL_CHECK_NAMES:
        op.execute(
            f"ALTER TABLE {_SCHEMA}.{_PROPOSAL_TABLE} "
            f'DROP CONSTRAINT IF EXISTS "{name}"'
        )
    op.create_check_constraint(
        op.f(_PROPOSAL_CHECK),
        _PROPOSAL_TABLE,
        _in("account_mode", modes),
        schema=_SCHEMA,
    )


def upgrade() -> None:
    _swap_proposal_account_mode_check(_NEW_ACCOUNT_MODES)


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    _swap_proposal_account_mode_check(_OLD_ACCOUNT_MODES)
