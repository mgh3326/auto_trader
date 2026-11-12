"""Add append-only decision evidence and order attempt records."""

from alembic import op

revision = "20261112_hk4182_decision"
down_revision = "20261110_hk3800_account_value"
branch_labels = None
depends_on = None

TABLES = (
    "decision_evidence",
    "decision_evidence_rungs",
    "decision_reference_prices",
    "decision_order_links",
    "decision_order_link_outcomes",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE review.decision_evidence (
            id bigserial PRIMARY KEY, decision_id uuid NOT NULL UNIQUE,
            decision_kind text NOT NULL CHECK (decision_kind IN ('proposal','direct_order','declined')),
            decided_at timestamptz NOT NULL DEFAULT now(), decided_by text NOT NULL,
            market text NOT NULL CHECK (market IN ('equity_kr','equity_us','crypto')),
            account_mode text CHECK (account_mode IN ('kis_live','toss_live','upbit')),
            symbol text NOT NULL, side text CHECK (side IN ('buy','sell')), strategy text,
            sleeve_id bigint REFERENCES review.strategy_sleeves(id) ON DELETE RESTRICT,
            candidate_call_id uuid, candidate_symbol text,
            candidate_link_basis text CHECK (candidate_link_basis IN ('recorded','inference')),
            currency text CHECK (currency IN ('KRW','USD')), stop_price numeric(24,8),
            stop_basis text CHECK (stop_basis IN ('support','pct_floor','volatility')),
            stop_source text CHECK (stop_source IN ('exit_plan','order_argument')),
            target_price numeric(24,8), horizon_sessions integer CHECK (horizon_sessions > 0),
            forecast_ids uuid[], report_item_uuid uuid, policy_version text, policy_content_hash text,
            evidence_hash text NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            CHECK (candidate_call_id IS NULL OR candidate_symbol IS NOT NULL),
            CHECK (candidate_call_id IS NULL OR candidate_link_basis IS NOT NULL),
            CHECK (stop_price IS NULL OR stop_source IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE TABLE review.decision_evidence_rungs (
            id bigserial PRIMARY KEY, decision_id uuid NOT NULL, rung_index integer NOT NULL,
            side text NOT NULL CHECK (side IN ('buy','sell')),
            planned_quantity numeric(24,8) CHECK (planned_quantity > 0),
            planned_notional numeric(28,8), limit_price numeric(24,8) CHECK (limit_price > 0),
            UNIQUE (decision_id, rung_index)
        )
    """)
    op.execute("""
        CREATE TABLE review.decision_reference_prices (
            id bigserial PRIMARY KEY, decision_id uuid NOT NULL, rung_index integer,
            kind text NOT NULL CHECK (kind IN ('limit','approval_price','quote_at_decision','candidate_price')),
            price numeric(24,8) NOT NULL CHECK (price > 0), asof timestamptz,
            recorded_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_decision_reference_prices ON review.decision_reference_prices (decision_id, COALESCE(rung_index, -1), kind)"
    )
    op.execute("""
        CREATE TABLE review.decision_order_links (
            id bigserial PRIMARY KEY, attempt_id uuid NOT NULL UNIQUE, decision_id uuid NOT NULL,
            account_scope text NOT NULL CHECK (account_scope IN ('kis_live','toss_live','upbit_live')),
            send_key text NOT NULL, proposal_id uuid, rung_index integer,
            linked_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((proposal_id IS NULL) = (rung_index IS NULL))
        )
    """)
    op.execute(
        "CREATE INDEX ix_decision_order_links_scope_key ON review.decision_order_links (account_scope, send_key)"
    )
    op.execute(
        "CREATE INDEX ix_decision_order_links_decision ON review.decision_order_links (decision_id)"
    )
    op.execute(
        "CREATE INDEX ix_decision_order_links_proposal ON review.decision_order_links (proposal_id, rung_index) WHERE proposal_id IS NOT NULL"
    )
    op.execute("""
        CREATE TABLE review.decision_order_link_outcomes (
            id bigserial PRIMARY KEY, attempt_id uuid NOT NULL UNIQUE REFERENCES review.decision_order_links(attempt_id),
            outcome text NOT NULL CHECK (outcome IN ('proven_not_sent','sent')),
            recorded_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE FUNCTION review.hk4182_append_only_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'review.% is append-only', TG_TABLE_NAME; END $$
    """)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON review.{table} FOR EACH ROW EXECUTE FUNCTION review.hk4182_append_only_guard()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON review.{table} FOR EACH STATEMENT EXECUTE FUNCTION review.hk4182_append_only_guard()"
        )
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON review.{table} FROM PUBLIC")
        op.execute(f"""
            DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON review.{table} TO at_app;
                GRANT USAGE, SELECT ON SEQUENCE review.{table}_id_seq TO at_app;
            END IF; END $$
        """)


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM review.{table}) THEN RAISE EXCEPTION 'review.{table} contains evidence'; END IF; END $$"
        )
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE review.{table}")
    op.execute("DROP FUNCTION review.hk4182_append_only_guard()")
