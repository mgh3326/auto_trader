"""Add operator-declared long-term quantity protection heads and revisions.

Revision ID: 20260925_rob728_lot_protection
Revises: 20260925_task691_toss_expired
Create Date: 2026-09-25

This is additive DDL only.  It creates no declaration, modifies no order or
execution ledger, and does not arm enforcement.  The immutable revision trail
is protected by database triggers against UPDATE, DELETE, and TRUNCATE.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260925_rob728_lot_protection"
down_revision: str | Sequence[str] | None = "20260925_task691_toss_expired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "protected_positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("protected_quantity", sa.Numeric(28, 8), nullable=False),
        sa.Column(
            "purpose",
            sa.Text(),
            server_default=sa.text("'long_term'"),
            nullable=False,
        ),
        sa.Column(
            "revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("last_confirmed_broker_held", sa.Numeric(28, 8), nullable=False),
        sa.Column("last_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_scope IN ('kis_live','toss_live','upbit_live')",
            name="ck_protected_positions_scope",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_protected_positions_market",
        ),
        sa.CheckConstraint(
            "(account_scope = 'upbit_live' AND market = 'crypto') "
            "OR (account_scope IN ('kis_live','toss_live') AND market IN ('kr','us'))",
            name="ck_protected_positions_scope_market",
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_protected_positions_symbol_nonempty",
        ),
        sa.CheckConstraint(
            "protected_quantity >= 0",
            name="ck_protected_positions_quantity_nonnegative",
        ),
        sa.CheckConstraint(
            "purpose IN ('long_term')",
            name="ck_protected_positions_purpose",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_protected_positions_revision_positive",
        ),
        sa.CheckConstraint(
            "last_confirmed_broker_held >= 0",
            name="ck_protected_positions_confirmed_held_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_scope",
            "market",
            "symbol",
            name="uq_protected_position_scope_market_symbol",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_protected_position_scope_market_symbol",
        "protected_positions",
        ["account_scope", "market", "symbol"],
        schema=_SCHEMA,
    )

    op.create_table(
        "protected_position_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("protected_position_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("previous_quantity", sa.Numeric(28, 8), nullable=True),
        sa.Column("new_quantity", sa.Numeric(28, 8), nullable=False),
        sa.Column("broker_held_observed", sa.Numeric(28, 8), nullable=False),
        sa.Column("broker_sellable_observed", sa.Numeric(28, 8), nullable=False),
        sa.Column("broker_observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('declare','increase','decrease','release','reconfirm')",
            name="ck_protected_position_revisions_action",
        ),
        sa.CheckConstraint(
            "new_quantity >= 0",
            name="ck_protected_position_revisions_new_nonnegative",
        ),
        sa.CheckConstraint(
            "previous_quantity IS NULL OR previous_quantity >= 0",
            name="ck_protected_position_revisions_previous_nonnegative",
        ),
        sa.CheckConstraint(
            "broker_held_observed >= 0",
            name="ck_protected_position_revisions_held_nonnegative",
        ),
        sa.CheckConstraint(
            "broker_sellable_observed >= 0",
            name="ck_protected_position_revisions_sellable_nonnegative",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0",
            name="ck_protected_position_revisions_reason_nonempty",
        ),
        sa.CheckConstraint(
            "origin IN ('invest_ui','operator_cli')",
            name="ck_protected_position_revisions_origin",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_protected_position_revisions_idempotency_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["protected_position_id"],
            ["review.protected_positions.id"],
            name="fk_pp_revisions_position",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "protected_position_id",
            "revision",
            name="uq_protected_position_revision",
        ),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_protected_position_revision_actor_idempotency",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_protected_position_revision_position_recorded",
        "protected_position_revisions",
        ["protected_position_id", "recorded_at"],
        schema=_SCHEMA,
    )

    op.execute(
        """
        CREATE FUNCTION review.reject_protected_position_revision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'review.protected_position_revisions is append-only; % rejected',
                TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protected_position_revisions_append_only
        BEFORE UPDATE OR DELETE ON review.protected_position_revisions
        FOR EACH ROW EXECUTE FUNCTION review.reject_protected_position_revision_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protected_position_revisions_truncate_append_only
        BEFORE TRUNCATE ON review.protected_position_revisions
        FOR EACH STATEMENT EXECUTE FUNCTION review.reject_protected_position_revision_mutation()
        """
    )
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON review.protected_position_revisions FROM PUBLIC"
    )


def downgrade() -> None:
    op.drop_table("protected_position_revisions", schema=_SCHEMA)
    op.drop_table("protected_positions", schema=_SCHEMA)
    op.execute(
        "DROP FUNCTION IF EXISTS review.reject_protected_position_revision_mutation()"
    )
