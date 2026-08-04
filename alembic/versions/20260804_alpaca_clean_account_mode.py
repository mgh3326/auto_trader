"""Add canonical Alpaca physical-account routing mode additively.

Existing account-mode expressions are widened only; no prior value is removed
or tightened. The ledger strategy_key is nullable for legacy rows and is not a
strategy/account hard-coded one-to-one mapping.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_alpaca_clean_account"
down_revision: str | Sequence[str] | None = (
    "20260727_alpaca_paper_lab",
    "20260803_research_kr_candles",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER = "review.alpaca_paper_order_ledger"
_LEDGER_CHECK = "ck_alpaca_paper_order_ledger_alpaca_paper_ledger_account_mode"
_LEDGER_TEMP = "alpaca_paper_ledger_account_mode_next"
_RETRO = "review.trade_retrospectives"
_RETRO_CHECK = "ck_trade_retrospectives_ck_trade_retrospectives_account_mode"
_RETRO_TEMP = "trade_retrospectives_account_mode_next"


def _swap(table: str, current: str, temporary: str, expression: str) -> None:
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{temporary}"')
    op.execute(
        f'ALTER TABLE {table} ADD CONSTRAINT "{temporary}" CHECK ({expression}) NOT VALID'
    )
    op.execute(f'ALTER TABLE {table} VALIDATE CONSTRAINT "{temporary}"')
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{current}"')
    op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{temporary}" TO "{current}"')


def upgrade() -> None:
    op.add_column(_LEDGER, sa.Column("strategy_key", sa.Text(), nullable=True))
    _swap(
        _LEDGER,
        _LEDGER_CHECK,
        _LEDGER_TEMP,
        "account_mode IN ('alpaca_paper','alpaca_paper_lab','alpaca_paper_crypto')",
    )
    _swap(
        _RETRO,
        _RETRO_CHECK,
        _RETRO_TEMP,
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live',"
        "'alpaca_paper','alpaca_paper_lab','alpaca_paper_crypto','upbit_live','paper')",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM review.alpaca_paper_order_ledger
                   WHERE account_mode = 'alpaca_paper_crypto') OR
           EXISTS (SELECT 1 FROM review.trade_retrospectives
                   WHERE account_mode = 'alpaca_paper_crypto') THEN
            RAISE EXCEPTION 'cannot downgrade: alpaca_paper_crypto rows exist';
        END IF;
        END $$;"""
    )
    _swap(
        _RETRO,
        _RETRO_CHECK,
        _RETRO_TEMP,
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live',"
        "'alpaca_paper','alpaca_paper_lab','upbit_live','paper')",
    )
    _swap(
        _LEDGER,
        _LEDGER_CHECK,
        _LEDGER_TEMP,
        "account_mode IN ('alpaca_paper','alpaca_paper_lab')",
    )
    op.drop_column(_LEDGER, "strategy_key")
