"""migrate crypto_candles_1d step3 finalize

Revision ID: 5fa5a347d85b
Revises: 181f946296ff
Create Date: 2026-05-20 21:53:38.574675

ROB-284 step 3 — destructive finalize. Operator MUST have taken the
crypto_candles_1d_pre_rob283 backup (see docs/runbooks/daily-candles-store.md)
before running this revision.

This step fails closed if any row still has NULL instrument_id /
base_volume / is_closed — re-run step 2 (backfill) or restore from the
backup table before retrying.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5fa5a347d85b'
down_revision: Union[str, Sequence[str], None] = '181f946296ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Safety check: refuse to drop legacy columns if any row has
    # instrument_id NULL or base_volume NULL or is_closed NULL — the step 2
    # backfill is incomplete.
    connection = op.get_bind()
    incomplete = connection.execute(
        sa.text(
            "SELECT count(*) FROM crypto_candles_1d "
            "WHERE instrument_id IS NULL OR base_volume IS NULL "
            "OR is_closed IS NULL"
        )
    ).scalar_one()
    if incomplete > 0:
        raise RuntimeError(
            f"ROB-284 step 3 refused: {incomplete} rows in crypto_candles_1d "
            "have NULL instrument_id / base_volume / is_closed. "
            "Re-run step 2 backfill (or restore from "
            "crypto_candles_1d_pre_rob283 backup) before retrying."
        )

    # Enforce NOT NULL.
    op.alter_column("crypto_candles_1d", "instrument_id", nullable=False)
    op.alter_column("crypto_candles_1d", "base_volume", nullable=False)
    op.alter_column("crypto_candles_1d", "is_closed", nullable=False)

    # Add FK now that instrument_id is fully populated.
    op.create_foreign_key(
        "fk_crypto_candles_1d_instrument",
        "crypto_candles_1d", "crypto_instruments",
        ["instrument_id"], ["id"],
    )

    # Drop legacy indexes/uniques first, then legacy columns.
    op.drop_constraint(
        "uq_crypto_candles_1d_time_symbol_market",
        "crypto_candles_1d",
        type_="unique",
    )
    op.drop_index(
        "ix_crypto_candles_1d_symbol_market_time_desc",
        table_name="crypto_candles_1d",
    )
    op.drop_column("crypto_candles_1d", "market")
    op.drop_column("crypto_candles_1d", "symbol")
    op.drop_column("crypto_candles_1d", "volume")
    op.drop_column("crypto_candles_1d", "value")

    # New PK = (instrument_id, time). Timescale hypertable accepts a
    # composite PK that includes the partitioning column.
    op.execute(
        "ALTER TABLE crypto_candles_1d "
        "ADD CONSTRAINT pk_crypto_candles_1d PRIMARY KEY (instrument_id, time)"
    )

    # CHECK constraints (OHLC sanity + non-negative).
    op.create_check_constraint(
        "ck_crypto_candles_1d_base_volume_nn",
        "crypto_candles_1d",
        "base_volume >= 0",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_quote_volume_nn",
        "crypto_candles_1d",
        "quote_volume IS NULL OR quote_volume >= 0",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_high_ge_low",
        "crypto_candles_1d",
        "high >= low",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_high_ge_oc",
        "crypto_candles_1d",
        "high >= open AND high >= close",
    )
    op.create_check_constraint(
        "ck_crypto_candles_1d_low_le_oc",
        "crypto_candles_1d",
        "low <= open AND low <= close",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Restore legacy shape from JOIN. Requires crypto_instruments still
    # present.
    op.drop_constraint(
        "ck_crypto_candles_1d_low_le_oc", "crypto_candles_1d", type_="check"
    )
    op.drop_constraint(
        "ck_crypto_candles_1d_high_ge_oc", "crypto_candles_1d", type_="check"
    )
    op.drop_constraint(
        "ck_crypto_candles_1d_high_ge_low", "crypto_candles_1d", type_="check"
    )
    op.drop_constraint(
        "ck_crypto_candles_1d_quote_volume_nn",
        "crypto_candles_1d",
        type_="check",
    )
    op.drop_constraint(
        "ck_crypto_candles_1d_base_volume_nn",
        "crypto_candles_1d",
        type_="check",
    )
    op.execute(
        "ALTER TABLE crypto_candles_1d DROP CONSTRAINT pk_crypto_candles_1d"
    )
    op.add_column(
        "crypto_candles_1d", sa.Column("value", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "crypto_candles_1d", sa.Column("volume", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "crypto_candles_1d", sa.Column("symbol", sa.Text(), nullable=True)
    )
    op.add_column(
        "crypto_candles_1d", sa.Column("market", sa.Text(), nullable=True)
    )
    op.execute(
        """
        UPDATE crypto_candles_1d c
        SET symbol = i.venue_symbol,
            market = CASE WHEN i.venue = 'upbit' THEN 'upbit_krw' ELSE i.venue END,
            volume = c.base_volume,
            -- Original f974ac12e573 schema had value NUMERIC NOT NULL. The
            -- new schema allows quote_volume IS NULL (sources without quote
            -- volume), so we COALESCE to 0 here so the NOT NULL restore
            -- below cannot fail on rows inserted after the upgrade. Operator
            -- runbook documents the 0-default for new-schema-era rows.
            value  = COALESCE(c.quote_volume, 0)
        FROM crypto_instruments i
        WHERE i.id = c.instrument_id
        """
    )
    op.alter_column("crypto_candles_1d", "symbol", nullable=False)
    op.alter_column("crypto_candles_1d", "market", nullable=False)
    op.alter_column("crypto_candles_1d", "volume", nullable=False)
    # ROB-284 — restore value NOT NULL to match the f974ac12e573 schema.
    op.alter_column("crypto_candles_1d", "value", nullable=False)
    op.alter_column("crypto_candles_1d", "instrument_id", nullable=True)
    op.alter_column("crypto_candles_1d", "is_closed", nullable=True)
    op.drop_constraint(
        "fk_crypto_candles_1d_instrument",
        "crypto_candles_1d",
        type_="foreignkey",
    )
    op.create_index(
        "ix_crypto_candles_1d_symbol_market_time_desc",
        "crypto_candles_1d",
        ["symbol", "market", sa.text("time DESC")],
    )
    op.create_unique_constraint(
        "uq_crypto_candles_1d_time_symbol_market",
        "crypto_candles_1d",
        ["time", "symbol", "market"],
    )
