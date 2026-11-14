"""KIS US operator inference detail and immutable audit."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "20261114_hk4178_us_inference"
down_revision = "20261112_hk4186_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_order_ledger",
        sa.Column("last_reconcile_detail", JSONB()),
        schema="review",
    )
    op.create_table(
        "kis_us_inference_expiry_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "ledger_id",
            sa.BigInteger(),
            sa.ForeignKey("review.live_order_ledger.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("batch_id", UUID(as_uuid=True), nullable=False),
        sa.Column("decision_ref", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("facts", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="review",
    )
    op.execute(
        """CREATE FUNCTION review.reject_kis_us_inference_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'KIS US inference audit is append-only'; END $$"""
    )
    op.execute(
        "CREATE TRIGGER kis_us_inference_event_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON review.kis_us_inference_expiry_events FOR EACH STATEMENT EXECUTE FUNCTION review.reject_kis_us_inference_event_mutation()"
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='at_app') THEN
       GRANT SELECT, INSERT ON review.kis_us_inference_expiry_events TO at_app;
       GRANT USAGE, SELECT ON SEQUENCE review.kis_us_inference_expiry_events_id_seq TO at_app;
       GRANT SELECT (last_reconcile_detail), UPDATE (last_reconcile_detail) ON review.live_order_ledger TO at_app;
    END IF; END $$""")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM review.kis_us_inference_expiry_events) THEN RAISE EXCEPTION 'cannot discard inference audit'; END IF; END $$"
    )
    op.drop_table("kis_us_inference_expiry_events", schema="review")
    op.execute("DROP FUNCTION review.reject_kis_us_inference_event_mutation()")
    op.drop_column("live_order_ledger", "last_reconcile_detail", schema="review")
