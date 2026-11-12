"""Link newly created proposals to immutable decision evidence."""

from alembic import op

revision = "20261112_hk4184_proposal_dec"
down_revision = "20261112_hk4182_decision"
branch_labels = None
depends_on = None

NUMERIC_EXPANSIONS = (
    """
        ALTER TABLE review.decision_evidence
            ALTER COLUMN stop_price TYPE numeric(38,12),
            ALTER COLUMN target_price TYPE numeric(38,12)
    """,
    """
        ALTER TABLE review.decision_evidence_rungs
            ALTER COLUMN planned_quantity TYPE numeric(38,12),
            ALTER COLUMN planned_notional TYPE numeric(38,12),
            ALTER COLUMN limit_price TYPE numeric(38,12)
    """,
    """
        ALTER TABLE review.decision_reference_prices
            ALTER COLUMN price TYPE numeric(38,12)
    """,
)


def upgrade() -> None:
    for statement in NUMERIC_EXPANSIONS:
        op.execute(statement)
    op.execute("""
        ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS decision_id uuid
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_order_proposals_decision_id ON review.order_proposals (decision_id)"
    )
    op.execute("""
        ALTER TABLE review.order_proposals ADD CONSTRAINT fk_order_proposals_decision_id
        FOREIGN KEY (decision_id) REFERENCES review.decision_evidence(decision_id) ON DELETE RESTRICT
    """)
    op.execute("""
        CREATE FUNCTION review.hk4184_decision_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'order_proposals.decision_id is immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER order_proposals_decision_immutable
        BEFORE UPDATE OF decision_id ON review.order_proposals
        FOR EACH ROW WHEN (OLD.decision_id IS DISTINCT FROM NEW.decision_id)
        EXECUTE FUNCTION review.hk4184_decision_immutable()
    """)
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
            GRANT EXECUTE ON FUNCTION review.hk4184_decision_immutable() TO at_app;
        END IF; END $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM review.order_proposals WHERE decision_id IS NOT NULL)
            OR EXISTS (SELECT 1 FROM review.decision_evidence)
            OR EXISTS (SELECT 1 FROM review.decision_evidence_rungs)
            OR EXISTS (SELECT 1 FROM review.decision_reference_prices)
        THEN RAISE EXCEPTION 'decision evidence exists'; END IF; END $$
    """)
    op.execute(
        "DROP TRIGGER order_proposals_decision_immutable ON review.order_proposals"
    )
    op.execute("DROP FUNCTION review.hk4184_decision_immutable()")
    op.execute("DROP INDEX review.ix_order_proposals_decision_id")
    op.execute("ALTER TABLE review.order_proposals DROP COLUMN decision_id")
