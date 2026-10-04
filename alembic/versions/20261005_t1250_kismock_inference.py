"""#1250 append-only audit table for the Q-46 kis_mock expired[inference] close.

Creates ``review.kis_mock_inference_expiry_events``: one row per kis_mock
ledger row closed by ``scripts/expire_kis_mock_rows_by_inference.py``
(``ledger_id`` UNIQUE, CHECK confines it to the decision's four ids 63/64/66/80,
fixed action / decision ref / after state), and triggers that reject UPDATE,
DELETE and TRUNCATE. No existing table, column or row is touched; the kis_mock
ledger rows themselves are closed later by the operator's CLI run, not here.

Locking: CREATE TABLE only; no existing table is locked.

Revision ID: 20261005_t1250_kismock_inf
Revises: 20261001_t1175_ledger_quar
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261005_t1250_kismock_inf"
down_revision: str | Sequence[str] | None = "20261001_t1175_ledger_quar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER_IDS_SQL = "ledger_id IN (63, 64, 66, 80)"

AUDIT_REJECT_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.reject_kis_mock_inference_expiry_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'review.% is append-only; % rejected',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "kis_mock_inference_expiry_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("operator_decision_ref", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("before_state", sa.Text(), nullable=False),
        sa.Column("after_state", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kis_mock_inference_expiry_events")),
        sa.UniqueConstraint(
            "ledger_id", name=op.f("uq_kis_mock_inference_expiry_ledger")
        ),
        sa.CheckConstraint(
            LEDGER_IDS_SQL,
            name=op.f("ck_kis_mock_inference_expiry_events_ledger_id"),
        ),
        sa.CheckConstraint(
            "action = 'expire_inference'",
            name=op.f("ck_kis_mock_inference_expiry_events_action"),
        ),
        sa.CheckConstraint(
            "operator_decision_ref = 'Q-46'",
            name=op.f("ck_kis_mock_inference_expiry_events_decision_ref"),
        ),
        sa.CheckConstraint(
            "after_state = 'expired'",
            name=op.f("ck_kis_mock_inference_expiry_events_after_state"),
        ),
        sa.CheckConstraint(
            "before_state IN ('accepted', 'pending')",
            name=op.f("ck_kis_mock_inference_expiry_events_before_state"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_kis_mock_inference_expiry_events_reason_nonblank"),
        ),
        sa.CheckConstraint(
            "btrim(actor) <> ''",
            name=op.f("ck_kis_mock_inference_expiry_events_actor_nonblank"),
        ),
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_inference_expiry_events_batch",
        "kis_mock_inference_expiry_events",
        ["batch_id"],
        schema="review",
    )
    op.execute(AUDIT_REJECT_FUNCTION_DDL)
    op.execute(
        "CREATE TRIGGER trg_kis_mock_inference_expiry_events_append_only "
        "BEFORE UPDATE OR DELETE ON review.kis_mock_inference_expiry_events "
        "FOR EACH ROW EXECUTE FUNCTION "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kis_mock_inference_expiry_events_truncate "
        "BEFORE TRUNCATE ON review.kis_mock_inference_expiry_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )

    # Stage-4 role may not exist in dev/CI databases — conditional GRANT.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.kis_mock_inference_expiry_events "
        "TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE "
        "review.kis_mock_inference_expiry_events_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_expiry_events_truncate "
        "ON review.kis_mock_inference_expiry_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_expiry_events_append_only "
        "ON review.kis_mock_inference_expiry_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )
    op.drop_index(
        "ix_kis_mock_inference_expiry_events_batch",
        table_name="kis_mock_inference_expiry_events",
        schema="review",
    )
    op.drop_table("kis_mock_inference_expiry_events", schema="review")
