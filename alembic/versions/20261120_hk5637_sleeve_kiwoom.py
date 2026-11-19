"""hk 5637 (3795 T1m-b): the Kiwoom mock account joins the sleeve combinations.

Additive: the ``review.strategy_sleeves.account_mode/market`` CHECK gains one
combination, ``kiwoom_mock`` / ``equity_kr`` (mock only). Every combination the
old CHECK accepted is still accepted, no live combination is added or changed,
and no row, column, index or trigger changes. No object is created, so the
conditional ``at_app`` GRANT below only re-asserts the existing SELECT and INSERT
grants (it is a no-op where they already hold, and never grants UPDATE or
DELETE).

The Python twin is ``app.models.strategy_sleeves.SLEEVE_ACCOUNT_MARKETS``; the
migration carries its own literal copy and a test pins the two to each other.

Downgrade refuses while any ``kiwoom_mock`` sleeve exists (its rows could not
satisfy the old CHECK, and deleting them is not allowed: the sleeve tables are
immutable), then restores the previous CHECK.

Revision ID: 20261120_hk5637_sleeve_kiwoom
Revises: 20261120_hk5528_mandate
Create Date: 2026-11-20
"""

# expand-contract: contract-step 5637
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261120_hk5637_sleeve_kiwoom"
down_revision: str | Sequence[str] | None = "20261120_hk5528_mandate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_strategy_sleeves_account_market"

_PREVIOUS_ACCOUNT_MARKET_SQL = (
    "(account_mode = 'kis_live' AND market = 'equity_kr') OR "
    "(account_mode = 'kis_live' AND market = 'equity_us') OR "
    "(account_mode = 'toss_live' AND market = 'equity_kr') OR "
    "(account_mode = 'toss_live' AND market = 'equity_us') OR "
    "(account_mode = 'upbit' AND market = 'crypto')"
)
ACCOUNT_MARKET_SQL = (
    _PREVIOUS_ACCOUNT_MARKET_SQL
    + " OR (account_mode = 'kiwoom_mock' AND market = 'equity_kr')"
)

#: One statement per entry; idempotent. The test schema bootstrap installs
#: these too, so the DB tests exercise this exact DDL.
CHECK_DDL: tuple[str, ...] = (
    f"ALTER TABLE review.strategy_sleeves DROP CONSTRAINT IF EXISTS {CONSTRAINT}",
    f"ALTER TABLE review.strategy_sleeves ADD CONSTRAINT {CONSTRAINT} "
    f"CHECK ({ACCOUNT_MARKET_SQL})",
)

DOWNGRADE_GUARD = """
    DO $$
    BEGIN
        LOCK TABLE review.strategy_sleeves IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (
            SELECT 1 FROM review.strategy_sleeves WHERE account_mode = 'kiwoom_mock'
        ) THEN
            RAISE EXCEPTION 'kiwoom_mock sleeves exist; export them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""


def upgrade() -> None:
    for statement in CHECK_DDL:
        op.execute(statement)
    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.strategy_sleeves TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.execute(f"ALTER TABLE review.strategy_sleeves DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE review.strategy_sleeves ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK ({_PREVIOUS_ACCOUNT_MARKET_SQL})"
    )
