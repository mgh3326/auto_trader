"""#1175 execution_ledger quarantine columns + append-only audit table.

Adds three nullable quarantine columns to ``review.execution_ledger`` (no
backfill, every existing row stays in effect), two CHECKs that keep a
quarantine all-or-nothing and confined to KIS websocket rows, a trigger that
makes a quarantined row terminal (any UPDATE or DELETE of it is refused, and a
TRUNCATE of the table is refused while one exists), and the append-only
``review.execution_ledger_quarantine_events`` audit table. A colliding write
against a quarantined key (for example a replayed phantom with a changed
payload, or a real fill that happens to share the key) therefore fails loudly
for operator review instead of rewriting or hiding anything.

Locking: ADD COLUMN (nullable, no default) is catalog-only, but the two
ADD CONSTRAINT CHECKs scan execution_ledger under the ACCESS EXCLUSIVE lock
the transaction already holds. Apply outside market hours like other ledger
DDL.

Revision ID: 20261001_t1175_ledger_quar
Revises: 20260930_rob1120_quotes
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261001_t1175_ledger_quar"
down_revision: str | Sequence[str] | None = "20260930_rob1120_quotes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUARANTINE_FIELDS_SQL = (
    "(quarantined_at IS NULL AND quarantine_reason IS NULL "
    "AND quarantined_by IS NULL) OR "
    "(quarantined_at IS NOT NULL AND quarantine_reason IS NOT NULL "
    "AND quarantined_by IS NOT NULL AND btrim(quarantine_reason) <> '' "
    "AND btrim(quarantined_by) <> '')"
)
QUARANTINE_SCOPE_SQL = (
    "quarantined_at IS NULL OR (source = 'websocket' AND broker = 'kis')"
)

GUARD_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.guard_execution_ledger_quarantine()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        IF EXISTS (
            SELECT 1 FROM review.execution_ledger WHERE quarantined_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'review.execution_ledger holds quarantined rows; TRUNCATE rejected'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NULL;
    END IF;
    IF OLD.quarantined_at IS NOT NULL THEN
        RAISE EXCEPTION 'review.execution_ledger row % is quarantined and terminal; % rejected',
            OLD.id, TG_OP USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

AUDIT_REJECT_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.reject_execution_ledger_quarantine_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'review.% is append-only; % rejected',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column(
        "execution_ledger",
        sa.Column("quarantined_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="review",
    )
    op.add_column(
        "execution_ledger",
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "execution_ledger",
        sa.Column("quarantined_by", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        op.f("ck_execution_ledger_quarantine_fields"),
        "execution_ledger",
        QUARANTINE_FIELDS_SQL,
        schema="review",
    )
    op.create_check_constraint(
        op.f("ck_execution_ledger_quarantine_scope"),
        "execution_ledger",
        QUARANTINE_SCOPE_SQL,
        schema="review",
    )
    op.execute(GUARD_FUNCTION_DDL)
    op.execute(
        "CREATE TRIGGER trg_execution_ledger_quarantine_guard "
        "BEFORE UPDATE OR DELETE ON review.execution_ledger "
        "FOR EACH ROW EXECUTE FUNCTION review.guard_execution_ledger_quarantine()"
    )
    op.execute(
        "CREATE TRIGGER trg_execution_ledger_quarantine_truncate "
        "BEFORE TRUNCATE ON review.execution_ledger "
        "FOR EACH STATEMENT EXECUTE FUNCTION "
        "review.guard_execution_ledger_quarantine()"
    )

    op.create_table(
        "execution_ledger_quarantine_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_execution_ledger_quarantine_events")
        ),
        sa.UniqueConstraint(
            "ledger_id", name=op.f("uq_execution_ledger_quarantine_ledger")
        ),
        sa.CheckConstraint(
            "action = 'quarantine'",
            name=op.f("ck_execution_ledger_quarantine_events_action"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_execution_ledger_quarantine_events_reason_nonblank"),
        ),
        sa.CheckConstraint(
            "btrim(actor) <> ''",
            name=op.f("ck_execution_ledger_quarantine_events_actor_nonblank"),
        ),
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_quarantine_events_batch",
        "execution_ledger_quarantine_events",
        ["batch_id"],
        schema="review",
    )
    op.execute(AUDIT_REJECT_FUNCTION_DDL)
    op.execute(
        "CREATE TRIGGER trg_execution_ledger_quarantine_events_append_only "
        "BEFORE UPDATE OR DELETE ON review.execution_ledger_quarantine_events "
        "FOR EACH ROW EXECUTE FUNCTION "
        "review.reject_execution_ledger_quarantine_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_execution_ledger_quarantine_events_truncate "
        "BEFORE TRUNCATE ON review.execution_ledger_quarantine_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION "
        "review.reject_execution_ledger_quarantine_event_mutation()"
    )

    # Stage-4 role may not exist in dev/CI databases — conditional GRANT.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.execution_ledger_quarantine_events "
        "TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE "
        "review.execution_ledger_quarantine_events_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_ledger_quarantine_events_truncate "
        "ON review.execution_ledger_quarantine_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_ledger_quarantine_events_append_only "
        "ON review.execution_ledger_quarantine_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.reject_execution_ledger_quarantine_event_mutation()"
    )
    op.drop_index(
        "ix_execution_ledger_quarantine_events_batch",
        table_name="execution_ledger_quarantine_events",
        schema="review",
    )
    op.drop_table("execution_ledger_quarantine_events", schema="review")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_ledger_quarantine_truncate "
        "ON review.execution_ledger"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_ledger_quarantine_guard "
        "ON review.execution_ledger"
    )
    op.execute("DROP FUNCTION IF EXISTS review.guard_execution_ledger_quarantine()")
    op.drop_constraint(
        op.f("ck_execution_ledger_quarantine_scope"),
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_execution_ledger_quarantine_fields"),
        "execution_ledger",
        schema="review",
        type_="check",
    )
    op.drop_column("execution_ledger", "quarantined_by", schema="review")
    op.drop_column("execution_ledger", "quarantine_reason", schema="review")
    op.drop_column("execution_ledger", "quarantined_at", schema="review")
