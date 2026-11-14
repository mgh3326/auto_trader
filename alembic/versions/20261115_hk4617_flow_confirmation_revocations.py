"""Allow append-only revocation of flow completeness confirmations."""

from alembic import op

revision = "20261115_hk4617_flow_revoke"
down_revision = "20261113_hk4282_flow_confirm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE review.account_flow_confirmation_revocations (
            confirmation_id BIGINT PRIMARY KEY
                REFERENCES review.account_flow_confirmations(id),
            revoked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            revoked_by TEXT NOT NULL CHECK (length(trim(revoked_by)) > 0),
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0)
        )
    """)
    op.execute("""
        CREATE TRIGGER account_flow_confirmation_revocations_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE
        ON review.account_flow_confirmation_revocations
        FOR EACH STATEMENT EXECUTE FUNCTION review.account_value_immutable()
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT
                ON review.account_flow_confirmation_revocations TO at_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE review.account_flow_confirmation_revocations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM review.account_flow_confirmation_revocations
            ) THEN
                RAISE EXCEPTION 'account_flow_confirmation_revocation_rows_retained';
            END IF;
        END $$
    """)
    op.execute("DROP TABLE review.account_flow_confirmation_revocations")
