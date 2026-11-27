"""Widen daily plan heads and rows to US/crypto and executable mode (hk 6981).

Design docs/design/6939-autonomous-strategy-orders.md §1.1 "Required widenings":
heads market kr→kr|us|crypto and mode plan_only→plan_only|executable; rows
execution_state plan_only→plan_only|executable; one symbol CHECK per market
(KR 6-char alphanumeric short code as in hk 6769, US DB dot form, crypto
Upbit KRW-XXX). Constraint swaps only, no new objects, so no grants.

Downgrade restores the KR plan-only rules and refuses while any widened value
exists.
"""

# expand-contract: contract-step 6981

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261127_hk6981_plan_widen"
down_revision: str | Sequence[str] | None = "20261127_hk6769_plan_alnum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEADS = "review.daily_plan_heads"
ROWS = "review.daily_plan_rows"

KR_SYMBOL = "symbol ~ '^[0-9A-Z]{6}$'"
US_SYMBOL = "symbol ~ '^[A-Z][A-Z0-9]{0,9}([.][A-Z0-9]{1,4})?$'"
CRYPTO_SYMBOL = "symbol ~ '^KRW-[A-Z0-9]{1,20}$'"

NEW = (
    (
        HEADS,
        "ck_daily_plan_heads_kr_plan_only",
        "ck_daily_plan_heads_market_mode",
        "market IN ('kr', 'us', 'crypto') AND mode IN ('plan_only', 'executable')",
    ),
    (
        ROWS,
        "ck_daily_plan_rows_plan_only",
        "ck_daily_plan_rows_execution_state",
        "execution_state IN ('plan_only', 'executable')",
    ),
    (
        ROWS,
        "ck_daily_plan_rows_kr_symbol",
        "ck_daily_plan_rows_market_symbol",
        f"(market = 'kr' AND {KR_SYMBOL}) OR (market = 'us' AND {US_SYMBOL}) "
        f"OR (market = 'crypto' AND {CRYPTO_SYMBOL})",
    ),
)
OLD = {
    "ck_daily_plan_heads_kr_plan_only": "market = 'kr' AND mode = 'plan_only'",
    "ck_daily_plan_rows_plan_only": "execution_state = 'plan_only'",
    "ck_daily_plan_rows_kr_symbol": f"market = 'kr' AND {KR_SYMBOL}",
}


def upgrade() -> None:
    for table, old, new, expression in NEW:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {new} CHECK ({expression})")


def downgrade() -> None:
    connection = op.get_bind()
    for table in (HEADS, ROWS):
        connection.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    widened = connection.execute(
        sa.text(
            f"SELECT (SELECT count(*) FROM {HEADS} "
            "WHERE market <> 'kr' OR mode <> 'plan_only') + "
            f"(SELECT count(*) FROM {ROWS} "
            "WHERE market <> 'kr' OR execution_state <> 'plan_only')"
        )
    ).scalar_one()
    if widened:
        raise RuntimeError(
            "refusing to downgrade: daily plan holds us/crypto or executable values"
        )
    for table, old, new, _ in NEW:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {new}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {old} CHECK ({OLD[old]})")
