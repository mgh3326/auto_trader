"""Add the dedicated Alpaca paper lab account mode to existing CHECKs.

Revision ID: 20260727_alpaca_paper_lab
Revises: 20260725_rob1010_crypto_venue
Create Date: 2026-07-27 00:00:00.000000

This reuses the existing Alpaca ledger and retrospective table/columns. It
does not add or change any unique index. Downgrade refuses to narrow the
constraints while lab rows still exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260727_alpaca_paper_lab"
down_revision: str | Sequence[str] | None = "20260725_rob1010_crypto_venue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_TABLE = "review.alpaca_paper_order_ledger"
_LEDGER_CHECK = "ck_alpaca_paper_order_ledger_alpaca_paper_ledger_account_mode"
_LEDGER_TEMP_CHECK = "ck_alpaca_paper_ledger_account_mode_next"
_RETROSPECTIVE_TABLE = "review.trade_retrospectives"
_RETROSPECTIVE_CHECK = "ck_trade_retrospectives_ck_trade_retrospectives_account_mode"
_RETROSPECTIVE_TEMP_CHECK = "ck_trade_retrospectives_account_mode_next"

_LEDGER_NEW = "account_mode IN ('alpaca_paper','alpaca_paper_lab')"
_LEDGER_OLD = "account_mode = 'alpaca_paper'"
_RETROSPECTIVE_NEW = (
    "account_mode IN ("
    "'kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper',"
    "'alpaca_paper_lab','upbit_live','paper'"
    ")"
)
_RETROSPECTIVE_OLD = (
    "account_mode IN ("
    "'kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper',"
    "'upbit_live','paper'"
    ")"
)


def _swap_check(
    table: str,
    current_name: str,
    temporary_name: str,
    expression: str,
) -> None:
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{temporary_name}"')
    op.execute(
        f'ALTER TABLE {table} ADD CONSTRAINT "{temporary_name}" '
        f"CHECK ({expression}) NOT VALID"
    )
    op.execute(f'ALTER TABLE {table} VALIDATE CONSTRAINT "{temporary_name}"')
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{current_name}"')
    op.execute(
        f'ALTER TABLE {table} RENAME CONSTRAINT "{temporary_name}" TO "{current_name}"'
    )


def upgrade() -> None:
    _swap_check(
        _LEDGER_TABLE,
        _LEDGER_CHECK,
        _LEDGER_TEMP_CHECK,
        _LEDGER_NEW,
    )
    _swap_check(
        _RETROSPECTIVE_TABLE,
        _RETROSPECTIVE_CHECK,
        _RETROSPECTIVE_TEMP_CHECK,
        _RETROSPECTIVE_NEW,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM review.alpaca_paper_order_ledger
                WHERE account_mode = 'alpaca_paper_lab'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: alpaca_paper_lab ledger rows exist';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM review.trade_retrospectives
                WHERE account_mode = 'alpaca_paper_lab'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: alpaca_paper_lab retrospective rows exist';
            END IF;
        END
        $$;
        """
    )
    _swap_check(
        _RETROSPECTIVE_TABLE,
        _RETROSPECTIVE_CHECK,
        _RETROSPECTIVE_TEMP_CHECK,
        _RETROSPECTIVE_OLD,
    )
    _swap_check(
        _LEDGER_TABLE,
        _LEDGER_CHECK,
        _LEDGER_TEMP_CHECK,
        _LEDGER_OLD,
    )
