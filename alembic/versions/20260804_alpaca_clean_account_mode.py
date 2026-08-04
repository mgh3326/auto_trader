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
_LEDGER_TABLE = "alpaca_paper_order_ledger"
_LEDGER_SCHEMA = "review"


def _table_exists(qualified_name: str) -> bool:
    """Keep the additive migration safe across legacy downgrade boundaries."""

    schema, table = qualified_name.split(".", maxsplit=1)
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'f'))"
            ),
            {"schema": schema, "table": table},
        )
        .scalar()
    )


def _column_exists(table: str, column: str) -> bool:
    schema, table_name = table.split(".", maxsplit=1)
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND column_name = :column)"
            ),
            {"schema": schema, "table": table_name, "column": column},
        )
        .scalar()
    )


def _swap(table: str, current: str, temporary: str, expression: str) -> None:
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{temporary}"')
    op.execute(
        f'ALTER TABLE {table} ADD CONSTRAINT "{temporary}" CHECK ({expression}) NOT VALID'
    )
    op.execute(f'ALTER TABLE {table} VALIDATE CONSTRAINT "{temporary}"')
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{current}"')
    op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{temporary}" TO "{current}"')


def upgrade() -> None:
    if _table_exists(_LEDGER):
        if not _column_exists(_LEDGER, "strategy_key"):
            op.add_column(
                _LEDGER_TABLE,
                sa.Column("strategy_key", sa.Text(), nullable=True),
                schema=_LEDGER_SCHEMA,
            )
        _swap(
            _LEDGER,
            _LEDGER_CHECK,
            _LEDGER_TEMP,
            "account_mode IN ('alpaca_paper','alpaca_paper_lab','alpaca_paper_crypto')",
        )
    if _table_exists(_RETRO):
        _swap(
            _RETRO,
            _RETRO_CHECK,
            _RETRO_TEMP,
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live',"
            "'alpaca_paper','alpaca_paper_lab','alpaca_paper_crypto','upbit_live','paper')",
        )


def downgrade() -> None:
    ledger_exists = _table_exists(_LEDGER)
    retro_exists = _table_exists(_RETRO)
    if ledger_exists or retro_exists:
        checks = []
        if ledger_exists:
            checks.append(
                "EXISTS (SELECT 1 FROM review.alpaca_paper_order_ledger "
                "WHERE account_mode = 'alpaca_paper_crypto')"
            )
        if retro_exists:
            checks.append(
                "EXISTS (SELECT 1 FROM review.trade_retrospectives "
                "WHERE account_mode = 'alpaca_paper_crypto')"
            )
        op.execute(
            "DO $$ BEGIN IF "
            + " OR ".join(checks)
            + " THEN RAISE EXCEPTION 'cannot downgrade: alpaca_paper_crypto rows exist'; "
            "END IF; END $$;"
        )
    if retro_exists:
        _swap(
            _RETRO,
            _RETRO_CHECK,
            _RETRO_TEMP,
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live',"
            "'alpaca_paper','alpaca_paper_lab','upbit_live','paper')",
        )
    if ledger_exists:
        _swap(
            _LEDGER,
            _LEDGER_CHECK,
            _LEDGER_TEMP,
            "account_mode IN ('alpaca_paper','alpaca_paper_lab')",
        )
        op.drop_column(_LEDGER_TABLE, "strategy_key", schema=_LEDGER_SCHEMA)
