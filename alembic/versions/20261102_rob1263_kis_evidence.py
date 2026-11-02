"""Add append-only KR KIS mock dispatch evidence (ROB-1263).

No existing table, binary reservation semantics, or activation setting changes.
"""

from alembic import op

revision = "20261102_rob1263_kis_evidence"
down_revision = "20261102_rob1470_sleeves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE review.kis_mock_dispatch_lineage (
            order_attempt_id TEXT PRIMARY KEY,
            lane_id TEXT NOT NULL,
            physical_account_id TEXT NOT NULL,
            claim_account_scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            initial_envelope JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_kis_mock_dispatch_scope_key UNIQUE (claim_account_scope, idempotency_key),
            CONSTRAINT ck_kis_mock_dispatch_lineage_lane_kr_kis_mock CHECK (lane_id = 'kr.kis.mock'),
            CONSTRAINT ck_kis_mock_dispatch_lineage_mock_fingerprint CHECK (physical_account_id ~ '^kismock:v1:[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE TABLE review.kis_mock_dispatch_evidence (
            id BIGSERIAL PRIMARY KEY,
            order_attempt_id TEXT NOT NULL REFERENCES review.kis_mock_dispatch_lineage(order_attempt_id),
            kind TEXT NOT NULL,
            broker_evidence_key TEXT NOT NULL,
            broker_order_id TEXT,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_kis_mock_dispatch_evidence_key UNIQUE (order_attempt_id, kind, broker_evidence_key),
            CONSTRAINT ck_kis_mock_dispatch_evidence_evidence_kind CHECK (kind IN ('ack','unknown','reject','expiry','partial_fill','cancel','terminal_reconciliation')),
            CONSTRAINT ck_kis_mock_dispatch_evidence_evidence_key_digest CHECK (broker_evidence_key ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_kis_mock_dispatch_evidence_exact_odno_required CHECK (kind IN ('unknown','reject') OR (broker_order_id IS NOT NULL AND broker_order_id ~ '^[0-9]+$'))
        )
    """)
    op.execute("""
        CREATE FUNCTION review.kis_mock_dispatch_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'kis_mock_dispatch_append_only' USING ERRCODE = '23000';
        END $$
    """)
    for table in ("kis_mock_dispatch_lineage", "kis_mock_dispatch_evidence"):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE OR TRUNCATE ON review.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION review.kis_mock_dispatch_immutable()
        """)
    op.execute("""
        CREATE FUNCTION review.kis_mock_dispatch_odno_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM review.kis_mock_dispatch_lineage
                WHERE order_attempt_id = NEW.order_attempt_id FOR UPDATE;
            IF NEW.broker_order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM review.kis_mock_dispatch_evidence
                WHERE order_attempt_id = NEW.order_attempt_id
                    AND broker_order_id IS NOT NULL
                    AND broker_order_id <> NEW.broker_order_id
            ) THEN
                RAISE EXCEPTION 'kis_mock_dispatch_odno_mismatch' USING ERRCODE = '23000';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER kis_mock_dispatch_odno_binding
        BEFORE INSERT ON review.kis_mock_dispatch_evidence
        FOR EACH ROW EXECUTE FUNCTION review.kis_mock_dispatch_odno_binding()
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON review.kis_mock_dispatch_lineage, review.kis_mock_dispatch_evidence TO at_app;
                GRANT UPDATE (order_attempt_id) ON review.kis_mock_dispatch_lineage TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE review.kis_mock_dispatch_evidence_id_seq TO at_app;
                GRANT EXECUTE ON FUNCTION review.kis_mock_dispatch_odno_binding() TO at_app;
                GRANT EXECUTE ON FUNCTION review.kis_mock_dispatch_immutable() TO at_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM review.kis_mock_dispatch_lineage)
                OR EXISTS (SELECT 1 FROM review.kis_mock_dispatch_evidence) THEN
                RAISE EXCEPTION 'kis_mock_dispatch_evidence_retained';
            END IF;
        END $$
    """)
    op.execute("DROP TABLE review.kis_mock_dispatch_evidence")
    op.execute("DROP TABLE review.kis_mock_dispatch_lineage")
    op.execute("DROP FUNCTION review.kis_mock_dispatch_odno_binding()")
    op.execute("DROP FUNCTION review.kis_mock_dispatch_immutable()")
