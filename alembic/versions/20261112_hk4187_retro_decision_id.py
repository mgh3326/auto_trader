"""Add a caller-supplied decision link to trade retrospectives."""

from alembic import op

revision = "20261112_hk4187_retro_decision"
down_revision = "20261112_hk4182_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE review.trade_retrospectives ADD COLUMN decision_id uuid NULL"
    )
    op.create_index(
        "ix_trade_retrospectives_decision_id",
        "trade_retrospectives",
        ["decision_id"],
        schema="review",
    )
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
            GRANT SELECT (decision_id), INSERT (decision_id), UPDATE (decision_id)
            ON review.trade_retrospectives TO at_app;
        END IF; END $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM review.trade_retrospectives
                WHERE decision_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: trade retrospective decision links exist';
            END IF;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
            REVOKE SELECT (decision_id), INSERT (decision_id), UPDATE (decision_id)
            ON review.trade_retrospectives FROM at_app;
        END IF; END $$
    """)
    op.drop_index(
        "ix_trade_retrospectives_decision_id",
        table_name="trade_retrospectives",
        schema="review",
    )
    op.execute("ALTER TABLE review.trade_retrospectives DROP COLUMN decision_id")
