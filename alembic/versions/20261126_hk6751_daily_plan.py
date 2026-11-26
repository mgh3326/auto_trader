"""Append-only KR daily plan tables (hk 6751)."""

# The fixed DDL creates the append-only function, triggers and conditional grants.
# expand-contract: contract-step 6751
from alembic import op

revision = "20261126_hk6751_daily_plan"
down_revision = "20261126_hk6750"
branch_labels = None
depends_on = None

_TABLES = (
    "daily_plan_heads",
    "daily_plan_input_bundles",
    "daily_plan_versions",
    "daily_plan_rows",
    "daily_plan_outcomes",
    "daily_plan_row_links",
)

_DDL = (
    """
CREATE TABLE review.daily_plan_heads (
    id UUID NOT NULL,
    market TEXT NOT NULL,
    session_date DATE NOT NULL,
    mode TEXT DEFAULT 'plan_only' NOT NULL,
    cutoff_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_plan_heads PRIMARY KEY (id),
    CONSTRAINT uq_daily_plan_heads_market UNIQUE (market, session_date),
    CONSTRAINT ck_daily_plan_heads_kr_plan_only CHECK (market = 'kr' AND mode = 'plan_only')
)
    """,
    """
CREATE TABLE review.daily_plan_input_bundles (
    id UUID NOT NULL,
    head_id UUID NOT NULL,
    sha256 TEXT NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_plan_input_bundles PRIMARY KEY (id),
    CONSTRAINT uq_daily_plan_input_bundles_head_id UNIQUE (head_id, sha256),
    CONSTRAINT uq_daily_plan_bundle_binding UNIQUE (head_id, id, sha256),
    CONSTRAINT ck_daily_plan_input_bundles_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_daily_plan_input_bundles_head_id_daily_plan_heads FOREIGN KEY(head_id) REFERENCES review.daily_plan_heads (id)
)
    """,
    """
CREATE TABLE review.daily_plan_versions (
    id UUID NOT NULL,
    head_id UUID NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'PUBLISHED' NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    input_bundle_id UUID,
    input_bundle_sha256 TEXT,
    strategy_id TEXT NOT NULL,
    strategy_rule_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_content_hash TEXT NOT NULL,
    metadata JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_plan_versions PRIMARY KEY (id),
    CONSTRAINT fk_daily_plan_versions_head_id_daily_plan_input_bundles FOREIGN KEY(head_id, input_bundle_id, input_bundle_sha256) REFERENCES review.daily_plan_input_bundles (head_id, id, sha256),
    CONSTRAINT uq_daily_plan_versions_head_seq UNIQUE (head_id, seq),
    CONSTRAINT uq_daily_plan_versions_head_idempotency UNIQUE (head_id, idempotency_key),
    CONSTRAINT ck_daily_plan_versions_kind CHECK (kind IN ('premarket', 'fill', 'event', 'session', 'close')),
    CONSTRAINT ck_daily_plan_versions_published_seq CHECK (seq > 0 AND status = 'PUBLISHED'),
    CONSTRAINT ck_daily_plan_versions_bundle_pair CHECK ((input_bundle_id IS NULL) = (input_bundle_sha256 IS NULL)),
    CONSTRAINT fk_daily_plan_versions_head_id_daily_plan_heads FOREIGN KEY(head_id) REFERENCES review.daily_plan_heads (id)
)
    """,
    """
CREATE TABLE review.daily_plan_rows (
    id UUID NOT NULL,
    version_id UUID NOT NULL,
    row_kind TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    account_ref TEXT,
    action TEXT NOT NULL,
    action_basis TEXT NOT NULL,
    execution_state TEXT DEFAULT 'plan_only' NOT NULL,
    adoption_key TEXT,
    adoption_content_sha256 TEXT,
    forecast_id UUID,
    body JSONB NOT NULL,
    CONSTRAINT pk_daily_plan_rows PRIMARY KEY (id),
    CONSTRAINT ck_daily_plan_rows_row_kind CHECK (row_kind IN ('holding', 'candidate')),
    CONSTRAINT ck_daily_plan_rows_action CHECK (action IN ('new', 'add', 'trim', 'risk_sell', 'hold', 'watch')),
    CONSTRAINT ck_daily_plan_rows_action_basis CHECK (action_basis IN ('rule', 'scored')),
    CONSTRAINT ck_daily_plan_rows_plan_only CHECK (execution_state = 'plan_only'),
    CONSTRAINT ck_daily_plan_rows_kr_symbol CHECK (market = 'kr' AND symbol ~ '^[0-9]{6}$'),
    CONSTRAINT ck_daily_plan_rows_account_ref CHECK ((row_kind = 'holding' AND account_ref IS NOT NULL AND account_ref <> '') OR (row_kind = 'candidate' AND account_ref IS NULL)),
    CONSTRAINT ck_daily_plan_rows_adoption_binding CHECK ((adoption_key IS NULL) = (adoption_content_sha256 IS NULL) AND (adoption_key IS NULL) = (forecast_id IS NULL)),
    CONSTRAINT fk_daily_plan_rows_version_id_daily_plan_versions FOREIGN KEY(version_id) REFERENCES review.daily_plan_versions (id),
    CONSTRAINT uq_daily_plan_rows_adoption_key UNIQUE (adoption_key)
)
    """,
    """
CREATE TABLE review.daily_plan_outcomes (
    id UUID NOT NULL,
    row_id UUID NOT NULL,
    horizon INTEGER NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_plan_outcomes PRIMARY KEY (id),
    CONSTRAINT uq_daily_plan_outcomes_row_id UNIQUE (row_id, horizon),
    CONSTRAINT ck_daily_plan_outcomes_horizon CHECK (horizon > 0),
    CONSTRAINT fk_daily_plan_outcomes_row_id_daily_plan_rows FOREIGN KEY(row_id) REFERENCES review.daily_plan_rows (id)
)
    """,
    """
CREATE TABLE review.daily_plan_row_links (
    id UUID NOT NULL,
    row_id UUID NOT NULL,
    state TEXT NOT NULL,
    seq INTEGER NOT NULL,
    decision_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_daily_plan_row_links PRIMARY KEY (id),
    CONSTRAINT ck_daily_plan_row_links_state CHECK (state IN ('confirmed', 'link_no_decision', 'link_ambiguous', 'link_mismatch', 'link_unresolved')),
    CONSTRAINT uq_daily_plan_row_links_row_id UNIQUE (row_id, seq),
    CONSTRAINT ck_daily_plan_row_links_link_seq CHECK (seq > 0),
    CONSTRAINT ck_daily_plan_row_links_decision_binding CHECK ((state = 'confirmed') = (decision_id IS NOT NULL)),
    CONSTRAINT fk_daily_plan_row_links_row_id_daily_plan_rows FOREIGN KEY(row_id) REFERENCES review.daily_plan_rows (id)
)
    """,
    """
CREATE UNIQUE INDEX uq_daily_plan_candidate_key ON review.daily_plan_rows (version_id, market, symbol) WHERE row_kind = 'candidate'
    """,
    """
CREATE UNIQUE INDEX uq_daily_plan_holding_key ON review.daily_plan_rows (version_id, market, symbol, account_ref) WHERE row_kind = 'holding'
    """,
)


def upgrade() -> None:
    for ddl in _DDL:
        op.execute(ddl)
    op.execute("""
        CREATE FUNCTION review.daily_plan_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'daily plan records are append-only' USING ERRCODE = '55000';
        END
        $$
    """)
    for table in _TABLES:
        op.execute(f"""
            CREATE TRIGGER daily_plan_append_only
            BEFORE UPDATE OR DELETE OR TRUNCATE ON review.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION review.daily_plan_reject_mutation()
        """)
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'at_app') THEN
                    REVOKE ALL ON TABLE review.{table} FROM at_app;
                    GRANT SELECT, INSERT ON TABLE review.{table} TO at_app;
                END IF;
            END $$
        """)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"LOCK TABLE review.{table} IN ACCESS EXCLUSIVE MODE")
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT FROM review.{table}) THEN
                    RAISE EXCEPTION 'cannot downgrade nonempty daily plan tables';
                END IF;
            END $$
        """)
    for table in reversed(_TABLES):
        op.drop_table(table, schema="review")
    op.execute("DROP FUNCTION review.daily_plan_reject_mutation()")
