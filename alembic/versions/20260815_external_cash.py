"""Add the append-only external-cash declaration ledger.

Revision ID: 20260815_external_cash
Revises: 20260814_lcapprove_b1
Create Date: 2026-08-15

This migration is additive DDL only.  In particular, it never inserts the
operator's initial parking balance; that declaration requires an authenticated
admin confirmation with an exact observed timestamp.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260815_external_cash"
down_revision: str | Sequence[str] | None = "20260814_lcapprove_b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "external_cash_declarations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("declaration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("location_key", sa.Text(), nullable=False),
        sa.Column("display_label", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("fresh_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("declared_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "origin",
            sa.Text(),
            server_default=sa.text("'invest_ui'"),
            nullable=False,
        ),
        sa.Column(
            "supersedes_declaration_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount >= 0", name="ck_external_cash_amount_nonnegative"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_external_cash_currency"),
        sa.CheckConstraint(
            "length(btrim(location_key)) > 0",
            name="ck_external_cash_location_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(display_label)) > 0",
            name="ck_external_cash_label_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(source_note)) > 0",
            name="ck_external_cash_note_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_external_cash_idempotency_nonempty",
        ),
        sa.CheckConstraint(
            "fresh_until > as_of", name="ck_external_cash_fresh_after_asof"
        ),
        sa.CheckConstraint("origin = 'invest_ui'", name="ck_external_cash_origin"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_external_cash_owner_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["declared_by_user_id"],
            ["users.id"],
            name="fk_external_cash_declared_by_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_declaration_id"],
            ["review.external_cash_declarations.declaration_id"],
            name="fk_external_cash_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("declaration_id", name="uq_external_cash_declaration_id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_external_cash_owner_idempotency",
        ),
        sa.UniqueConstraint(
            "supersedes_declaration_id",
            name="uq_external_cash_supersedes",
        ),
        schema="review",
    )
    op.execute(
        "CREATE INDEX ix_external_cash_owner_location_currency_asof "
        "ON review.external_cash_declarations "
        "(owner_user_id, location_key, currency, as_of DESC, recorded_at DESC)"
    )
    op.create_index(
        "ix_external_cash_supersedes_declaration_id",
        "external_cash_declarations",
        ["supersedes_declaration_id"],
        schema="review",
    )

    op.execute(
        """
        CREATE FUNCTION review.validate_external_cash_correction()
        RETURNS trigger AS $$
        DECLARE
            previous review.external_cash_declarations%ROWTYPE;
        BEGIN
            IF NEW.supersedes_declaration_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF NEW.supersedes_declaration_id = NEW.declaration_id THEN
                RAISE EXCEPTION 'external cash declaration cannot supersede itself'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO previous
              FROM review.external_cash_declarations
             WHERE declaration_id = NEW.supersedes_declaration_id
             FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'superseded external cash declaration is missing'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF previous.owner_user_id IS DISTINCT FROM NEW.owner_user_id
               OR previous.location_key IS DISTINCT FROM NEW.location_key
               OR previous.currency IS DISTINCT FROM NEW.currency THEN
                RAISE EXCEPTION 'external cash correction scope mismatch'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION review.reject_external_cash_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'review.external_cash_declarations is append-only; % rejected',
                TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_external_cash_correction_scope
        BEFORE INSERT ON review.external_cash_declarations
        FOR EACH ROW EXECUTE FUNCTION review.validate_external_cash_correction()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_external_cash_append_only
        BEFORE UPDATE OR DELETE ON review.external_cash_declarations
        FOR EACH ROW EXECUTE FUNCTION review.reject_external_cash_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_external_cash_truncate_append_only
        BEFORE TRUNCATE ON review.external_cash_declarations
        FOR EACH STATEMENT EXECUTE FUNCTION review.reject_external_cash_mutation()
        """
    )
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE "
        "ON review.external_cash_declarations FROM PUBLIC"
    )


def downgrade() -> None:
    op.drop_table("external_cash_declarations", schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.reject_external_cash_mutation()")
    op.execute("DROP FUNCTION IF EXISTS review.validate_external_cash_correction()")
