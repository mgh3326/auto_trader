"""Accept 6-character alphanumeric KRX short codes in daily plan rows (hk 6769).

20261126_hk6751 allowed six digits only, but KR short codes can hold upper-case
ASCII letters (for example 0256C0). One such held or candidate symbol rejected
the whole publication. That revision's CHECK text is already in deployed
databases, so this additive revision swaps the constraint.

Downgrade restores the 6-digit rule and refuses while any non-digit symbol exists.
"""

# expand-contract: contract-step 6769

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261127_hk6769_plan_alnum"
down_revision: str | Sequence[str] | None = "20261127_hk6959_krx_alnum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "review.daily_plan_rows"
CONSTRAINT = "ck_daily_plan_rows_kr_symbol"
OLD_EXPR = "market = 'kr' AND symbol ~ '^[0-9]{6}$'"
NEW_EXPR = "market = 'kr' AND symbol ~ '^[0-9A-Z]{6}$'"


def _swap(expression: str) -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK ({expression})")


def upgrade() -> None:
    _swap(NEW_EXPR)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE"))
    non_digit = connection.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE symbol !~ '^[0-9]{{6}}$'")
    ).scalar_one()
    if non_digit:
        raise RuntimeError(
            f"refusing to downgrade: {TABLE} contains alphanumeric short codes"
        )
    _swap(OLD_EXPR)
