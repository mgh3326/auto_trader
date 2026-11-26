"""Add append-only KRX Open API official daily trading facts (hk 6586)."""

# expand-contract: contract-step 6586

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261127_hk6586_krx_official"
down_revision: str | Sequence[str] | None = "hk6784_crypto_fee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "krx_official_daily"
TRIGGER_FUNCTION = "krx_official_daily_reject_mutation"

# Idempotent guard DDL. The test schema bootstrap reads this tuple (one source).
GUARD_DDL: tuple[str, ...] = (
    f"""CREATE OR REPLACE FUNCTION public.{TRIGGER_FUNCTION}()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is insert-only', TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END
    $$""",
    f"DROP TRIGGER IF EXISTS trg_{TABLE}_insert_only ON {TABLE}",
    f"CREATE TRIGGER trg_{TABLE}_insert_only BEFORE UPDATE OR DELETE ON {TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION public.{TRIGGER_FUNCTION}()",
    f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_truncate ON {TABLE}",
    f"CREATE TRIGGER trg_{TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
    f"FOR EACH STATEMENT EXECUTE FUNCTION public.{TRIGGER_FUNCTION}()",
)
# Stage-4 role may not exist in development or CI databases.
GRANT_DDL = (
    f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
    GRANT SELECT, INSERT ON {TABLE} TO at_app; END IF; END $$""",
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("symbol", sa.String(6), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("open_price", sa.Numeric(), nullable=True),
        sa.Column("high_price", sa.Numeric(), nullable=True),
        sa.Column("low_price", sa.Numeric(), nullable=True),
        sa.Column("close_price", sa.Numeric(), nullable=True),
        sa.Column("volume", sa.Numeric(), nullable=True),
        sa.Column("trade_value", sa.Numeric(), nullable=True),
        sa.Column("market_cap", sa.Numeric(), nullable=True),
        sa.Column("listed_shares", sa.Numeric(), nullable=True),
        sa.Column("full_day_zero_volume", sa.Boolean(), nullable=True),
        sa.Column(
            "field_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "session_date"),
        sa.CheckConstraint("symbol ~ '^[0-9]{6}$'", name="symbol_short_code"),
        sa.CheckConstraint("market IN ('kospi','kosdaq')", name="market"),
        sa.CheckConstraint("volume IS NULL OR volume >= 0", name="volume_nonnegative"),
        sa.CheckConstraint(
            "trade_value IS NULL OR trade_value >= 0",
            name="trade_value_nonnegative",
        ),
        sa.CheckConstraint(
            "(volume IS NULL AND full_day_zero_volume IS NULL) "
            "OR (volume IS NOT NULL AND full_day_zero_volume = (volume = 0))",
            name="zero_volume_matches_volume",
        ),
    )
    # expand-contract: contract-step 6586
    for statement in GUARD_DDL:
        op.execute(statement)
    # expand-contract: contract-step 6586
    for statement in GRANT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE"))
    count = connection.execute(
        sa.text(f"SELECT count(*) FROM public.{TABLE}")
    ).scalar_one()
    if count:
        raise RuntimeError(f"refusing to downgrade: {TABLE} contains records")
    # expand-contract: contract-step 6586
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_truncate ON {TABLE}")
    # expand-contract: contract-step 6586
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_insert_only ON {TABLE}")
    op.drop_table(TABLE)
    # expand-contract: contract-step 6586
    op.execute(f"DROP FUNCTION IF EXISTS public.{TRIGGER_FUNCTION}()")
