"""Accept 6-character alphanumeric KRX short codes; reject NULL zero-volume flags (hk 6959).

The first live run (2026-11-27, a30bd7d) already applied 20261127_hk6586, so its
CHECK text is frozen in the database. Changing that revision would not reach
production; this additive revision swaps the two CHECK constraints instead.

- symbol: 6-digit only -> 6 characters of digits and upper-case ASCII letters
  (KRX new-format codes such as 0256C0).
- zero-volume rule: a non-NULL volume now also requires a non-NULL flag. In SQL,
  ``NULL = true`` is NULL and a CHECK that evaluates to NULL passes, so volume=0
  with a NULL flag used to be accepted.

Downgrade restores the 6-digit rule and refuses while any non-digit symbol exists.
"""

# expand-contract: contract-step 6959

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261127_hk6959_krx_alnum"
down_revision: str | Sequence[str] | None = "20261127_hk6586_krx_official"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "krx_official_daily"
SYMBOL_OLD = "ck_krx_official_daily_symbol_short_code"
SYMBOL_NEW_EXPR = "symbol ~ '^[0-9A-Z]{6}$'"
SYMBOL_OLD_EXPR = "symbol ~ '^[0-9]{6}$'"
ZERO_CHECK = "ck_krx_official_daily_zero_volume_matches_volume"
ZERO_OLD_EXPR = (
    "(volume IS NULL AND full_day_zero_volume IS NULL) "
    "OR (volume IS NOT NULL AND full_day_zero_volume = (volume = 0))"
)
ZERO_NEW_EXPR = (
    "(volume IS NULL AND full_day_zero_volume IS NULL) "
    "OR (volume IS NOT NULL AND full_day_zero_volume IS NOT NULL "
    "AND full_day_zero_volume = (volume = 0))"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {SYMBOL_OLD}")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SYMBOL_OLD} CHECK ({SYMBOL_NEW_EXPR})"
    )
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {ZERO_CHECK}")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {ZERO_CHECK} CHECK ({ZERO_NEW_EXPR})"
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE"))
    non_digit = connection.execute(
        sa.text(f"SELECT count(*) FROM public.{TABLE} WHERE symbol !~ '^[0-9]{{6}}$'")
    ).scalar_one()
    if non_digit:
        raise RuntimeError(
            f"refusing to downgrade: {TABLE} contains alphanumeric short codes"
        )
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {ZERO_CHECK}")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {ZERO_CHECK} CHECK ({ZERO_OLD_EXPR})"
    )
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {SYMBOL_OLD}")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SYMBOL_OLD} CHECK ({SYMBOL_OLD_EXPR})"
    )
