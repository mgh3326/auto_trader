"""Add append-only KR KIS mock pre-send position baselines (hk 2690).

Additive and operator-applied. Downgrade refuses while rows exist.
"""

from alembic import op

revision = "20261109_hk2690_kis_pos_base"
down_revision = "20261103_rob1487_perf_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE review.kis_mock_position_baseline (
            order_attempt_id TEXT PRIMARY KEY REFERENCES review.kis_mock_dispatch_lineage(order_attempt_id),
            symbol TEXT NOT NULL,
            quantity BIGINT NOT NULL,
            read_digest TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_kis_mock_position_baseline_quantity CHECK (quantity >= 0),
            CONSTRAINT ck_kis_mock_position_baseline_digest CHECK (read_digest ~ '^[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE TRIGGER kis_mock_position_baseline_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON review.kis_mock_position_baseline
        FOR EACH STATEMENT EXECUTE FUNCTION review.kis_mock_dispatch_immutable()
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON review.kis_mock_position_baseline TO at_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM review.kis_mock_position_baseline) THEN
                RAISE EXCEPTION 'kis_mock_position_baseline_retained';
            END IF;
        END $$
    """)
    op.execute("DROP TABLE review.kis_mock_position_baseline")
