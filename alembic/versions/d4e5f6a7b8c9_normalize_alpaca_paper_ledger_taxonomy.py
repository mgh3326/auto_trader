"""Normalize Alpaca Paper ledger taxonomy (ROB-90)

Adds canonical lifecycle states, record_kind, lifecycle_correlation_id,
leg_role, validation_attempt fields, fee/settlement fields, qty_delta.
Replaces old lifecycle CHECK and per-order unique constraint with
partial unique indexes to support multiple rows per order lifecycle.

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-05-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "alpaca_paper_order_ledger"
_SCHEMA = "review"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Step 1: Add new columns (nullable/defaulted first)
    # ------------------------------------------------------------------
    # record_kind — NOT NULL with server default 'execution' so existing rows get it.
    op.add_column(
        _TABLE,
        sa.Column(
            "record_kind",
            sa.Text(),
            nullable=False,
            server_default="execution",
        ),
        schema=_SCHEMA,
    )
    # lifecycle_correlation_id — add nullable first, then populate, then set NOT NULL.
    op.add_column(
        _TABLE,
        sa.Column("lifecycle_correlation_id", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("leg_role", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("validation_attempt_no", sa.SmallInteger(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("validation_outcome", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("confirm_flag", sa.Boolean(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("fee_amount", sa.Numeric(20, 4), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("fee_currency", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("settlement_status", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "settlement_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("qty_delta", sa.Numeric(20, 8), nullable=True),
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Step 2: Populate lifecycle_correlation_id from client_order_id
    # ------------------------------------------------------------------
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_correlation_id = client_order_id "
        f"WHERE lifecycle_correlation_id IS NULL"
    )

    # ------------------------------------------------------------------
    # Step 3: Compatibility — map old lifecycle states to canonical
    # ------------------------------------------------------------------
    # validation_failed → anomaly, record_kind → validation_attempt
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = 'anomaly', "
        f"    record_kind = 'validation_attempt', "
        f"    validation_outcome = 'failed', "
        f"    confirm_flag = false "
        f"WHERE lifecycle_state = 'validation_failed'"
    )
    # open → submitted
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = 'submitted', confirm_flag = true "
        f"WHERE lifecycle_state = 'open'"
    )
    # partially_filled → submitted (broker status preserved in order_status)
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = 'submitted', confirm_flag = true "
        f"WHERE lifecycle_state = 'partially_filled'"
    )
    # canceled → anomaly
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = 'anomaly', confirm_flag = true "
        f"WHERE lifecycle_state = 'canceled'"
    )
    # unexpected → anomaly
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = 'anomaly' "
        f"WHERE lifecycle_state = 'unexpected'"
    )
    # previewed rows with no broker order → record_kind = 'preview'
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET record_kind = 'preview' "
        f"WHERE lifecycle_state = 'previewed' AND broker_order_id IS NULL"
    )
    # remaining execution rows — set confirm_flag = true
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET confirm_flag = true "
        f"WHERE confirm_flag IS NULL AND record_kind = 'execution'"
    )
    # settlement_status = 'n_a' for all existing rows
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET settlement_status = 'n_a' "
        f"WHERE settlement_status IS NULL"
    )

    # ------------------------------------------------------------------
    # Step 4: Make lifecycle_correlation_id NOT NULL
    # ------------------------------------------------------------------
    op.alter_column(
        _TABLE,
        "lifecycle_correlation_id",
        nullable=False,
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Step 5: Replace old lifecycle CHECK with canonical states
    # ------------------------------------------------------------------
    op.drop_constraint(
        "alpaca_paper_ledger_lifecycle_state",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "alpaca_paper_ledger_lifecycle_state",
        _TABLE,
        "lifecycle_state IN ("
        "'planned','previewed','validated','submitted','filled',"
        "'position_reconciled','sell_validated','closed','final_reconciled','anomaly'"
        ")",
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Step 6: Add new CHECK constraints
    # ------------------------------------------------------------------
    op.create_check_constraint(
        "alpaca_paper_ledger_record_kind",
        _TABLE,
        "record_kind IN ('plan','preview','validation_attempt','execution','reconcile','anomaly')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "alpaca_paper_ledger_validation_outcome",
        _TABLE,
        "validation_outcome IN ('passed','failed','skipped','n_a')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "alpaca_paper_ledger_leg_role",
        _TABLE,
        "leg_role IS NULL OR leg_role IN ('buy','sell','roundtrip')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "alpaca_paper_ledger_settlement_status",
        _TABLE,
        "settlement_status IN ('pending','settled','failed','n_a')",
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Step 7: Replace old unique constraint with partial unique indexes
    # ------------------------------------------------------------------
    op.drop_constraint(
        "uq_alpaca_paper_ledger_client_order_id",
        _TABLE,
        schema=_SCHEMA,
        type_="unique",
    )
    # Non-validation records: unique by (client_order_id, record_kind)
    op.create_index(
        "uq_alpaca_paper_ledger_client_order_kind",
        _TABLE,
        ["client_order_id", "record_kind"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where="validation_attempt_no IS NULL",
    )
    # Validation attempts: unique by (correlation_id, side, attempt_no)
    op.create_index(
        "uq_alpaca_paper_ledger_validation_attempt",
        _TABLE,
        ["lifecycle_correlation_id", "side", "validation_attempt_no"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where="record_kind = 'validation_attempt'",
    )

    # ------------------------------------------------------------------
    # Step 8: Add new regular indexes
    # ------------------------------------------------------------------
    op.create_index(
        "ix_alpaca_paper_ledger_correlation_id",
        _TABLE,
        ["lifecycle_correlation_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_alpaca_paper_ledger_record_kind",
        _TABLE,
        ["record_kind"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Drop new indexes before normalizing rows back to the ROB-84 shape.
    # ------------------------------------------------------------------
    op.drop_index(
        "ix_alpaca_paper_ledger_record_kind",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_correlation_id",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "uq_alpaca_paper_ledger_validation_attempt",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "uq_alpaca_paper_ledger_client_order_kind",
        table_name=_TABLE,
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Drop ROB-90 CHECK constraints, then map canonical states back to the
    # ROB-84 state vocabulary before restoring the old lifecycle CHECK.
    # ------------------------------------------------------------------
    op.drop_constraint(
        "alpaca_paper_ledger_settlement_status",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "alpaca_paper_ledger_leg_role",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "alpaca_paper_ledger_validation_outcome",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "alpaca_paper_ledger_record_kind",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "alpaca_paper_ledger_lifecycle_state",
        _TABLE,
        schema=_SCHEMA,
        type_="check",
    )

    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        f"SET lifecycle_state = CASE "
        f"WHEN lifecycle_state = 'planned' THEN 'previewed' "
        f"WHEN lifecycle_state = 'validated' THEN 'submitted' "
        f"WHEN lifecycle_state = 'position_reconciled' THEN 'filled' "
        f"WHEN lifecycle_state = 'sell_validated' THEN 'submitted' "
        f"WHEN lifecycle_state = 'closed' THEN 'filled' "
        f"WHEN lifecycle_state = 'final_reconciled' THEN 'filled' "
        f"WHEN lifecycle_state = 'anomaly' AND validation_outcome = 'failed' "
        f"THEN 'validation_failed' "
        f"WHEN lifecycle_state = 'anomaly' THEN 'unexpected' "
        f"ELSE lifecycle_state END"
    )

    op.create_check_constraint(
        "alpaca_paper_ledger_lifecycle_state",
        _TABLE,
        "lifecycle_state IN ("
        "'previewed','validation_failed','submitted','open',"
        "'partially_filled','filled','canceled','unexpected'"
        ")",
        schema=_SCHEMA,
    )

    # ROB-90 permits multiple record kinds per client_order_id. ROB-84 did not.
    # Preserve rows on downgrade by suffixing duplicate keys before restoring the
    # legacy unique constraint.
    op.execute(
        f"WITH ranked AS ("
        f"  SELECT id, client_order_id, "
        f"         row_number() OVER ("
        f"           PARTITION BY client_order_id "
        f"           ORDER BY CASE record_kind "
        f"             WHEN 'execution' THEN 0 "
        f"             WHEN 'preview' THEN 1 "
        f"             WHEN 'plan' THEN 2 "
        f"             WHEN 'validation_attempt' THEN 3 "
        f"             WHEN 'reconcile' THEN 4 "
        f"             ELSE 5 END, id"
        f"         ) AS rn "
        f"  FROM {_SCHEMA}.{_TABLE}"
        f") "
        f"UPDATE {_SCHEMA}.{_TABLE} AS ledger "
        f"SET client_order_id = ranked.client_order_id || '-legacy-' || ranked.id::text "
        f"FROM ranked "
        f"WHERE ledger.id = ranked.id AND ranked.rn > 1"
    )

    op.create_unique_constraint(
        "uq_alpaca_paper_ledger_client_order_id",
        _TABLE,
        ["client_order_id"],
        schema=_SCHEMA,
    )

    # ------------------------------------------------------------------
    # Drop added columns
    # ------------------------------------------------------------------
    for col in [
        "qty_delta",
        "settlement_at",
        "settlement_status",
        "fee_currency",
        "fee_amount",
        "confirm_flag",
        "validation_outcome",
        "validation_attempt_no",
        "leg_role",
        "lifecycle_correlation_id",
        "record_kind",
    ]:
        op.drop_column(_TABLE, col, schema=_SCHEMA)
