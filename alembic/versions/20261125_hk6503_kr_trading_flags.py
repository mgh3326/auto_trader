"""hk 6503: per-symbol KR trading flag state (관리·시장경고·정리매매·거래정지).

Revision ID: 20261125_hk6503_kr_trading_flags
Revises: 20261125_hk6036_upbit_shadow
Create Date: 2026-11-25

Additive. One new ``public.kr_symbol_trading_flags`` table, keyed by
(symbol, category). Absence of a row is *unknown*. The table is written only by
``app/services/kr_trading_flags/store.py``. Nothing existing is altered.

Conditional ``at_app`` grant (hard rule 18): SELECT, INSERT, UPDATE (the upsert
updates an existing row when a newer observation arrives). No DELETE: a stale
observation is replaced by a newer one, never removed.

Downgrade refuses while any row exists, because deleting the table would erase
observations that cannot be re-derived from the sources after the fact.
"""

# expand-contract: contract-step 6503
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261125_hk6503_kr_trading_flags"
down_revision: str | Sequence[str] | None = "20261125_hk6036_upbit_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "kr_symbol_trading_flags"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("symbol", sa.String(length=6), primary_key=True),
        sa.Column("category", sa.String(length=20), primary_key=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("raw_code", sa.String(length=20), nullable=True),
        sa.Column("reason_code", sa.String(length=20), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9A-Z]{6}$'",
            name="ck_kr_symbol_trading_flags_symbol_format",
        ),
        sa.CheckConstraint(
            "category IN ('management','market_alert','delisting_trade','trading_halt')",
            name="ck_kr_symbol_trading_flags_category",
        ),
        sa.CheckConstraint(
            "source IN ('kis_master','naver_intraday')",
            name="ck_kr_symbol_trading_flags_source",
        ),
        sa.CheckConstraint(
            "(category = 'management' AND state IN ('designated','none','unmapped'))"
            " OR (category = 'market_alert' AND"
            " state IN ('none','caution','warning','risk','unmapped'))"
            " OR (category = 'delisting_trade' AND"
            " state IN ('in_progress','none','unmapped'))"
            " OR (category = 'trading_halt' AND state IN ('halted','none','unmapped'))",
            name="ck_kr_symbol_trading_flags_state_by_category",
        ),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT, UPDATE ON public.kr_symbol_trading_flags TO at_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_rows = bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE})")).scalar()
    if has_rows:
        raise RuntimeError(
            f"refusing to drop {TABLE}: it holds observations that cannot be "
            "re-derived after the fact; export or delete them deliberately first"
        )
    op.drop_table(TABLE)
